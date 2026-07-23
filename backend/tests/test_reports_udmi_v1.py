"""Versioned UDMI report contract coverage across every report renderer."""

from __future__ import annotations

import copy
import io
import json
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, current_thread
from unittest import mock

from harness import ApiTestCase
from openpyxl import load_workbook
from pydantic import ValidationError

_API_KEY = "test-reports-udmi-v1-key"
_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


def _metric_groups(
    *,
    assets: tuple[int, int, int, int, int],
    payloads: tuple[int, int, int, int],
    faults: tuple[int, int, int, int, int, int],
    issues: tuple[int, int],
) -> dict[str, dict[str, int]]:
    return {
        "asset_metrics": dict(
            zip(
                ("expected", "observed", "not_observed", "with_issues", "successfully_validated"),
                assets,
                strict=True,
            )
        ),
        "payload_metrics": dict(
            zip(
                ("expected", "received", "with_issues", "successfully_validated"),
                payloads,
                strict=True,
            )
        ),
        "fault_metrics": dict(
            zip(
                (
                    "payload_formatting_issues",
                    "missing_points",
                    "point_naming_issues",
                    "additional_points",
                    "stale_or_cadence",
                    "other_issues",
                ),
                faults,
                strict=True,
            )
        ),
        "issue_metrics": dict(zip(("blocking", "warning"), issues, strict=True)),
    }


_TOTALS = _metric_groups(
    assets=(3, 2, 1, 2, 1),
    payloads=(7, 5, 2, 4),
    faults=(1, 1, 1, 1, 1, 1),
    issues=(3, 3),
)

_V1_SUMMARY = {
    "schema_version": "1.0",
    **_TOTALS,
    "system_metrics": [
        {
            "system": "BMS",
            **_metric_groups(
                assets=(2, 2, 0, 1, 1),
                payloads=(6, 5, 1, 4),
                faults=(1, 1, 1, 1, 0, 0),
                issues=(2, 2),
            ),
        },
        {
            "system": "LTG",
            **_metric_groups(
                assets=(1, 0, 1, 1, 0),
                payloads=(1, 0, 1, 0),
                faults=(0, 0, 0, 0, 1, 1),
                issues=(1, 1),
            ),
        },
    ],
    "asset_results": [
        {
            "asset_id": "AHU-1",
            "system": "BMS",
            "observed": True,
            "expected_payloads": 3,
            "received_payloads": 3,
            "all_expected_payloads_received": True,
            "all_received_payloads_successfully_validated": True,
            "successfully_validated": True,
            "issue_count": 0,
            "blocking_issue_count": 0,
            "last_observed_at": "2026-07-22T10:20:30+00:00",
            "payload_results": [],
        },
        {
            "asset_id": "FCU-2",
            "system": "BMS",
            "observed": True,
            "expected_payloads": 3,
            "received_payloads": 2,
            "all_expected_payloads_received": False,
            # The received subset passed, but one expected payload is missing.
            # The schedule must use successfully_validated and show No.
            "all_received_payloads_successfully_validated": True,
            "successfully_validated": False,
            "issue_count": 3,
            "blocking_issue_count": 2,
            "last_observed_at": None,
            "payload_results": [],
        },
        {
            "asset_id": "LIGHT-3",
            "system": "LTG",
            "observed": False,
            "expected_payloads": 1,
            "received_payloads": 0,
            "all_expected_payloads_received": False,
            "all_received_payloads_successfully_validated": False,
            "successfully_validated": False,
            "issue_count": 3,
            "blocking_issue_count": 1,
            "last_observed_at": None,
            "payload_results": [],
        },
    ],
    "fault_rows": [
        {
            "issue_id": f"issue-{index}",
            "asset_id": "FCU-2" if index < 4 else "LIGHT-3",
            "system": "BMS" if index < 4 else "LTG",
            "payload_type": "pointset",
            "category": category,
            "severity": "high" if index % 2 == 0 else "low",
            "description": f"Recorded {category} evidence.",
            "point_name": "zone_air_temperature_sensor",
            "expected_value": "expected",
            "observed_value": "observed",
            "suggested_action": "Review the retained evidence and correct the source.",
            "raw_evidence_uri": f"evidence://issue-{index}",
        }
        for index, category in enumerate(
            (
                "payload_formatting_issues",
                "missing_points",
                "point_naming_issues",
                "additional_points",
                "stale_or_cadence",
                "other_issues",
            ),
            start=1,
        )
    ],
}


def _expected_detail_columns() -> tuple[str, ...]:
    return (
        "Issue ID",
        "Asset ID",
        "System",
        "Payload",
        "Category",
        "Point",
        "Expected",
        "Observed",
        "Suggested Action",
        "Description",
    )


_SCOPABLE_SUMMARY = {
    "schema_version": "1.1",
    "asset_metrics": {
        "expected": 2,
        "observed": 2,
        "not_observed": 0,
        "with_issues": 2,
        "successfully_validated": 1,
        "unexpected": 1,
    },
    "payload_metrics": {
        "expected": 5,
        "received": 4,
        "not_received": 1,
        "with_issues": 2,
        "successfully_validated": 3,
    },
    "fault_metrics": {
        "payload_formatting_issues": 0,
        "missing_points": 1,
        "point_naming_issues": 1,
        "additional_points": 1,
        "stale_or_cadence": 0,
        "other_issues": 2,
    },
    "issue_metrics": {"blocking": 4, "warning": 1},
    "system_metrics": [
        {
            "system": "BMS",
            **_metric_groups(
                assets=(1, 1, 0, 1, 0),
                payloads=(3, 2, 1, 1),
                faults=(0, 1, 1, 0, 0, 1),
                issues=(3, 0),
            ),
        },
        {
            "system": "LTG",
            **_metric_groups(
                assets=(1, 1, 0, 1, 1),
                payloads=(2, 2, 1, 2),
                faults=(0, 0, 0, 1, 0, 0),
                issues=(0, 1),
            ),
        },
    ],
    "asset_results": [
        {
            "asset_id": "A-1",
            "system": "BMS",
            "observed": True,
            "expected_payloads": 3,
            "received_payloads": 2,
            "all_expected_payloads_received": False,
            "all_received_payloads_successfully_validated": False,
            "successfully_validated": False,
            "issue_count": 3,
            "blocking_issue_count": 3,
            "last_observed_at": "2026-07-23T10:02:00+00:00",
            "payload_results": [
                {
                    "payload_type": "state",
                    "expected": True,
                    "received": True,
                    "has_issues": False,
                    "blocking_issue_count": 0,
                    "successfully_validated": True,
                    "topic": "site/a-1/state",
                    "received_at": "2026-07-23T10:00:00+00:00",
                },
                {
                    "payload_type": "metadata",
                    "expected": True,
                    "received": False,
                    "has_issues": True,
                    "blocking_issue_count": 1,
                    "successfully_validated": False,
                    "topic": "site/a-1/metadata",
                    "received_at": None,
                },
                {
                    "payload_type": "pointset",
                    "expected": True,
                    "received": True,
                    "has_issues": True,
                    "blocking_issue_count": 1,
                    "successfully_validated": False,
                    "topic": "site/a-1/pointset",
                    "received_at": "2026-07-23T10:02:00+00:00",
                },
            ],
        },
        {
            "asset_id": "B-1",
            "system": "LTG",
            "observed": True,
            "expected_payloads": 2,
            "received_payloads": 2,
            "all_expected_payloads_received": True,
            "all_received_payloads_successfully_validated": True,
            "successfully_validated": True,
            "issue_count": 1,
            "blocking_issue_count": 0,
            "last_observed_at": "2026-07-23T10:03:00+00:00",
            "payload_results": [
                {
                    "payload_type": "state",
                    "expected": True,
                    "received": True,
                    "has_issues": True,
                    "blocking_issue_count": 0,
                    "successfully_validated": True,
                    "topic": "site/b-1/state",
                    "received_at": "2026-07-23T10:01:00+00:00",
                },
                {
                    "payload_type": "pointset",
                    "expected": True,
                    "received": True,
                    "has_issues": False,
                    "blocking_issue_count": 0,
                    "successfully_validated": True,
                    "topic": "site/b-1/pointset",
                    "received_at": "2026-07-23T10:03:00+00:00",
                },
            ],
        },
    ],
    "fault_rows": [
        {
            "issue_id": "a-metadata-missing",
            "asset_id": "A-1",
            "system": "BMS",
            "payload_type": "metadata",
            "category": "missing_points",
            "severity": "high",
            "description": "Metadata was not received.",
            "point_name": "manufacturer",
            "expected_value": "Acme",
            "observed_value": None,
            "suggested_action": "Publish metadata.",
            "raw_evidence_uri": "evidence://metadata",
        },
        {
            "issue_id": "a-point-name",
            "asset_id": "A-1",
            "system": "BMS",
            "payload_type": "pointset",
            "category": "point_naming_issues",
            "severity": "high",
            "description": "Point name differs from the register.",
            "point_name": "zone_temp",
            "expected_value": "zone_air_temperature_sensor",
            "observed_value": "zone_temp",
            "suggested_action": "Rename the point.",
            "raw_evidence_uri": "evidence://point",
        },
        {
            "issue_id": "a-asset-wide",
            "asset_id": "A-1",
            "system": "BMS",
            "payload_type": None,
            "category": "other_issues",
            "severity": "high",
            "description": "Asset-wide finding.",
            "point_name": None,
            "expected_value": None,
            "observed_value": None,
            "suggested_action": "Review the asset.",
            "raw_evidence_uri": "evidence://asset",
        },
        {
            "issue_id": "run-wide",
            "asset_id": None,
            "system": "Unspecified",
            "payload_type": None,
            "category": "other_issues",
            "severity": "high",
            "description": "Run-wide broker finding.",
            "point_name": None,
            "expected_value": None,
            "observed_value": None,
            "suggested_action": "Review broker evidence.",
            "raw_evidence_uri": "evidence://run",
        },
        {
            "issue_id": "b-state-note",
            "asset_id": "B-1",
            "system": "LTG",
            "payload_type": "state",
            "category": "additional_points",
            "severity": "low",
            "description": "State carries an extra field.",
            "point_name": "extra",
            "expected_value": None,
            "observed_value": "present",
            "suggested_action": "Review the field.",
            "raw_evidence_uri": "evidence://state",
        },
    ],
    "unexpected_devices": [
        {
            "id": "rogue-1",
            "topic_root": "site/rogue-1",
            "topics": ["site/rogue-1/state"],
            "last_seen": "2026-07-23T10:04:00+00:00",
        }
    ],
    "unexpected_devices_measured": True,
    "unexpected_devices_measurement_scope": "site/#",
}


class UdmiV1ReportTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path

        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-reports-udmi-v1-")
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

    def _seed_run(
        self,
        *,
        project_id: str = "demo-project",
        site_id: str = "demo-site",
        job_type: str = "udmi_validation",
        status: str = "succeeded",
        summary: dict | None = _V1_SUMMARY,
        parameters: dict | None = None,
        issues: list[dict] | None = None,
    ) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        service = RunService()
        run = service.create_job_run(
            JobCreateRequest(
                project_id=project_id,
                site_id=site_id,
                job_type=job_type,
                parameters=parameters or {},
            ),
            expected_job_type=job_type,
        )
        if status != "queued":
            service.update_run_status(
                run.run_id,
                status=status,
                stage="done" if status in {"succeeded", "failed", "cancelled"} else status,
                progress_percent=100 if status in {"succeeded", "failed", "cancelled"} else 50,
            )
        if summary is not None:
            service.update_result_summary(run.run_id, {"validation_summary_v1": summary})
        if issues is not None:
            service.replace_issues(run.run_id, issues)
        return run.run_id

    def _create_report(
        self,
        output_format: str,
        source_run_ids: list[str],
        *,
        title: str | None = "  Site & <A> Validation  ",
        report_type: str = "udmi_validation",
        udmi_scope: dict | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "project_id": "demo-project",
            "site_id": "demo-site",
            "report_type": report_type,
            "output_format": output_format,
            "source_run_ids": source_run_ids,
        }
        if title is not None:
            payload["report_title"] = title
        if udmi_scope is not None:
            payload["udmi_scope"] = udmi_scope
        response = self.client.post("/api/v1/reports", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _download(self, report_id: str):
        response = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_title_defaults_trimming_and_validation(self) -> None:
        custom = self._create_report("zip", [], title="  Site A Validation  ")
        self.assertEqual(custom["report_title"], "Site A Validation")
        stored = self.client.get(f"/api/v1/reports/{custom['report_id']}").json()
        self.assertEqual(stored["report_title"], "Site A Validation")

        udmi_default = self._create_report("zip", [], title=None)
        self.assertEqual(udmi_default["report_title"], "UDMI Validation Report")
        general_default = self._create_report(
            "zip",
            [],
            title=None,
            report_type="evidence_pack",
        )
        self.assertEqual(general_default["report_title"], "Smart Commissioning Report")

        from app.schemas.jobs import ReportRequest

        for title in (
            "   ",
            "bad\ncontrol",
            "bad\ud800surrogate",
            "bad\ufffecharacter",
            "bad\uffffcharacter",
            "x" * 161,
        ):
            with self.subTest(title=repr(title)), self.assertRaises(ValidationError):
                ReportRequest(
                    project_id="demo-project",
                    site_id="demo-site",
                    report_type="udmi_validation",
                    report_title=title,
                )

    def test_source_run_scope_is_validated_before_report_creation(self) -> None:
        missing = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "udmi_validation",
                "source_run_ids": ["missing-run"],
            },
        )
        self.assertEqual(missing.status_code, 422)
        self.assertIn("was not found", missing.json()["detail"])

        other_scope = self._seed_run(project_id="other-project")
        wrong_scope = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "udmi_validation",
                "source_run_ids": [other_scope],
            },
        )
        self.assertEqual(wrong_scope.status_code, 422)
        self.assertIn("does not belong", wrong_scope.json()["detail"])

    def test_udmi_sources_must_have_the_right_type_and_be_terminal(self) -> None:
        wrong_type = self._seed_run(job_type="bacnet_validation")
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "udmi_validation",
                "source_run_ids": [wrong_type],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("must be a UDMI validation run", response.json()["detail"])

        for status in ("queued", "running"):
            with self.subTest(status=status):
                source_id = self._seed_run(status=status)
                response = self.client.post(
                    "/api/v1/reports",
                    json={
                        "project_id": "demo-project",
                        "site_id": "demo-site",
                        "report_type": "udmi_validation",
                        "source_run_ids": [source_id],
                    },
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn("is not terminal", response.json()["detail"])
                self.assertIn(status, response.json()["detail"])

    def test_malformed_current_contract_fails_but_absent_legacy_contract_exports(self) -> None:
        malformed = {"schema_version": "1.1", "asset_metrics": {"expected": 1}}
        unknown = copy.deepcopy(_SCOPABLE_SUMMARY)
        unknown["schema_version"] = "2.0"

        for summary in (malformed, unknown):
            with self.subTest(schema_version=summary["schema_version"]):
                source_id = self._seed_run(summary=summary)
                response = self.client.post(
                    "/api/v1/reports",
                    json={
                        "project_id": "demo-project",
                        "site_id": "demo-site",
                        "report_type": "udmi_validation",
                        "output_format": "zip",
                        "source_run_ids": [source_id],
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("malformed or unsupported", response.json()["detail"])

        malformed_nested: list[tuple[str, dict]] = []
        bad_system = copy.deepcopy(_SCOPABLE_SUMMARY)
        bad_system["system_metrics"] = [None]
        malformed_nested.append(("system", bad_system))
        bad_asset = copy.deepcopy(_SCOPABLE_SUMMARY)
        bad_asset["asset_results"] = [None]
        malformed_nested.append(("asset", bad_asset))
        bad_fault = copy.deepcopy(_SCOPABLE_SUMMARY)
        bad_fault["fault_rows"] = [None]
        malformed_nested.append(("fault", bad_fault))
        bad_payload = copy.deepcopy(_SCOPABLE_SUMMARY)
        bad_payload["asset_results"][0]["payload_results"] = [
            {"payload_type": "state"}
        ]
        malformed_nested.append(("payload", bad_payload))

        for label, summary in malformed_nested:
            with self.subTest(nested=label):
                source_id = self._seed_run(summary=summary)
                response = self.client.post(
                    "/api/v1/reports",
                    json={
                        "project_id": "demo-project",
                        "site_id": "demo-site",
                        "report_type": "udmi_validation",
                        "output_format": "zip",
                        "source_run_ids": [source_id],
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("malformed", response.json()["detail"])

        legacy_source = self._seed_run(summary=None)
        legacy_report = self._create_report("zip", [legacy_source])
        with zipfile.ZipFile(io.BytesIO(self._download(legacy_report["report_id"]).content)) as archive:
            self.assertIn("validation_summary.json", archive.namelist())
            self.assertNotIn("asset_validation_schedule.json", archive.namelist())
            legacy_summary = json.loads(archive.read("validation_summary.json"))
        self.assertIn("columns", legacy_summary)
        self.assertNotIn("schema_version", legacy_summary)

    def test_filtered_scope_is_exact_recomputed_and_persisted_canonically(self) -> None:
        source_id = self._seed_run(
            summary=copy.deepcopy(_SCOPABLE_SUMMARY),
            parameters={
                "assets": [
                    {"expected_schedule": {"asset_id": "A-1", "project_site": "Site A"}},
                    {"expected_schedule": {"asset_id": "B-1", "project_site": "Site B"}},
                ]
            },
        )
        scope = {
            "schema_version": "1.0",
            "selected_payloads": [
                {
                    "source_run_id": source_id,
                    "asset_id": "A-1",
                    "payload_type": "pointset",
                },
                {
                    "source_run_id": source_id,
                    "asset_id": "A-1",
                    "payload_type": "metadata",
                },
            ],
            "unexpected_device_ids": [],
            "filters": {
                "text": "provenance label only",
                "verdict": "fail",
                "topic_contains": "a-1",
                "system": "BMS",
                "observation": "all",
                "category": "validation",
            },
        }
        report = self._create_report("zip", [source_id], udmi_scope=scope)

        from app.services.run_service import RunService

        stored = RunService().get_run(report["report_id"])
        selected = stored.parameters["udmi_scope"]["selected_payloads"]
        self.assertEqual(
            [row["payload_type"] for row in selected],
            ["metadata", "pointset"],
        )

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            header = json.loads(archive.read("summary.json"))
            summary = json.loads(archive.read("validation_summary.json"))
            schedule = json.loads(archive.read("asset_validation_schedule.json"))
            matrix = json.loads(archive.read("fault_matrix.json"))
            details = json.loads(archive.read("fault_details.json"))
            findings = json.loads(archive.read("findings.json"))

        self.assertEqual(header["Project"], "Site A")
        self.assertEqual(header["Site"], "Site A")
        self.assertEqual(set(header), {"Project", "Site", "Report ID", "Generated"})
        self.assertEqual(
            summary["asset_metrics"],
            {
                "expected": 1,
                "observed": 1,
                "not_observed": 0,
                "with_issues": 1,
                "successfully_validated": 0,
                "unexpected": 0,
            },
        )
        self.assertEqual(
            summary["payload_metrics"],
            {
                "expected": 2,
                "received": 1,
                "not_received": 1,
                "with_issues": 1,
                "successfully_validated": 0,
            },
        )
        self.assertEqual(summary["issue_metrics"], {"blocking": 2, "warning": 0})
        self.assertEqual(summary["filter_provenance"]["text"], "provenance label only")
        self.assertEqual([row["asset_id"] for row in schedule["rows"]], ["A-1"])
        self.assertEqual(
            [row["payload_type"] for row in schedule["rows"][0]["payload_results"]],
            ["metadata", "pointset"],
        )
        self.assertEqual(len(matrix["rows"]), 1)
        self.assertTrue(matrix["rows"][0]["missing_points"])
        self.assertTrue(matrix["rows"][0]["point_naming_issues"])
        self.assertFalse(matrix["rows"][0]["other_issues"])
        self.assertEqual(
            {row["issue_id"] for row in details["rows"]},
            {"a-metadata-missing", "a-point-name"},
        )
        self.assertEqual(
            {row["issue_id"] for row in findings},
            {row["issue_id"] for row in details["rows"]},
        )
        self.assertTrue(
            all(
                "source_run_id" not in row
                and "severity" not in row
                and "raw_evidence_uri" not in row
                for row in details["rows"]
            )
        )
        self.assertTrue(
            all(
                row["source_run_id"] == source_id
                and row["severity"]
                and row["raw_evidence_uri"]
                for row in findings
            )
        )

    def test_empty_filtered_scope_is_valid_and_yields_empty_report(self) -> None:
        source_id = self._seed_run(summary=copy.deepcopy(_SCOPABLE_SUMMARY))
        report = self._create_report(
            "zip",
            [source_id],
            udmi_scope={
                "schema_version": "1.0",
                "selected_payloads": [],
                "unexpected_device_ids": [],
                "filters": {},
            },
        )
        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            header = json.loads(archive.read("summary.json"))
            summary = json.loads(archive.read("validation_summary.json"))
            schedule = json.loads(archive.read("asset_validation_schedule.json"))
            matrix = json.loads(archive.read("fault_matrix.json"))
            findings = json.loads(archive.read("findings.json"))
        self.assertEqual(header["Project"], "Not recorded")
        self.assertEqual(header["Site"], "Not recorded")
        self.assertEqual(summary["asset_metrics"]["expected"], 0)
        self.assertEqual(summary["payload_metrics"]["expected"], 0)
        self.assertEqual(summary["payload_metrics"]["not_received"], 0)
        self.assertEqual(schedule["rows"], [])
        self.assertEqual(matrix["rows"], [])
        self.assertEqual(findings, [])

    def test_full_expected_asset_scope_ignores_nonexpected_payload_evidence(self) -> None:
        summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        asset = next(row for row in summary["asset_results"] if row["asset_id"] == "A-1")
        state = next(
            row for row in asset["payload_results"] if row["payload_type"] == "state"
        )
        asset["payload_results"] = [
            state,
            {
                "payload_type": "metadata",
                "expected": False,
                "received": True,
                "has_issues": False,
                "blocking_issue_count": 0,
                "successfully_validated": True,
                "topic": "site/a-1/metadata",
                "received_at": "2026-07-23T10:01:00+00:00",
            },
        ]
        asset["expected_payloads"] = 1
        asset["received_payloads"] = 1
        asset["all_expected_payloads_received"] = True
        asset["all_received_payloads_successfully_validated"] = True
        asset["successfully_validated"] = False
        asset["issue_count"] = 1
        asset["blocking_issue_count"] = 1
        summary["asset_results"] = [asset]
        summary["fault_rows"] = [
            next(row for row in summary["fault_rows"] if row["issue_id"] == "a-asset-wide")
        ]
        summary["unexpected_devices"] = []
        summary["asset_metrics"]["unexpected"] = 0

        source_id = self._seed_run(summary=summary)
        report = self._create_report(
            "zip",
            [source_id],
            udmi_scope={
                "schema_version": "1.0",
                "selected_payloads": [
                    {
                        "source_run_id": source_id,
                        "asset_id": "A-1",
                        "payload_type": "state",
                    }
                ],
                "unexpected_device_ids": [],
                "filters": {},
            },
        )

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            rendered = json.loads(archive.read("validation_summary.json"))
            details = json.loads(archive.read("fault_details.json"))

        self.assertEqual(rendered["asset_metrics"]["expected"], 1)
        self.assertEqual(rendered["asset_metrics"]["successfully_validated"], 0)
        self.assertEqual(rendered["payload_metrics"]["expected"], 1)
        self.assertEqual(rendered["issue_metrics"]["blocking"], 1)
        self.assertEqual(
            [row["issue_id"] for row in details["rows"]],
            ["a-asset-wide"],
        )

    def test_filtered_scope_rejects_unknown_legacy_and_ambiguous_references(self) -> None:
        source_id = self._seed_run(summary=copy.deepcopy(_SCOPABLE_SUMMARY))

        def request(scope: dict, sources: list[str] | None = None, report_type: str = "udmi_validation"):
            return self.client.post(
                "/api/v1/reports",
                json={
                    "project_id": "demo-project",
                    "site_id": "demo-site",
                    "report_type": report_type,
                    "source_run_ids": sources if sources is not None else [source_id],
                    "udmi_scope": scope,
                },
            )

        unknown = request(
            {
                "selected_payloads": [
                    {
                        "source_run_id": "another-run",
                        "asset_id": "A-1",
                        "payload_type": "state",
                    }
                ]
            }
        )
        self.assertEqual(unknown.status_code, 422)
        self.assertIn("not present", unknown.json()["detail"])

        non_expected_summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        b_asset = next(
            asset
            for asset in non_expected_summary["asset_results"]
            if asset["asset_id"] == "B-1"
        )
        b_asset["payload_results"].append(
            {
                "payload_type": "metadata",
                "expected": False,
                "received": True,
                "has_issues": False,
                "blocking_issue_count": 0,
                "successfully_validated": True,
                "topic": "site/b-1/metadata",
                "received_at": "2026-07-23T10:03:30+00:00",
            }
        )
        non_expected_id = self._seed_run(summary=non_expected_summary)
        non_expected = request(
            {
                "selected_payloads": [
                    {
                        "source_run_id": non_expected_id,
                        "asset_id": "B-1",
                        "payload_type": "metadata",
                    }
                ]
            },
            sources=[non_expected_id],
        )
        self.assertEqual(non_expected.status_code, 422)
        self.assertIn("expected payloads only", non_expected.json()["detail"])

        legacy = copy.deepcopy(_V1_SUMMARY)
        for asset in legacy["asset_results"]:
            asset.pop("payload_results", None)
        legacy_id = self._seed_run(summary=legacy)
        legacy_response = request(
            {"selected_payloads": []},
            sources=[legacy_id],
        )
        self.assertEqual(legacy_response.status_code, 422)
        self.assertIn("predates exact payload filtering", legacy_response.json()["detail"])

        no_source = request({"selected_payloads": []}, sources=[])
        self.assertEqual(no_source.status_code, 422)
        wrong_type = request(
            {"selected_payloads": []},
            sources=[source_id],
            report_type="evidence_pack",
        )
        self.assertEqual(wrong_type.status_code, 422)

        second_id = self._seed_run(summary=copy.deepcopy(_SCOPABLE_SUMMARY))
        ambiguous = request(
            {
                "selected_payloads": [],
                "unexpected_device_ids": ["rogue-1"],
            },
            sources=[source_id, second_id],
        )
        self.assertEqual(ambiguous.status_code, 422)
        self.assertIn("uniquely", ambiguous.json()["detail"])

    def test_unexpected_device_selection_only_affects_unexpected_metric(self) -> None:
        source_id = self._seed_run(summary=copy.deepcopy(_SCOPABLE_SUMMARY))
        report = self._create_report(
            "zip",
            [source_id],
            udmi_scope={
                "selected_payloads": [],
                "unexpected_device_ids": ["rogue-1"],
            },
        )
        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            summary = json.loads(archive.read("validation_summary.json"))
            schedule = json.loads(archive.read("asset_validation_schedule.json"))
            matrix = json.loads(archive.read("fault_matrix.json"))
            details = json.loads(archive.read("fault_details.json"))
        self.assertEqual(summary["asset_metrics"]["unexpected"], 1)
        self.assertEqual(summary["asset_metrics"]["expected"], 0)
        self.assertEqual(summary["payload_metrics"]["expected"], 0)
        self.assertEqual([row["id"] for row in summary["unexpected_devices"]], ["rogue-1"])
        self.assertEqual(schedule["rows"], [])
        self.assertEqual(matrix["rows"], [])
        self.assertEqual(details["rows"], [])

    def test_schema_1_payload_issue_counts_include_received_payloads_only(self) -> None:
        summary = {
            "schema_version": "1.0",
            **_metric_groups(
                assets=(1, 0, 1, 1, 0),
                payloads=(1, 0, 1, 0),
                faults=(0, 0, 0, 0, 0, 1),
                issues=(1, 0),
            ),
            "system_metrics": [
                {
                    "system": "BMS",
                    **_metric_groups(
                        assets=(1, 0, 1, 1, 0),
                        payloads=(1, 0, 1, 0),
                        faults=(0, 0, 0, 0, 0, 1),
                        issues=(1, 0),
                    ),
                }
            ],
            "asset_results": [
                {
                    "asset_id": "A-1",
                    "system": "BMS",
                    "observed": False,
                    "expected_payloads": 1,
                    "received_payloads": 0,
                    "all_expected_payloads_received": False,
                    "all_received_payloads_successfully_validated": False,
                    "successfully_validated": False,
                    "issue_count": 1,
                    "blocking_issue_count": 1,
                    "last_observed_at": None,
                    "payload_results": [
                        {
                            "payload_type": "state",
                            "expected": True,
                            "received": False,
                            "has_issues": True,
                            "blocking_issue_count": 1,
                            "successfully_validated": False,
                            "topic": "site/a-1/state",
                            "received_at": None,
                        }
                    ],
                }
            ],
            "fault_rows": [
                {
                    "issue_id": "state-not-received",
                    "asset_id": "A-1",
                    "system": "BMS",
                    "payload_type": "state",
                    "category": "other_issues",
                    "severity": "high",
                    "description": "State was not received.",
                }
            ],
        }
        source_id = self._seed_run(summary=summary)
        report = self._create_report("zip", [source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            rendered = json.loads(archive.read("validation_summary.json"))

        self.assertEqual(
            rendered["payload_metrics"],
            {
                "expected": 1,
                "received": 0,
                "not_received": 1,
                "with_issues": 0,
                "successfully_validated": 0,
            },
        )
        self.assertEqual(rendered["system_metrics"][0]["payload_metrics"]["with_issues"], 0)
        self.assertEqual(rendered["system_metrics"][0]["payload_metrics"]["not_received"], 1)

    def test_retired_unexpected_fault_is_removed_from_old_persisted_summary(self) -> None:
        summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        summary["fault_metrics"]["other_issues"] += 1
        summary["issue_metrics"]["blocking"] += 1
        summary["fault_rows"].append(
            {
                "issue_id": "legacy-unexpected",
                "asset_id": "rogue-legacy",
                "system": "Unspecified",
                "payload_type": None,
                "category": "other_issues",
                "severity": "high",
                "description": "Legacy unexpected publisher finding.",
            }
        )
        source_id = self._seed_run(
            summary=summary,
            issues=[
                {
                    "issue_id": "legacy-unexpected",
                    "asset_id": "rogue-legacy",
                    "issue_type": "unexpected_device",
                    "severity": "high",
                    "description": "Legacy unexpected publisher finding.",
                }
            ],
        )
        report = self._create_report("zip", [source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            rendered = json.loads(archive.read("validation_summary.json"))
            details = json.loads(archive.read("fault_details.json"))

        self.assertEqual(rendered["asset_metrics"]["unexpected"], 1)
        self.assertEqual(rendered["fault_metrics"]["other_issues"], 2)
        self.assertEqual(rendered["issue_metrics"]["blocking"], 4)
        self.assertNotIn(
            "legacy-unexpected",
            {row["issue_id"] for row in details["rows"]},
        )

    def test_report_snapshot_survives_source_mutation_and_integrity_is_stable(self) -> None:
        source_id = self._seed_run(
            summary=copy.deepcopy(_SCOPABLE_SUMMARY),
            parameters={
                "assets": [
                    {"expected_schedule": {"asset_id": "A-1", "project_site": "Site A"}},
                    {"expected_schedule": {"asset_id": "B-1", "project_site": "Site A"}},
                ]
            },
        )
        report = self._create_report("zip", [source_id])

        from app.services.reports_integrity import INTEGRITY_KEY
        from app.services.run_service import RunService

        service = RunService()
        stored_before_download = service.get_run(report["report_id"])
        self.assertIsInstance(
            stored_before_download.parameters.get("udmi_report_snapshot"),
            dict,
        )
        self.assertIn("report_generated_at", stored_before_download.result_summary)
        self.assertNotIn(INTEGRITY_KEY, stored_before_download.result_summary)

        service.update_result_summary(
            source_id,
            {"validation_summary_v1": copy.deepcopy(_V1_SUMMARY)},
        )
        first = self._download(report["report_id"]).content
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            header = json.loads(archive.read("summary.json"))
            rendered = json.loads(archive.read("validation_summary.json"))
        self.assertEqual(header["Project"], "Site A")
        self.assertEqual(rendered["asset_metrics"]["expected"], 2)

        integrity_after_first = dict(
            service.get_run(report["report_id"]).result_summary[INTEGRITY_KEY]
        )
        service.update_result_summary(
            source_id,
            {"validation_summary_v1": copy.deepcopy(_SCOPABLE_SUMMARY)},
        )
        second = self._download(report["report_id"]).content
        integrity_after_second = dict(
            service.get_run(report["report_id"]).result_summary[INTEGRITY_KEY]
        )

        self.assertEqual(first, second)
        self.assertEqual(integrity_after_first, integrity_after_second)

    def test_legacy_first_download_initialization_is_atomic(self) -> None:
        from app.api.routes import reports as reports_module
        from app.services.reports_integrity import INTEGRITY_KEY
        from app.services.run_service import RunService
        from smart_commissioning_core.integrity import sha256_bytes

        report = self._create_report("zip", [], report_type="evidence_pack")
        service = RunService()
        # Emulate a report persisted before report_generated_at and integrity
        # were initialized during report creation.
        service.update_result_summary(report["report_id"], {}, merge=False)
        run_a = service.get_run(report["report_id"])
        run_b = service.get_run(report["report_id"])

        timestamp_barrier = Barrier(2)
        timestamp_base = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)

        class ConcurrentClock:
            @classmethod
            def now(cls, _timezone: object) -> datetime:
                timestamp_barrier.wait(timeout=10)
                offset = 0 if current_thread().name.endswith("_0") else 1
                return timestamp_base + timedelta(seconds=offset)

        with (
            mock.patch.object(reports_module, "datetime", ConcurrentClock),
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="report-timestamp") as executor,
        ):
            timestamps = [
                future.result(timeout=10)
                for future in (
                    executor.submit(reports_module._generated_at, run_a),
                    executor.submit(reports_module._generated_at, run_b),
                )
            ]

        self.assertEqual(timestamps[0], timestamps[1])
        artifact_a, _ = reports_module._build_report_artifact(run_a, "zip")
        artifact_b, _ = reports_module._build_report_artifact(run_b, "zip")
        self.assertEqual(artifact_a, artifact_b)

        integrity_barrier = Barrier(2)

        def concurrent_metadata(artifact: bytes) -> dict[str, object]:
            integrity_barrier.wait(timeout=10)
            return {
                "algorithm": "sha256",
                "hash": sha256_bytes(artifact),
                "signature_algorithm": "ed25519",
                "signature": None,
                "public_key_pem": None,
                "public_key_fingerprint": None,
                "signed_at": current_thread().name,
            }

        with (
            mock.patch.object(
                reports_module,
                "build_integrity_metadata",
                side_effect=concurrent_metadata,
            ),
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="report-integrity") as executor,
        ):
            metadata = [
                future.result(timeout=10)
                for future in (
                    executor.submit(reports_module._persist_integrity, run_a, artifact_a),
                    executor.submit(reports_module._persist_integrity, run_b, artifact_b),
                )
            ]

        self.assertEqual(metadata[0], metadata[1])
        fresh = service.get_run(report["report_id"])
        rebuilt, _ = reports_module._build_report_artifact(fresh, "zip")
        self.assertEqual(fresh.result_summary[INTEGRITY_KEY], metadata[0])
        self.assertEqual(fresh.result_summary[INTEGRITY_KEY]["hash"], sha256_bytes(rebuilt))
        self.assertEqual(self._download(report["report_id"]).content, rebuilt)

    def test_same_asset_id_in_two_sources_does_not_cross_contaminate_scope(self) -> None:
        first_summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        second_summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        template = {
            "asset_id": "A-1",
            "system": "BMS",
            "payload_type": "state",
            "category": "other_issues",
            "severity": "low",
            "point_name": None,
            "expected_value": None,
            "observed_value": None,
            "suggested_action": "Review state evidence.",
            "raw_evidence_uri": "evidence://state-scope",
        }
        first_summary["fault_rows"].append(
            {**template, "issue_id": "first-state", "description": "First source."}
        )
        second_summary["fault_rows"].append(
            {**template, "issue_id": "second-state", "description": "Second source."}
        )
        first_id = self._seed_run(summary=first_summary)
        second_id = self._seed_run(summary=second_summary)
        report = self._create_report(
            "zip",
            [first_id, second_id],
            udmi_scope={
                "selected_payloads": [
                    {
                        "source_run_id": second_id,
                        "asset_id": "A-1",
                        "payload_type": "state",
                    }
                ]
            },
        )
        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            findings = json.loads(archive.read("findings.json"))
            summary = json.loads(archive.read("validation_summary.json"))
        self.assertEqual([row["issue_id"] for row in findings], ["second-state"])
        self.assertEqual(findings[0]["source_run_id"], second_id)
        self.assertEqual(summary["asset_metrics"]["expected"], 1)

    def test_run_wide_fault_never_creates_a_fault_matrix_asset(self) -> None:
        source_id = self._seed_run(summary=copy.deepcopy(_SCOPABLE_SUMMARY))
        report = self._create_report("zip", [source_id])
        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            matrix = json.loads(archive.read("fault_matrix.json"))
            details = json.loads(archive.read("fault_details.json"))
        self.assertEqual({row["asset_id"] for row in matrix["rows"]}, {"A-1", "B-1"})
        self.assertIn(None, {row["asset_id"] for row in details["rows"]})

    def test_failed_and_cancelled_sources_are_prominent_in_every_renderer(self) -> None:
        failed = self._seed_run(status="failed")
        cancelled = self._seed_run(status="cancelled", summary=None)

        for output_format in ("zip", "pdf", "docx", "xlsx"):
            with self.subTest(output_format=output_format):
                report = self._create_report(output_format, [failed, cancelled])
                content = self._download(report["report_id"]).content
                if output_format == "zip":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        report_summary = json.loads(archive.read("summary.json"))
                        validation = json.loads(archive.read("validation_summary.json"))
                    self.assertEqual(
                        set(report_summary),
                        {"Project", "Site", "Report ID", "Generated"},
                    )
                    self.assertEqual(validation["report_job_status"], "succeeded")
                    self.assertFalse(validation["scope_complete"])
                    self.assertEqual(validation["scope_status"], "incomplete")
                    self.assertEqual(
                        {row["status"] for row in validation["incomplete_source_runs"]},
                        {"failed", "cancelled"},
                    )
                elif output_format == "pdf":
                    self.assertIn(b"Validation Scope Incomplete", content)
                    self.assertIn(b"INCOMPLETE", content)
                    self.assertNotIn(b"Status: succeeded", content)
                elif output_format == "docx":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        document = archive.read("word/document.xml")
                    self.assertIn(b"Validation Scope Incomplete", document)
                    self.assertIn(b"INCOMPLETE", document)
                    self.assertNotIn(b"Status: succeeded", document)
                else:
                    workbook = load_workbook(io.BytesIO(content))
                    executive = workbook["Executive Summary"]
                    metadata = {
                        executive.cell(row, 1).value: executive.cell(row, 2).value
                        for row in range(2, executive.max_row + 1)
                    }
                    self.assertIn("INCOMPLETE", metadata["Validation Scope Incomplete"])
                    self.assertNotIn("Status", metadata)
                    self.assertNotIn("Validation scope", metadata)

    def test_zip_contains_versioned_summary_schedule_and_fault_sections(self) -> None:
        source_id = self._seed_run()
        report = self._create_report("zip", [source_id])
        content = self._download(report["report_id"]).content
        self.assertNotIn(b"freshness", content.lower())

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            self.assertTrue(
                {
                    "summary.json",
                    "validation_summary.json",
                    "asset_validation_schedule.json",
                    "fault_matrix.json",
                    "fault_details.json",
                    "metric_definitions.json",
                }.issubset(names)
            )
            summary = json.loads(archive.read("validation_summary.json"))
            schedule = json.loads(archive.read("asset_validation_schedule.json"))
            matrix = json.loads(archive.read("fault_matrix.json"))
            details = json.loads(archive.read("fault_details.json"))
            definitions = json.loads(archive.read("metric_definitions.json"))

        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(summary["report_title"], "Site & <A> Validation")
        self.assertEqual(
            summary["asset_metrics"],
            {**_TOTALS["asset_metrics"], "unexpected": 0},
        )
        self.assertEqual(
            summary["payload_metrics"],
            {**_TOTALS["payload_metrics"], "not_received": 2},
        )
        self.assertEqual(summary["overall_compliance"], "1/3 (33%)")
        self.assertEqual(summary["payloads_correct"], "4/7 (57%)")
        self.assertEqual(summary["payloads_incorrect"], "3/7 (43%)")
        self.assertEqual(summary["system_metrics"][0]["system"], "BMS")
        self.assertIn("T", summary["last_validation_run_at"])
        self.assertIn("T", summary["report_generated_at"])
        fcu = next(row for row in schedule["rows"] if row["asset_id"] == "FCU-2")
        self.assertFalse(fcu["all_expected_payloads_received"])
        self.assertFalse(fcu["successfully_validated"])
        matrix_categories = {
            category
            for row in matrix["rows"]
            for category, value in row.items()
            if category in _TOTALS["fault_metrics"] and value is True
        }
        self.assertEqual(matrix_categories, set(_TOTALS["fault_metrics"]))
        self.assertEqual({row["category"] for row in details["rows"]}, set(_TOTALS["fault_metrics"]))
        definition_by_metric = {row["metric"]: row["definition"] for row in definitions["rows"]}
        self.assertIn("divided by expected payloads", definition_by_metric["Payloads Correct %"])
        self.assertIn(
            "divided by expected payloads",
            definition_by_metric["Payloads Incorrect %"],
        )

    def test_pdf_is_landscape_complete_and_deterministic(self) -> None:
        source_id = self._seed_run()
        report = self._create_report("pdf", [source_id])
        first = self._download(report["report_id"]).content
        second = self._download(report["report_id"]).content
        self.assertEqual(first, second)
        self.assertIn(b"/MediaBox [0 0 842 595]", first)
        for text in (
            b"Site & <A> Validation",
            b"Executive Summary",
            b"Metrics by System",
            b"Asset Validation Schedule",
            b"Fault Matrix",
            b"Faults in Detail",
            b"Metric Definitions",
            b"Overall Compliance",
            b"1/3 \\(33%\\)",
            b"Payloads Correct %",
            b"4/7 \\(57%\\)",
            b"Payloads Incorrect %",
            b"3/7 \\(43%\\)",
            b"1/2 \\(50%\\)",
        ):
            self.assertIn(text, first)
        self.assertNotIn(b"freshness", first.lower())
        self.assertNotIn(b"Online", first)
        self.assertNotIn(b"Offline", first)
        self.assertLess(first.find(b"Metric Definitions"), first.find(b"Executive Summary"))
        self.assertNotIn(b"Output format:", first)
        self.assertNotIn(b"Source runs:", first)

        from app.services.report_pdf import PdfDocument

        long_title = "T" * 160
        document = PdfDocument(header_left="ELECTRACOM", header_right=long_title, landscape=True)
        document.add_paragraph("Body remains below the branding band.")
        long_header_pdf = document.render()
        self.assertNotIn(long_title.encode("ascii"), long_header_pdf)
        self.assertIn(b"\x85", long_header_pdf)

    def test_docx_is_landscape_escaped_complete_and_deterministic(self) -> None:
        source_id = self._seed_run()
        report = self._create_report("docx", [source_id])
        first = self._download(report["report_id"]).content
        second = self._download(report["report_id"]).content
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            document = archive.read("word/document.xml")
            header = archive.read("word/header1.xml")
        self.assertIn(b'w:orient="landscape"', document)
        self.assertIn(b'w:w="16838" w:h="11906"', document)
        self.assertIn(b"Site &amp; &lt;A&gt; Validation", document)
        self.assertIn(b"Site &amp; &lt;A&gt; Validation", header)
        self.assertIn(b"Asset Validation Schedule", document)
        self.assertIn(b"Faults in Detail", document)
        self.assertIn(b"Overall Compliance", document)
        self.assertIn(b"1/3 (33%)", document)
        self.assertIn(b"Payloads Correct %", document)
        self.assertIn(b"4/7 (57%)", document)
        self.assertIn(b"Payloads Incorrect %", document)
        self.assertIn(b"3/7 (43%)", document)
        self.assertNotIn(b"freshness", document.lower())
        self.assertLess(document.find(b"Metric Definitions"), document.find(b"Executive Summary"))
        self.assertNotIn(b">Source Run<", document)
        self.assertNotIn(b">Severity<", document)
        self.assertNotIn(b">Evidence URI<", document)
        self.assertIn(b'<w:jc w:val="center"/>', document)
        self.assertIn(b'<w:vAlign w:val="center"/>', document)
        self.assertIn(b"<w:insideH", document)
        self.assertIn(b"<w:insideV", document)

    def test_register_project_site_drives_all_udmi_report_headers(self) -> None:
        source_id = self._seed_run(
            summary=copy.deepcopy(_SCOPABLE_SUMMARY),
            parameters={
                "assets": [
                    {"expected_schedule": {"asset_id": "A-1", "project_site": "Site A"}}
                ]
            },
        )
        for output_format in ("zip", "pdf", "docx", "xlsx"):
            with self.subTest(output_format=output_format):
                report = self._create_report(output_format, [source_id])
                content = self._download(report["report_id"]).content
                if output_format == "zip":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        metadata = json.loads(archive.read("summary.json"))
                    self.assertEqual(metadata["Project"], "Site A")
                    self.assertEqual(metadata["Site"], "Site A")
                    self.assertEqual(set(metadata), {"Project", "Site", "Report ID", "Generated"})
                elif output_format == "pdf":
                    self.assertIn(b"Project: Site A", content)
                    self.assertIn(b"Site: Site A", content)
                    self.assertNotIn(b"Project: demo-project", content)
                elif output_format == "docx":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        document = archive.read("word/document.xml")
                    self.assertIn(b"Project: Site A", document)
                    self.assertIn(b"Site: Site A", document)
                    self.assertNotIn(b"Project: demo-project", document)
                else:
                    workbook = load_workbook(io.BytesIO(content))
                    executive = workbook["Executive Summary"]
                    metadata = {
                        executive.cell(row, 1).value: executive.cell(row, 2).value
                        for row in range(2, 7)
                    }
                    self.assertEqual(metadata["Project"], "Site A")
                    self.assertEqual(metadata["Site"], "Site A")
                    self.assertNotIn("Output format", metadata)

    def test_long_fault_detail_is_complete_wrapped_centered_and_gridded(self) -> None:
        summary = copy.deepcopy(_SCOPABLE_SUMMARY)
        long_description = "LONG_START " + ("measured commissioning detail " * 80) + "LONG_END"
        summary["fault_rows"][1]["description"] = long_description
        source_id = self._seed_run(summary=summary)

        pdf_report = self._create_report("pdf", [source_id], title="Detail Test")
        pdf_content = self._download(pdf_report["report_id"]).content
        self.assertIn(b"LONG_START", pdf_content)
        self.assertIn(b"LONG_END", pdf_content)
        self.assertNotIn(b"\x85", pdf_content)
        self.assertNotIn(b"(Source Run)", pdf_content)
        self.assertNotIn(b"(Evidence URI)", pdf_content)

        docx_report = self._create_report("docx", [source_id], title="Detail Test")
        with zipfile.ZipFile(io.BytesIO(self._download(docx_report["report_id"]).content)) as archive:
            document = archive.read("word/document.xml")
        self.assertIn(long_description.encode("ascii"), document)

        xlsx_report = self._create_report("xlsx", [source_id], title="Detail Test")
        workbook = load_workbook(io.BytesIO(self._download(xlsx_report["report_id"]).content))
        details = workbook["Faults in Detail"]
        headers = [cell.value for cell in details[1]]
        self.assertEqual(headers, list(_expected_detail_columns()))
        description_column = headers.index("Description") + 1
        description_cell = next(
            details.cell(row, description_column)
            for row in range(2, details.max_row + 1)
            if details.cell(row, description_column).value == long_description
        )
        self.assertTrue(description_cell.alignment.wrap_text)
        self.assertEqual(description_cell.alignment.horizontal, "center")
        self.assertEqual(description_cell.alignment.vertical, "center")
        self.assertEqual(description_cell.border.left.style, "thin")

    def test_xlsx_print_layout_filters_styles_and_asset_verdict(self) -> None:
        source_id = self._seed_run()
        report = self._create_report("xlsx", [source_id])
        first = self._download(report["report_id"]).content
        second = self._download(report["report_id"]).content
        self.assertEqual(first, second)
        workbook = load_workbook(io.BytesIO(first))
        self.assertEqual(
            workbook.sheetnames,
            [
                "Metric Definitions",
                "Executive Summary",
                "Metrics by System",
                "Asset Validation Schedule",
                "Fault Matrix",
                "Faults in Detail",
            ],
        )
        for sheet in workbook.worksheets:
            self.assertEqual(sheet.page_setup.orientation, "landscape")
            self.assertEqual(sheet.page_setup.fitToWidth, 1)
            self.assertTrue(sheet.freeze_panes)

        assets = workbook["Asset Validation Schedule"]
        self.assertEqual(assets.freeze_panes, "A2")
        self.assertTrue(assets.auto_filter.ref)
        headers = {cell.value: cell.column for cell in assets[1]}
        fcu_row = next(
            row
            for row in range(2, assets.max_row + 1)
            if assets.cell(row, headers["Asset ID"]).value == "FCU-2"
        )
        self.assertEqual(assets.cell(fcu_row, headers["All Payloads Received"]).value, "No")
        self.assertEqual(assets.cell(fcu_row, headers["All Payloads Validated"]).value, "No")
        self.assertEqual(assets.cell(fcu_row, headers["Evidence Timestamp"]).value, "\N{EM DASH}")

        systems = workbook["Metrics by System"]
        system_headers = {cell.value: cell.column for cell in systems[1]}
        bms_row = next(
            row
            for row in range(2, systems.max_row + 1)
            if systems.cell(row, system_headers["System"]).value == "BMS"
        )
        self.assertEqual(systems.cell(bms_row, system_headers["Completion"]).value, "1/2 (50%)")
        executive = workbook["Executive Summary"]
        compliance_row = next(
            row
            for row in range(2, executive.max_row + 1)
            if executive.cell(row, 1).value == "Overall Compliance"
        )
        self.assertEqual(executive.cell(compliance_row, 2).value, "1/3 (33%)")
        supporting = {
            executive.cell(row, 1).value: executive.cell(row, 2).value
            for row in range(2, executive.max_row + 1)
        }
        self.assertEqual(supporting["Payloads Correct %"], "4/7 (57%)")
        self.assertEqual(supporting["Payloads Incorrect %"], "3/7 (43%)")
        all_values = " ".join(
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
            if cell.value is not None
        )
        self.assertNotIn("freshness", all_values.casefold())
        self.assertNotIn("offline", all_values.casefold())

        from app.api.routes.reports import _completion, _payload_correctness

        self.assertEqual(_completion({"expected": 0, "successfully_validated": 0}), "N/A")
        self.assertEqual(
            _payload_correctness({"expected": 0, "successfully_validated": 0}),
            ("N/A", "N/A"),
        )

    def test_udmi_xlsx_treats_untrusted_text_as_literal_cells(self) -> None:
        summary = copy.deepcopy(_V1_SUMMARY)
        summary["system_metrics"][0]["system"] = '=HYPERLINK("https://invalid/system","x")'
        summary["asset_results"][0]["system"] = '=HYPERLINK("https://invalid/system","x")'
        summary["asset_results"][0]["asset_id"] = "+cmd|' /C calc'!A0"
        summary["fault_rows"][0]["system"] = '=HYPERLINK("https://invalid/system","x")'
        summary["fault_rows"][0]["asset_id"] = "+cmd|' /C calc'!A0"
        summary["fault_rows"][0]["issue_id"] = "@SUM(1+1)"
        summary["fault_rows"][0]["description"] = "-2+3"
        summary["fault_rows"][0]["observed_value"] = (
            "observed\x01\ud800\ufffe\uffffvalue"
        )
        source_id = self._seed_run(summary=summary)
        title = '=HYPERLINK("https://invalid/title","x")'
        report = self._create_report("xlsx", [source_id], title=title)
        xlsx_content = self._download(report["report_id"]).content
        workbook = load_workbook(io.BytesIO(xlsx_content))

        executive = workbook["Executive Summary"]
        self.assertEqual(executive["A1"].value, f"'{title}")
        systems = workbook["Metrics by System"]
        assets = workbook["Asset Validation Schedule"]
        details = workbook["Faults in Detail"]
        guarded_values = [
            next(
                cell
                for row in systems.iter_rows()
                for cell in row
                if cell.value == f"'{summary['system_metrics'][0]['system']}"
            ),
            next(
                cell
                for row in assets.iter_rows()
                for cell in row
                if cell.value == f"'{summary['asset_results'][0]['asset_id']}"
            ),
            next(
                cell
                for row in details.iter_rows()
                for cell in row
                if cell.value == "'@SUM(1+1)"
            ),
            next(
                cell
                for row in details.iter_rows()
                for cell in row
                if cell.value == "'-2+3"
            ),
        ]
        self.assertTrue(all(cell.data_type == "s" for cell in guarded_values))
        detail_headers = {cell.value: cell.column for cell in details[1]}
        first_issue_row = next(
            row
            for row in range(2, details.max_row + 1)
            if details.cell(row, detail_headers["Issue ID"]).value == "'@SUM(1+1)"
        )
        self.assertEqual(
            details.cell(first_issue_row, detail_headers["Observed"]).value,
            "observedvalue",
        )
        with zipfile.ZipFile(io.BytesIO(xlsx_content)) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                member = archive.read(name)
                self.assertNotIn(b"\x01", member)
                ElementTree.fromstring(member)

        docx_report = self._create_report("docx", [source_id], title="Control Character Test")
        docx_content = self._download(docx_report["report_id"]).content
        with zipfile.ZipFile(io.BytesIO(docx_content)) as archive:
            document = archive.read("word/document.xml")
        self.assertIn(b"observedvalue", document)
        self.assertNotIn(b"\x01", document)
        ElementTree.fromstring(document)

        legacy_title = '=HYPERLINK("https://invalid/legacy-title","x")'
        legacy_report = self._create_report(
            "xlsx",
            [],
            title=legacy_title,
            report_type="evidence_pack",
        )
        legacy_workbook = load_workbook(
            io.BytesIO(self._download(legacy_report["report_id"]).content)
        )
        legacy_title_cell = legacy_workbook["Report Summary"]["B2"]
        self.assertEqual(legacy_title_cell.value, f"'{legacy_title}")
        self.assertEqual(legacy_title_cell.data_type, "s")


if __name__ == "__main__":
    unittest.main()
