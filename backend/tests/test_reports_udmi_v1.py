"""Versioned UDMI report contract coverage across every report renderer."""

from __future__ import annotations

import copy
import io
import json
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile

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


class UdmiV1ReportTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

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
    ) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        service = RunService()
        run = service.create_job_run(
            JobCreateRequest(
                project_id=project_id,
                site_id=site_id,
                job_type=job_type,
                parameters={},
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
        return run.run_id

    def _create_report(
        self,
        output_format: str,
        source_run_ids: list[str],
        *,
        title: str | None = "  Site & <A> Validation  ",
        report_type: str = "udmi_validation",
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
                    self.assertEqual(report_summary["Status"], "succeeded")
                    self.assertIn("INCOMPLETE", report_summary["Validation scope"])
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
                    self.assertIn(b"Status: succeeded", content)
                elif output_format == "docx":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        document = archive.read("word/document.xml")
                    self.assertIn(b"Validation Scope Incomplete", document)
                    self.assertIn(b"INCOMPLETE", document)
                    self.assertIn(b"Status: succeeded", document)
                else:
                    workbook = load_workbook(io.BytesIO(content))
                    executive = workbook["Executive Summary"]
                    metadata = {
                        executive.cell(row, 1).value: executive.cell(row, 2).value
                        for row in range(2, executive.max_row + 1)
                    }
                    self.assertEqual(metadata["Status"], "succeeded")
                    self.assertIn("INCOMPLETE", metadata["Validation scope"])
                    self.assertIn("INCOMPLETE", metadata["Validation Scope Incomplete"])

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
        self.assertEqual(summary["asset_metrics"], _TOTALS["asset_metrics"])
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
                "Executive Summary",
                "Metrics by System",
                "Asset Validation Schedule",
                "Fault Matrix",
                "Faults in Detail",
                "Metric Definitions",
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
