"""Report content tests for the per-head Discovery inventory (field ask 2026-07-15).

A report scoped to discovery runs must carry the ACTUAL inventory those runs
recorded — IP hosts, BACnet devices/points, expected-but-silent BACnet devices,
and MQTT topics — not just run metadata. All in-process against the shared
temporary SQLite DB (no live infra). Covers:

  * the six inventory sections in a fixed order across zip/pdf/docx/xlsx;
  * honest rendering: an empty port list is a blank, never a fabricated verdict;
    a per-point read_error is shown as the error, never a value; the silent
    section reads inconclusive (never "fail"); MQTT payloads are excluded;
  * deterministic ordering: DB-ordered device/point/topic rows plus the explicit
    (asset_id, instance) sort of the silent rows;
  * byte-reproducibility + evidence verify across all four formats (the hard
    constraint — the artifacts are Ed25519/SHA-256 signed);
  * gating: non-discovery reports carry no inventory member/sheets; a mixed
    validation+discovery report carries both; an empty discovery run renders its
    section with the empty note rather than omitting it; a pre-v0.1.12 bacnet run
    (no expected_not_responding key) omits the silent section without error.

Source rows are seeded through RunService's public API + DiscoveryRepository —
the same records a real discovery run persists — so the report path is exercised
exactly as in production without needing a network scan. All ids/addresses are
fictional (public repo).
"""

import io
import json
import unittest
import zipfile

from harness import ApiTestCase

_API_KEY = "test-reports-discovery-inventory-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_EM_DASH = "—"

# -- fictional fixtures (public repo) ----------------------------------------

_IP_SUMMARY = {"hosts_scanned": 3, "hosts_responsive": 2}
_IP_DEVICES = [
    {
        "project_id": "demo-project",
        "site_id": "demo-site",
        "address": "10.10.0.11",
        "device_type": "ip_host",
        "name": "plc-01.example.test",
        "attributes": {
            "open_ports": [80, 443],
            "forbidden_open_ports": [23],
            "unexpected_open_ports": [8080],
            "missing_expected_ports": [502],
            "mac_address": "02:00:5e:00:00:11",
        },
    },
    {
        # A responder with no open ports — the honest-blank case: every port
        # cell must render _NO_VALUE, never a synthesised "fail".
        "project_id": "demo-project",
        "site_id": "demo-site",
        "address": "10.10.0.12",
        "device_type": "ip_host",
        "name": None,
        "attributes": {
            "open_ports": [],
            "forbidden_open_ports": [],
            "unexpected_open_ports": [],
            "missing_expected_ports": [],
            "mac_address": None,
        },
    },
]

_BACNET_SUMMARY = {
    "expected_device_count": 3,
    "expected_responding_count": 1,
    # Seeded OUT OF (asset_id) ORDER to prove the report sorts them.
    "expected_not_responding": [
        {
            "asset_id": "VAV-9",
            "asset_name": "VAV Zone 9",
            "device_instance": 2099,
            "address": "10.20.0.9",
            "directed_probe_sent": True,
        },
        {
            "asset_id": "AHU-2",
            "asset_name": "AHU Controller 2",
            "device_instance": 2002,
            "address": "10.20.0.6",
            "directed_probe_sent": False,
        },
    ],
}
_BACNET_DEVICES = [
    {
        "address": "10.20.0.5",
        "device_type": "bacnet_device",
        "name": "AHU Controller",
        "vendor": "AcmeBAS",
        "model": "BC-100",
        "attributes": {
            "asset_id": "AHU-1",
            "device_instance": 2001,
            "register_asset_id": "REG-AHU-1",
            "register_asset_name": "AHU-1 Supply",
        },
    },
]
_BACNET_POINTS = [
    {
        "device_ref": "AHU-1",
        "point_id": "analogInput:1",
        "point_name": "Supply Air Temp",
        "observed_value": {"value": 21.4},
        "units": "degC",
        "attributes": {"object_type": "analogInput", "device_instance": 2001},
    },
    {
        # A per-point read failure: rendered as the error string, never a value.
        "device_ref": "AHU-1",
        "point_id": "analogValue:3",
        "point_name": "Occupied Setpoint",
        "observed_value": {},
        "units": None,
        "attributes": {"object_type": "analogValue", "read_error": "present_value_read_failed"},
    },
]

_MQTT_SUMMARY = {"topics_discovered": 2, "messages_captured": 5}
_MQTT_TOPICS = [
    {
        "topic": "site/ahu/1/temp",
        "message_count": 4,
        "last_payload": {"value": 21.4},
        "attributes": {"device_ref": "AHU-1"},
    },
    {
        # Non-JSON payload stored as a marker; the report excludes payloads.
        "topic": "site/raw/blob",
        "message_count": 1,
        "last_payload": {"_raw_present": True},
        "attributes": {"device_ref": "raw-1"},
    },
]

# A validation result_summary (for the gating / mixed-report tests). Same shape
# the validation report suite seeds.
_VALIDATION_SUMMARY = {
    "expected_devices": 10,
    "publishing_seen": 8,
    "not_publishing": 2,
    "not_publishing_devices": ["AHU-7", "FCU-3"],
    "issue_count": 0,
    "blocking_issue_count": 0,
    "payload_conformance_percent": 97,
}


class DiscoveryInventoryReportApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-reports-discovery-")
        atexit.register(shutil.rmtree, cls._temp_runtime, ignore_errors=True)
        secrets_root = Path(cls._temp_runtime) / "secrets"
        secrets_root.mkdir(parents=True, exist_ok=True)

        import app.services.reports_integrity as integrity_module

        cls._patcher = mock.patch.object(integrity_module, "SECRETS_ROOT", secrets_root)
        cls._patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        cls._patcher.stop()

    # -- seeding helpers -------------------------------------------------------

    def _seed_discovery_run(
        self,
        job_type: str,
        result_summary: dict,
        *,
        devices: list[dict] | None = None,
        points: list[dict] | None = None,
        topics: list[dict] | None = None,
    ) -> str:
        from smart_commissioning_core.db.repositories import DiscoveryRepository

        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        run_service = RunService()
        run = run_service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type=job_type,
                parameters={},
            ),
            expected_job_type=job_type,
        )
        run_service.update_run_status(run.run_id, status="succeeded", stage="done", progress_percent=100)
        run_service.update_result_summary(run.run_id, result_summary)
        repository = DiscoveryRepository(run_service.engine)
        if devices is not None:
            repository.replace_devices(run.run_id, devices)
        if points is not None:
            repository.replace_points(run.run_id, points)
        if topics is not None:
            repository.replace_topics(run.run_id, topics)
        return run.run_id

    def _seed_validation_run(self, result_summary: dict) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        run_service = RunService()
        run = run_service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type="udmi_validation",
                parameters={},
            ),
            expected_job_type="udmi_validation",
        )
        run_service.update_run_status(run.run_id, status="succeeded", stage="done", progress_percent=100)
        run_service.update_result_summary(run.run_id, result_summary)
        return run.run_id

    def _seed_all_three(self) -> list[str]:
        ip_run = self._seed_discovery_run("ip_discovery", _IP_SUMMARY, devices=_IP_DEVICES)
        bacnet_run = self._seed_discovery_run(
            "bacnet_discovery", _BACNET_SUMMARY, devices=_BACNET_DEVICES, points=_BACNET_POINTS
        )
        mqtt_run = self._seed_discovery_run("mqtt_discovery", _MQTT_SUMMARY, topics=_MQTT_TOPICS)
        return [ip_run, bacnet_run, mqtt_run]

    def _create_report(self, output_format: str, source_run_ids: list[str], report_type: str = "evidence_pack") -> str:
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": report_type,
                "output_format": output_format,
                "source_run_ids": source_run_ids,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["report_id"]

    def _download(self, report_id: str):
        response = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _zip_member(self, content: bytes, name: str) -> dict:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return json.loads(archive.read(name))

    def _zip_names(self, content: bytes) -> set[str]:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return set(archive.namelist())

    # -- zip inventory content -------------------------------------------------

    def test_zip_inventory_sections_and_content(self) -> None:
        report_id = self._create_report("zip", self._seed_all_three())
        content = self._download(report_id).content

        inventory = self._zip_member(content, "discovery_inventory.json")
        titles = [section["title"] for section in inventory["sections"]]
        self.assertEqual(
            titles,
            [
                "Discovery summary",
                "Discovered IP hosts",
                "Discovered BACnet devices",
                "Discovered BACnet points",
                "Expected BACnet devices not responding",
                "Discovered MQTT topics",
            ],
        )
        sections = {section["title"]: section for section in inventory["sections"]}

        # IP host row: persisted facts verbatim; the no-open-ports responder is
        # a blank, never a fabricated verdict.
        ip_rows = sections["Discovered IP hosts"]["rows"]
        self.assertEqual(len(ip_rows), 2)
        self.assertEqual(ip_rows[0]["Address"], "10.10.0.11")
        self.assertEqual(ip_rows[0]["Hostname"], "plc-01.example.test")
        self.assertEqual(ip_rows[0]["MAC"], "02:00:5e:00:00:11")
        self.assertEqual(ip_rows[0]["Open Ports"], "80, 443")
        self.assertEqual(ip_rows[0]["Forbidden Open"], "23")
        self.assertEqual(ip_rows[1]["Open Ports"], _EM_DASH)
        self.assertEqual(ip_rows[1]["Hostname"], _EM_DASH)
        self.assertEqual(ip_rows[1]["MAC"], _EM_DASH)

        # BACnet device row: instance, register identity, and a point count.
        device_rows = sections["Discovered BACnet devices"]["rows"]
        self.assertEqual(len(device_rows), 1)
        self.assertEqual(device_rows[0]["Instance"], "2001")
        self.assertEqual(device_rows[0]["Register Asset"], "AHU-1 Supply")
        self.assertEqual(device_rows[0]["Points"], "2")

        # BACnet point rows: an observed value, and a read failure shown as the
        # error string — never a value.
        point_rows = sections["Discovered BACnet points"]["rows"]
        self.assertEqual(point_rows[0]["Value"], "21.4")
        self.assertEqual(point_rows[1]["Value"], "present_value_read_failed")
        self.assertEqual(point_rows[1]["Units"], _EM_DASH)

        # Silent rows: sorted by (asset_id, instance) despite the seed order, with
        # a sent/not-sent Directed Who-Is cell and the inconclusive note.
        silent = sections["Expected BACnet devices not responding"]
        self.assertEqual([row["Instance"] for row in silent["rows"]], ["2002", "2099"])
        self.assertEqual(silent["rows"][0]["Register Asset"], "AHU Controller 2 (AHU-2)")
        self.assertEqual(silent["rows"][0]["Directed Who-Is"], "not sent")
        self.assertEqual(silent["rows"][1]["Directed Who-Is"], "sent")
        self.assertIn("neither confirmed present nor absent", silent["note"])
        self.assertNotIn("fail", silent["note"].lower())

        # MQTT rows: topic/count/device_ref, and NO payload anywhere.
        mqtt_rows = sections["Discovered MQTT topics"]["rows"]
        self.assertEqual(mqtt_rows[0]["Topic"], "site/ahu/1/temp")
        self.assertEqual(mqtt_rows[0]["Messages"], "4")
        self.assertEqual(mqtt_rows[0]["Device Ref"], "AHU-1")
        self.assertNotIn("_raw_present", json.dumps(inventory))
        self.assertNotIn("last_payload", json.dumps(inventory))

        # Summary counts are authored from result_summary fields per head.
        summary_counts = {row["Type"]: row["Counts"] for row in sections["Discovery summary"]["rows"]}
        self.assertEqual(summary_counts["ip_discovery"], "3 hosts scanned, 2 responsive")
        self.assertEqual(summary_counts["bacnet_discovery"], "1 devices, 2 points read, 1/3 expected devices responding")
        self.assertEqual(summary_counts["mqtt_discovery"], "2 topics, 5 messages")

    # -- byte reproducibility + verify across all four formats -----------------

    def test_all_formats_byte_reproducible_and_verifiable(self) -> None:
        source_run_ids = self._seed_all_three()
        for output_format in ("pdf", "xlsx", "docx", "zip"):
            report_id = self._create_report(output_format, source_run_ids)
            first = self._download(report_id).content
            second = self._download(report_id).content
            self.assertEqual(first, second, f"{output_format} artifact must be reproducible")

            verify = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
            self.assertEqual(verify.status_code, 200, verify.text)
            body = verify.json()
            self.assertTrue(body["hash_matches"], (output_format, body))
            self.assertTrue(body["signature_valid"], (output_format, body))

    # -- format content spot-checks --------------------------------------------

    def test_pdf_carries_inventory(self) -> None:
        report_id = self._create_report("pdf", self._seed_all_three())
        content = self._download(report_id).content
        self.assertTrue(content.startswith(b"%PDF-1.4"))
        for expected in (
            b"Discovered BACnet devices",
            b"Discovered MQTT topics",
            b"AHU Controller",
            b"site/ahu/1/temp",
            b"present_value_read_failed",
        ):
            self.assertIn(expected, content)

    def test_docx_carries_inventory_titles_and_empty_note(self) -> None:
        # An IP run with zero device rows must still render its section with the
        # empty note, not omit it.
        empty_ip = self._seed_discovery_run("ip_discovery", {"hosts_scanned": 0, "hosts_responsive": 0}, devices=[])
        bacnet_run = self._seed_discovery_run(
            "bacnet_discovery", _BACNET_SUMMARY, devices=_BACNET_DEVICES, points=_BACNET_POINTS
        )
        report_id = self._create_report("docx", [empty_ip, bacnet_run])
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        for expected in (
            "Discovery summary",
            "Discovered IP hosts",
            "Discovered BACnet devices",
            "AHU-1 Supply",
            "an empty scan is a recorded result",
            "neither confirmed present nor absent",
        ):
            self.assertIn(expected, document_xml)

    def test_xlsx_sheet_names_and_cells(self) -> None:
        report_id = self._create_report("xlsx", self._seed_all_three())
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            all_xml = "".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if name.endswith(".xml")
            )
        for sheet_name in (
            "Discovery summary",
            "Discovered IP hosts",
            "Discovered BACnet devices",
            "Discovered BACnet points",
            # The 38-char title is remapped to a 31-char-safe sheet name.
            "Expected not responding",
            "Discovered MQTT topics",
        ):
            self.assertIn(sheet_name, workbook_xml)
        for expected in ("10.10.0.11", "AHU Controller", "site/ahu/1/temp", "present_value_read_failed"):
            self.assertIn(expected, all_xml)

    # -- gating ----------------------------------------------------------------

    def test_validation_only_report_has_no_inventory(self) -> None:
        run_id = self._seed_validation_run(_VALIDATION_SUMMARY)
        report_id = self._create_report("zip", [run_id], report_type="udmi_validation")
        content = self._download(report_id).content
        self.assertNotIn("discovery_inventory.json", self._zip_names(content))
        # The validation members are still present (existing shape preserved).
        self.assertIn("validation_summary.json", self._zip_names(content))

    def test_mixed_report_carries_validation_and_inventory(self) -> None:
        validation_run = self._seed_validation_run(_VALIDATION_SUMMARY)
        bacnet_run = self._seed_discovery_run(
            "bacnet_discovery", _BACNET_SUMMARY, devices=_BACNET_DEVICES, points=_BACNET_POINTS
        )
        report_id = self._create_report("zip", [validation_run, bacnet_run])
        content = self._download(report_id).content
        names = self._zip_names(content)
        self.assertIn("validation_summary.json", names)
        self.assertIn("discovery_inventory.json", names)
        titles = [s["title"] for s in self._zip_member(content, "discovery_inventory.json")["sections"]]
        self.assertIn("Discovered BACnet devices", titles)

    def test_empty_discovery_run_renders_empty_note(self) -> None:
        empty_mqtt = self._seed_discovery_run("mqtt_discovery", {"topics_discovered": 0, "messages_captured": 0}, topics=[])
        report_id = self._create_report("zip", [empty_mqtt])
        inventory = self._zip_member(self._download(report_id).content, "discovery_inventory.json")
        mqtt = next(s for s in inventory["sections"] if s["title"] == "Discovered MQTT topics")
        self.assertEqual(mqtt["rows"], [])
        self.assertIn("an empty scan is a recorded result", mqtt["note"])

    def test_pre_v0112_bacnet_run_omits_silent_section(self) -> None:
        # A bacnet run recorded before v0.1.12 never persisted the
        # expected_not_responding key; the silent section is omitted (no error),
        # while the device/point sections still render.
        pre_summary = {"expected_device_count": 1, "expected_responding_count": 1}
        run_id = self._seed_discovery_run(
            "bacnet_discovery", pre_summary, devices=_BACNET_DEVICES, points=_BACNET_POINTS
        )
        report_id = self._create_report("zip", [run_id])
        inventory = self._zip_member(self._download(report_id).content, "discovery_inventory.json")
        titles = [s["title"] for s in inventory["sections"]]
        self.assertIn("Discovered BACnet devices", titles)
        self.assertNotIn("Expected BACnet devices not responding", titles)
        # The expected-responding fragment still appears: it is gated on the two
        # count keys, which this run does carry — only the silent list is absent.
        summary_counts = next(s for s in inventory["sections"] if s["title"] == "Discovery summary")["rows"][0]["Counts"]
        self.assertIn("1/1 expected devices responding", summary_counts)


if __name__ == "__main__":
    unittest.main()
