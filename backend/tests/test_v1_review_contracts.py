import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.schemas.configuration import SecretMaterialRequest
from app.schemas.jobs import JobCreateRequest, ReportRequest
from app.services import import_service as import_service_module
from app.services.configuration_service import DEFAULT_CONFIGURATION, ConfigurationService
from app.services.import_service import PROFILES, ImportService
from app.services.run_service import RunService
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.mqtt_config_publish import validate_and_publish_config
from smart_commissioning_core.mqtt_transport import MqttMessage
from smart_commissioning_core.udmi_validation import validate_udmi_full_report
from sqlalchemy.engine import Engine


def _temporary_engine(temp_dir: str) -> Engine:
    """Engine for a per-test SQLite database with the schema created."""
    engine = create_engine_from_url(default_sqlite_url(Path(temp_dir)))
    Base.metadata.create_all(engine)
    return engine


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

    def test_source_interface_seeds_empty_meaning_never_chosen(self) -> None:
        # Empty means "never chosen": the frontend seeds its wired-first
        # default only for this value, so the seed and the merge backfill for
        # legacy snapshots must both stay empty (the Auto sentinel is stored
        # only on an explicit dropdown pick) and empty must validate cleanly.
        self.assertEqual(DEFAULT_CONFIGURATION.device.values["Source Interface"], "")

        legacy = DEFAULT_CONFIGURATION.model_copy(deep=True)
        del legacy.device.values["Source Interface"]
        merged = ConfigurationService()._merge_with_defaults(legacy)
        self.assertEqual(merged.device.values["Source Interface"], "")

        result = ConfigurationService().validate(DEFAULT_CONFIGURATION.model_copy(deep=True))
        self.assertNotIn("Source Interface", " ".join(result.errors))

    def test_legacy_mqtt_config_derives_use_tls_from_port(self) -> None:
        # A config saved before the "Use TLS" control must keep its port-based
        # security: the merge must NOT force the static "Enabled" default onto a
        # legacy plaintext (1883) broker, and must keep TLS for a legacy 8883 one.
        plain = DEFAULT_CONFIGURATION.model_copy(deep=True)
        del plain.mqtt.values["Use TLS"]
        plain.mqtt.values["Port"] = "1883"
        merged_plain = ConfigurationService()._merge_with_defaults(plain)
        self.assertEqual(merged_plain.mqtt.values["Use TLS"], "Disabled")

        secure = DEFAULT_CONFIGURATION.model_copy(deep=True)
        del secure.mqtt.values["Use TLS"]
        secure.mqtt.values["Port"] = "8883"
        merged_secure = ConfigurationService()._merge_with_defaults(secure)
        self.assertEqual(merged_secure.mqtt.values["Use TLS"], "Enabled")

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
            engine = _temporary_engine(temp_dir)
            try:
                service = ConfigurationService(engine=engine)

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
            finally:
                engine.dispose()


class ImportTemplateReviewTests(unittest.TestCase):
    def test_default_templates_include_required_columns_and_example_row(self) -> None:
        service = ImportService()

        csv_template = service.build_template("ip_register", "csv").decode("utf-8-sig")
        xlsx_template = service.build_template("bacnet_points", "xlsx")

        self.assertIn("Expected IP address", csv_template)
        self.assertIn("AHU-L03-017", csv_template)
        self.assertGreater(len(xlsx_template), 1000)


class ImportRegisterFlexibilityTests(unittest.TestCase):
    """Register comments from on-site testing: asset one-of, optional Notes /
    Payload type, topic wildcards + lists, UDMI metadata columns."""

    _MQTT_BASE = {
        "Project/site": "Site A",
        "System": "BMS",
        "Expected topic": "hv/ems/01/em/EM-1001001/#",
        "Expected schema version": "1.5.2",
        "Expected points": "energy_sensor,power_sensor",
        "Expected units": "kwh,kw",
        "Expected reporting interval": "60",
        "Source protocol": "MQTT",
    }

    def _mqtt(self, **overrides: str) -> list:
        return PROFILES["mqtt_register"].validate_row({**self._MQTT_BASE, **overrides}, 2)

    def test_asset_id_or_name_is_one_of(self) -> None:
        self.assertEqual(self._mqtt(**{"Asset name": "Meter 9"}), [])  # name only
        self.assertEqual(self._mqtt(**{"Asset ID": "MTR-9"}), [])  # id only
        codes = [e.code for e in self._mqtt()]  # neither
        self.assertIn("missing_asset_identity", codes)

    def test_notes_and_payload_type_optional(self) -> None:
        # Blank Notes + blank Payload type must not reject the row.
        self.assertEqual(self._mqtt(**{"Asset ID": "MTR-9", "Notes": "", "Payload type": ""}), [])

    def test_topic_accepts_wildcard_and_comma_list(self) -> None:
        ok_list = self._mqtt(**{"Asset ID": "MTR-9", "Expected topic": "a/b/metadata,a/b/state,a/b/events/pointset"})
        self.assertEqual(ok_list, [])
        bad = self._mqtt(**{"Asset ID": "MTR-9", "Expected topic": "has a space/x"})
        self.assertIn("invalid_topic", [e.code for e in bad])
        for unsafe in ("#", "+", "a/#/state", "a/b/custom", "site/+/state"):
            with self.subTest(topic=unsafe):
                errors = self._mqtt(**{"Asset ID": "MTR-9", "Expected topic": unsafe})
                self.assertIn("invalid_topic", [error.code for error in errors])

    def test_payload_type_is_blank_or_a_supported_udmi_payload(self) -> None:
        self.assertEqual(self._mqtt(**{"Asset ID": "MTR-9", "Payload type": "state"}), [])
        errors = self._mqtt(**{"Asset ID": "MTR-9", "Payload type": "telemetry"})
        self.assertIn("invalid_payload_type", [error.code for error in errors])

    def test_expected_units_may_be_blank_per_point_but_not_exceed_points(self) -> None:
        self.assertEqual(self._mqtt(**{"Asset ID": "MTR-9", "Expected units": "kwh"}), [])
        self.assertEqual(
            self._mqtt(**{"Asset ID": "MTR-9", "Expected points": "energy_sensor,status_flag,power_sensor", "Expected units": "kwh,,kw"}),
            [],
        )
        errors = self._mqtt(**{"Asset ID": "MTR-9", "Expected units": "kwh,kw,volts"})

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].row_number, 2)
        self.assertEqual(errors[0].field, "Expected units")
        self.assertEqual(errors[0].code, "unit_without_point")
        self.assertIn("without a corresponding Expected point", errors[0].message)

    def test_mqtt_template_exposes_metadata_columns(self) -> None:
        columns = PROFILES["mqtt_register"].template_columns
        for column in ("Site", "Serial number", "Room", "GUID", "Make", "Model", "Firmware"):
            self.assertIn(column, columns)

    def test_ip_register_asset_one_of(self) -> None:
        ip_base = {
            "Project/site": "Site A",
            "System": "BMS",
            "Expected IP address": "10.10.25.117",
            "Expected services/ports": "443/tcp",
        }
        self.assertEqual(PROFILES["ip_register"].validate_row({**ip_base, "Asset name": "AHU"}, 2), [])
        self.assertIn(
            "missing_asset_identity",
            [e.code for e in PROFILES["ip_register"].validate_row(ip_base, 2)],
        )


class IpRegisterUdpWarningTests(unittest.TestCase):
    """UDP port entries in an ip_register are accepted but warned about: the
    IP scan engine is TCP-connect only, so a /udp entry is never verified
    (UDP 47808 / BACnet/IP belongs to the BACnet discovery run)."""

    _BASE = {
        "Project/site": "Site A",
        "System": "BMS",
        "Asset ID": "AHU-1",
        "Expected IP address": "10.10.25.117",
        "Expected services/ports": "443/tcp",
    }

    def _warnings(self, **overrides: str) -> list:
        row = {**self._BASE, **overrides}
        # Warnings never reject: the row must stay error-free.
        self.assertEqual(PROFILES["ip_register"].validate_row(row, 2), [])
        return PROFILES["ip_register"].collect_warnings(row, 2)

    def test_udp_expected_service_warns_and_names_the_bacnet_run(self) -> None:
        warnings = self._warnings(**{"Expected services/ports": "47808/udp, 443/tcp"})

        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning.row_number, 2)
        self.assertEqual(warning.field, "Expected services/ports")
        self.assertEqual(warning.code, "udp_port_not_verified")
        self.assertEqual(
            warning.message,
            "47808/udp is a UDP service — the IP scan verifies TCP ports only. "
            "UDP 47808 (BACnet/IP) is verified by the BACnet discovery run.",
        )

    def test_pure_tcp_rows_produce_no_warnings(self) -> None:
        self.assertEqual(self._warnings(), [])
        self.assertEqual(self._warnings(**{"Ports that should not be enabled": "23/tcp, 21/tcp"}), [])

    def test_udp_in_ports_that_should_not_be_enabled_warns_generically(self) -> None:
        warnings = self._warnings(**{"Ports that should not be enabled": "69/udp"})

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].field, "Ports that should not be enabled")
        self.assertEqual(warnings[0].code, "udp_port_not_verified")
        self.assertEqual(
            warnings[0].message,
            "69/udp is a UDP service — the IP scan verifies TCP ports only "
            "and cannot check this entry.",
        )

    def test_udp_match_is_case_insensitive_and_whitespace_tolerant(self) -> None:
        warnings = self._warnings(**{"Expected services/ports": "443/tcp,  47808 / UDP "})

        self.assertEqual([w.code for w in warnings], ["udp_port_not_verified"])
        self.assertIn("BACnet discovery run", warnings[0].message)

    def test_create_import_accepts_udp_rows_and_reports_summary_warnings(self) -> None:
        csv_bytes = (
            b"Project/site,System,Asset ID,Expected IP address,"
            b"Expected services/ports,Ports that should not be enabled\n"
            b'Site A,BMS,AHU-1,10.10.25.117,"47808/udp, 443/tcp",23/tcp\n'
            b"Site A,BMS,AHU-2,10.10.25.118,443/tcp,69/udp\n"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            engine = _temporary_engine(temp_dir)
            try:
                with mock.patch.object(import_service_module, "IMPORT_FILES_ROOT", Path(temp_dir)):
                    summary, report = ImportService(engine=engine).create_import(
                        import_type="ip_register",
                        file_name="register.csv",
                        file_bytes=csv_bytes,
                        project_id=None,
                        site_id=None,
                    )
            finally:
                engine.dispose()

        # Informational only: every row accepted, nothing on the error path.
        self.assertEqual(summary.status, "accepted")
        self.assertEqual((summary.accepted_rows, summary.rejected_rows), (2, 0))
        self.assertEqual(report.errors, [])
        self.assertEqual(
            [(w.row_number, w.field) for w in summary.warnings],
            [(2, "Expected services/ports"), (3, "Ports that should not be enabled")],
        )


class ReportReviewTests(unittest.TestCase):
    def test_report_requests_preserve_docx_and_xlsx_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = _temporary_engine(temp_dir)
            try:
                service = RunService(engine=engine)

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
            finally:
                engine.dispose()


class UdmiRegisterScheduleTests(unittest.TestCase):
    def test_expected_schedule_built_from_register_row(self) -> None:
        from app.api.routes.validation import _expected_schedule_from_register_row

        schedule = _expected_schedule_from_register_row(
            {
                "Asset ID": "EM-9",
                "Make": "Acme",
                "Model": "M1",
                "GUID": "ifc://g",
                "Serial number": "SN1",
                "Firmware": "1.2",
                "Site": "B",
                "Room": "L3",
                "Expected points": "energy_sensor,power_sensor",
                "Expected units": "kwh,kw",
                "Expected schema version": "1.5.2",
                "Expected reporting interval": "20",
            }
        )

        self.assertEqual(schedule["manufacturer"], "Acme")
        self.assertEqual(schedule["serial"], "SN1")
        self.assertEqual(schedule["units"], {"energy_sensor": "kwh", "power_sensor": "kw"})
        # The template's Expected schema version drives the payload version match.
        self.assertEqual(schedule["udmi_version"], "1.5.2")
        self.assertEqual(schedule["reporting_interval_seconds"], "20")
        # Blank register fields are dropped so the matcher only checks what's set.
        self.assertNotIn("model", _expected_schedule_from_register_row({"Asset ID": "x"}))
        self.assertNotIn("udmi_version", _expected_schedule_from_register_row({"Asset ID": "x"}))

    def test_asset_entry_derives_capture_topics_from_wildcard(self) -> None:
        from app.api.routes.validation import _asset_entry_from_row

        entry = _asset_entry_from_row(
            {"Asset ID": "EM-9", "Make": "Acme", "Expected topic": "hv/ems/01/em/EM-1001001/#"}
        )
        self.assertEqual(entry["expected_schedule"]["manufacturer"], "Acme")
        self.assertEqual(entry["state_topic"], "hv/ems/01/em/EM-1001001/state")
        self.assertEqual(entry["metadata_topic"], "hv/ems/01/em/EM-1001001/metadata")
        self.assertEqual(entry["pointset_topic"], "hv/ems/01/em/EM-1001001/events/pointset")
        # A wildcard also captures the legacy singular event/pointset convention.
        self.assertEqual(entry["extra_capture_topics"], ["hv/ems/01/em/EM-1001001/event/pointset"])

    def test_explicit_topic_list_has_no_legacy_alias(self) -> None:
        from app.api.routes.validation import _capture_topics_from_expected

        topics = _capture_topics_from_expected("a/b/metadata,a/b/state,a/b/events/pointset")
        self.assertEqual(topics["pointset_topic"], "a/b/events/pointset")
        self.assertNotIn("extra_capture_topics", topics)

    def test_blank_payload_type_expands_one_explicit_topic_to_the_whole_asset(self) -> None:
        from app.api.routes.validation import _asset_entry_from_row

        entry = _asset_entry_from_row(
            {
                "Asset ID": "EM-9",
                "Payload type": "",
                "Expected topic": "a/b/events/pointset",
            }
        )

        self.assertEqual(entry["state_topic"], "a/b/state")
        self.assertEqual(entry["metadata_topic"], "a/b/metadata")
        self.assertEqual(entry["pointset_topic"], "a/b/events/pointset")
        self.assertEqual(entry["extra_capture_topics"], ["a/b/event/pointset"])

    def test_explicit_payload_type_limits_a_wildcard_to_that_payload(self) -> None:
        from app.api.routes.validation import _asset_entry_from_row

        entry = _asset_entry_from_row(
            {
                "Asset ID": "EM-9",
                "Payload type": "pointset",
                "Expected topic": "a/b/#",
            }
        )

        self.assertNotIn("state_topic", entry)
        self.assertNotIn("metadata_topic", entry)
        self.assertEqual(entry["pointset_topic"], "a/b/events/pointset")


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

    def test_review_flags_serial_firmware_site_and_room(self) -> None:
        result = validate_udmi_full_report(
            {
                "expected_schedule": {
                    "asset_id": "EM-1",
                    "serial": "SN-AAA",
                    "firmware": "1.0.0",
                    "site": "Block B",
                    "room": "L3",
                },
                "state_payload": {"system": {"serial_no": "SN-BBB", "software": {"firmware": "2.0.0"}}},
                "metadata_payload": {"system": {"location": {"site": "Block C", "section": "L4"}}},
            }
        )

        descriptions = " ".join(issue.description for issue in result.issues)
        self.assertIn("serial number does not match", descriptions)
        self.assertIn("firmware version does not match", descriptions)
        self.assertIn("site does not match", descriptions)
        self.assertIn("room/section does not match", descriptions)

    def test_assets_list_captures_and_reviews_per_asset(self) -> None:
        # Live capture is ONE shared subscription across every asset's topics;
        # messages route back per entry and the matcher fans out so a mismatch
        # is flagged for EACH asset in one run.
        def fake_capture(_settings: object, *, topics: list[str], **_kwargs: object) -> list[MqttMessage]:
            return [
                MqttMessage(topic=topic, payload=b'{"system":{"hardware":{"make":"WrongCo"}}}')
                for topic in topics
                if topic.endswith("/state")
            ]

        result = validate_udmi_full_report(
            {
                "use_live_broker": True,
                "assets": [
                    {"expected_schedule": {"asset_id": "A1", "manufacturer": "Acme"}, "state_topic": "site/a1/state"},
                    {"expected_schedule": {"asset_id": "A2", "manufacturer": "Globex"}, "state_topic": "site/a2/state"},
                ],
            },
            live_capture=fake_capture,
        )

        manufacturer_issues = [i for i in result.issues if "manufacturer does not match" in i.description]
        self.assertEqual({i.asset_id for i in manufacturer_issues}, {"A1", "A2"})

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

    def test_payload_views_present_for_direct_inputs(self) -> None:
        # mq9m4bnv: per-payload-type expected-vs-observed view from pasted inputs.
        result = validate_udmi_full_report(
            {
                "expected_schedule": {
                    "asset_id": "AHU-1000001",
                    "manufacturer": "ExpectedCo",
                    "model": "Model-A",
                    "guid": "ifc://expected",
                    "units": {"co2_concentration_sensor": "parts_per_million"},
                },
                "state_payload": {"system": {"hardware": {"make": "ExpectedCo", "model": "Model-A"}}},
                "metadata_payload": {"system": {"physical_tag": {"asset": {"guid": "ifc://expected"}}}},
                "pointset_payload": {"points": {"co2_concentration_sensor": {"present_value": 500}}},
            }
        )
        self.assertEqual(result.result_summary["payload_view_source"], "direct_inputs")
        views = result.result_summary["payload_views"]
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["asset_id"], "AHU-1000001")
        by_type = {pt["payload_type"]: pt for pt in views[0]["payload_types"]}
        self.assertEqual(set(by_type), {"state", "metadata", "pointset"})
        self.assertTrue(all(pt["observed_present"] for pt in by_type.values()))
        # Observed payload passes through verbatim; expected is a UDMI-shaped
        # template with register constraints and explicit device placeholders.
        self.assertEqual(by_type["state"]["observed"]["system"]["hardware"]["make"], "ExpectedCo")
        # Template timestamp is the build time (RFC 3339), not the old epoch
        # sentinel that read as a broken device clock on site.
        template_timestamp = by_type["state"]["expected"]["timestamp"]
        self.assertRegex(template_timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertFalse(template_timestamp.startswith("1970"))
        self.assertEqual(by_type["state"]["expected"]["system"]["hardware"], {"make": "ExpectedCo", "model": "Model-A"})
        self.assertEqual(by_type["state"]["expected"]["system"]["serial_no"], "<device serial number>")
        self.assertEqual(
            by_type["metadata"]["expected"]["system"]["physical_tag"]["asset"]["guid"],
            "ifc://expected",
        )

    def test_payload_views_from_live_capture(self) -> None:
        def fake_capture(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            return [
                MqttMessage(
                    topic="334os/b1/ahu-1000001/state",
                    payload=b'{"system":{"hardware":{"make":"ExpectedCo","model":"Model-A"}}}',
                ),
                MqttMessage(
                    topic="334os/b1/ahu-1000001/events/pointset",
                    payload=b'{"pointset":{"points":{"co2_concentration_sensor":{"present_value":500}}}}',
                ),
            ]

        result = validate_udmi_full_report(
            {
                "broker_host": "mqtt.example.local",
                "expected_schedule": {"asset_id": "AHU-1000001", "manufacturer": "ExpectedCo"},
                "state_topic": "334os/b1/ahu-1000001/state",
                "pointset_topic": "334os/b1/ahu-1000001/events/pointset",
                "use_live_broker": True,
            },
            live_capture=fake_capture,
        )
        self.assertEqual(result.result_summary["payload_view_source"], "live_capture")
        by_type = {
            pt["payload_type"]: pt for pt in result.result_summary["payload_views"][0]["payload_types"]
        }
        self.assertEqual(by_type["state"]["observed"]["system"]["hardware"]["make"], "ExpectedCo")
        self.assertTrue(by_type["state"]["observed_present"])

    def test_failed_live_capture_does_not_mislabel_pasted_payloads(self) -> None:
        # Audit fix: a failed/timed-out live capture leaves the pasted default
        # payloads in place with NO captured topics; they must NOT be relabelled
        # "live_capture" (which would present pasted values as real device data).
        def failing_capture(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise OSError("broker unreachable")

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
                "state_payload": {"system": {"hardware": {"make": "ExpectedCo", "model": "Model-A"}}},
                "metadata_payload": {"system": {"physical_tag": {"asset": {"guid": "ifc://expected"}}}},
                "pointset_payload": {"points": {"co2_concentration_sensor": {"present_value": 500}}},
                "state_topic": "334os/b1/ahu-1000001/state",
                "use_live_broker": True,
            },
            live_capture=failing_capture,
        )

        self.assertEqual(result.result_summary["captured_topics"], [])
        self.assertEqual(result.result_summary["payload_view_source"], "direct_inputs")
        self.assertTrue(any("Live MQTT capture failed" in issue.description for issue in result.issues))

    def test_payload_views_empty_for_fixture_path(self) -> None:
        # The bundled fixture carries no payload JSON, so the view must stay empty
        # and be labelled rather than fabricated.
        result = validate_udmi_full_report({})
        self.assertEqual(result.result_summary["payload_views"], [])
        self.assertEqual(result.result_summary["payload_view_source"], "none")

    def test_payload_views_multi_asset_from_assets_list(self) -> None:
        # A multi-AHU run supplies a per-asset `assets` list; the payload view
        # emits one entry per asset (single top-level params stay back-compat).
        result = validate_udmi_full_report(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "AHU-1", "manufacturer": "Acme"},
                        "state_payload": {"system": {"hardware": {"make": "Acme"}}},
                    },
                    {
                        "expected_schedule": {"asset_id": "AHU-2", "units": {"co2": "parts_per_million"}},
                        "pointset_payload": {"points": {"co2": {"present_value": 400}}},
                    },
                ]
            }
        )
        views = result.result_summary["payload_views"]
        self.assertEqual({view["asset_id"] for view in views}, {"AHU-1", "AHU-2"})
        self.assertEqual(result.result_summary["payload_view_source"], "direct_inputs")

    def test_review_issues_fan_out_across_assets_list(self) -> None:
        # Multi-asset run: issue generation runs once per `assets` entry and
        # aggregates, so both assets surface their own mismatches in one run.
        result = validate_udmi_full_report(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "AHU-1", "manufacturer": "ExpectedCo"},
                        "state_payload": {"system": {"hardware": {"make": "ObservedCo"}}},
                    },
                    {
                        "expected_schedule": {
                            "asset_id": "AHU-2",
                            "points": ["co2"],
                            "units": {"co2": "parts_per_million"},
                        },
                        "metadata_payload": {"pointset": {"points": {"co2": {}}}},
                        "pointset_payload": {"points": {"co2": {"present_value": "high"}}},
                    },
                ]
            }
        )
        flagged_assets = {issue.asset_id for issue in result.issues}
        self.assertIn("AHU-1", flagged_assets)
        self.assertIn("AHU-2", flagged_assets)
        descriptions = " ".join(issue.description for issue in result.issues)
        self.assertIn("manufacturer does not match", descriptions)
        self.assertIn("does not declare units", descriptions)


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
                # A live publish is now an authorized active operation gated in
                # the engine core; supply authorization to exercise the live path.
                "authorized": True,
            },
            broker_publisher=fake_publisher,
        )

        self.assertEqual(result.result_summary["status"], "succeeded")
        self.assertEqual(result.result_summary["broker_publish_attempted"], True)
        self.assertEqual(result.result_summary["broker_status_detail"], "live_pointset_received")
        self.assertEqual(calls["pointset_topic"], "334os/b1/ahu-1000001/events/pointset")


if __name__ == "__main__":
    unittest.main()
