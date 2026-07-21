import math
from collections.abc import Callable

from smart_commissioning_core.mqtt_transport import (
    MqttConnectionSettings,
    set_secret_resolver,  # re-exported for callers that import it from this module
)

ConfigurationValuesProvider = Callable[[], tuple[dict[str, object], dict[str, object]]]

_configuration_values_provider: ConfigurationValuesProvider | None = None

# Hard safety backstop for an "indefinite" (blank Run time) capture: 48 hours. A
# blank capture runs until the operator presses Stop run, every expected topic is
# seen (UDMI), or the distinct-topic cap — but never past this ceiling, so the
# broker socket and its thread cannot be held forever. Both capture engines pass
# this as the transport timeout when the resolved window is None, while keeping
# ``capture_mode`` reported as "indefinite" in the summary. The API route caps
# (discovery.MQTT_MAX_CAPTURE_SECONDS / validation.MAX_UDMI_CAPTURE_SECONDS) equal
# this value, and the worker actor time limits sit one hour above it.
INDEFINITE_BACKSTOP_SECONDS = 172_800.0

__all__ = [
    "INDEFINITE_BACKSTOP_SECONDS",
    "ConfigurationValuesProvider",
    "build_mqtt_connection_settings",
    "parse_bool",
    "parse_capture_seconds",
    "parse_float",
    "parse_int",
    "set_configuration_values_provider",
    "set_secret_resolver",
]


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

    port = parse_int(parameters.get("broker_port") or mqtt_values.get("Port"), default=8883)
    use_tls = _resolve_use_tls(parameters, mqtt_values, port)

    source_ip = _string(parameters.get("source_ip"))
    source_address = (source_ip, 0) if source_ip else None

    return MqttConnectionSettings(
        host=host,
        port=port,
        client_id=_string(parameters.get("client_id")) or _string(mqtt_values.get("Client ID")) or "smart-commissioning-tool",
        keep_alive=parse_int(parameters.get("keep_alive") or mqtt_values.get("Keep Alive Interval"), default=60),
        username=_optional_secret(parameters.get("username") or mqtt_values.get("MQTT Username")),
        password=_optional_secret(parameters.get("password") or mqtt_values.get("MQTT Password")),
        use_tls=use_tls,
        ca_certificate=_optional_secret(certificate_values.get("CA Certificate")),
        client_certificate=_optional_secret(certificate_values.get("Client Certificate")),
        private_key=_optional_secret(certificate_values.get("Private Key")),
        timeout_seconds=parse_float(parameters.get("connect_timeout_seconds"), default=5.0),
        source_address=source_address,
    )


def _resolve_use_tls(parameters: dict[str, object], mqtt_values: dict[str, object], port: int) -> bool:
    """Resolve whether the broker connection uses TLS (secure) or plaintext.

    Precedence, so an explicit secure/non-secure choice always wins over the
    legacy port heuristic:

        1. An explicit ``use_tls`` job parameter (chosen per run).
        2. The persisted ``"Use TLS"`` configuration selection (Enabled/Disabled)
           — the Configuration page's secure/non-secure control.
        3. Back-compat fallback: infer from the port (8883 = TLS), matching the
           historical behaviour for configs saved before the control existed.
    """
    explicit = parameters.get("use_tls")
    if explicit is not None:
        if isinstance(explicit, bool):
            return explicit
        explicit_text = str(explicit).strip().casefold()
        if explicit_text in {"1", "true", "yes", "enabled", "on"}:
            return True
        if explicit_text in {"0", "false", "no", "disabled", "off"}:
            return False
        raise ValueError("use_tls must be a boolean value.")
    configured = _string(mqtt_values.get("Use TLS"))
    if configured:
        if configured.casefold() == "enabled":
            return True
        if configured.casefold() == "disabled":
            return False
        raise ValueError("Use TLS must be Enabled or Disabled.")
    return port == 8883


def _string(value: object) -> str:
    return str(value or "").strip()


def _broker_error_status(error: Exception) -> str:
    """Coarse status label for an MQTT error — never the raw text (may carry creds)."""
    text = str(error).casefold()
    if "tls" in text or "certificate" in text or "ssl" in text:
        return "tls_error"
    if "username" in text or "password" in text or "authorised" in text or "authorized" in text:
        return "authentication_error"
    if "timed out" in text or "timeout" in text:
        return "broker_timeout"
    # A SUBACK rejection (e.g. an ACL that denies the topic filter) means the broker
    # was REACHED and authenticated but refused the subscribe — sending the operator
    # down the firewall/host/port path (the "broker_unreachable" default) wastes a
    # site visit. Checked after timeout so "timed out acknowledging the subscription"
    # still reads as a timeout.
    if "rejected the subscription" in text:
        return "subscription_rejected"
    return "broker_unreachable"


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "enabled", "on"}
    return bool(value)


def parse_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def parse_capture_seconds(value: object, *, default: float) -> float | None:
    """Capture window in seconds, or None for an indefinite capture (mq9nhbzu).

    A MISSING value keeps the caller's default window (back-compat). An
    EXPLICIT 0, empty string, or negative value means "run until stopped (via
    cancellation) or a completion condition" — represented as None downstream.
    Shared by the mqtt_discovery and UDMI validation capture paths so the
    blank/0 => indefinite convention cannot drift between them.
    """
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return None
    # Explicit 0 / negative => indefinite. Do NOT route 0 through parse_float:
    # it treats the falsy 0 as "missing" and returns the default window.
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # Non-finite values ("nan"/"inf"/"-inf") parse as floats but would yield a
    # bounded window whose deadline never expires (NaN/inf comparisons), so
    # treat them as invalid input and keep the default — checked BEFORE the
    # <= 0 rule so "-inf" cannot masquerade as an explicit indefinite request.
    if not math.isfinite(seconds):
        return default
    return None if seconds <= 0 else seconds


def parse_int(value: object, *, default: int) -> int:
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
