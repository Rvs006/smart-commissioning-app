# Service package marker.
# Wires the shared smart_commissioning_core MQTT defaults to this service's
# persisted configuration so live broker runs pick up stored settings.
from smart_commissioning_core.mqtt_settings import set_configuration_values_provider


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


set_configuration_values_provider(_configuration_values)
