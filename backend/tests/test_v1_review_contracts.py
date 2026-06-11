import tempfile
import unittest
from pathlib import Path

from app.schemas.configuration import SecretMaterialRequest
from app.schemas.jobs import JobCreateRequest, ReportRequest
from app.services.configuration_service import ConfigurationService, DEFAULT_CONFIGURATION
from app.services.discovery_observations import build_observation, parse_port_specification
from app.services.import_service import ImportService
from app.services.run_service import RunService
from smart_commissioning_core.mqtt_config_publish import validate_and_publish_config
from smart_commissioning_core.mqtt_transport import MqttMessage
from smart_commissioning_core.udmi_validation import validate_udmi_full_report


class ConfigurationReviewTests(unittest.TestCase):
    def test_defaults_include_v1_review_fields(self) -> None:
        configuration = DEFAULT_CONFIGURATION

        self.assertIn("Subnet Mask", configuration.device.values)
        self.assertIn("DNS Servers", configuration.device.values)
        self.assertIn("BBMD UDP Port", configuration.bacnet.values)
        self.assertIn("Foreign Device", configuration.bacnet.values)
        self.assertIn("TTL", configuration.bacnet.values)
        self.assertIn("MQTT Broker FQDN or IP Address", configuration.mqtt.values)
        self.assertIn("Client ID", configuration.mqtt.values)
        self.assertIn("Keep Alive Interval", configuration.mqtt.values)
        self.assertIn("MQTT Username", configuration.mqtt.values)
        self.assertIn("MQTT Password", configuration.mqtt.values)
        self.assertIn("Last Backup Status", configuration.backups.values)
        self.assertIn("Restore Action", configuration.backups.values)
        self.assertNotIn("Certificate Validity", configuration.certificates.values)
        self.assertEqual(configuration.bacnet.values["Foreign Device"], "Disabled")

    def test_validation_rejects_bad_network_and_backup_values(self) -> None:
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.device.values["Subnet Mask"] = "bad-mask"
        configuration.device.values["DNS Servers"] = "not-an-ip"
        configuration.bacnet.values["TTL"] = "0"
        configuration.mqtt.values["Keep Alive Interval"] = "0"
        configuration.backups.values["Encrypted Backups"] = "Maybe"

        result = ConfigurationService().validate(configuration)

        self.assertFalse(result.valid)
        self.assertIn("Subnet Mask must be a valid IPv4 subnet mask.", result.errors)
        self.assertIn("DNS server 'not-an-ip' must be a valid IPv4 or IPv6 address.", result.errors)
        self.assertIn("BACnet TTL must be greater than zero.", result.errors)
        self.assertIn("MQTT Keep Alive Interval must be greater than zero.", result.errors)
        self.assertIn("Encrypted Backups must be Enabled or Disabled.", result.errors)

    def test_bbmd_locks_out_foreign_device_mode(self) -> None:
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.bacnet.values["BBMD"] = "Enabled"
        configuration.bacnet.values["Foreign Device"] = "Enabled"

        result = ConfigurationService().validate(configuration)

        self.assertFalse(result.valid)
        self.assertIn("Foreign Device must be Disabled when BBMD is Enabled.", result.errors)

    def test_secret_storage_returns_masked_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConfigurationService(path=Path(temp_dir) / "configuration.json")

            response = service.store_secret(
                SecretMaterialRequest(
                    field="CA Certificate",
                    file_name="ca.pem",
                    content="-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
                )
            )

            self.assertTrue(response.secret_ref.startswith("secret://ca-certificate-"))
            self.assertTrue(response.masked)
            self.assertEqual(response.validity, "stored")
            self.assertEqual(len(response.fingerprint), 16)


class DiscoveryReviewTests(unittest.TestCase):
    def test_ports_support_protocols_and_common_fallback(self) -> None:
        default_ports = parse_port_specification("")
        explicit_ports = parse_port_specification("47808/udp, 443/tcp")

        self.assertEqual([(port.port, port.protocol) for port in default_ports], [(47808, "udp"), (80, "tcp"), (443, "tcp")])
        self.assertEqual([(port.port, port.protocol, port.service) for port in explicit_ports], [(47808, "udp", "BACnet"), (443, "tcp", "HTTPS")])

    def test_mac_match_takes_precedence(self) -> None:
        observation = build_observation(
            {"ip_address": "192.168.4.203", "mac_address": "c0:a6:f3:f2:f3:2f", "hostname": "network-chip"},
            {"asset_id": "Milesight UG-65", "ip_address": "192.168.4.201", "mac_address": "C0-A6-F3-F2-F3-2F"},
        )

        self.assertEqual(observation.asset_id, "Milesight UG-65")
        self.assertEqual(observation.match_basis, "mac")
        self.assertEqual(observation.mac_address, "C0:A6:F3:F2:F3:2F")
        self.assertIsNotNone(observation.last_seen_at)


class ImportTemplateReviewTests(unittest.TestCase):
    def test_default_templates_include_required_columns_and_example_row(self) -> None:
        service = ImportService()

        csv_template = service.build_template("ip_register", "csv").decode("utf-8-sig")
        xlsx_template = service.build_template("bacnet_points", "xlsx")

        self.assertIn("Expected IP address", csv_template)
        self.assertIn("AHU-L03-017", csv_template)
        self.assertGreater(len(xlsx_template), 1000)


class ReportReviewTests(unittest.TestCase):
    def test_report_requests_preserve_docx_and_xlsx_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RunService(root=Path(temp_dir))

            _, excel_report = service.create_report_run(
                ReportRequest(
                    project_id="demo-project",
                    site_id="demo-site",
                    report_type="issue_report",
                    output_format="xlsx",
                )
            )
            _, word_report = service.create_report_run(
                ReportRequest(
                    project_id="demo-project",
                    site_id="demo-site",
                    report_type="evidence_pack",
                    output_format="docx",
                )
            )

            self.assertTrue(excel_report.file_name.endswith(".xlsx"))
            self.assertEqual(excel_report.output_format, "xlsx")
            self.assertTrue(word_report.file_name.endswith(".docx"))
            self.assertEqual(word_report.output_format, "docx")


class UdmiReviewTests(unittest.TestCase):
    def test_review_payloads_emit_precise_udmi_issues(self) -> None:
        result = validate_udmi_full_report(
            {
                "expected_schedule": {
                    "asset_id": "AHU-1000001",
                    "manufacturer": "ExpectedCo",
                    "model": "Model-A",
                    "guid": "ifc://expected",
                    "units": {"co2_concentration_sensor": "parts_per_million", "bad_temp_sensor": "dagrees_celsius"},
                },
                "state_payload": {
                    "timestamp": "2026-04-01T10:47:38.697+01:00",
                    "system": {"hardware": {"make": "ObservedCo", "model": "Model-B"}},
                },
                "metadata_payload": {
                    "timestamp": "2026-04-01T10:48:00.000+01:00",
                    "system": {"physical_tag": {"asset": {"guid": "ifc://observed"}}},
                    "pointset": {"points": {"co2_concentration_sensor": {"units": "parts_per_million"}}},
                },
                "pointset_payload": {
                    "timestamp": "2026-04-01T10:48:56.312+01:00",
                    "points": {"co2_concentration_sensor": {"present_value": "high"}},
                },
            }
        )

        descriptions = " ".join(issue.description for issue in result.issues)
        self.assertIn("manufacturer does not match", descriptions)
        self.assertIn("model does not match", descriptions)
        self.assertIn("Metadata GUID does not match", descriptions)
        self.assertIn("dagrees_celsius", descriptions)
        self.assertIn("should be numeric", descriptions)
        self.assertEqual(result.result_summary["message_count"], 3)
        self.assertEqual(result.result_summary["payload_last_seen"], "2026-04-01T10:48:56.312+01:00")
        self.assertEqual(result.result_summary["source"], "schedule_payload_inputs")

    def test_live_udmi_capture_populates_payload_inputs(self) -> None:
        def fake_capture(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            return [
                MqttMessage(
                    topic="334os/b1/ahu-1000001/state",
                    payload=b'{"timestamp":"2026-04-01T10:47:38.697+01:00","system":{"hardware":{"make":"ExpectedCo","model":"Model-A"}}}',
                ),
                MqttMessage(
                    topic="334os/b1/ahu-1000001/metadata",
                    payload=b'{"timestamp":"2026-04-01T10:48:00.000+01:00","system":{"physical_tag":{"asset":{"guid":"ifc://expected"}}},"pointset":{"points":{"co2_concentration_sensor":{"units":"parts_per_million"}}}}',
                ),
                MqttMessage(
                    topic="334os/b1/ahu-1000001/events/pointset",
                    payload=b'{"timestamp":"2026-04-01T10:48:56.312+01:00","pointset":{"points":{"co2_concentration_sensor":{"present_value":500}}}}',
                ),
            ]

        result = validate_udmi_full_report(
            {
                "broker_host": "mqtt.example.local",
                "expected_schedule": {
                    "asset_id": "AHU-1000001",
                    "manufacturer": "ExpectedCo",
                    "model": "Model-A",
                    "guid": "ifc://expected",
                    "units": {"co2_concentration_sensor": "parts_per_million"},
                },
                "metadata_topic": "334os/b1/ahu-1000001/metadata",
                "pointset_topic": "334os/b1/ahu-1000001/events/pointset",
                "state_topic": "334os/b1/ahu-1000001/state",
                "use_live_broker": True,
            },
            live_capture=fake_capture,
        )

        self.assertEqual(result.result_summary["broker_capture_attempted"], True)
        self.assertEqual(result.result_summary["broker_status_detail"], "live_payloads_captured")
        self.assertEqual(result.result_summary["message_count"], 3)
        self.assertEqual(result.result_summary["issue_count"], 0)


class MqttConfigPublishReviewTests(unittest.TestCase):
    def test_job_create_request_accepts_nested_pointset_parameters(self) -> None:
        request = JobCreateRequest.model_validate(
            {
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": "mqtt_config_publish",
                "parameters": {
                    "topic": "334os/b1/ahu-1000001/config",
                    "payload": '{"pointset":{"points":{"supply_air_temperature_setpoint":{"set_value":22}}}}',
                    "confirmed": True,
                    "expected_point": "supply_air_temperature_setpoint",
                    "expected_value": 22,
                    "next_pointset_payload": {
                        "pointset": {
                            "points": {
                                "supply_air_temperature_setpoint": {
                                    "present_value": 22,
                                }
                            }
                        }
                    },
                },
            }
        )

        next_pointset = request.parameters["next_pointset_payload"]

        self.assertIsInstance(next_pointset, dict)
        self.assertEqual(
            next_pointset["pointset"]["points"]["supply_air_temperature_setpoint"]["present_value"],
            22,
        )

    def test_publish_requires_confirmation_and_valid_topic(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "334os/b1/ahu-1000001/events/pointset",
                "payload": "{}",
                "confirmed": False,
            }
        )

        self.assertEqual(result.result_summary["status"], "failed")
        self.assertEqual([issue.issue_type for issue in result.issues], ["publish_not_confirmed", "invalid_config_topic"])

    def test_publish_success_observes_next_pointset_override(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "334os/b1/ahu-1000001/config",
                "payload": '{"pointset":{"points":{"supply_air_temperature_setpoint":{"set_value":22}}}}',
                "confirmed": True,
                "expected_point": "supply_air_temperature_setpoint",
                "expected_value": 22,
                "next_pointset_payload": {
                    "pointset": {"points": {"supply_air_temperature_setpoint": {"present_value": 22}}}
                },
            }
        )

        self.assertEqual(result.result_summary["status"], "succeeded")
        self.assertEqual(result.issues, [])
        self.assertEqual(result.result_summary["message_count"], 1)

    def test_publish_surfaces_connection_detail(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "334os/b1/ahu-1000001/config",
                "payload": "{}",
                "confirmed": True,
                "simulate_error": "tls",
            }
        )

        self.assertEqual(result.issues[0].status_detail, "tls_error")

    def test_publish_can_use_live_broker_pointset_message(self) -> None:
        calls: dict[str, object] = {}

        def fake_publisher(*args: object, **kwargs: object) -> MqttMessage:
            calls["settings"] = args[0]
            calls.update(kwargs)
            return MqttMessage(
                topic=str(kwargs["pointset_topic"]),
                payload=b'{"pointset":{"points":{"supply_air_temperature_setpoint":{"present_value":22}}}}',
            )

        result = validate_and_publish_config(
            {
                "broker_host": "mqtt.example.local",
                "broker_port": 1883,
                "topic": "334os/b1/ahu-1000001/config",
                "payload": '{"pointset":{"points":{"supply_air_temperature_setpoint":{"set_value":22}}}}',
                "confirmed": True,
                "expected_point": "supply_air_temperature_setpoint",
                "expected_value": 22,
                "pointset_topic": "334os/b1/ahu-1000001/events/pointset",
                "use_live_broker": True,
            },
            broker_publisher=fake_publisher,
        )

        self.assertEqual(result.result_summary["status"], "succeeded")
        self.assertEqual(result.result_summary["broker_publish_attempted"], True)
        self.assertEqual(result.result_summary["broker_status_detail"], "live_pointset_received")
        self.assertEqual(calls["pointset_topic"], "334os/b1/ahu-1000001/events/pointset")


if __name__ == "__main__":
    unittest.main()
