from collections.abc import Callable

from smart_commissioning_core.mqtt_transport import MqttConnectionSettings

ConfigurationValuesProvider = Callable[[], tuple[dict[str, object], dict[str, object]]]

_configuration_values_provider: ConfigurationValuesProvider | None = None


def set_configuration_values_provider(provider: ConfigurationValuesProvider | None) -> None:
    """Register a callable returning (mqtt_values, certificate_values) used as connection defaults.

    Services that own persisted configuration (for example the API's ConfigurationService)
    register a provider at start-up. Services without configuration access leave it unset,
    in which case only explicit job parameters are used.
    """
    global _configuration_values_provider
    _configuration_values_provider = provider


def _configuration_values() -> tuple[dict[str, object], dict[str, object]]:
    if _configuration_values_provider is None:
        return {}, {}
    return _configuration_values_provider()


def build_mqtt_connection_settings(parameters: dict[str, object]) -> MqttConnectionSettings:
    mqtt_values, certificate_values = _configuration_values()
    host = _string(parameters.get("broker_host")) or _string(mqtt_values.get("MQTT Broker FQDN or IP Address"))
    if not host:
        raise ValueError("Live broker mode requires an MQTT broker FQDN or IP address.")

    port = _int(parameters.get("broker_port") or mqtt_values.get("Port"), default=8883)
    use_tls = _bool(parameters.get("use_tls")) or port == 8883

    return MqttConnectionSettings(
        host=host,
        port=port,
        client_id=_string(parameters.get("client_id")) or _string(mqtt_values.get("Client ID")) or "smart-commissioning-tool",
        keep_alive=_int(parameters.get("keep_alive") or mqtt_values.get("Keep Alive Interval"), default=60),
        username=_optional_secret(parameters.get("username") or mqtt_values.get("MQTT Username")),
        password=_optional_secret(parameters.get("password") or mqtt_values.get("MQTT Password")),
        use_tls=use_tls,
        ca_certificate=_optional_secret(certificate_values.get("CA Certificate")),
        client_certificate=_optional_secret(certificate_values.get("Client Certificate")),
        private_key=_optional_secret(certificate_values.get("Private Key")),
        timeout_seconds=_float(parameters.get("connect_timeout_seconds"), default=5.0),
    )


def parse_bool(value: object) -> bool:
    return _bool(value)


def parse_float(value: object, *, default: float) -> float:
    return _float(value, default=default)


def parse_int(value: object, *, default: int) -> int:
    return _int(value, default=default)


def _string(value: object) -> str:
    return str(value or "").strip()


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "enabled", "on"}
    return bool(value)


def _float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _int(value: object, *, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_secret(value: object) -> str | None:
    text = _string(value)
    if not text or set(text) == {"*"}:
        return None
    return text
