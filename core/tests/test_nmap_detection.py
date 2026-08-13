from __future__ import annotations

import ntpath
import unittest

from smart_commissioning_core.engines.ip.nmap_detection import (
    UNINSTALL_REGISTRY_PATH,
    WinregUninstallRegistry,
    detect_nmap_candidates,
)


class _RegistryKey:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeWinreg:
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019
    KEY_WOW64_32KEY = 0x0200
    KEY_WOW64_64KEY = 0x0100

    def __init__(self, entries: dict[int, dict[str, dict[str, object]]]) -> None:
        self.entries = entries
        self.open_calls: list[tuple[object, str, int]] = []

    def OpenKey(self, root: object, path: str, _reserved: int, access: int) -> _RegistryKey:
        self.open_calls.append((root, path, access))
        view = access & (self.KEY_WOW64_32KEY | self.KEY_WOW64_64KEY)
        if path == UNINSTALL_REGISTRY_PATH:
            return _RegistryKey((view, None))
        parent, name = path.rsplit("\\", 1)
        if parent != UNINSTALL_REGISTRY_PATH or name not in self.entries.get(view, {}):
            raise FileNotFoundError(path)
        return _RegistryKey((view, name))

    def EnumKey(self, key: tuple[int, str | None], index: int) -> str:
        names = list(self.entries.get(key[0], {}))
        if index >= len(names):
            raise OSError("no more data")
        return names[index]

    def QueryValueEx(self, key: tuple[int, str | None], name: str) -> tuple[object, int]:
        assert key[1] is not None
        values = self.entries[key[0]][key[1]]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1


class _DetectionPaths:
    def __init__(
        self,
        *,
        roots: tuple[str, ...],
        files: tuple[str, ...],
        directories: tuple[str, ...],
        reparse: tuple[str, ...] = (),
        user_writable: tuple[str, ...] = (),
    ) -> None:
        self._roots = tuple(ntpath.normpath(item) for item in roots)
        self._files = {ntpath.normcase(ntpath.normpath(item)) for item in files}
        self._directories = {ntpath.normcase(ntpath.normpath(item)) for item in directories}
        self._reparse = {ntpath.normcase(ntpath.normpath(item)) for item in reparse}
        self._user_writable = {ntpath.normcase(ntpath.normpath(item)) for item in user_writable}

    def protected_install_roots(self) -> tuple[str, ...]:
        return self._roots

    def canonicalize(self, path: str) -> str:
        return ntpath.normpath(path)

    def is_regular_file(self, path: str) -> bool:
        return ntpath.normcase(ntpath.normpath(path)) in self._files

    def is_directory(self, path: str) -> bool:
        return ntpath.normcase(ntpath.normpath(path)) in self._directories

    def has_reparse_component(self, path: str) -> bool:
        key = ntpath.normcase(ntpath.normpath(path))
        return any(key == item or key.startswith(item + "\\") for item in self._reparse)

    def is_user_writable(self, path: str) -> bool:
        return ntpath.normcase(ntpath.normpath(path)) in self._user_writable


class NmapDetectionTests(unittest.TestCase):
    def test_detects_only_official_hklm_32_and_64_bit_uninstall_entries(self) -> None:
        entries = {
            _FakeWinreg.KEY_WOW64_32KEY: {
                "Nmap": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"C:\Program Files (x86)\Nmap",
                }
            },
            _FakeWinreg.KEY_WOW64_64KEY: {
                "Nmap64": {
                    "DisplayName": "Nmap 7.99",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.99",
                    "InstallLocation": r"C:\Program Files\Nmap",
                },
                "Other": {
                    "DisplayName": "Other Scanner",
                    "Publisher": "Someone Else",
                    "DisplayVersion": "1.0",
                    "InstallLocation": r"C:\Program Files\Other",
                },
            },
        }
        winreg = _FakeWinreg(entries)
        paths = _DetectionPaths(
            roots=(r"C:\Program Files", r"C:\Program Files (x86)"),
            directories=(
                r"C:\Program Files",
                r"C:\Program Files (x86)",
                r"C:\Program Files\Nmap",
                r"C:\Program Files (x86)\Nmap",
            ),
            files=(r"C:\Program Files\Nmap\nmap.exe", r"C:\Program Files (x86)\Nmap\nmap.exe"),
        )

        candidates = detect_nmap_candidates(
            registry=WinregUninstallRegistry(winreg),
            paths=paths,
        )

        self.assertEqual([item.registry_view for item in candidates], ["32", "64"])
        self.assertEqual([item.version for item in candidates], ["7.98", "7.99"])
        root_opens = [call for call in winreg.open_calls if call[1] == UNINSTALL_REGISTRY_PATH]
        self.assertEqual(
            root_opens,
            [
                (
                    _FakeWinreg.HKEY_LOCAL_MACHINE,
                    UNINSTALL_REGISTRY_PATH,
                    _FakeWinreg.KEY_READ | _FakeWinreg.KEY_WOW64_32KEY,
                ),
                (
                    _FakeWinreg.HKEY_LOCAL_MACHINE,
                    UNINSTALL_REGISTRY_PATH,
                    _FakeWinreg.KEY_READ | _FakeWinreg.KEY_WOW64_64KEY,
                ),
            ],
        )

    def test_rejects_stale_user_profile_unc_reparse_and_writable_candidates(self) -> None:
        view = _FakeWinreg.KEY_WOW64_64KEY
        entries = {
            view: {
                "Stale": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"C:\Program Files\Missing Nmap",
                },
                "ProfilePoison": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"C:\Users\operator\AppData\Roaming\Nmap",
                },
                "NetworkShare": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"\\fileserver\tools\Nmap",
                },
                "Reparse": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"C:\Program Files\Nmap Link",
                },
                "Writable": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"C:\Program Files\Writable Nmap",
                },
            }
        }
        paths = _DetectionPaths(
            roots=(r"C:\Program Files", r"C:\Program Files (x86)"),
            directories=(
                r"C:\Program Files",
                r"C:\Program Files (x86)",
                r"C:\Program Files\Nmap Link",
                r"C:\Program Files\Writable Nmap",
            ),
            files=(
                r"C:\Program Files\Nmap Link\nmap.exe",
                r"C:\Program Files\Writable Nmap\nmap.exe",
            ),
            reparse=(r"C:\Program Files\Nmap Link",),
            user_writable=(r"C:\Program Files\Writable Nmap",),
        )

        candidates = detect_nmap_candidates(
            registry=WinregUninstallRegistry(_FakeWinreg(entries)),
            paths=paths,
        )

        self.assertEqual(candidates, ())

    def test_accepts_an_explicitly_protected_machine_custom_root(self) -> None:
        view = _FakeWinreg.KEY_WOW64_64KEY
        entries = {
            view: {
                "Nmap": {
                    "DisplayName": "Nmap 7.98",
                    "Publisher": "Insecure.Com LLC",
                    "DisplayVersion": "7.98",
                    "InstallLocation": r"D:\Company Managed\Nmap",
                }
            }
        }
        paths = _DetectionPaths(
            roots=(r"D:\Company Managed",),
            directories=(r"D:\Company Managed", r"D:\Company Managed\Nmap"),
            files=(r"D:\Company Managed\Nmap\nmap.exe",),
        )

        candidates = detect_nmap_candidates(
            registry=WinregUninstallRegistry(_FakeWinreg(entries)),
            paths=paths,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].executable_path, r"D:\Company Managed\Nmap\nmap.exe")


if __name__ == "__main__":
    unittest.main()
