"""Write-once report artifact storage and signed manifests."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from smart_commissioning_core.integrity import (
    cryptography_available,
    sha256_bytes,
    verify_bytes,
)

from app.core.runtime import ARTIFACTS_ROOT, ensure_runtime_directories
from app.services.reports_integrity import fingerprint_for_pem, load_signing_key

REPORT_SNAPSHOT_SCHEMA_VERSION = "2.0"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.1"
REPORT_RENDERER_VERSION = "0.1.41"

_SIGNED_MANIFEST_FIELDS = (
    "schema_version",
    "report_id",
    "snapshot_sha256",
    "file_name",
    "media_type",
    "byte_size",
    "renderer_version",
    "artifact_sha256",
    "artifact_relpath",
    "origin",
    "signing_key_id",
    "signed_at",
    "evidence_set_id",
)
_LEGACY_SIGNED_MANIFEST_FIELDS = tuple(
    field for field in _SIGNED_MANIFEST_FIELDS if field != "evidence_set_id"
)
_COMPLETE_MANIFEST_FIELDS = frozenset(
    {
        *_SIGNED_MANIFEST_FIELDS,
        "signature_algorithm",
        "signature",
        "public_key_pem",
        "signed_manifest_sha256",
    }
)
_LEGACY_COMPLETE_MANIFEST_FIELDS = frozenset(
    {
        *_LEGACY_SIGNED_MANIFEST_FIELDS,
        "signature_algorithm",
        "signature",
        "public_key_pem",
        "signed_manifest_sha256",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+$"
)


class ReportArtifactIntegrityError(RuntimeError):
    """A report artifact is absent or no longer bound to its requested run."""


def canonical_json_bytes(value: object) -> bytes:
    """Return the canonical UTF-8 representation used for hashes/signatures."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="backslashreplace")


def snapshot_sha256(snapshot: object) -> str:
    return sha256_bytes(canonical_json_bytes(snapshot))


def store_report_artifact(
    *,
    report_id: str,
    snapshot_hash: str,
    file_name: str,
    media_type: str,
    artifact: bytes,
    origin: str,
    signed_at: str,
    evidence_set_id: str | None = None,
) -> dict[str, Any]:
    """Durably store exact bytes and return a signed ArtifactManifestV1.

    The content-addressed target is written through a temporary sibling plus
    ``os.replace``. Repeating the call is idempotent only when the existing
    target has the same bytes.
    """

    if not cryptography_available():
        raise RuntimeError("Report artifact signing requires the cryptography package.")
    if not artifact:
        raise ValueError("A report artifact must contain at least one byte.")

    ensure_runtime_directories()
    root = ARTIFACTS_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifact_hash = sha256_bytes(artifact)
    suffix = Path(file_name).suffix.lower()
    stored_name = f"{report_id}-{artifact_hash}{suffix}"
    target = (root / stored_name).resolve()
    if target.parent != root:
        raise ValueError("Report artifact path escaped the artifact root.")

    # Build the complete signed manifest before publishing bytes. A signing
    # failure must not leave an artifact that cannot be reached safely.
    key = load_signing_key()
    manifest: dict[str, Any] = {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA_VERSION,
        "report_id": report_id,
        "snapshot_sha256": snapshot_hash,
        "file_name": file_name,
        "media_type": media_type,
        "byte_size": len(artifact),
        "renderer_version": REPORT_RENDERER_VERSION,
        "artifact_sha256": artifact_hash,
        "artifact_relpath": stored_name,
        "origin": origin,
        "signing_key_id": key.public_key_fingerprint(),
        "signed_at": signed_at,
        "evidence_set_id": evidence_set_id,
    }
    signed_body = canonical_json_bytes(manifest)
    manifest.update(
        {
            "signature_algorithm": "ed25519",
            "signature": base64.b64encode(key.sign(signed_body)).decode("ascii"),
            "public_key_pem": key.public_key_pem(),
            "signed_manifest_sha256": sha256_bytes(signed_body),
        }
    )
    if target.exists():
        existing = target.read_bytes()
        if existing != artifact:
            raise RuntimeError("A report artifact path already contains different bytes.")
    else:
        _atomic_write(target, artifact)
    return manifest


def load_report_artifact(manifest: dict[str, Any]) -> bytes:
    """Read and verify stored bytes without database or filesystem mutation."""

    relative = manifest.get("artifact_relpath")
    if not isinstance(relative, str) or not relative or Path(relative).name != relative:
        raise RuntimeError("Stored report artifact path is invalid.")
    root = ARTIFACTS_ROOT.resolve()
    target = (root / relative).resolve()
    if target.parent != root or not target.is_file():
        raise FileNotFoundError(relative)
    artifact = target.read_bytes()
    expected_size = manifest.get("byte_size")
    if not isinstance(expected_size, int) or expected_size != len(artifact):
        raise RuntimeError("Stored report artifact size does not match its manifest.")
    expected_hash = manifest.get("artifact_sha256")
    if not isinstance(expected_hash, str) or sha256_bytes(artifact) != expected_hash:
        raise RuntimeError("Stored report artifact hash does not match its manifest.")
    return artifact


def load_owned_report_artifact(
    *,
    report_id: str,
    manifest: Mapping[str, Any],
    synchronized: Mapping[str, Any] | None = None,
    require_valid_signature: bool = True,
) -> tuple[bytes, str]:
    """Verify ownership, signature, and bytes for one sealed report manifest."""

    owned_manifest = dict(manifest)
    if owned_manifest.get("report_id") != report_id:
        raise ReportArtifactIntegrityError(
            "Stored report artifact ownership does not match the requested report."
        )
    if require_valid_signature and not verify_signed_manifest(owned_manifest):
        raise ReportArtifactIntegrityError(
            "Stored report artifact manifest has an invalid signature."
        )
    media_type = owned_manifest.get("media_type")
    if not isinstance(media_type, str) or not media_type:
        raise ReportArtifactIntegrityError("Stored report manifest has no media type.")

    try:
        if synchronized is None:
            relative = owned_manifest.get("artifact_relpath")
            if not isinstance(relative, str) or not relative.startswith(
                f"{report_id}-"
            ):
                raise ReportArtifactIntegrityError(
                    "Stored report artifact path is not owned by the report."
                )
            artifact = load_report_artifact(owned_manifest)
        else:
            synchronized_manifest = synchronized.get("manifest")
            if not isinstance(synchronized_manifest, Mapping):
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact has no signed manifest."
                )
            synchronized_manifest = dict(synchronized_manifest)
            if synchronized_manifest.get("report_id") != report_id:
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact ownership does not match the requested report."
                )
            if require_valid_signature and not verify_signed_manifest(
                synchronized_manifest
            ):
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact manifest has an invalid signature."
                )
            if synchronized_manifest != owned_manifest:
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact manifest conflicts with sealed evidence."
                )
            expected_hash = owned_manifest.get("artifact_sha256")
            expected_size = owned_manifest.get("byte_size")
            if (
                synchronized.get("artifact_sha256") != expected_hash
                or synchronized.get("byte_size") != expected_size
            ):
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact record conflicts with its signed manifest."
                )
            storage_relpath = synchronized.get("storage_relpath")
            if not isinstance(storage_relpath, str):
                raise ReportArtifactIntegrityError(
                    "Synchronized artifact storage path is invalid."
                )
            if not isinstance(expected_hash, str) or type(expected_size) is not int:
                raise ReportArtifactIntegrityError(
                    "Stored report artifact manifest has invalid byte metadata."
                )
            artifact = load_content_addressed_artifact(
                storage_relpath,
                expected_hash=expected_hash,
                expected_size=expected_size,
            )
    except ReportArtifactIntegrityError:
        raise
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise ReportArtifactIntegrityError(str(error)) from error
    return artifact, media_type


def delete_report_artifact(manifest: dict[str, Any], *, report_id: str) -> bool:
    """Remove one report-owned local artifact without touching shared sync bytes.

    The local writer stores a single basename shaped ``{report_id}-{sha256}.ext``.
    Requiring that exact ownership boundary prevents a forged or stale manifest
    from traversing directories or deleting another report's file. Missing bytes
    are already clean and return ``False``.
    """

    if manifest.get("report_id") != report_id:
        raise RuntimeError("Stored report artifact ownership does not match the report.")
    relative = manifest.get("artifact_relpath")
    if not isinstance(relative, str) or not relative or Path(relative).name != relative:
        raise RuntimeError("Stored report artifact path is invalid.")
    if not relative.startswith(f"{report_id}-"):
        raise RuntimeError("Stored report artifact path is not owned by the report.")
    root = ARTIFACTS_ROOT.resolve()
    target = root / relative
    if target.parent.resolve() != root:
        raise RuntimeError("Stored report artifact path escaped the artifact root.")
    try:
        # Unlink the owned directory entry, not its resolved target. A stale or
        # malicious same-directory symlink must never delete another report's
        # bytes, and a dangling symlink still needs to be removed.
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def store_content_addressed_artifact(artifact: bytes, artifact_sha256: str) -> str:
    """Store verified sync bytes under a hub-owned SHA-256 path."""

    if not artifact or sha256_bytes(artifact) != artifact_sha256:
        raise ValueError("Artifact bytes do not match the supplied SHA-256.")
    ensure_runtime_directories()
    root = ARTIFACTS_ROOT.resolve()
    relative = Path("sha256") / artifact_sha256[:2] / artifact_sha256
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ValueError("Content-addressed artifact path escaped the artifact root.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != artifact:
            raise RuntimeError("Content-addressed artifact contains different bytes.")
    else:
        _atomic_write(target, artifact)
    return relative.as_posix()


def load_content_addressed_artifact(
    storage_relpath: str,
    *,
    expected_hash: str,
    expected_size: int,
) -> bytes:
    """Read exact hub-owned sync bytes and verify size plus SHA-256."""

    relative = Path(storage_relpath)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("Stored synchronized artifact path is invalid.")
    root = ARTIFACTS_ROOT.resolve()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(storage_relpath)
    artifact = target.read_bytes()
    if len(artifact) != expected_size:
        raise RuntimeError("Stored synchronized artifact size does not match its record.")
    if sha256_bytes(artifact) != expected_hash:
        raise RuntimeError("Stored synchronized artifact hash does not match its record.")
    return artifact


def verify_signed_manifest(manifest: dict[str, Any]) -> bool:
    """Verify the embedded public key and detached manifest signature."""

    if not cryptography_available():
        return False
    manifest_fields = (
        _SIGNED_MANIFEST_FIELDS
        if set(manifest) == _COMPLETE_MANIFEST_FIELDS
        else _LEGACY_SIGNED_MANIFEST_FIELDS
        if set(manifest) == _LEGACY_COMPLETE_MANIFEST_FIELDS
        else None
    )
    if manifest_fields is None:
        return False
    schema_version = manifest.get("schema_version")
    if schema_version not in {"1.0", ARTIFACT_MANIFEST_SCHEMA_VERSION}:
        return False
    if schema_version == ARTIFACT_MANIFEST_SCHEMA_VERSION and manifest_fields != _SIGNED_MANIFEST_FIELDS:
        return False
    if schema_version == "1.0" and manifest_fields != _LEGACY_SIGNED_MANIFEST_FIELDS:
        return False
    if manifest.get("signature_algorithm") != "ed25519":
        return False
    try:
        report_id = manifest["report_id"]
        snapshot_hash = manifest["snapshot_sha256"]
        file_name = manifest["file_name"]
        media_type = manifest["media_type"]
        byte_size = manifest["byte_size"]
        renderer_version = manifest["renderer_version"]
        artifact_hash = manifest["artifact_sha256"]
        artifact_relpath = manifest["artifact_relpath"]
        origin = manifest["origin"]
        signing_key_id = manifest["signing_key_id"]
        signed_at = manifest["signed_at"]
        evidence_set_id = manifest.get("evidence_set_id")
        if not isinstance(report_id, str) or not 1 <= len(report_id) <= 64:
            return False
        if not isinstance(snapshot_hash, str) or not _SHA256_RE.fullmatch(snapshot_hash):
            return False
        if (
            not isinstance(file_name, str)
            or not 1 <= len(file_name) <= 512
            or "/" in file_name
            or "\\" in file_name
            or file_name in {".", ".."}
        ):
            return False
        if (
            not isinstance(media_type, str)
            or len(media_type) > 255
            or not _MEDIA_TYPE_RE.fullmatch(media_type)
        ):
            return False
        if type(byte_size) is not int or byte_size < 1:
            return False
        if not isinstance(renderer_version, str) or not 1 <= len(renderer_version) <= 64:
            return False
        if not isinstance(artifact_hash, str) or not _SHA256_RE.fullmatch(artifact_hash):
            return False
        if not isinstance(artifact_relpath, str) or not 1 <= len(artifact_relpath) <= 1024:
            return False
        relative = PurePosixPath(artifact_relpath)
        if (
            "\\" in artifact_relpath
            or relative.is_absolute()
            or ".." in relative.parts
            or "." in relative.parts
        ):
            return False
        if not isinstance(origin, str) or not 1 <= len(origin) <= 255:
            return False
        if not isinstance(signing_key_id, str) or not _KEY_FINGERPRINT_RE.fullmatch(
            signing_key_id
        ):
            return False
        if not isinstance(signed_at, str):
            return False
        if evidence_set_id is not None and (
            not isinstance(evidence_set_id, str)
            or not re.fullmatch(r"evidence_[0-9a-f]{24}", evidence_set_id)
        ):
            return False
        signed_timestamp = datetime.fromisoformat(signed_at)
        if signed_timestamp.tzinfo is None:
            return False
        unsigned = {field: manifest[field] for field in manifest_fields}
        raw_signature = manifest["signature"]
        public_key = manifest["public_key_pem"]
        if not isinstance(raw_signature, str) or len(raw_signature) > 1024:
            return False
        if not isinstance(public_key, str) or not 1 <= len(public_key) <= 8192:
            return False
        signature = base64.b64decode(raw_signature, validate=True)
        signed_manifest_hash = manifest["signed_manifest_sha256"]
        if not isinstance(signed_manifest_hash, str) or not _SHA256_RE.fullmatch(
            signed_manifest_hash
        ):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    signed_body = canonical_json_bytes(unsigned)
    if signed_manifest_hash != sha256_bytes(signed_body):
        return False
    if fingerprint_for_pem(public_key) != signing_key_id:
        return False
    return verify_bytes(signed_body, signature, public_key)


def _atomic_write(target: Path, artifact: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(artifact)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
