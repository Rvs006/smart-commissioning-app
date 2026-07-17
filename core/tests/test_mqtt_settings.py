"""Tests for MQTT connection-settings resolution (secure/non-secure TLS)."""

import socket
import unittest

from smart_commissioning_core.mqtt_settings import (
    MqttSettingsError,
    _broker_error_status,
    build_mqtt_connection_settings,
    parse_capture_seconds,
    set_configuration_values_provider,
)


class ResolveUseTlsTests(unittest.TestCase):
    """The Configuration page's secure/non-secure control must be authoritative.

    ``build_mqtt_connection_settings`` resolves ``use_tls`` from (in order) an
    explicit job parameter, the persisted ``"Use TLS"`` selection, then the
    legacy port heuristic (8883 = TLS) for configs saved before the control.
    """

    def tearDown(self) -> None:
        set_configuration_values_provider(None)

    def _provide(self, mqtt_values: dict[str, object]) -> None:
        set_configuration_values_provider(lambda: (mqtt_values, {}))

    def test_use_tls_disabled_overrides_secure_port(self) -> None:
        # Operator picked "not secure" even though the port is the TLS default.
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883", "Use TLS": "Disabled"})
        settings = build_mqtt_connection_settings({})
        self.assertFalse(settings.use_tls)
        self.assertEqual(settings.port, 8883)

    def test_use_tls_enabled_on_plain_port(self) -> None:
        # Operator picked "secure" against a non-8883 port — the choice wins.
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "1883", "Use TLS": "Enabled"})
        settings = build_mqtt_connection_settings({})
        self.assertTrue(settings.use_tls)

    def test_explicit_parameter_wins_over_configuration(self) -> None:
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883", "Use TLS": "Enabled"})
        settings = build_mqtt_connection_settings({"use_tls": False})
        self.assertFalse(settings.use_tls)

    def test_malformed_explicit_parameter_is_rejected(self) -> None:
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883", "Use TLS": "Enabled"})
        with self.assertRaisesRegex(ValueError, "use_tls must be a boolean"):
            build_mqtt_connection_settings({"use_tls": "Maybe"})

    def test_malformed_persisted_selection_is_rejected(self) -> None:
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883", "Use TLS": "Maybe"})
        with self.assertRaisesRegex(ValueError, "Use TLS must be Enabled or Disabled"):
            build_mqtt_connection_settings({})

    def test_port_inference_when_control_absent(self) -> None:
        # Legacy config without the "Use TLS" field keeps the port-based default.
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883"})
        self.assertTrue(build_mqtt_connection_settings({}).use_tls)
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "1883"})
        self.assertFalse(build_mqtt_connection_settings({}).use_tls)


class BuildMqttConnectionSettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_configuration_values_provider(None)

    def test_missing_broker_host_raises_settings_error(self) -> None:
        # No host in parameters and no configuration provider => a configuration
        # gap, surfaced as MqttSettingsError (not a network failure). The
        # explicit reset guards against a provider leaked by another test.
        set_configuration_values_provider(None)
        with self.assertRaises(MqttSettingsError):
            build_mqtt_connection_settings({})


class BrokerErrorStatusTests(unittest.TestCase):
    """Coarse, credential-free labels — config gap and DNS are distinct from
    an unreachable broker so the operator is pointed at the right fix."""

    def test_settings_error_is_configuration_gap(self) -> None:
        self.assertEqual(_broker_error_status(MqttSettingsError("x")), "broker_not_configured")

    def test_gaierror_is_dns_failure(self) -> None:
        self.assertEqual(
            _broker_error_status(socket.gaierror(11001, "getaddrinfo failed")),
            "dns_resolution_failed",
        )

    def test_name_resolution_text_is_dns_failure(self) -> None:
        # A gaierror message forwarded inside a plain OSError still classifies.
        self.assertEqual(
            _broker_error_status(OSError("Temporary failure in name resolution")),
            "dns_resolution_failed",
        )

    def test_connection_refused_is_unreachable(self) -> None:
        self.assertEqual(_broker_error_status(OSError("connection refused")), "broker_unreachable")

    def test_timeout_still_classifies(self) -> None:
        self.assertEqual(_broker_error_status(TimeoutError("timed out")), "broker_timeout")


class ParseCaptureSecondsTests(unittest.TestCase):
    """Blank/0/negative => indefinite (None); junk and non-finite => default.

    "nan"/"inf" parse as floats but would yield a bounded window whose deadline
    never expires (NaN/inf comparisons), so they must fall back to the default
    — and "-inf" must not slip through the <= 0 rule as explicit-indefinite.
    """

    def test_missing_value_keeps_default(self) -> None:
        self.assertEqual(parse_capture_seconds(None, default=30.0), 30.0)

    def test_blank_zero_and_negative_are_indefinite(self) -> None:
        for value in ("", "   ", 0, "0", -5, "-2.5"):
            with self.subTest(value=value):
                self.assertIsNone(parse_capture_seconds(value, default=30.0))

    def test_valid_value_is_used(self) -> None:
        self.assertEqual(parse_capture_seconds("2.5", default=30.0), 2.5)
        self.assertEqual(parse_capture_seconds(9, default=30.0), 9.0)

    def test_unparseable_value_keeps_default(self) -> None:
        self.assertEqual(parse_capture_seconds("soon", default=30.0), 30.0)

    def test_non_finite_values_keep_default(self) -> None:
        for value in ("nan", "inf", "-inf", "NaN", "Infinity", float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertEqual(parse_capture_seconds(value, default=30.0), 30.0)


if __name__ == "__main__":
    unittest.main()
