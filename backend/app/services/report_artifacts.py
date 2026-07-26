"""Write-once report artifact storage and signed manifests."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from smart_commissioning_core.integrity import (
    cryptography_available,
    sha256_bytes,
    verify_bytes,
)

from app.core.runtime import ARTIFACTS_ROOT, ensure_runtime_directories
from app.services.reports_integrity import load_signing_key

REPORT_SNAPSHOT_SCHEMA_VERSION = "2.0"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "1.0"
REPORT_RENDERER_VERSION = "0.1.26"

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
)


def canonical_json_bytes(value: object) -> bytes:
    """Return the canonical UTF-8 representation used for hashes/signatures."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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

    if target.exists():
        existing = target.read_bytes()
        if existing != artifact:
            raise RuntimeError("A report artifact path already contains different bytes.")
    else:
        _atomic_write(target, artifact)

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


def verify_signed_manifest(manifest: dict[str, Any]) -> bool:
    """Verify the embedded public key and detached manifest signature."""

    if not cryptography_available():
        return False
    try:
        unsigned = {field: manifest[field] for field in _SIGNED_MANIFEST_FIELDS}
        signature = base64.b64decode(str(manifest["signature"]), validate=True)
        public_key = str(manifest["public_key_pem"])
    except (KeyError, TypeError, ValueError):
        return False
    signed_body = canonical_json_bytes(unsigned)
    if manifest.get("signed_manifest_sha256") != sha256_bytes(signed_body):
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
