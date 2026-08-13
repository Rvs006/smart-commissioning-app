"""Concrete, in-process Win32 adapters for the operator-managed Nmap lane.

The module is import-safe on non-Windows hosts.  Every live operation fails
closed unless ``sys.platform == "win32"``.  It deliberately contains no
process search, shell, installer, downloader, DNS, or socket-connect path.
"""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from smart_commissioning_core.engines.ip.nmap_detection import (
    WindowsDetectionPaths,
    WinregUninstallRegistry,
)
from smart_commissioning_core.engines.ip.nmap_profiles import (
    nmap_device_name_for_interface_id,
)
from smart_commissioning_core.engines.ip.nmap_runner import (
    NmapJobLimitsV1,
    NmapProcessLaunchV1,
    NmapRuntimeCapabilityProbe,
    NmapRuntimeCapabilitySnapshotV1,
    NmapVisibleInterfaceV1,
    NmapWindowsProcessApi,
)
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapAuthenticodeEvidenceV1,
    NmapTrustBackend,
)
from smart_commissioning_core.source_interface import enumerate_source_interfaces

_IS_WINDOWS = sys.platform == "win32"
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_VOLUME_NAME_DOS = 0
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_GROUP_SECURITY_INFORMATION = 0x2
_DACL_SECURITY_INFORMATION = 0x4
_WIN_LOCAL_SYSTEM_SID = 22
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_SECURITY_IMPERSONATION = 2
_MAXIMUM_ALLOWED = 0x02000000
_ERROR_INSUFFICIENT_BUFFER = 122
_WRITE_RIGHTS = (
    0x00000002
    | 0x00000004
    | 0x00000010
    | 0x00000040
    | 0x00000100
    | 0x00010000
    | 0x00040000
    | 0x00080000
)
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_FILE_GENERIC_EXECUTE = 0x001200A0
_FILE_ALL_ACCESS = 0x001F01FF
_MAX_PRIVILEGE_SET_BYTES = 64 * 1024
_MAX_CANONICAL_PATH_CHARS = 32_767
_MAX_MANIFEST_FILES = 65_536

_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SC_STATUS_PROCESS_INFO = 0
_SERVICE_RUNNING = 4

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_STILL_ACTIVE = 259
_ERROR_BROKEN_PIPE = 109
_ERROR_HANDLE_EOF = 38
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_MACHINE_GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _require_windows() -> None:
    if not _IS_WINDOWS:
        raise OSError("The operator-managed Nmap adapter requires Windows")


def nmap_uninstall_registry() -> WinregUninstallRegistry:
    """Return the concrete two-view HKLM uninstall reader."""

    _require_windows()
    import winreg

    return WinregUninstallRegistry(winreg)


def _windows_quote_argument(value: str) -> str:
    """Quote one argument using the CommandLineToArgvW-compatible grammar."""

    if not isinstance(value, str) or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows process arguments must be bounded strings")
    if value and not any(character in value for character in ' \t"'):
        return value
    result: list[str] = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            result.append("\\" * (backslashes * 2 + 1))
            result.append('"')
            backslashes = 0
            continue
        if backslashes:
            result.append("\\" * backslashes)
            backslashes = 0
        result.append(character)
    if backslashes:
        result.append("\\" * (backslashes * 2))
    result.append('"')
    return "".join(result)


def _windows_command_line(application_name: str, arguments: tuple[str, ...]) -> str:
    values = (application_name, *arguments)
    command_line = " ".join(_windows_quote_argument(value) for value in values)
    if len(command_line) > 32_767:
        raise ValueError("Nmap command line exceeds the Windows process limit")
    return command_line


class _GenericMapping(ctypes.Structure):
    _fields_ = [
        ("generic_read", wintypes.DWORD),
        ("generic_write", wintypes.DWORD),
        ("generic_execute", wintypes.DWORD),
        ("generic_all", wintypes.DWORD),
    ]


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _Guid:
        parsed = uuid.UUID(value)
        fields = parsed.fields
        return cls(
            fields[0],
            fields[1],
            fields[2],
            (ctypes.c_ubyte * 8)(
                fields[3],
                fields[4],
                *fields[5].to_bytes(6, "big"),
            ),
        )


class _WintrustFileInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("file_path", wintypes.LPCWSTR),
        ("file_handle", wintypes.HANDLE),
        ("known_subject", ctypes.POINTER(_Guid)),
    ]


class _WintrustDataUnion(ctypes.Union):
    _fields_ = [("file", ctypes.POINTER(_WintrustFileInfo))]


class _WintrustData(ctypes.Structure):
    _anonymous_ = ("choice",)
    _fields_ = [
        ("size", wintypes.DWORD),
        ("policy_callback_data", ctypes.c_void_p),
        ("sip_client_data", ctypes.c_void_p),
        ("ui_choice", wintypes.DWORD),
        ("revocation_checks", wintypes.DWORD),
        ("union_choice", wintypes.DWORD),
        ("choice", _WintrustDataUnion),
        ("state_action", wintypes.DWORD),
        ("state_data", wintypes.HANDLE),
        ("url_reference", wintypes.LPCWSTR),
        ("provider_flags", wintypes.DWORD),
        ("ui_context", wintypes.DWORD),
        ("signature_settings", ctypes.c_void_p),
    ]


class _CryptDataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _CryptIntegerBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _CryptAlgorithmIdentifier(ctypes.Structure):
    _fields_ = [("object_id", ctypes.c_char_p), ("parameters", _CryptDataBlob)]


class _CryptAttribute(ctypes.Structure):
    _fields_ = [
        ("object_id", ctypes.c_char_p),
        ("value_count", wintypes.DWORD),
        ("values", ctypes.POINTER(_CryptDataBlob)),
    ]


class _CryptAttributes(ctypes.Structure):
    _fields_ = [
        ("count", wintypes.DWORD),
        ("attributes", ctypes.POINTER(_CryptAttribute)),
    ]


class _CmsgSignerInfo(ctypes.Structure):
    _fields_ = [
        ("version", wintypes.DWORD),
        ("issuer", _CryptDataBlob),
        ("serial_number", _CryptIntegerBlob),
        ("hash_algorithm", _CryptAlgorithmIdentifier),
        ("hash_encryption_algorithm", _CryptAlgorithmIdentifier),
        ("encrypted_hash", _CryptDataBlob),
        ("auth_attributes", _CryptAttributes),
        ("unauth_attributes", _CryptAttributes),
    ]


class _CertInfoIssuerSerial(ctypes.Structure):
    _fields_ = [("issuer", _CryptDataBlob), ("serial_number", _CryptIntegerBlob)]


class _CertContext(ctypes.Structure):
    _fields_ = [
        ("encoding_type", wintypes.DWORD),
        ("encoded", ctypes.POINTER(ctypes.c_ubyte)),
        ("encoded_size", wintypes.DWORD),
        ("cert_info", ctypes.c_void_p),
        ("cert_store", wintypes.HANDLE),
    ]


class CtypesNmapTrustBackend(WindowsDetectionPaths, NmapTrustBackend):
    """Protected-path, ACL, Authenticode, version, and manifest Win32 backend."""

    def protected_install_roots(self) -> tuple[str, ...]:
        _require_windows()
        roots = {
            self.canonicalize(value)
            for value in (
                _known_folder_path("ProgramFiles"),
                _known_folder_path("ProgramFilesX86"),
            )
            if value
        }
        return tuple(sorted(roots, key=ntpath.normcase))

    def canonicalize(self, path: str) -> str:
        _require_windows()
        candidate = ntpath.normpath(str(path).strip())
        if not candidate or "\x00" in candidate:
            raise ValueError("Windows path is invalid")
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            candidate,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            get_final = kernel32.GetFinalPathNameByHandleW
            get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            get_final.restype = wintypes.DWORD
            required = get_final(handle, None, 0, _VOLUME_NAME_DOS)
            if required <= 0 or required > _MAX_CANONICAL_PATH_CHARS:
                raise OSError("canonical Windows path is unavailable")
            buffer = ctypes.create_unicode_buffer(required + 1)
            written = get_final(handle, buffer, len(buffer), _VOLUME_NAME_DOS)
            if written <= 0 or written >= len(buffer):
                raise OSError("canonical Windows path is unavailable")
            resolved = buffer.value
        finally:
            kernel32.CloseHandle(handle)
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
        return ntpath.normpath(resolved)

    def is_regular_file(self, path: str) -> bool:
        attributes = _file_attributes(path)
        return attributes is not None and not attributes & _FILE_ATTRIBUTE_DIRECTORY

    def is_directory(self, path: str) -> bool:
        attributes = _file_attributes(path)
        return attributes is not None and bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)

    def has_reparse_component(self, path: str) -> bool:
        _require_windows()
        candidate = ntpath.normpath(path)
        drive, tail = ntpath.splitdrive(candidate)
        if not drive or drive.startswith("\\"):
            return True
        current = f"{drive}\\"
        for component in (item for item in tail.split("\\") if item):
            current = ntpath.join(current, component)
            attributes = _file_attributes(current)
            if attributes is None or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        return False

    def is_user_writable(self, path: str) -> bool:
        try:
            _owner, dacl, descriptor = _named_file_security(path)
        except OSError:
            return True
        try:
            if not dacl:
                return True
            return bool(_current_process_token_access(descriptor) & _WRITE_RIGHTS)
        except OSError:
            return True
        finally:
            _local_free(descriptor)

    def is_administrator_owned(self, path: str) -> bool:
        try:
            owner, _dacl, descriptor = _named_file_security(path)
        except OSError:
            return False
        try:
            return any(
                _equal_sid(owner, _well_known_sid(sid_type))
                for sid_type in (
                    _WIN_LOCAL_SYSTEM_SID,
                    _WIN_BUILTIN_ADMINISTRATORS_SID,
                )
            )
        except OSError:
            return False
        finally:
            _local_free(descriptor)

    def authenticode(self, path: str) -> NmapAuthenticodeEvidenceV1:
        _require_windows()
        trusted = _win_verify_trust(path)
        publisher, certificate = _embedded_signer_certificate(path)
        return NmapAuthenticodeEvidenceV1(
            trusted=trusted,
            publisher=publisher,
            signer_sha256=hashlib.sha256(certificate).hexdigest(),
        )

    def file_version(self, path: str) -> str:
        return _file_product_version(path)

    def iter_files(self, directory: str) -> Iterable[str]:
        root = Path(directory)
        count = 0
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            for filename in filenames:
                count += 1
                if count > _MAX_MANIFEST_FILES:
                    raise ValueError("Nmap data directory exceeds the manifest file ceiling")
                yield str(Path(current, filename))

    def read_file(self, path: str, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        with open(path, "rb", buffering=0) as stream:
            payload = stream.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("Nmap data file exceeds its byte ceiling")
        return payload


class CtypesNmapRuntimeCapabilityProbe(NmapRuntimeCapabilityProbe):
    """Network-free MachineGuid, NIC, Npcap service/config, and token snapshot."""

    def __init__(self, *, winreg_module: object | None = None) -> None:
        if winreg_module is None:
            _require_windows()
            import winreg

            winreg_module = winreg
        self._winreg = winreg_module

    def snapshot(self) -> NmapRuntimeCapabilitySnapshotV1:
        machine_guid = _registry_machine_guid(self._winreg)
        installed = _registry_key_exists(
            self._winreg,
            r"SYSTEM\CurrentControlSet\Services\npcap",
        )
        npcap_version = _npcap_version(self._winreg) if installed else None
        if installed and npcap_version is None:
            raise ValueError("Installed Npcap version is unavailable")
        running = installed and _npcap_service_running()
        admin_only = installed and bool(
            _registry_dword(
                self._winreg,
                r"SYSTEM\CurrentControlSet\Services\npcap\Parameters",
                "AdminOnly",
                default=0,
            )
        )
        administrator = _token_is_administrator()
        raw_rights = bool(running and (not admin_only or administrator))
        interfaces = tuple(
            NmapVisibleInterfaceV1(
                interface_id=item.interface_id,
                interface_name=item.interface_name,
                source_ip=item.source_ip,
                nmap_device_name=(
                    nmap_device_name_for_interface_id(item.interface_id) if running else None
                ),
            )
            for item in enumerate_source_interfaces()[:256]
            if item.is_up
        )
        return NmapRuntimeCapabilitySnapshotV1(
            executor_identity=f"machine-guid:{machine_guid.casefold()}",
            interfaces=interfaces,
            npcap_installed=installed,
            npcap_version=npcap_version,
            npcap_service_running=running,
            npcap_admin_only=admin_only,
            current_token_is_administrator=administrator,
            current_token_has_raw_rights=raw_rights,
        )


def _known_folder_path(name: Literal["ProgramFiles", "ProgramFilesX86"]) -> str:
    _require_windows()
    folder_ids = {
        "ProgramFiles": "905e63b6-c1bf-494e-b29c-65b732d3d21a",
        "ProgramFilesX86": "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e",
    }
    shell32 = ctypes.WinDLL("shell32.dll", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32.dll", use_last_error=True)
    value = wintypes.LPWSTR()
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(_Guid.parse(folder_ids[name])),
        0,
        None,
        ctypes.byref(value),
    )
    if result != 0 or not value.value:
        raise ctypes.WinError(result)
    try:
        return value.value
    finally:
        ole32.CoTaskMemFree(value)


def _file_attributes(path: str) -> int | None:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    function = kernel32.GetFileAttributesW
    function.argtypes = [wintypes.LPCWSTR]
    function.restype = wintypes.DWORD
    attributes = int(function(path))
    return None if attributes == _INVALID_FILE_ATTRIBUTES else attributes


def _named_file_security(path: str) -> tuple[int, int, int]:
    _require_windows()
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = get_security(
        path,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _GROUP_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise ctypes.WinError(result)
    return int(owner.value or 0), int(dacl.value or 0), int(descriptor.value or 0)


def _well_known_sid(sid_type: int) -> ctypes.Array[ctypes.c_char]:
    _require_windows()
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    size = wintypes.DWORD(68)
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi32.CreateWellKnownSid(sid_type, None, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer


def _current_process_token_access(descriptor: int) -> int:
    """Return file rights granted to the effective process token.

    AccessCheck evaluates the token's user SID and every enabled group SID,
    including local and domain groups.  Any API uncertainty raises so the
    caller can reject the path fail closed.
    """

    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.DuplicateToken.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateToken.restype = wintypes.BOOL
    advapi32.AccessCheck.argtypes = [
        ctypes.c_void_p,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(_GenericMapping),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.AccessCheck.restype = wintypes.BOOL

    process_token = wintypes.HANDLE()
    impersonation_token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            _TOKEN_QUERY | _TOKEN_DUPLICATE,
            ctypes.byref(process_token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.DuplicateToken(
            process_token,
            _SECURITY_IMPERSONATION,
            ctypes.byref(impersonation_token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        mapping = _GenericMapping(
            _FILE_GENERIC_READ,
            _FILE_GENERIC_WRITE,
            _FILE_GENERIC_EXECUTE,
            _FILE_ALL_ACCESS,
        )
        privilege_size = wintypes.DWORD()
        granted_access = wintypes.DWORD()
        access_status = wintypes.BOOL()
        first_result = advapi32.AccessCheck(
            ctypes.c_void_p(descriptor),
            impersonation_token,
            wintypes.DWORD(_MAXIMUM_ALLOWED),
            ctypes.byref(mapping),
            None,
            ctypes.byref(privilege_size),
            ctypes.byref(granted_access),
            ctypes.byref(access_status),
        )
        if first_result:
            return int(granted_access.value) if access_status.value else 0
        error = ctypes.get_last_error()
        if (
            error != _ERROR_INSUFFICIENT_BUFFER
            or privilege_size.value <= 0
            or privilege_size.value > _MAX_PRIVILEGE_SET_BYTES
        ):
            raise ctypes.WinError(error)

        privilege_set = ctypes.create_string_buffer(privilege_size.value)
        if not advapi32.AccessCheck(
            ctypes.c_void_p(descriptor),
            impersonation_token,
            wintypes.DWORD(_MAXIMUM_ALLOWED),
            ctypes.byref(mapping),
            privilege_set,
            ctypes.byref(privilege_size),
            ctypes.byref(granted_access),
            ctypes.byref(access_status),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(granted_access.value) if access_status.value else 0
    finally:
        if impersonation_token.value:
            kernel32.CloseHandle(impersonation_token)
        if process_token.value:
            kernel32.CloseHandle(process_token)


def _equal_sid(left: int, right: object) -> bool:
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    return bool(advapi32.EqualSid(ctypes.c_void_p(left), right))


def _local_free(pointer: int) -> None:
    if pointer:
        ctypes.WinDLL("kernel32.dll", use_last_error=True).LocalFree(ctypes.c_void_p(pointer))


def _win_verify_trust(path: str) -> bool:
    wintrust = ctypes.WinDLL("wintrust.dll", use_last_error=True)
    wintrust.WinVerifyTrust.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
    ]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    action = _Guid.parse("00aac56b-cd44-11d0-8cc2-00c04fc295ee")
    file_info = _WintrustFileInfo(
        ctypes.sizeof(_WintrustFileInfo),
        path,
        None,
        None,
    )
    data = _WintrustData()
    data.size = ctypes.sizeof(_WintrustData)
    data.ui_choice = 2
    data.revocation_checks = 0
    data.union_choice = 1
    data.file = ctypes.pointer(file_info)
    data.state_action = 1
    data.provider_flags = 0x10 | 0x1000 | 0x2000
    status = int(wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data)))
    data.state_action = 2
    wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    return status == 0


def _embedded_signer_certificate(path: str) -> tuple[str, bytes]:
    """Return the embedded signer certificate selected by CryptQueryObject.

    WinVerifyTrust remains the trust authority.  The first embedded certificate
    is used only to pin the leaf certificate digest and display name.  Official
    Nmap Authenticode packages place the signer first; admin confirmation then
    pins that exact digest for every later launch.
    """

    crypt32 = _crypt32()
    encoding = wintypes.DWORD()
    content = wintypes.DWORD()
    file_format = wintypes.DWORD()
    store = wintypes.HANDLE()
    message = wintypes.HANDLE()
    context = ctypes.c_void_p()
    query = crypt32.CryptQueryObject
    if not query(
        1,
        ctypes.c_wchar_p(path),
        0x400,
        2,
        0,
        ctypes.byref(encoding),
        ctypes.byref(content),
        ctypes.byref(file_format),
        ctypes.byref(store),
        ctypes.byref(message),
        ctypes.byref(context),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    certificate_context = ctypes.c_void_p()
    signer_buffer: ctypes.Array[ctypes.c_char] | None = None
    try:
        signer_size = wintypes.DWORD()
        if not crypt32.CryptMsgGetParam(message, 6, 0, None, ctypes.byref(signer_size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if signer_size.value <= 0 or signer_size.value > 1_048_576:
            raise OSError("Authenticode signer metadata is unavailable")
        signer_buffer = ctypes.create_string_buffer(signer_size.value)
        if not crypt32.CryptMsgGetParam(
            message,
            6,
            0,
            signer_buffer,
            ctypes.byref(signer_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        signer = ctypes.cast(
            signer_buffer,
            ctypes.POINTER(_CmsgSignerInfo),
        ).contents
        issuer_serial = _CertInfoIssuerSerial(signer.issuer, signer.serial_number)
        certificate_context = crypt32.CertFindCertificateInStore(
            store,
            encoding.value,
            0,
            5,
            ctypes.byref(issuer_serial),
            None,
        )
        if not certificate_context:
            raise OSError("Authenticode signer certificate is unavailable")
        certificate = ctypes.cast(
            certificate_context,
            ctypes.POINTER(_CertContext),
        ).contents
        encoded = ctypes.string_at(certificate.encoded, certificate.encoded_size)
        name_size = crypt32.CertGetNameStringW(certificate_context, 4, 0, None, None, 0)
        if name_size <= 1 or name_size > 513:
            raise OSError("Authenticode publisher is unavailable")
        name = ctypes.create_unicode_buffer(name_size)
        if crypt32.CertGetNameStringW(
            certificate_context,
            4,
            0,
            None,
            name,
            len(name),
        ) != name_size:
            raise OSError("Authenticode publisher is unavailable")
        return name.value, encoded
    finally:
        if certificate_context:
            crypt32.CertFreeCertificateContext(certificate_context)
        if message:
            crypt32.CryptMsgClose(message)
        if store:
            crypt32.CertCloseStore(store, 0)


class _VsFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("signature", wintypes.DWORD),
        ("structure_version", wintypes.DWORD),
        ("file_version_ms", wintypes.DWORD),
        ("file_version_ls", wintypes.DWORD),
        ("product_version_ms", wintypes.DWORD),
        ("product_version_ls", wintypes.DWORD),
        ("file_flags_mask", wintypes.DWORD),
        ("file_flags", wintypes.DWORD),
        ("file_os", wintypes.DWORD),
        ("file_type", wintypes.DWORD),
        ("file_subtype", wintypes.DWORD),
        ("file_date_ms", wintypes.DWORD),
        ("file_date_ls", wintypes.DWORD),
    ]


def _file_product_version(path: str) -> str:
    _require_windows()
    version = ctypes.WinDLL("version.dll", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = int(version.GetFileVersionInfoSizeW(path, None))
    if size <= 0 or size > 16 * 1024 * 1024:
        raise OSError("Nmap file version is unavailable")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise OSError("Nmap file version is unavailable")
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VsFixedFileInfo)).contents
    if fixed.signature != 0xFEEF04BD:
        raise OSError("Nmap file version metadata is invalid")
    parts = [
        fixed.product_version_ms >> 16,
        fixed.product_version_ms & 0xFFFF,
        fixed.product_version_ls >> 16,
        fixed.product_version_ls & 0xFFFF,
    ]
    while len(parts) > 2 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(part) for part in parts)


def _registry_machine_guid(winreg: object) -> str:
    value = _registry_value(
        winreg,
        r"SOFTWARE\Microsoft\Cryptography",
        "MachineGuid",
    )
    text = str(value).strip()
    if not _MACHINE_GUID_PATTERN.fullmatch(text):
        raise ValueError("Windows MachineGuid is unavailable")
    return text


def _registry_key_exists(winreg: object, path: str) -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            path,
            0,
            int(winreg.KEY_READ),
        ):
            return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _registry_value(winreg: object, path: str, name: str) -> object:
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        path,
        0,
        int(winreg.KEY_READ),
    ) as key:
        value, _value_type = winreg.QueryValueEx(key, name)
    return value


def _registry_dword(winreg: object, path: str, name: str, *, default: int) -> int:
    try:
        value = _registry_value(winreg, path, name)
    except (AttributeError, OSError, TypeError, ValueError):
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _npcap_version(winreg: object) -> str | None:
    paths = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NpcapInst",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\NpcapInst",
        r"SYSTEM\CurrentControlSet\Services\npcap\Parameters",
    )
    for path in paths:
        for name in ("DisplayVersion", "NpcapVersion", "Version"):
            try:
                value = _registry_value(winreg, path, name)
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            text = str(value).strip()
            if text and len(text) <= 128 and not any(character in text for character in "\x00\r\n"):
                return text
    return None


class _ServiceStatusProcess(ctypes.Structure):
    _fields_ = [
        ("service_type", wintypes.DWORD),
        ("current_state", wintypes.DWORD),
        ("controls_accepted", wintypes.DWORD),
        ("win32_exit_code", wintypes.DWORD),
        ("service_specific_exit_code", wintypes.DWORD),
        ("check_point", wintypes.DWORD),
        ("wait_hint", wintypes.DWORD),
        ("process_id", wintypes.DWORD),
        ("service_flags", wintypes.DWORD),
    ]


def _npcap_service_running() -> bool:
    _require_windows()
    advapi32 = _service_advapi32()
    manager = advapi32.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not manager:
        return False
    service = None
    try:
        service = advapi32.OpenServiceW(manager, "npcap", _SERVICE_QUERY_STATUS)
        if not service:
            return False
        status = _ServiceStatusProcess()
        needed = wintypes.DWORD()
        if not advapi32.QueryServiceStatusEx(
            service,
            _SC_STATUS_PROCESS_INFO,
            ctypes.byref(status),
            ctypes.sizeof(status),
            ctypes.byref(needed),
        ):
            return False
        return status.current_state == _SERVICE_RUNNING
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(manager)


def _token_is_administrator() -> bool:
    _require_windows()
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    membership = wintypes.BOOL()
    sid = _well_known_sid(_WIN_BUILTIN_ADMINISTRATORS_SID)
    if not advapi32.CheckTokenMembership(None, sid, ctypes.byref(membership)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(membership.value)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", wintypes.BOOL),
    ]


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved_2_size", wintypes.WORD),
        ("reserved_2", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("startup", _StartupInfoW), ("attribute_list", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic", _JobBasicLimitInformation),
        ("io", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("total_terminated_processes", wintypes.DWORD),
    ]


@dataclass
class _WindowsNmapProcess:
    process: int
    thread: int | None
    stdout_read: int
    stderr_read: int
    process_id: int


@dataclass
class _WindowsNmapJob:
    handle: int


class CtypesNmapWindowsProcessApi(NmapWindowsProcessApi):
    """CreateProcessW and kill-on-close Job Object implementation.

    The process is created suspended with a non-null application path.  An
    explicit PROC_THREAD_ATTRIBUTE_HANDLE_LIST permits only the two pipe write
    handles, so ambient inheritable handles cannot cross the child boundary.
    """

    def system_root(self) -> str:
        _require_windows()
        kernel32 = _process_kernel32()
        size = int(kernel32.GetWindowsDirectoryW(None, 0))
        if size <= 0 or size > _MAX_CANONICAL_PATH_CHARS:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = int(kernel32.GetWindowsDirectoryW(buffer, len(buffer)))
        if written <= 0 or written >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        return ntpath.normpath(buffer.value)

    def monotonic(self) -> float:
        return time.monotonic()

    def create_kill_on_close_job(self, limits: NmapJobLimitsV1) -> _WindowsNmapJob:
        _require_windows()
        validated = NmapJobLimitsV1.model_validate(limits)
        kernel32 = _process_kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = _JobExtendedLimitInformation()
        information.basic.per_process_user_time = int(
            validated.cpu_time_seconds * 10_000_000
        )
        information.basic.active_process_limit = validated.active_process_limit
        information.basic.limit_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_PROCESS_TIME
            | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        information.process_memory_limit = validated.process_memory_bytes
        information.job_memory_limit = validated.job_memory_bytes
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        return _WindowsNmapJob(handle=int(handle))

    def create_process_suspended(
        self,
        launch: NmapProcessLaunchV1,
    ) -> _WindowsNmapProcess:
        _require_windows()
        validated = NmapProcessLaunchV1.model_validate(launch)
        kernel32 = _process_kernel32()
        security = _SecurityAttributes(
            length=ctypes.sizeof(_SecurityAttributes),
            security_descriptor=None,
            inherit_handle=True,
        )
        stdout_read = wintypes.HANDLE()
        stdout_write = wintypes.HANDLE()
        stderr_read = wintypes.HANDLE()
        stderr_write = wintypes.HANDLE()
        handles_to_close: list[int] = []
        attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
        attribute_list = ctypes.c_void_p()
        created = False
        process_info = _ProcessInformation()
        try:
            for read_handle, write_handle in (
                (stdout_read, stdout_write),
                (stderr_read, stderr_write),
            ):
                if not kernel32.CreatePipe(
                    ctypes.byref(read_handle),
                    ctypes.byref(write_handle),
                    ctypes.byref(security),
                    0,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                handles_to_close.extend((int(read_handle.value), int(write_handle.value)))
                if not kernel32.SetHandleInformation(
                    read_handle,
                    _HANDLE_FLAG_INHERIT,
                    0,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())

            attribute_size = ctypes.c_size_t()
            kernel32.InitializeProcThreadAttributeList(
                None,
                1,
                0,
                ctypes.byref(attribute_size),
            )
            if attribute_size.value <= 0 or attribute_size.value > 1_048_576:
                raise OSError("Windows process attribute list is unavailable")
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            if not kernel32.InitializeProcThreadAttributeList(
                attribute_list,
                1,
                0,
                ctypes.byref(attribute_size),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            inherited_handles = (wintypes.HANDLE * 2)(
                stdout_write.value,
                stderr_write.value,
            )
            if not kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(inherited_handles),
                ctypes.sizeof(inherited_handles),
                None,
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            startup = _StartupInfoExW()
            startup.startup.cb = ctypes.sizeof(_StartupInfoExW)
            startup.startup.flags = _STARTF_USESTDHANDLES
            startup.startup.stdin = None
            startup.startup.stdout = stdout_write
            startup.startup.stderr = stderr_write
            startup.attribute_list = attribute_list
            command_line = ctypes.create_unicode_buffer(
                _windows_command_line(validated.application_name, validated.arguments)
            )
            environment = _environment_block(validated.environment)
            created = bool(
                kernel32.CreateProcessW(
                    validated.application_name,
                    command_line,
                    None,
                    None,
                    True,
                    _CREATE_SUSPENDED
                    | _CREATE_NO_WINDOW
                    | _CREATE_UNICODE_ENVIRONMENT
                    | _EXTENDED_STARTUPINFO_PRESENT,
                    environment,
                    validated.working_directory,
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
            )
            if not created:
                raise ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(stdout_write)
            kernel32.CloseHandle(stderr_write)
            handles_to_close.remove(int(stdout_write.value))
            handles_to_close.remove(int(stderr_write.value))
            return _WindowsNmapProcess(
                process=int(process_info.process),
                thread=int(process_info.thread),
                stdout_read=int(stdout_read.value),
                stderr_read=int(stderr_read.value),
                process_id=int(process_info.process_id),
            )
        except Exception:
            if created:
                if process_info.process:
                    kernel32.TerminateProcess(process_info.process, 0xC000013A)
                    kernel32.CloseHandle(process_info.process)
                if process_info.thread:
                    kernel32.CloseHandle(process_info.thread)
            for handle in handles_to_close:
                if handle:
                    kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise
        finally:
            if attribute_list:
                kernel32.DeleteProcThreadAttributeList(attribute_list)

    def assign_process_to_job(
        self,
        job: _WindowsNmapJob,
        process: _WindowsNmapProcess,
    ) -> None:
        kernel32 = _process_kernel32()
        if not kernel32.AssignProcessToJobObject(job.handle, process.process):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume_process(self, process: _WindowsNmapProcess) -> None:
        if process.thread is None:
            raise RuntimeError("Nmap process thread has already been resumed")
        kernel32 = _process_kernel32()
        result = int(kernel32.ResumeThread(process.thread))
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(process.thread)
        process.thread = None

    def read_pipe(
        self,
        process: _WindowsNmapProcess,
        stream: Literal["stdout", "stderr"],
        max_bytes: int,
    ) -> bytes:
        if max_bytes <= 0 or max_bytes > 1_048_576:
            raise ValueError("pipe read size is outside its bounded range")
        handle = process.stdout_read if stream == "stdout" else process.stderr_read
        kernel32 = _process_kernel32()
        buffer = ctypes.create_string_buffer(max_bytes)
        received = wintypes.DWORD()
        if not kernel32.ReadFile(
            handle,
            buffer,
            max_bytes,
            ctypes.byref(received),
            None,
        ):
            error = ctypes.get_last_error()
            if error in {_ERROR_BROKEN_PIPE, _ERROR_HANDLE_EOF}:
                return b""
            raise ctypes.WinError(error)
        return buffer.raw[: received.value]

    def wait_process(
        self,
        process: _WindowsNmapProcess,
        timeout_seconds: float,
    ) -> int | None:
        if timeout_seconds < 0 or timeout_seconds > 3600:
            raise ValueError("process wait is outside its bounded range")
        kernel32 = _process_kernel32()
        milliseconds = min(0xFFFFFFFE, max(0, int(timeout_seconds * 1000 + 0.999)))
        result = int(kernel32.WaitForSingleObject(process.process, milliseconds))
        if result == _WAIT_TIMEOUT:
            return None
        if result != _WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.process, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exit_code.value == _STILL_ACTIVE:
            return None
        return int(exit_code.value)

    def terminate_job(self, job: _WindowsNmapJob, exit_code: int) -> None:
        kernel32 = _process_kernel32()
        if not kernel32.TerminateJobObject(job.handle, wintypes.UINT(exit_code & 0xFFFFFFFF)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate_suspended_process(
        self,
        process: _WindowsNmapProcess,
        exit_code: int,
        timeout_seconds: float,
    ) -> bool:
        if timeout_seconds < 0 or timeout_seconds > 5:
            raise ValueError("suspended process cleanup wait is outside its bounded range")
        kernel32 = _process_kernel32()
        if not kernel32.TerminateProcess(
            process.process,
            wintypes.UINT(exit_code & 0xFFFFFFFF),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        result = int(
            kernel32.WaitForSingleObject(
                process.process,
                max(0, int(timeout_seconds * 1000 + 0.999)),
            )
        )
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def wait_job_empty(self, job: _WindowsNmapJob, timeout_seconds: float) -> bool:
        if timeout_seconds < 0 or timeout_seconds > 60:
            raise ValueError("job cleanup wait is outside its bounded range")
        kernel32 = _process_kernel32()
        deadline = time.monotonic() + timeout_seconds
        while True:
            accounting = _JobBasicAccountingInformation()
            if not kernel32.QueryInformationJobObject(
                job.handle,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active_processes == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def close_process(self, process: _WindowsNmapProcess) -> None:
        kernel32 = _process_kernel32()
        handles = [process.stdout_read, process.stderr_read, process.thread, process.process]
        process.stdout_read = 0
        process.stderr_read = 0
        process.thread = None
        process.process = 0
        for handle in handles:
            if handle:
                kernel32.CloseHandle(wintypes.HANDLE(handle))

    def close_job(self, job: _WindowsNmapJob) -> None:
        if not job.handle:
            return
        handle = job.handle
        job.handle = 0
        _process_kernel32().CloseHandle(
            wintypes.HANDLE(handle)
        )


def _environment_block(values: tuple[tuple[str, str], ...]) -> ctypes.Array[ctypes.c_wchar]:
    rows = [f"{key}={value}" for key, value in sorted(values, key=lambda row: row[0].casefold())]
    payload = "\x00".join(rows) + "\x00\x00"
    if len(payload) > 32_767:
        raise ValueError("Nmap environment exceeds the Windows process limit")
    return ctypes.create_unicode_buffer(payload)


def _service_advapi32() -> object:
    library = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    library.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    library.OpenSCManagerW.restype = wintypes.HANDLE
    library.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    library.OpenServiceW.restype = wintypes.HANDLE
    library.QueryServiceStatusEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    library.QueryServiceStatusEx.restype = wintypes.BOOL
    library.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    library.CloseServiceHandle.restype = wintypes.BOOL
    return library


def _crypt32() -> object:
    library = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    library.CryptQueryObject.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.CryptQueryObject.restype = wintypes.BOOL
    library.CryptMsgGetParam.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    library.CryptMsgGetParam.restype = wintypes.BOOL
    library.CertFindCertificateInStore.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    library.CertFindCertificateInStore.restype = ctypes.c_void_p
    library.CertGetNameStringW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    library.CertGetNameStringW.restype = wintypes.DWORD
    library.CertFreeCertificateContext.argtypes = [ctypes.c_void_p]
    library.CertFreeCertificateContext.restype = wintypes.BOOL
    library.CryptMsgClose.argtypes = [wintypes.HANDLE]
    library.CryptMsgClose.restype = wintypes.BOOL
    library.CertCloseStore.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.CertCloseStore.restype = wintypes.BOOL
    return library


def _process_kernel32() -> object:
    library = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    library.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    library.GetWindowsDirectoryW.restype = wintypes.UINT
    library.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    library.CreateJobObjectW.restype = wintypes.HANDLE
    library.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    library.SetInformationJobObject.restype = wintypes.BOOL
    library.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
    ]
    library.CreatePipe.restype = wintypes.BOOL
    library.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    library.SetHandleInformation.restype = wintypes.BOOL
    library.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    library.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    library.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    library.UpdateProcThreadAttribute.restype = wintypes.BOOL
    library.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    library.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessInformation),
    ]
    library.CreateProcessW.restype = wintypes.BOOL
    library.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    library.AssignProcessToJobObject.restype = wintypes.BOOL
    library.ResumeThread.argtypes = [wintypes.HANDLE]
    library.ResumeThread.restype = wintypes.DWORD
    library.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    library.ReadFile.restype = wintypes.BOOL
    library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.WaitForSingleObject.restype = wintypes.DWORD
    library.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    library.GetExitCodeProcess.restype = wintypes.BOOL
    library.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    library.TerminateJobObject.restype = wintypes.BOOL
    library.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    library.TerminateProcess.restype = wintypes.BOOL
    library.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    library.QueryInformationJobObject.restype = wintypes.BOOL
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    return library
