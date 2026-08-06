"""API regressions for the v0.1.25 Workbench report fixes."""

from __future__ import annotations

import io
import json
import zipfile
from unittest import mock
from urllib.parse import quote

from harness import ApiTestCase

_API_KEY = "test-reports-v0125-key"
_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


def _empty_fault_metrics() -> dict[str, int]:
    return {
        "payload_formatting_issues": 0,
        "missing_points": 0,
        "point_naming_issues": 0,
        "additional_points": 0,
        "stale_or_cadence": 0,
        "other_issues": 0,
    }


def _summary(
    asset_ids: list[str],
    *,
    unexpected_metric: int = 0,
    unexpected_devices: list[dict[str, object]] | None = None,
    include_unexpected_list: bool = True,
) -> dict[str, object]:
    asset_count = len(asset_ids)
    asset_metrics = {
        "expected": asset_count,
        "observed": asset_count,
        "not_observed": 0,
        "with_issues": 0,
        "successfully_validated": asset_count,
        "unexpected": unexpected_metric,
    }
    payload_metrics = {
        "expected": asset_count,
        "received": asset_count,
        "not_received": 0,
        "with_issues": 0,
        "successfully_validated": asset_count,
    }
    result: dict[str, object] = {
        "schema_version": "1.1",
        "asset_metrics": asset_metrics,
        "payload_metrics": payload_metrics,
        "fault_metrics": _empty_fault_metrics(),
        "issue_metrics": {"blocking": 0, "warning": 0},
        "system_metrics": [
            {
                "system": "BMS",
                "asset_metrics": {
                    key: value for key, value in asset_metrics.items() if key != "unexpected"
                },
                "payload_metrics": dict(payload_metrics),
                "fault_metrics": _empty_fault_metrics(),
                "issue_metrics": {"blocking": 0, "warning": 0},
            }
        ],
        "asset_results": [
            {
                "asset_id": asset_id,
                "system": "BMS",
                "observed": True,
                "expected_payloads": 1,
                "received_payloads": 1,
                "all_expected_payloads_received": True,
                "all_received_payloads_successfully_validated": True,
                "successfully_validated": True,
                "issue_count": 0,
                "blocking_issue_count": 0,
                "last_observed_at": "2026-07-24T10:00:00+00:00",
                "payload_results": [
                    {
                        "payload_type": "state",
                        "expected": True,
                        "received": True,
                        "has_issues": False,
                        "blocking_issue_count": 0,
                        "successfully_validated": True,
                        "topic": f"site/{asset_id}/state",
                        "received_at": "2026-07-24T10:00:00+00:00",
                    }
                ],
            }
            for asset_id in asset_ids
        ],
        "fault_rows": [],
    }
    if include_unexpected_list:
        result["unexpected_devices"] = list(unexpected_devices or [])
        result["unexpected_devices_measured"] = True
        result["unexpected_devices_measurement_scope"] = "site/#"
    return result


class ReportsV0125RegressionTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path

        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-reports-v0125-")
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

    def _seed_source(
        self,
        summary: dict[str, object],
        *,
        asset_topic_discovery: dict[str, object] | None = None,
    ) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService
        from lifecycle_helpers import finish_run

        service = RunService()
        assets = [
            {"expected_schedule": {"asset_id": row["asset_id"], "project_site": "Site A"}}
            for row in summary["asset_results"]
            if isinstance(row, dict)
        ]
        run = service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type="udmi_validation",
                parameters={"assets": assets},
            ),
            expected_job_type="udmi_validation",
        )
        result_summary: dict[str, object] = {"validation_summary_v1": summary}
        if asset_topic_discovery is not None:
            result_summary["asset_topic_discovery"] = asset_topic_discovery
        finish_run(service, run.run_id, summary=result_summary)
        return run.run_id

    def _create_report(
        self,
        *,
        source_run_ids: list[str],
        report_title: str | None = None,
        udmi_scope: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "project_id": "demo-project",
            "site_id": "demo-site",
            "report_type": "udmi_validation",
            "output_format": "zip",
            "source_run_ids": source_run_ids,
            "udmi_report_variant": "technical",
        }
        if report_title is not None:
            payload["report_title"] = report_title
        if udmi_scope is not None:
            payload["udmi_scope"] = udmi_scope
        response = self.client.post("/api/v1/reports", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _download(self, report_id: object):
        response = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_custom_title_drives_api_download_and_export_member_name(self) -> None:
        report = self._create_report(
            source_run_ids=[],
            report_title="  Café / 東京: Phase * 1?  ",
        )
        expected_name = f"Café_東京_Phase_1_{report['report_id']}.zip"

        self.assertEqual(report["report_title"], "Café / 東京: Phase * 1?")
        self.assertEqual(report["file_name"], expected_name)
        fetched = self.client.get(f"/api/v1/reports/{report['report_id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["file_name"], expected_name)

        download = self._download(report["report_id"])
        disposition = download.headers["content-disposition"]
        self.assertIn(
            f'filename="Cafe_Phase_1_{report["report_id"]}.zip"',
            disposition,
        )
        self.assertIn(f"filename*=UTF-8''{quote(expected_name, safe='')}", disposition)

        exported = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [report["report_id"]]},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            self.assertEqual(
                archive.namelist(), [expected_name, "report_set_manifest.json"]
            )
            self.assertEqual(archive.read(expected_name), download.content)

    def test_default_blank_and_pre_marker_runs_keep_compatible_names(self) -> None:
        default = self._create_report(source_run_ids=[])
        self.assertEqual(
            default["file_name"],
            f"udmi_validation_{default['report_id']}.zip",
        )

        blank = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "udmi_validation",
                "output_format": "zip",
                "source_run_ids": [],
                "report_title": "   ",
            },
        )
        self.assertEqual(blank.status_code, 422, blank.text)

        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        service = RunService()
        legacy = service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type="report_generation",
                parameters={
                    "report_type": "udmi_validation",
                    "output_format": "pdf",
                    "source_run_ids": [],
                    "report_title": "Legacy Custom Title",
                },
            ),
            expected_job_type="report_generation",
        )
        response = self.client.get(f"/api/v1/reports/{legacy.run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["report_title"], "Legacy Custom Title")
        self.assertEqual(
            response.json()["file_name"],
            f"udmi_validation_{legacy.run_id}.pdf",
        )

    def test_three_asset_scope_is_frozen_exactly_and_never_falls_back_to_all(self) -> None:
        source_id = self._seed_source(_summary(["A-1", "B-1", "C-1", "D-1", "E-1"]))
        selected_ids = ["A-1", "C-1", "E-1"]
        report = self._create_report(
            source_run_ids=[source_id],
            udmi_scope={
                "schema_version": "1.0",
                "selected_payloads": [
                    {
                        "source_run_id": source_id,
                        "asset_id": asset_id,
                        "payload_type": "state",
                    }
                    for asset_id in selected_ids
                ],
                "unexpected_device_ids": [],
                "filters": {"text": "selected three"},
            },
        )

        from app.services.run_service import RunService

        stored = RunService().get_run(str(report["report_id"]))
        snapshot = stored.parameters["udmi_report_snapshot"]
        self.assertEqual(snapshot["asset_metrics"]["expected"], 3)
        self.assertEqual(snapshot["payload_metrics"]["expected"], 3)
        self.assertEqual(
            [row["asset_id"] for row in snapshot["asset_results"]],
            selected_ids,
        )

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            summary = json.loads(archive.read("validation_summary.json"))
            schedule = json.loads(archive.read("asset_validation_schedule.json"))
        self.assertEqual(summary["asset_metrics"]["expected"], 3)
        self.assertEqual(summary["payload_metrics"]["expected"], 3)
        self.assertEqual([row["asset_id"] for row in schedule["rows"]], selected_ids)
        self.assertNotIn("B-1", {row["asset_id"] for row in schedule["rows"]})
        self.assertNotIn("D-1", {row["asset_id"] for row in schedule["rows"]})

    def test_asset_topic_discovery_is_retained_in_zip_and_combined_export(self) -> None:
        ledger = {
            "enabled": True,
            "scope": "demo-site/#",
            "capture_complete": True,
            "asset_results": [
                {
                    "asset_id": "A-1",
                    "status": "alternate_topic_observed",
                    "observed_alternate_topics": [
                        {
                            "topic": "demo-site/alternate/A-1/state",
                            "message_count": 1,
                        }
                    ],
                }
            ],
        }
        source_id = self._seed_source(
            _summary(["A-1"]),
            asset_topic_discovery=ledger,
        )
        report = self._create_report(source_run_ids=[source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            self.assertIn("asset_topic_discovery.json", archive.namelist())
            discovery = json.loads(archive.read("asset_topic_discovery.json"))
        expected = {
            "source_runs": [
                {
                    "source_run_id": source_id,
                    "asset_topic_discovery": ledger,
                }
            ]
        }
        self.assertEqual(discovery, expected)

        exported = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [report["report_id"]]},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as outer_archive:
            with zipfile.ZipFile(io.BytesIO(outer_archive.read(report["file_name"]))) as report_archive:
                self.assertEqual(
                    json.loads(report_archive.read("asset_topic_discovery.json")),
                    expected,
                )

    def test_asset_topic_discovery_member_is_omitted_without_a_source_ledger(self) -> None:
        source_id = self._seed_source(_summary(["A-1"]))
        report = self._create_report(source_run_ids=[source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            self.assertNotIn("asset_topic_discovery.json", archive.namelist())

    def test_explicit_unexpected_rows_override_a_stale_zero_metric(self) -> None:
        devices = [
            {
                "id": device_id,
                "topic_root": f"site/{device_id}",
                "topics": [f"site/{device_id}/state"],
                "last_seen": "2026-07-24T10:00:00+00:00",
            }
            for device_id in ("rogue-1", "rogue-2")
        ]
        source_id = self._seed_source(
            _summary(
                ["A-1"],
                unexpected_metric=0,
                unexpected_devices=devices,
            )
        )
        report = self._create_report(source_run_ids=[source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            summary = json.loads(archive.read("validation_summary.json"))
        self.assertEqual(summary["asset_metrics"]["unexpected"], 2)
        self.assertEqual(
            [row["id"] for row in summary["unexpected_devices"]],
            ["rogue-1", "rogue-2"],
        )

    def test_legacy_snapshot_without_unexpected_rows_keeps_numeric_metric(self) -> None:
        source_id = self._seed_source(
            _summary(
                ["A-1"],
                unexpected_metric=7,
                include_unexpected_list=False,
            )
        )
        report = self._create_report(source_run_ids=[source_id])

        with zipfile.ZipFile(io.BytesIO(self._download(report["report_id"]).content)) as archive:
            summary = json.loads(archive.read("validation_summary.json"))
        self.assertEqual(summary["asset_metrics"]["unexpected"], 7)
        self.assertEqual(summary["unexpected_devices"], [])
