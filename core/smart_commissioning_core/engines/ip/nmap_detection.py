"""Read-only discovery of separately installed Nmap candidates on Windows.

Discovery is deliberately narrower than trust.  It reads only the two HKLM
uninstall views and accepts candidates only below protected installation roots
reported by an injected Windows path adapter.  It never inspects ``PATH``, the
current directory, user profiles, or the network, and it starts no process.
"""

from __future__ import annotations

import ntpath
import re
from collections.abc import Iterable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

UNINSTALL_REGISTRY_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_MAX_UNINSTALL_KEYS_PER_VIEW = 4096
_MAX_REGISTRY_TEXT_LENGTH = 4096
_OFFICIAL_DISPLAY_NAME = re.compile(r"^nmap(?:\s|$)", re.IGNORECASE)


class NmapUninstallEntryV1(BaseModel):
    """Bounded metadata read from one HKLM uninstall registry view."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    registry_view: Literal["32", "64"]
    registry_key: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=512)
    publisher: str = Field(default="", max_length=512)
    version: str = Field(default="", max_length=128)
    install_location: str = Field(min_length=1, max_length=2048)


class NmapDetectionCandidateV1(BaseModel):
    """A path-safe candidate which still requires cryptographic trust checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    registry_view: Literal["32", "64"]
    registry_key: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=512)
    registry_publisher: str = Field(default="", max_length=512)
    version: str = Field(default="", max_length=128)
    install_root: str = Field(min_length=3, max_length=2048)
    executable_path: str = Field(min_length=3, max_length=2048)
    data_directory: str = Field(min_length=3, max_length=2048)


class WindowsDetectionPaths(Protocol):
    """In-process Windows path/security operations used during discovery."""

    def protected_install_roots(self) -> tuple[str, ...]: ...

    def canonicalize(self, path: str) -> str: ...

    def is_regular_file(self, path: str) -> bool: ...

    def is_directory(self, path: str) -> bool: ...

    def has_reparse_component(self, path: str) -> bool: ...

    def is_user_writable(self, path: str) -> bool: ...


class UninstallRegistry(Protocol):
    def iter_entries(self) -> Iterable[NmapUninstallEntryV1]: ...


class WinregUninstallRegistry:
    """Adapter over an injected ``winreg``-compatible Windows API module."""

    def __init__(self, winreg_module: object) -> None:
        self._winreg = winreg_module

    def iter_entries(self) -> Iterable[NmapUninstallEntryV1]:
        winreg = self._winreg
        views = (
            ("32", int(winreg.KEY_WOW64_32KEY)),
            ("64", int(winreg.KEY_WOW64_64KEY)),
        )
        for label, view_flag in views:
            access = int(winreg.KEY_READ) | view_flag
            hive = winreg.HKEY_LOCAL_MACHINE
            try:
                root_context = winreg.OpenKey(hive, UNINSTALL_REGISTRY_PATH, 0, access)
                with root_context as root:
                    names = _bounded_registry_subkeys(winreg, root)
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            for name in names:
                key_path = f"{UNINSTALL_REGISTRY_PATH}\\{name}"
                try:
                    entry_context = winreg.OpenKey(hive, key_path, 0, access)
                    with entry_context as entry_key:
                        display_name = _registry_text(winreg, entry_key, "DisplayName")
                        install_location = _registry_text(
                            winreg,
                            entry_key,
                            "InstallLocation",
                        )
                        publisher = _registry_text(winreg, entry_key, "Publisher", required=False)
                        version = _registry_text(
                            winreg,
                            entry_key,
                            "DisplayVersion",
                            required=False,
                        )
                except (AttributeError, OSError, TypeError, ValueError):
                    continue
                if not _OFFICIAL_DISPLAY_NAME.match(display_name):
                    continue
                try:
                    yield NmapUninstallEntryV1(
                        registry_view=label,
                        registry_key=name,
                        display_name=display_name,
                        publisher=publisher,
                        version=version,
                        install_location=install_location,
                    )
                except ValueError:
                    continue


def detect_nmap_candidates(
    *,
    registry: UninstallRegistry,
    paths: WindowsDetectionPaths,
) -> tuple[NmapDetectionCandidateV1, ...]:
    """Return deterministic protected-root candidates without executing Nmap."""

    roots = _protected_roots(paths)
    candidates: dict[str, NmapDetectionCandidateV1] = {}
    for entry in registry.iter_entries():
        try:
            install_root = _canonical_local_path(paths, entry.install_location)
            if not _is_contained_by_any(install_root, roots):
                continue
            executable_path = _canonical_local_path(paths, ntpath.join(install_root, "nmap.exe"))
            if not _is_contained(install_root, executable_path):
                continue
            if not paths.is_directory(install_root) or not paths.is_regular_file(executable_path):
                continue
            if any(
                paths.has_reparse_component(item) or paths.is_user_writable(item)
                for item in (install_root, executable_path)
            ):
                continue
        except (OSError, TypeError, ValueError):
            continue
        key = ntpath.normcase(executable_path)
        candidate = NmapDetectionCandidateV1(
            registry_view=entry.registry_view,
            registry_key=entry.registry_key,
            display_name=entry.display_name,
            registry_publisher=entry.publisher,
            version=entry.version,
            install_root=install_root,
            executable_path=executable_path,
            data_directory=install_root,
        )
        existing = candidates.get(key)
        if existing is None or _candidate_order(candidate) < _candidate_order(existing):
            candidates[key] = candidate
    return tuple(sorted(candidates.values(), key=_candidate_order))


def _bounded_registry_subkeys(winreg: object, root: object) -> tuple[str, ...]:
    names: list[str] = []
    for index in range(_MAX_UNINSTALL_KEYS_PER_VIEW + 1):
        try:
            name = winreg.EnumKey(root, index)
        except OSError:
            break
        if index == _MAX_UNINSTALL_KEYS_PER_VIEW:
            return ()
        if isinstance(name, str) and 0 < len(name) <= 512:
            names.append(name)
    return tuple(names)


def _registry_text(
    winreg: object,
    key: object,
    name: str,
    *,
    required: bool = True,
) -> str:
    try:
        raw, _value_type = winreg.QueryValueEx(key, name)
    except OSError:
        if required:
            raise
        return ""
    if not isinstance(raw, str):
        if required:
            raise ValueError("required registry metadata is not text")
        return ""
    value = raw.strip()
    if len(value) > _MAX_REGISTRY_TEXT_LENGTH or (required and not value):
        raise ValueError("registry metadata is invalid")
    return value


def _protected_roots(paths: WindowsDetectionPaths) -> tuple[str, ...]:
    roots: list[str] = []
    for raw in paths.protected_install_roots():
        try:
            root = _canonical_local_path(paths, raw)
            if paths.is_directory(root) and not paths.has_reparse_component(root) and not paths.is_user_writable(root):
                roots.append(root)
        except (OSError, TypeError, ValueError):
            continue
    return tuple(sorted(set(roots), key=lambda item: ntpath.normcase(item)))


def _canonical_local_path(paths: WindowsDetectionPaths, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("path metadata is invalid")
    candidate = paths.canonicalize(value.strip())
    drive, tail = ntpath.splitdrive(candidate)
    if not drive or drive.startswith("\\") or not tail.startswith("\\"):
        raise ValueError("path must be an absolute local Windows path")
    normalized = ntpath.normpath(candidate)
    if normalized in (drive, f"{drive}\\"):
        raise ValueError("volume roots are not installation directories")
    return normalized


def _is_contained(parent: str, child: str) -> bool:
    try:
        return ntpath.normcase(ntpath.commonpath((parent, child))) == ntpath.normcase(parent)
    except ValueError:
        return False


def _is_contained_by_any(path: str, roots: tuple[str, ...]) -> bool:
    return any(_is_contained(root, path) and ntpath.normcase(root) != ntpath.normcase(path) for root in roots)


def _candidate_order(candidate: NmapDetectionCandidateV1) -> tuple[int, str]:
    return (int(candidate.registry_view), ntpath.normcase(candidate.executable_path))
