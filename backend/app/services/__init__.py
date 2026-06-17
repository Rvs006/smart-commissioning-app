# Service package marker.
# Wires the shared smart_commissioning_core MQTT defaults to this service's
# persisted configuration so live broker runs pick up stored settings.
from smart_commissioning_core.mqtt_settings import (
    set_configuration_values_provider,
    set_secret_resolver,
)


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.configuration_service import DEFAULT_CONFIGURATION, ConfigurationService

    try:
        # mask_secrets=False: this is the internal provider path — the MQTT
        # connection builder needs the real stored credentials, not the
        # API-boundary '********' mask.
        configuration = ConfigurationService().load(mask_secrets=False)
    except SQLAlchemyError:
        # Best-effort defaults: connection parameter resolution must not fail
        # when the database is unreachable or not migrated yet (the previous
        # file-backed load() seeded defaults and could never fail either).
        configuration = DEFAULT_CONFIGURATION
    return configuration.mqtt.values, configuration.certificates.values


def _resolve_secret(ref: str) -> bytes | None:
    """Resolve a ``secret://`` cert ref to its DECRYPTED bytes for live TLS.

    Backs the core transport's secret-resolver hook with the API's decrypting
    reader (``ConfigurationService.read_secret_material``) so a stored
    ``secret://`` CA / client-cert / private-key reference becomes the real
    PEM material loaded into the live MQTT SSLContext. Returns ``None`` (never
    raises) for a non-secret ref or any read/decrypt failure, so a missing
    secret degrades to "no material loaded" rather than aborting the handshake
    setup with a credential-bearing error.
    """
    if not isinstance(ref, str) or not ref.startswith("secret://"):
        return None
    try:
        from app.services.configuration_service import read_secret_material

        return read_secret_material(ref).encode("utf-8")
    except Exception:
        return None


set_configuration_values_provider(_configuration_values)
set_secret_resolver(_resolve_secret)
