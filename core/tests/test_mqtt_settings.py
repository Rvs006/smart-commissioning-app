"""Tests for MQTT connection-settings resolution (secure/non-secure TLS)."""

import unittest

from smart_commissioning_core.mqtt_settings import (
    build_mqtt_connection_settings,
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

    def test_port_inference_when_control_absent(self) -> None:
        # Legacy config without the "Use TLS" field keeps the port-based default.
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "8883"})
        self.assertTrue(build_mqtt_connection_settings({}).use_tls)
        self._provide({"MQTT Broker FQDN or IP Address": "broker.test", "Port": "1883"})
        self.assertFalse(build_mqtt_connection_settings({}).use_tls)


if __name__ == "__main__":
    unittest.main()
