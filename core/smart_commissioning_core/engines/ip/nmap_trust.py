"""Fail-closed trust inspection for an operator-installed Windows Nmap copy."""

from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smart_commissioning_core.engines.ip.nmap_detection import NmapDetectionCandidateV1
from smart_commissioning_core.engines.ip.nmap_profiles import NmapReviewedScriptV1
from smart_commissioning_core.run_context import canonical_sha256

_NPSL_VERSION = re.compile(
    r"Nmap Public Source License(?:\s*\(NPSL\))?\s*,?\s*Version\s+"
    r"([0-9]+(?:\.[0-9]+)*)",
    re.IGNORECASE,
)
_LICENCE_NAMES = frozenset({"license", "license.txt", "npsl", "npsl.txt"})
_DEFAULT_MAX_DATA_FILES = 8192
_DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_MANIFEST_BYTES = 512 * 1024 * 1024


class NmapTrustReason(StrEnum):
    AVAILABLE = "available"
    PATH_REJECTED = "path_rejected"
    REPARSE_REJECTED = "reparse_rejected"
    ACL_REJECTED = "acl_rejected"
    SIGNATURE_REJECTED = "signature_rejected"
    PUBLISHER_REJECTED = "publisher_rejected"
    VERSION_REJECTED = "version_rejected"
    EXECUTABLE_DIGEST_REJECTED = "executable_digest_rejected"
    DATA_MANIFEST_REJECTED = "data_manifest_rejected"
    LICENCE_REJECTED = "licence_rejected"
    REVIEWED_SCRIPT_REJECTED = "reviewed_script_rejected"
    FINGERPRINT_DRIFT = "fingerprint_drift"
    INSPECTION_FAILED = "inspection_failed"


class NmapAuthenticodeEvidenceV1(BaseModel):
    """Result of WinVerifyTrust plus signer-certificate extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    trusted: bool
    publisher: str = Field(min_length=1, max_length=512)
    signer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NmapTrustPolicyV1(BaseModel):
    """Administrator-approved exact publisher, release, signer, and hash policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    permitted_publishers: tuple[str, ...] = Field(min_length=1, max_length=8)
    permitted_versions: tuple[str, ...] = Field(min_length=1, max_length=32)
    permitted_signer_sha256: tuple[str, ...] = Field(min_length=1, max_length=32)
    permitted_executable_sha256: tuple[str, ...] = Field(min_length=1, max_length=64)
    permitted_npsl_versions: tuple[str, ...] = Field(min_length=1, max_length=16)
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = Field(default=(), max_length=64)
    max_data_files: int = Field(default=8192, ge=1, le=65_536)
    max_file_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    max_manifest_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024 * 1024,
    )

    @model_validator(mode="after")
    def _canonical_policy(self) -> NmapTrustPolicyV1:
        text_groups = (
            self.permitted_publishers,
            self.permitted_versions,
            self.permitted_npsl_versions,
        )
        if any(any(not item or len(item) > 512 for item in group) for group in text_groups):
            raise ValueError("Nmap trust text policy contains an invalid value")
        digest_groups = (self.permitted_signer_sha256, self.permitted_executable_sha256)
        if any(any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in group) for group in digest_groups):
            raise ValueError("Nmap trust digests must be lowercase SHA-256 values")
        for group in (*text_groups, *digest_groups):
            if (
                len({item.casefold() for item in group}) != len(group)
                or tuple(sorted(group, key=str.casefold)) != group
            ):
                raise ValueError("Nmap trust policy values must be unique and sorted")
        if tuple(sorted(self.reviewed_scripts, key=lambda item: item.name)) != self.reviewed_scripts:
            raise ValueError("reviewed Nmap scripts must be unique and sorted")
        if len({item.name for item in self.reviewed_scripts}) != len(self.reviewed_scripts):
            raise ValueError("reviewed Nmap scripts must be unique and sorted")
        return self


class NmapInstallationFingerprintV1(BaseModel):
    """Protected exact identity persisted by the one-time admin confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    install_root: str = Field(min_length=3, max_length=2048)
    executable_path: str = Field(min_length=3, max_length=2048)
    data_directory: str = Field(min_length=3, max_length=2048)
    publisher: str = Field(min_length=1, max_length=512)
    signer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(min_length=1, max_length=128)
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_file_count: int = Field(ge=1, le=65_536)
    data_total_bytes: int = Field(ge=1, le=4 * 1024 * 1024 * 1024)
    licence_relative_path: str = Field(min_length=1, max_length=255)
    licence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    npsl_version: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = Field(default=(), max_length=64)
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_aggregate(self) -> NmapInstallationFingerprintV1:
        if self.fingerprint_sha256 != canonical_sha256(_fingerprint_payload(self)):
            raise ValueError("Nmap installation aggregate fingerprint is invalid")
        return self


class NmapTrustResultV1(BaseModel):
    """Sanitized capability result; local details stay in the fingerprint record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    available: bool
    reason: NmapTrustReason
    fingerprint: NmapInstallationFingerprintV1 | None = None

    @model_validator(mode="after")
    def _consistent_result(self) -> NmapTrustResultV1:
        if self.reason is NmapTrustReason.AVAILABLE:
            consistent = self.available and self.fingerprint is not None
        else:
            consistent = not self.available and self.fingerprint is None
        if not consistent:
            raise ValueError("Nmap trust result is inconsistent")
        return self


class NmapTrustBackend(Protocol):
    """Injected Windows file, ACL, Authenticode, and version APIs."""

    def protected_install_roots(self) -> tuple[str, ...]: ...

    def canonicalize(self, path: str) -> str: ...

    def is_regular_file(self, path: str) -> bool: ...

    def is_directory(self, path: str) -> bool: ...

    def has_reparse_component(self, path: str) -> bool: ...

    def is_user_writable(self, path: str) -> bool: ...

    def is_administrator_owned(self, path: str) -> bool: ...

    def authenticode(self, path: str) -> NmapAuthenticodeEvidenceV1: ...

    def file_version(self, path: str) -> str: ...

    def iter_files(self, directory: str) -> Iterable[str]: ...

    def read_file(self, path: str, *, max_bytes: int) -> bytes: ...


class _TrustFailure(Exception):
    def __init__(self, reason: NmapTrustReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def inspect_nmap_installation(
    *,
    candidate: NmapDetectionCandidateV1,
    policy: NmapTrustPolicyV1,
    backend: NmapTrustBackend,
) -> NmapTrustResultV1:
    """Inspect one candidate without process or network activity."""

    try:
        fingerprint = _inspect(candidate=candidate, policy=policy, backend=backend)
    except _TrustFailure as error:
        return _unavailable(error.reason)
    except (OSError, TypeError, ValueError, UnicodeError):
        return _unavailable(NmapTrustReason.INSPECTION_FAILED)
    return NmapTrustResultV1(
        available=True,
        reason=NmapTrustReason.AVAILABLE,
        fingerprint=fingerprint,
    )


def inspect_nmap_installation_for_administrator_approval(
    *,
    candidate: NmapDetectionCandidateV1,
    backend: NmapTrustBackend,
) -> NmapTrustResultV1:
    """Inspect one locally installed Nmap copy before a global-admin approval.

    This is intentionally narrower than the normal policy inspection: an
    administrator has not approved the exact publisher, release and digests
    yet, so the first observation records those values rather than comparing
    them to a pre-existing allowlist. The non-negotiable local checks still
    apply: protected install location, path/ACL safety, trusted Authenticode,
    bounded manifest construction, and a parseable NPSL file.
    """

    try:
        fingerprint = _inspect(candidate=candidate, policy=None, backend=backend)
    except _TrustFailure as error:
        return _unavailable(error.reason)
    except (OSError, TypeError, ValueError, UnicodeError):
        return _unavailable(NmapTrustReason.INSPECTION_FAILED)
    return NmapTrustResultV1(
        available=True,
        reason=NmapTrustReason.AVAILABLE,
        fingerprint=fingerprint,
    )


def revalidate_nmap_installation(
    *,
    candidate: NmapDetectionCandidateV1,
    confirmed: NmapInstallationFingerprintV1,
    policy: NmapTrustPolicyV1,
    backend: NmapTrustBackend,
) -> NmapTrustResultV1:
    """Repeat every trust check immediately before launch and compare exactly."""

    current = inspect_nmap_installation(candidate=candidate, policy=policy, backend=backend)
    if not current.available or current.fingerprint is None:
        return current
    if current.fingerprint != confirmed:
        return _unavailable(NmapTrustReason.FINGERPRINT_DRIFT)
    return current


def _inspect(
    *,
    candidate: NmapDetectionCandidateV1,
    policy: NmapTrustPolicyV1 | None,
    backend: NmapTrustBackend,
) -> NmapInstallationFingerprintV1:
    protected_roots = tuple(_canonical_path(backend, item) for item in backend.protected_install_roots())
    install_root = _canonical_path(backend, candidate.install_root)
    executable_path = _canonical_path(backend, candidate.executable_path)
    data_directory = _canonical_path(backend, candidate.data_directory)
    if not protected_roots or not any(
        _contained(root, install_root) and ntpath.normcase(root) != ntpath.normcase(install_root)
        for root in protected_roots
    ):
        raise _TrustFailure(NmapTrustReason.PATH_REJECTED)
    if not _contained(install_root, executable_path) or not _contained(install_root, data_directory):
        raise _TrustFailure(NmapTrustReason.PATH_REJECTED)
    if (
        install_root != ntpath.normpath(candidate.install_root)
        or executable_path != ntpath.normpath(candidate.executable_path)
        or data_directory != ntpath.normpath(candidate.data_directory)
        or ntpath.basename(executable_path).casefold() != "nmap.exe"
        or not backend.is_directory(install_root)
        or not backend.is_directory(data_directory)
        or not backend.is_regular_file(executable_path)
    ):
        raise _TrustFailure(NmapTrustReason.PATH_REJECTED)
    _check_path_security(backend, (install_root, data_directory, executable_path))

    signature = NmapAuthenticodeEvidenceV1.model_validate(backend.authenticode(executable_path))
    if not signature.trusted:
        raise _TrustFailure(NmapTrustReason.SIGNATURE_REJECTED)
    version = backend.file_version(executable_path).strip()
    if not version:
        raise _TrustFailure(NmapTrustReason.VERSION_REJECTED)
    if policy is not None:
        permitted_publishers = {item.casefold() for item in policy.permitted_publishers}
        if signature.publisher.casefold() not in permitted_publishers:
            raise _TrustFailure(NmapTrustReason.PUBLISHER_REJECTED)
        if signature.signer_sha256 not in policy.permitted_signer_sha256:
            raise _TrustFailure(NmapTrustReason.SIGNATURE_REJECTED)
        if version not in policy.permitted_versions:
            raise _TrustFailure(NmapTrustReason.VERSION_REJECTED)

    executable_key = ntpath.normcase(ntpath.relpath(executable_path, data_directory))
    retained_paths = {
        executable_key,
        *(
            ntpath.normcase(ntpath.join("scripts", f"{script.name}.nse"))
            for script in (() if policy is None else policy.reviewed_scripts)
        ),
    }
    manifest_rows, contents = _build_manifest(
        backend=backend,
        data_directory=data_directory,
        max_data_files=(policy.max_data_files if policy is not None else _DEFAULT_MAX_DATA_FILES),
        max_file_bytes=(policy.max_file_bytes if policy is not None else _DEFAULT_MAX_FILE_BYTES),
        max_manifest_bytes=(
            policy.max_manifest_bytes if policy is not None else _DEFAULT_MAX_MANIFEST_BYTES
        ),
        retained_paths=retained_paths,
    )
    executable = contents.get(executable_key)
    if executable is None:
        raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
    executable_sha256 = hashlib.sha256(executable).hexdigest()
    if policy is not None and executable_sha256 not in policy.permitted_executable_sha256:
        raise _TrustFailure(NmapTrustReason.EXECUTABLE_DIGEST_REJECTED)

    for script in (() if policy is None else policy.reviewed_scripts):
        relative = ntpath.normcase(ntpath.join("scripts", f"{script.name}.nse"))
        script_payload = contents.get(relative)
        if script_payload is None or hashlib.sha256(script_payload).hexdigest() != script.sha256:
            raise _TrustFailure(NmapTrustReason.REVIEWED_SCRIPT_REJECTED)

    licence_rows = [row for row in manifest_rows if ntpath.basename(str(row["path"])).casefold() in _LICENCE_NAMES]
    if len(licence_rows) != 1:
        raise _TrustFailure(NmapTrustReason.LICENCE_REJECTED)
    licence_relative_path = str(licence_rows[0]["path"])
    licence = contents[ntpath.normcase(licence_relative_path)]
    try:
        licence_text = licence.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise _TrustFailure(NmapTrustReason.LICENCE_REJECTED) from error
    match = _NPSL_VERSION.search(licence_text[:65_536])
    if match is None or (
        policy is not None and match.group(1) not in policy.permitted_npsl_versions
    ):
        raise _TrustFailure(NmapTrustReason.LICENCE_REJECTED)

    payload = {
        "install_root": install_root,
        "executable_path": executable_path,
        "data_directory": data_directory,
        "publisher": signature.publisher,
        "signer_sha256": signature.signer_sha256,
        "version": version,
        "executable_sha256": executable_sha256,
        "data_manifest_sha256": canonical_sha256(manifest_rows),
        "data_file_count": len(manifest_rows),
        "data_total_bytes": sum(int(row["size_bytes"]) for row in manifest_rows),
        "licence_relative_path": licence_relative_path,
        "licence_sha256": hashlib.sha256(licence).hexdigest(),
        "npsl_version": match.group(1),
        "reviewed_scripts": () if policy is None else policy.reviewed_scripts,
    }
    return NmapInstallationFingerprintV1(
        **payload,
        fingerprint_sha256=canonical_sha256(payload),
    )


def _build_manifest(
    *,
    backend: NmapTrustBackend,
    data_directory: str,
    max_data_files: int,
    max_file_bytes: int,
    max_manifest_bytes: int,
    retained_paths: set[str],
) -> tuple[list[dict[str, object]], dict[str, bytes]]:
    rows: list[dict[str, object]] = []
    contents: dict[str, bytes] = {}
    seen_paths: set[str] = set()
    total = 0
    for index, raw_path in enumerate(backend.iter_files(data_directory)):
        if index >= max_data_files:
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
        path = _canonical_path(backend, raw_path)
        if not _contained(data_directory, path) or not backend.is_regular_file(path):
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
        _check_path_security(backend, (path,))
        relative = ntpath.normcase(ntpath.normpath(ntpath.relpath(path, data_directory)))
        if relative in (".", "..") or relative.startswith("..\\") or relative in seen_paths:
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
        seen_paths.add(relative)
        try:
            payload = backend.read_file(path, max_bytes=max_file_bytes)
        except (OSError, TypeError, ValueError) as error:
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED) from error
        if not isinstance(payload, bytes) or len(payload) > max_file_bytes:
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
        total += len(payload)
        if total > max_manifest_bytes:
            raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
        if relative in retained_paths or ntpath.basename(relative).casefold() in _LICENCE_NAMES:
            contents[relative] = payload
        rows.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if not rows:
        raise _TrustFailure(NmapTrustReason.DATA_MANIFEST_REJECTED)
    rows.sort(key=lambda row: str(row["path"]))
    return rows, contents


def _check_path_security(backend: NmapTrustBackend, paths: Iterable[str]) -> None:
    for path in paths:
        if backend.has_reparse_component(path):
            raise _TrustFailure(NmapTrustReason.REPARSE_REJECTED)
        if backend.is_user_writable(path) or not backend.is_administrator_owned(path):
            raise _TrustFailure(NmapTrustReason.ACL_REJECTED)


def _canonical_path(backend: NmapTrustBackend, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _TrustFailure(NmapTrustReason.PATH_REJECTED)
    canonical = ntpath.normpath(backend.canonicalize(value.strip()))
    drive, tail = ntpath.splitdrive(canonical)
    if not drive or drive.startswith("\\") or not tail.startswith("\\"):
        raise _TrustFailure(NmapTrustReason.PATH_REJECTED)
    return canonical


def _contained(parent: str, child: str) -> bool:
    try:
        return ntpath.normcase(ntpath.commonpath((parent, child))) == ntpath.normcase(parent)
    except ValueError:
        return False


def _fingerprint_payload(fingerprint: NmapInstallationFingerprintV1) -> dict[str, object]:
    return fingerprint.model_dump(exclude={"schema_version", "fingerprint_sha256"}, mode="json")


def _unavailable(reason: NmapTrustReason) -> NmapTrustResultV1:
    return NmapTrustResultV1(available=False, reason=reason, fingerprint=None)
