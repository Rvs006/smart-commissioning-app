from __future__ import annotations

import ctypes
import inspect
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from smart_commissioning_core.engines.ip.nmap_runner import (
    NmapJobLimitsV1,
    NmapProcessLaunchV1,
)
from smart_commissioning_core.engines.ip.nmap_windows import (
    CtypesNmapRuntimeCapabilityProbe,
    CtypesNmapTrustBackend,
    CtypesNmapWindowsProcessApi,
    _windows_command_line,
)
from smart_commissioning_core.source_interface import SourceInterfaceCandidateV1


class _Registry:
    HKEY_LOCAL_MACHINE = object()
    KEY_READ = 1

    def __init__(self) -> None:
        self.values = {
            (r"SOFTWARE\Microsoft\Cryptography", "MachineGuid"): (
                "11111111-1111-1111-1111-111111111111",
                1,
            ),
            (r"SYSTEM\CurrentControlSet\Services\npcap", "Start"): (3, 4),
            (r"SYSTEM\CurrentControlSet\Services\npcap\Parameters", "AdminOnly"): (
                1,
                4,
            ),
            (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NpcapInst",
                "DisplayVersion",
            ): ("1.79", 1),
        }

    class _Key:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> _Registry._Key:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def OpenKey(self, _hive: object, path: str, *_args: object) -> _Registry._Key:
        if not any(key_path == path for key_path, _name in self.values):
            raise FileNotFoundError(path)
        return self._Key(path)

    def QueryValueEx(self, key: _Registry._Key, name: str) -> tuple[object, int]:
        try:
            return self.values[(key.path, name)]
        except KeyError as error:
            raise FileNotFoundError(name) from error


class _WinFunction:
    def __init__(self, callback: object) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self.callback(*args)


class _TokenAccessAdvapi:
    def __init__(self, *, ace_grantee: str) -> None:
        self.ace_grantee = ace_grantee
        self.access_checks: list[tuple[int, int, str]] = []
        self.OpenProcessToken = _WinFunction(self._open_process_token)
        self.DuplicateToken = _WinFunction(self._duplicate_token)
        self.AccessCheck = _WinFunction(self._access_check)

    @staticmethod
    def _open_process_token(_process: object, _access: int, token: object) -> bool:
        token._obj.value = 101
        return True

    @staticmethod
    def _duplicate_token(_token: object, _level: int, duplicate: object) -> bool:
        duplicate._obj.value = 202
        return True

    def _access_check(
        self,
        _descriptor: object,
        token: object,
        desired_access: int,
        _mapping: object,
        privilege_set: object,
        privilege_set_length: object,
        granted_access: object,
        access_status: object,
    ) -> bool:
        token_value = int(token.value)
        desired_value = int(desired_access.value)
        if privilege_set is None:
            privilege_set_length._obj.value = 64
            if hasattr(ctypes, "set_last_error"):
                ctypes.set_last_error(122)
            return False
        self.access_checks.append((token_value, desired_value, self.ace_grantee))
        granted_access._obj.value = 0x00000002
        access_status._obj.value = True
        return True


class _TokenAccessKernel32:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.GetCurrentProcess = _WinFunction(self._get_current_process)
        self.CloseHandle = _WinFunction(self._close_handle)

    @staticmethod
    def _get_current_process() -> int:
        return -1

    def _close_handle(self, handle: object) -> bool:
        self.closed.append(int(handle.value))
        return True


class NmapWindowsBoundaryTests(unittest.TestCase):
    def test_command_line_quotes_every_windows_argument_without_a_shell(self) -> None:
        command_line = _windows_command_line(
            r"C:\Program Files\Nmap\nmap.exe",
            (
                "--noninteractive",
                r"C:\ProgramData\Smart Commissioning\targets.txt",
                'interface "A"',
                "trailing\\",
            ),
        )

        self.assertEqual(
            command_line,
            '"C:\\Program Files\\Nmap\\nmap.exe" --noninteractive '
            '"C:\\ProgramData\\Smart Commissioning\\targets.txt" '
            '"interface \\"A\\"" trailing\\',
        )

    def test_runtime_snapshot_is_in_process_and_reports_admin_only_npcap(self) -> None:
        candidate = SourceInterfaceCandidateV1(
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Building Controls",
            source_ip="192.0.2.10",
            prefix_length=24,
            is_up=True,
            default_route_metric=25,
        )
        probe = CtypesNmapRuntimeCapabilityProbe(winreg_module=_Registry())

        with (
            patch(
                "smart_commissioning_core.engines.ip.nmap_windows.enumerate_source_interfaces",
                return_value=[candidate],
            ),
            patch(
                "smart_commissioning_core.engines.ip.nmap_windows._npcap_service_running",
                return_value=True,
            ),
            patch(
                "smart_commissioning_core.engines.ip.nmap_windows._token_is_administrator",
                return_value=False,
            ),
        ):
            snapshot = probe.snapshot()

        self.assertEqual(
            snapshot.executor_identity,
            "machine-guid:11111111-1111-1111-1111-111111111111",
        )
        self.assertTrue(snapshot.npcap_installed)
        self.assertEqual(snapshot.npcap_version, "1.79")
        self.assertTrue(snapshot.npcap_service_running)
        self.assertTrue(snapshot.npcap_admin_only)
        self.assertFalse(snapshot.current_token_is_administrator)
        self.assertFalse(snapshot.current_token_has_raw_rights)
        self.assertEqual(snapshot.interfaces[0].source_ip, "192.0.2.10")
        self.assertEqual(
            snapshot.interfaces[0].nmap_device_name,
            r"\Device\NPF_{00000000-0000-0000-0000-000000000001}",
        )

    def test_module_has_no_process_search_shell_download_or_network_helper(self) -> None:
        import smart_commissioning_core.engines.ip.nmap_windows as module

        source = inspect.getsource(module)
        forbidden = (
            "import subprocess",
            "from subprocess",
            "shell=True",
            "os.system(",
            "which(",
            "urlopen(",
            "requests.",
            "create_connection(",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_suspended_process_termination_reports_only_a_verified_exit(self) -> None:
        class _Kernel32:
            def __init__(self) -> None:
                self.terminated: tuple[int, int] | None = None
                self.waited: tuple[int, int] | None = None

            def TerminateProcess(self, process: int, exit_code: object) -> bool:
                self.terminated = (process, int(exit_code.value))
                return True

            def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
                self.waited = (process, milliseconds)
                return 0

        kernel32 = _Kernel32()
        process = SimpleNamespace(process=41)

        with patch(
            "smart_commissioning_core.engines.ip.nmap_windows._process_kernel32",
            return_value=kernel32,
        ):
            terminated = CtypesNmapWindowsProcessApi().terminate_suspended_process(
                process,
                0xC000013A,
                1.25,
            )

        self.assertTrue(terminated)
        self.assertEqual(kernel32.terminated, (41, 0xC000013A))
        self.assertEqual(kernel32.waited, (41, 1_250))

    def test_suspended_process_termination_reports_unverified_after_bounded_timeout(self) -> None:
        class _Kernel32:
            def __init__(self) -> None:
                self.waited: tuple[int, int] | None = None

            def TerminateProcess(self, _process: int, _exit_code: object) -> bool:
                return True

            def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
                self.waited = (process, milliseconds)
                return 258

        kernel32 = _Kernel32()
        process = SimpleNamespace(process=53)

        with patch(
            "smart_commissioning_core.engines.ip.nmap_windows._process_kernel32",
            return_value=kernel32,
        ):
            terminated = CtypesNmapWindowsProcessApi().terminate_suspended_process(
                process,
                0xC000013A,
                0.5,
            )

        self.assertFalse(terminated)
        self.assertEqual(kernel32.waited, (53, 500))

    def test_token_access_rejects_current_user_and_enabled_domain_group_write_aces(self) -> None:
        for ace_grantee in (
            "current-user-sid",
            r"enabled-domain-group:BUILDING\\ControlsOperators",
        ):
            with self.subTest(ace_grantee=ace_grantee):
                advapi32 = _TokenAccessAdvapi(ace_grantee=ace_grantee)
                kernel32 = _TokenAccessKernel32()

                def _windows_library(
                    name: str,
                    _kernel32: object = kernel32,
                    _advapi32: object = advapi32,
                    **_kwargs: object,
                ) -> object:
                    return _kernel32 if name == "kernel32.dll" else _advapi32

                with (
                    patch(
                        "smart_commissioning_core.engines.ip.nmap_windows._named_file_security",
                        return_value=(301, 302, 303),
                    ),
                    patch(
                        "smart_commissioning_core.engines.ip.nmap_windows._local_free",
                    ),
                    patch(
                        "smart_commissioning_core.engines.ip.nmap_windows._IS_WINDOWS",
                        True,
                    ),
                    patch(
                        "smart_commissioning_core.engines.ip.nmap_windows.ctypes.WinDLL",
                        side_effect=_windows_library,
                        create=True,
                    ),
                    patch(
                        "smart_commissioning_core.engines.ip.nmap_windows.ctypes.get_last_error",
                        return_value=122,
                        create=True,
                    ),
                ):
                    writable = CtypesNmapTrustBackend().is_user_writable(
                        r"C:\\Program Files\\Nmap\\nmap.exe"
                    )

                self.assertTrue(writable)
                self.assertEqual(
                    advapi32.access_checks,
                    [(202, 0x02000000, ace_grantee)],
                )
                self.assertEqual(kernel32.closed, [202, 101])

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Win32 APIs")
    def test_live_runtime_probe_never_opens_the_npcap_driver(self) -> None:
        probe = CtypesNmapRuntimeCapabilityProbe()
        snapshot = probe.snapshot()

        self.assertTrue(snapshot.executor_identity.startswith("machine-guid:"))
        self.assertLessEqual(len(snapshot.interfaces), 256)
        self.assertIsInstance(snapshot.npcap_installed, bool)
        self.assertIsInstance(snapshot.current_token_is_administrator, bool)
        for interface in snapshot.interfaces:
            with self.subTest(interface_id=interface.interface_id):
                expected = None
                if snapshot.npcap_service_running:
                    guid = interface.interface_id.removeprefix("windows-guid:").upper()
                    expected = rf"\Device\NPF_{{{guid}}}"
                self.assertEqual(interface.nmap_device_name, expected)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Win32 APIs")
    def test_live_createprocess_is_suspended_job_bound_and_pipe_only(self) -> None:
        windows = CtypesNmapWindowsProcessApi()
        system_root = windows.system_root()
        temporary = os.environ.get("TEMP") or os.environ.get("TMP") or system_root
        launch = NmapProcessLaunchV1(
            application_name=sys.executable,
            arguments=(
                "-c",
                "import sys;sys.stdout.write('out');sys.stderr.write('err')",
            ),
            working_directory=os.getcwd(),
            environment=(
                ("SystemRoot", system_root),
                ("TEMP", temporary),
                ("TMP", temporary),
                ("WINDIR", system_root),
            ),
        )
        job = windows.create_kill_on_close_job(
            NmapJobLimitsV1(cpu_time_seconds=10),
        )
        process = None
        try:
            process = windows.create_process_suspended(launch)
            windows.assign_process_to_job(job, process)
            windows.resume_process(process)
            stdout = windows.read_pipe(process, "stdout", 64)
            stderr = windows.read_pipe(process, "stderr", 64)
            exit_code = windows.wait_process(process, 10)

            self.assertEqual(stdout, b"out")
            self.assertEqual(stderr, b"err")
            self.assertEqual(exit_code, 0)
            self.assertTrue(windows.wait_job_empty(job, 5))
        finally:
            if process is not None:
                windows.close_process(process)
            windows.close_job(job)

    @unittest.skipUnless(
        sys.platform == "win32" and Path(r"C:\Program Files\Git\cmd\git.exe").is_file(),
        "requires one embedded-signed protected-root executable",
    )
    def test_live_trust_backend_uses_final_path_acl_signature_and_version_apis(self) -> None:
        backend = CtypesNmapTrustBackend()
        executable = r"C:\Program Files\Git\cmd\git.exe"

        self.assertEqual(backend.canonicalize(executable), executable)
        self.assertFalse(backend.has_reparse_component(executable))
        self.assertFalse(backend.is_user_writable(executable))
        self.assertTrue(backend.is_administrator_owned(executable))
        signature = backend.authenticode(executable)
        self.assertTrue(signature.trusted)
        self.assertTrue(signature.publisher)
        self.assertRegex(signature.signer_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(backend.file_version(executable), r"^\d+(?:\.\d+)+$")


if __name__ == "__main__":
    unittest.main()
