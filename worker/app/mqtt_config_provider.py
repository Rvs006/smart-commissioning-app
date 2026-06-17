"""Worker-side MQTT configuration values provider + secret resolver.

Phase 2 carry-forward: the worker process needs broker connection defaults so
the MQTT discovery / live UDMI capture / config-publish engines can connect when
run via the worker (not just the API). It registers a configuration-values
provider with ``smart_commissioning_core.mqtt_settings`` that reads the CURRENT
configuration snapshot from the SAME database the backend writes to (via the
shared ``ConfigurationRepository``).

WHAT THIS WIRES (and its honest limits):

* MQTT broker host / port / client id / keep-alive / username / password are
  read from the stored ``mqtt`` section. So an MQTT discovery run dispatched to
  the worker WITHOUT explicit broker params in its run parameters still resolves
  a broker host from stored configuration. Run parameters (``broker_host`` etc.)
  always take precedence (see ``build_mqtt_connection_settings``), so a run can
  also fully self-describe its broker without any stored configuration.

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

  - When the secrets volume is NOT reachable (no shared volume / no key file),
    the worker does NOT register a decrypting resolver. ``secret://`` references
    then resolve to nothing, and cert material must instead be supplied via run
    PARAMETERS (``ca_certificate`` / ``client_certificate`` / ``private_key`` as
    plain filesystem paths the worker can read, or a CA via parameters). This is
    the documented limitation: full mutual-TLS on the worker needs the shared
    secrets volume.

The provider is best-effort: any database error yields empty MQTT defaults so a
worker without a reachable/migrated database still imports and runs jobs that
fully self-describe their broker via run parameters. The real TLS handshake
against a live broker stays on-site-untested.
"""

import os
from pathlib import Path

from smart_commissioning_core.mqtt_settings import (
    set_configuration_values_provider,
    set_secret_resolver,
)

# These mirror backend ConfigurationService.DEFAULT_PROJECT_ID / DEFAULT_SITE_ID.
# The single-tenant edge deployment uses one project/site; if a deployment uses
# others, the broker should instead be supplied via run parameters.
_DEFAULT_PROJECT_ID = "demo-project"
_DEFAULT_SITE_ID = "demo-site"

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


def _certificate_values() -> dict[str, object]:
    """Stored certificate references, returned ONLY when the secret store is reachable.

    When the shared secrets volume is present the worker can decrypt the
    backend's secret:// references, so it surfaces them as connection defaults and
    worker mutual-TLS is possible. When the store is unreachable the worker
    returns no cert defaults and relies on run-parameter cert material instead
    (documented limitation), so it never advertises secret:// refs it cannot
    resolve.
    """
    if not _secret_store_reachable():
        return {}
    try:
        from smart_commissioning_core.db.repositories import ConfigurationRepository

        from app.db import get_engine

        payload = ConfigurationRepository(get_engine()).get_current(
            _DEFAULT_PROJECT_ID, _DEFAULT_SITE_ID
        )
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    section = payload.get("certificates")
    values = section.get("values") if isinstance(section, dict) else None
    return values if isinstance(values, dict) else {}


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    """Return (mqtt_values, certificate_values) from the stored configuration.

    Certificate values are populated only when the shared secret store is
    reachable (see _certificate_values); otherwise they are empty and cert
    material must come from run parameters.
    """
    try:
        from smart_commissioning_core.db.repositories import ConfigurationRepository

        from app.db import get_engine

        payload = ConfigurationRepository(get_engine()).get_current(
            _DEFAULT_PROJECT_ID, _DEFAULT_SITE_ID
        )
    except Exception:
        # Best-effort: a worker without a reachable/migrated DB still runs jobs
        # whose run parameters carry the broker settings.
        return {}, _certificate_values()

    if not isinstance(payload, dict):
        return {}, _certificate_values()
    mqtt_section = payload.get("mqtt")
    mqtt_values = mqtt_section.get("values") if isinstance(mqtt_section, dict) else None
    return (mqtt_values if isinstance(mqtt_values, dict) else {}), _certificate_values()


def register_worker_mqtt_configuration_provider() -> None:
    """Register the worker's configuration-values provider and secret resolver.

    Idempotent. The secret resolver is always registered; it self-gates on
    whether the shared secret store is reachable (returning ``None`` when it is
    not), so worker mutual-TLS becomes possible exactly when the secrets volume
    is shared and stays off (cert material via run parameters) otherwise.
    """
    set_configuration_values_provider(_configuration_values)
    set_secret_resolver(_resolve_secret)
