"""Tests for MQTT connection-settings resolution (secure/non-secure TLS)."""

import unittest

from smart_commissioning_core.mqtt_settings import (
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


class BrokerErrorStatusTests(unittest.TestCase):
    """The coarse status label must not send an operator down the wrong path."""

    def test_subscription_rejection_is_not_labelled_unreachable(self) -> None:
        from smart_commissioning_core.mqtt_settings import _broker_error_status
        from smart_commissioning_core.mqtt_transport import MqttTransportError

        # SUBACK 0x80 (an ACL denying the topic filter): the broker was reached and
        # authenticated, so "broker_unreachable" would send the operator to check
        # firewalls/hosts/ports instead of the broker's topic ACL.
        status = _broker_error_status(MqttTransportError("MQTT broker rejected the subscription."))
        self.assertEqual(status, "subscription_rejected")

    def test_labels_that_must_stay_stable(self) -> None:
        from smart_commissioning_core.mqtt_settings import _broker_error_status

        self.assertEqual(_broker_error_status(Exception("TLS handshake failed")), "tls_error")
        self.assertEqual(_broker_error_status(Exception("bad username or password")), "authentication_error")
        # A SUBACK acknowledgement timeout still reads as a timeout, not a rejection.
        self.assertEqual(
            _broker_error_status(Exception("MQTT broker timed out acknowledging the subscription.")),
            "broker_timeout",
        )
        self.assertEqual(_broker_error_status(Exception("connection refused")), "broker_unreachable")


if __name__ == "__main__":
    unittest.main()
