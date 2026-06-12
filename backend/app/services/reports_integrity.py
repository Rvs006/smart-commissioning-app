"""Evidence integrity for generated report artifacts.

When a report/evidence artifact is produced (see app.api.routes.reports), its
bytes are hashed (SHA-256) and signed (detached Ed25519) and the result is
persisted on the run record under ``result_summary["integrity"]`` — no schema
migration, since reports are already derivable from the stored run record.

The signing key lives under the runtime secrets root with the same owner-only
(0600) file pattern the configuration secret store uses, created on first use.

Honesty note: signing requires the ``cryptography`` package (a backend
dependency). If it is somehow absent, hashing still runs and signing degrades
to ``signature=None`` rather than crashing — :func:`build_integrity_metadata`
and the verify path both branch on :func:`cryptography_available`.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smart_commissioning_core.integrity import (
    SigningKey,
    cryptography_available,
    public_key_fingerprint,
    sha256_bytes,
    verify_bytes,
)

from app.core.runtime import SECRETS_ROOT, ensure_runtime_directories

# Reuse the secrets root so the signing key is backed up alongside the encrypted
# PEMs (backup_service includes the whole secrets dir).
_SIGNING_KEY_FILE = ".evidence_signing_key"

# result_summary key under which integrity metadata is stored on the run record.
INTEGRITY_KEY = "integrity"


def signing_key_path() -> Path:
    """Absolute path of the persisted evidence signing key under the secrets root."""
    return SECRETS_ROOT / _SIGNING_KEY_FILE


def load_signing_key() -> SigningKey:
    """Load (or create on first use) the evidence signing key, owner-only."""
    ensure_runtime_directories()
    return SigningKey.load_or_create(signing_key_path())


def build_integrity_metadata(artifact: bytes) -> dict[str, Any]:
    """Hash + sign ``artifact`` and return the metadata to persist on the run.

    Shape (JSON-safe, stored under result_summary["integrity"]):

        {
          "algorithm": "sha256",
          "hash": "<hex sha256 of the artifact bytes>",
          "signature_algorithm": "ed25519",
          "signature": "<base64 detached signature>" | None,
          "public_key_pem": "<verifier PEM>" | None,
          "public_key_fingerprint": "<short fingerprint>" | None,
          "signed_at": "<iso8601 UTC>",
        }

    When ``cryptography`` is unavailable the signature fields are None so the
    hash is still recorded and the run record never fails to serialize.
    """
    metadata: dict[str, Any] = {
        "algorithm": "sha256",
        "hash": sha256_bytes(artifact),
        "signature_algorithm": "ed25519",
        "signature": None,
        "public_key_pem": None,
        "public_key_fingerprint": None,
        "signed_at": datetime.now(UTC).isoformat(),
    }
    if cryptography_available():
        key = load_signing_key()
        signature = key.sign(artifact)
        metadata["signature"] = base64.b64encode(signature).decode("ascii")
        metadata["public_key_pem"] = key.public_key_pem()
        metadata["public_key_fingerprint"] = key.public_key_fingerprint()
    return metadata


def verify_artifact(artifact: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
    """Recompute the hash and verify the signature of ``artifact`` vs ``metadata``.

    Returns the verify-endpoint response shape:

        {
          "hash_matches": bool,
          "signature_valid": bool | None,   # None when not signed / no crypto
          "key_matches_current": bool | None,  # None when no embedded/current key
          "signed_at": str | None,
          "public_key_fingerprint": str | None,
          "stored_hash": str | None,
          "computed_hash": str,
        }

    Key pinning (tamper-of-stored-record detection): a signature is only checked
    against the ``public_key_pem`` embedded in the *same* stored metadata blob,
    so an attacker who can rewrite the run record could swap in a different
    keypair's hash + signature + public key and produce an internally-consistent
    record. To surface that, the embedded public-key fingerprint is also
    cross-checked against the CURRENT evidence signing key; a mismatch sets
    ``key_matches_current=False`` (never silently passing) so a swapped-key
    record is detectable even when its self-signature verifies.
    """
    computed_hash = sha256_bytes(artifact)
    stored_hash = metadata.get("hash") if isinstance(metadata, dict) else None
    hash_matches = bool(stored_hash) and stored_hash == computed_hash

    signature_valid: bool | None = None
    signature_b64 = metadata.get("signature") if isinstance(metadata, dict) else None
    public_key_pem = metadata.get("public_key_pem") if isinstance(metadata, dict) else None
    if signature_b64 and public_key_pem and cryptography_available():
        try:
            signature = base64.b64decode(signature_b64)
            signature_valid = verify_bytes(artifact, signature, public_key_pem)
        except (ValueError, TypeError):
            signature_valid = False

    # The route falls back to fingerprint_for_pem() when this is None.
    fingerprint = metadata.get("public_key_fingerprint") if isinstance(metadata, dict) else None

    key_matches_current = _key_matches_current(public_key_pem, fingerprint)

    return {
        "hash_matches": hash_matches,
        "signature_valid": signature_valid,
        "key_matches_current": key_matches_current,
        "signed_at": metadata.get("signed_at") if isinstance(metadata, dict) else None,
        "public_key_fingerprint": fingerprint,
        "stored_hash": stored_hash,
        "computed_hash": computed_hash,
    }


def _key_matches_current(public_key_pem: str | None, fingerprint: str | None) -> bool | None:
    """True iff the stored record's key matches the CURRENT signing key.

    Returns None when the comparison cannot be made (record carries no key, or
    cryptography is unavailable) so the caller can leave the field absent of a
    definite verdict. A definite False means the embedded key is NOT the current
    signing key — a swapped-key stored record, surfaced rather than passed.
    """
    if not cryptography_available():
        return None
    if not public_key_pem and not fingerprint:
        return None

    try:
        current_fingerprint = load_signing_key().public_key_fingerprint()
    except Exception:  # noqa: BLE001 - never let key loading break verification
        return None

    # Prefer deriving the embedded fingerprint from the PEM itself (authoritative)
    # so a tampered/forged ``public_key_fingerprint`` string cannot mask a
    # swapped key. Fall back to the stored fingerprint only when no PEM is present.
    embedded_fingerprint = fingerprint_for_pem(public_key_pem) if public_key_pem else fingerprint
    if embedded_fingerprint is None:
        embedded_fingerprint = fingerprint
    if embedded_fingerprint is None:
        return None

    return embedded_fingerprint == current_fingerprint


def fingerprint_for_pem(public_key_pem: str) -> str | None:
    """Best-effort fingerprint of a stored verifier PEM (used as a fallback)."""
    if not cryptography_available():
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        loaded = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        if isinstance(loaded, Ed25519PublicKey):
            raw = loaded.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            return public_key_fingerprint(raw)
    except (ValueError, TypeError):
        return None
    return None
