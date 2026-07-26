"""Worker-side resolver for versioned ``secret://`` material.

Lifecycle-v2 workers load broker and certificate references only from the
stored RunContextV1. This module deliberately registers an EMPTY configuration
provider so no current-configuration or demo-scope fallback can affect a run.

WHAT THIS WIRES (and its honest limits):

* CERTIFICATE / mutual-TLS material is resolved ONLY when the worker can reach
  the SAME secret store the backend writes to:

  - The backend stores cert material as Fernet-encrypted files under its secrets
    root (``SMART_COMMISSIONING_SECRETS_ROOT``), keyed by a per-install key file
    ``.secret_store_key``. To decrypt a ``secret://`` reference the worker must
    therefore see the SAME secrets directory AND the SAME key file.
    DEPLOYMENT REQUIREMENT: mount the backend's secrets root into the worker as a
    SHARED VOLUME and point ``SMART_COMMISSIONING_SECRETS_ROOT`` at it (default:
    ``backend/runtime/secrets`` resolved relative to the repo). When that volume
    is present, the worker registers a secret resolver so the live MQTT
    SSLContext loads the real CA / client-cert / private-key material and worker
    mutual-TLS becomes possible (not silently empty).

  - When the store is not reachable, resolution returns ``None``. Connection
    setup then fails honestly; plaintext paths and blank-certificate fallback
    are not accepted by the context contract.
"""

import os
from pathlib import Path

from smart_commissioning_core.mqtt_settings import (
    set_configuration_values_provider,
    set_secret_resolver,
)

# Mirrors backend ConfigurationService._SECRET_STORE_KEY_FILE: the per-install
# Fernet key the backend used to encrypt secret material at rest. The worker
# must see the SAME file (shared secrets volume) to decrypt secret:// refs.
_SECRET_STORE_KEY_FILE = ".secret_store_key"

# Repo-relative default for the backend secrets root, used when the
# SMART_COMMISSIONING_SECRETS_ROOT env var is unset. Matches
# backend/app/core/runtime.py's default (backend/runtime/secrets) so a
# single-host dev deployment shares the directory without extra config.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SECRETS_ROOT = _REPOSITORY_ROOT / "backend" / "runtime" / "secrets"


def _secrets_root() -> Path:
    """Resolve the secrets root the worker should read (shared with the backend)."""
    return Path(
        os.getenv("SMART_COMMISSIONING_SECRETS_ROOT", str(_DEFAULT_SECRETS_ROOT))
    ).expanduser()


def _secret_store_reachable() -> bool:
    """True when the worker can see the backend's secret store + decryption key.

    Requires both the secrets directory AND the per-install key file to exist;
    without the key the worker cannot decrypt anything, so we treat the store as
    unreachable and fall back to run-parameter cert material.
    """
    root = _secrets_root()
    return root.is_dir() and (root / _SECRET_STORE_KEY_FILE).is_file()


def _secret_path(secret_ref: str) -> Path | None:
    """Map a ``secret://<name>`` ref to its on-disk file, rejecting traversal.

    Mirrors backend ConfigurationService._secret_path so the worker reads the
    exact file the backend wrote. Returns ``None`` for an invalid reference
    rather than raising (the resolver must never raise into the TLS path).
    """
    if not isinstance(secret_ref, str) or not secret_ref.startswith("secret://"):
        return None
    name = secret_ref.removeprefix("secret://").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    return _secrets_root() / f"{name}.pem"


def _resolve_secret(secret_ref: str) -> bytes | None:
    """Resolve a ``secret://`` cert ref to DECRYPTED bytes from the shared store.

    Reads the Fernet-encrypted file the backend wrote and decrypts it with the
    shared ``.secret_store_key``; legacy plaintext files (pre-encryption) stay
    readable via the fallback, matching the backend's read_secret_material.
    Returns ``None`` (never raises) for a non-secret ref, an unreachable store,
    or any read/decrypt failure so a missing secret degrades to "no material
    loaded" rather than aborting handshake setup with a credential-bearing
    error.
    """
    if not _secret_store_reachable():
        return None
    path = _secret_path(secret_ref)
    if path is None or not path.is_file():
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken

        key = (_secrets_root() / _SECRET_STORE_KEY_FILE).read_bytes().strip()
        raw = path.read_bytes()
        try:
            return Fernet(key).decrypt(raw)
        except InvalidToken:
            # Legacy plaintext material written before encryption-at-rest.
            return raw
    except Exception:
        return None


def resolve_worker_secret(secret_ref: str) -> bytes | None:
    """Resolve one stored reference in memory without logging or persistence."""
    return _resolve_secret(secret_ref)


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    """Return no defaults; every effective setting must come from RunContextV1."""
    return {}, {}


def register_worker_mqtt_secret_resolver() -> None:
    """Install context-only defaults plus the read-only secret resolver."""
    set_configuration_values_provider(_configuration_values)
    set_secret_resolver(_resolve_secret)


def register_worker_mqtt_configuration_provider() -> None:
    """Compatibility alias; it no longer reads current configuration."""
    register_worker_mqtt_secret_resolver()
