# Service package marker.
# Wires the shared smart_commissioning_core MQTT defaults to this service's
# persisted configuration so live broker runs pick up stored settings.
from smart_commissioning_core.mqtt_settings import set_configuration_values_provider


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    from app.services.configuration_service import ConfigurationService

    configuration = ConfigurationService().load()
    return configuration.mqtt.values, configuration.certificates.values


set_configuration_values_provider(_configuration_values)
