"""Report content tests for the end-to-end validation report (field ask 2026-07-14).

All in-process against the shared temporary SQLite DB (no live infra). Covers:

  * the new "pdf" output format end to end: accepted at creation, downloaded as
    application/pdf bytes starting %PDF-1.4, byte-reproducible across
    downloads, and re-verifiable through the evidence verify endpoint;
  * the validation sections (Summary / Failure detail / Silent systems) across
    the docx, xlsx, zip, and pdf artifacts, including the compliance % fed by
    payload_conformance_percent and the silent device ids;
  * the pre-upgrade fallback: a validation source run recorded by an older app
    version (no payload_conformance_percent / blocking_issue_count /
    not_publishing_devices) still renders — liveness-labelled compliance, a
    blocking count derived from the run's own issue records (so the ≤99 clamp
    still fires), and a placeholder silent-systems row instead of ids;
  * the ELECTRACOM report branding (field ask 2026-07-15): the text-only
    header/footer band on the pdf (wordmark + rule on every page, footer
    wordmark + page number + run id), the real OOXML header1.xml/footer1.xml
    parts and their relationships/content-type/sectPr wiring in the docx, and
    the per-sheet oddHeader/oddFooter in the xlsx — with a deliberate re-pin of
    byte-reproducibility and verify over the now-branded bytes (the branding
    changes every artifact's bytes, so this coverage lands in the same commit).

Source runs are seeded through RunService's public API (create_job_run +
update_result_summary + replace_issues) — the same records a real
udmi_validation run persists — so the report path is exercised exactly as in
production without needing a broker.
"""

import io
import json
import unittest
import zipfile

from harness import ApiTestCase

_API_KEY = "test-reports-validation-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_EM_DASH = "—"

# A post-upgrade udmi_validation result_summary (the fields the report reads).
_UPGRADED_SUMMARY = {
    "expected_devices": 10,
    "publishing_seen": 8,
    "not_publishing": 2,
    "not_publishing_devices": ["AHU-7", "FCU-3"],
    "issue_count": 1,
    "blocking_issue_count": 1,
    "payload_conformance_percent": 97,
    "capture_window_seconds": 300,
    "capture_mode": "bounded",
}

# A pre-upgrade record: liveness counts only, none of the new fields.
_PRE_UPGRADE_SUMMARY = {
    "expected_devices": 4,
    "publishing_seen": 3,
    "not_publishing": 1,
    "issue_count": 0,
}

_POINT_ISSUE = {
    "issue_id": "iss-001",
    "asset_id": "AHU-1",
    "issue_type": "unit_mismatch",
    "severity": "high",
    "description": "Reported unit does not match the register.",
    "point_name": "supply_air_temp",
    "expected_value": "degC",
    "observed_value": "degF",
    "suggested_action": "Update the device pointset units to degC.",
}


class ValidationReportApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        # Point the evidence signing key at a temp secrets dir (same patching
        # pattern as test_evidence_api.py) so downloads sign against it.
        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-reports-validation-")
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

    # -- helpers ---------------------------------------------------------------

    def _seed_validation_run(self, result_summary: dict, issues: list[dict] | None = None) -> str:
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
        if issues:
            run_service.replace_issues(run.run_id, issues)
        return run.run_id

    def _create_report(self, output_format: str, source_run_ids: list[str]) -> str:
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "udmi_validation",
                "output_format": output_format,
                "source_run_ids": source_run_ids,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["output_format"], output_format)
        return body["report_id"]

    def _download(self, report_id: str):
        response = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _zip_member(self, content: bytes, name: str) -> dict:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return json.loads(archive.read(name))

    # -- pdf format ------------------------------------------------------------

    def test_pdf_report_downloads_with_sections_and_content(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("pdf", [run_id])
        download = self._download(report_id)
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertIn(".pdf", download.headers["content-disposition"])
        self.assertTrue(download.content.startswith(b"%PDF-1.4"), download.content[:16])
        # Content streams are uncompressed, so section titles and cell text are
        # directly visible as PDF literal strings.
        for expected in (
            b"Summary",
            b"Failure detail",
            b"Silent systems",
            b"97%",
            b"AHU-7",
            b"FCU-3",
            b"supply_air_temp",
            # The full silent-systems note word-wraps across PDF text lines, so
            # assert a fragment that always sits on its first line.
            b"Silent systems are devices",
            # Failure detail = slim identity table + per-finding paragraphs:
            # a bold "Issue ID — Asset — Point" lead (em dash = WinAnsi 0x97)
            # followed by the long-text fields in full, never truncated cells.
            b"iss-001 \x97 AHU-1 \x97 supply_air_temp",
            b"Expected: degC",
            b"Observed: degF",
            b"Suggested Action: Update the device pointset units to degC.",
            b"Description: Reported unit does not match the register.",
        ):
            self.assertIn(expected, download.content)

    def test_pdf_download_is_byte_reproducible_and_verifiable(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("pdf", [run_id])
        first = self._download(report_id)
        second = self._download(report_id)
        self.assertEqual(first.content, second.content, "pdf artifact must be reproducible")

        verify = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
        self.assertEqual(verify.status_code, 200, verify.text)
        body = verify.json()
        self.assertTrue(body["hash_matches"], body)
        self.assertTrue(body["signature_valid"], body)

    # -- section content across the other formats -------------------------------

    def test_docx_carries_validation_sections(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("docx", [run_id])
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        for expected in (
            "Summary",
            "Failure detail",
            "Silent systems",
            "97%",
            "AHU-7",
            "FCU-3",
            "supply_air_temp",
            "Update the device pointset units to degC.",
            "neither validated nor failed",
        ):
            self.assertIn(expected, document_xml)

    def test_xlsx_carries_validation_sections(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("xlsx", [run_id])
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            all_xml = "".join(
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if name.endswith(".xml")
            )
        for sheet_name in ("Summary", "Failure detail", "Silent systems"):
            self.assertIn(sheet_name, workbook_xml)
        for expected in ("97%", "AHU-7", "FCU-3", "supply_air_temp", "neither validated nor failed"):
            self.assertIn(expected, all_xml)

    def test_zip_validation_summary_rows_and_silent_ids(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("zip", [run_id])
        download = self._download(report_id)

        summary = self._zip_member(download.content, "validation_summary.json")
        self.assertEqual(len(summary["rows"]), 1)
        row = summary["rows"][0]
        self.assertEqual(row["Source Run"], run_id)
        self.assertEqual(row["Type"], "udmi_validation")
        self.assertEqual(row["Expected Devices"], "10")
        self.assertEqual(row["Publishing"], "8")
        self.assertEqual(row["Silent"], "2")
        self.assertEqual(row["Blocking Issues"], "1")
        self.assertEqual(row["Compliance %"], "97%")
        self.assertEqual(summary["overall"]["Total Devices"], 10)
        self.assertEqual(summary["overall"]["Total Silent"], 2)
        self.assertEqual(summary["overall"]["Total Blocking Issues"], 1)
        self.assertEqual(summary["overall"]["Overall Compliance %"], "97%")

        silent = self._zip_member(download.content, "silent_systems.json")
        self.assertEqual(
            silent["rows"],
            [
                {"Source Run": run_id, "Device ID": "AHU-7"},
                {"Source Run": run_id, "Device ID": "FCU-3"},
            ],
        )
        self.assertIn("neither validated nor failed", silent["note"])

        # Findings carry the new point-level columns.
        findings = self._zip_member(download.content, "findings.json")
        self.assertEqual(findings[0]["Point"], "supply_air_temp")
        self.assertEqual(findings[0]["Expected"], "degC")
        self.assertEqual(findings[0]["Observed"], "degF")
        self.assertEqual(findings[0]["Suggested Action"], "Update the device pointset units to degC.")

    # -- pre-upgrade fallback ----------------------------------------------------

    def test_pre_upgrade_source_run_renders_liveness_fallback(self) -> None:
        run_id = self._seed_validation_run(_PRE_UPGRADE_SUMMARY)
        report_id = self._create_report("zip", [run_id])
        download = self._download(report_id)

        summary = self._zip_member(download.content, "validation_summary.json")
        row = summary["rows"][0]
        # No payload_conformance_percent recorded -> liveness ratio, marked as such.
        self.assertEqual(row["Compliance %"], "75% (liveness)")
        # No blocking_issue_count recorded -> derived from the run's own issue
        # records (none here), not shown as absent.
        self.assertEqual(row["Blocking Issues"], "0")
        self.assertEqual(summary["overall"]["Overall Compliance %"], "75% (liveness)")

        silent = self._zip_member(download.content, "silent_systems.json")
        self.assertEqual(
            silent["rows"],
            [{"Source Run": run_id, "Device ID": "(ids not recorded by this run's app version)"}],
        )

    def test_pre_upgrade_source_run_renders_in_pdf(self) -> None:
        run_id = self._seed_validation_run(_PRE_UPGRADE_SUMMARY)
        report_id = self._create_report("pdf", [run_id])
        download = self._download(report_id)
        self.assertTrue(download.content.startswith(b"%PDF-1.4"))
        # "(" and ")" are escaped in PDF literal strings.
        self.assertIn(b"75% \\(liveness\\)", download.content)
        self.assertIn(b"ids not recorded by this run's app version", download.content)

    def test_pre_upgrade_blocking_derived_from_issue_records_clamps_compliance(self) -> None:
        # A pre-upgrade run (no blocking_issue_count persisted) whose devices
        # all published, but with a critical finding on record: the count
        # derived from the run's own issues must keep "100% (liveness)" out of
        # the report, per-row and overall.
        run_id = self._seed_validation_run(
            {
                "expected_devices": 4,
                "publishing_seen": 4,
                "not_publishing": 0,
                "issue_count": 1,
            },
            [{**_POINT_ISSUE, "severity": "critical"}],
        )
        report_id = self._create_report("zip", [run_id])
        summary = self._zip_member(self._download(report_id).content, "validation_summary.json")

        row = summary["rows"][0]
        self.assertEqual(row["Blocking Issues"], "1")
        self.assertEqual(row["Compliance %"], "99% (liveness)")
        self.assertEqual(summary["overall"]["Total Blocking Issues"], 1)
        self.assertEqual(summary["overall"]["Overall Compliance %"], "99% (liveness)")

    def test_duplicate_source_run_ids_are_deduped(self) -> None:
        # The same id scoped twice must not double Summary devices/blocking or
        # duplicate finding rows.
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("zip", [run_id, run_id])
        download = self._download(report_id)

        summary = self._zip_member(download.content, "validation_summary.json")
        self.assertEqual(len(summary["rows"]), 1)
        self.assertEqual(summary["overall"]["Total Devices"], 10)
        self.assertEqual(summary["overall"]["Total Blocking Issues"], 1)
        findings = self._zip_member(download.content, "findings.json")
        self.assertEqual(len(findings), 1)

    def test_null_compliance_renders_as_dash(self) -> None:
        run_id = self._seed_validation_run(
            {
                "expected_devices": 0,
                "publishing_seen": 0,
                "not_publishing": 0,
                "not_publishing_devices": [],
                "issue_count": 0,
                "blocking_issue_count": 0,
                "payload_conformance_percent": None,
            }
        )
        report_id = self._create_report("zip", [run_id])
        summary = self._zip_member(self._download(report_id).content, "validation_summary.json")
        self.assertEqual(summary["rows"][0]["Compliance %"], _EM_DASH)
        self.assertEqual(summary["overall"]["Overall Compliance %"], _EM_DASH)

    # -- overall compliance math ---------------------------------------------------

    def test_overall_compliance_is_device_weighted_across_runs(self) -> None:
        upgraded = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        pre_upgrade = self._seed_validation_run(_PRE_UPGRADE_SUMMARY)
        report_id = self._create_report("zip", [upgraded, pre_upgrade])
        summary = self._zip_member(self._download(report_id).content, "validation_summary.json")

        self.assertEqual(len(summary["rows"]), 2)
        overall = summary["overall"]
        self.assertEqual(overall["Total Devices"], 14)
        self.assertEqual(overall["Total Silent"], 3)
        self.assertEqual(overall["Total Blocking Issues"], 1)
        # Device-weighted: floor((97*10 + 100*3) / (10 + 4)) = floor(1270/14) = 90,
        # labelled (liveness) because a liveness-only run contributed.
        self.assertEqual(overall["Overall Compliance %"], "90% (liveness)")

    # -- ELECTRACOM report branding ----------------------------------------------

    def test_pdf_branding_on_every_page(self) -> None:
        # Enough issues to force multiple pages (each finding adds an identity
        # row plus per-finding paragraphs), so the every-page furniture is tested
        # across a page break, not just on page 1.
        issues = [
            {**_POINT_ISSUE, "issue_id": f"iss-{index:03d}", "asset_id": f"AHU-{index}"}
            for index in range(60)
        ]
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, issues)
        report_id = self._create_report("pdf", [run_id])
        content = self._download(report_id).content

        # Page objects only: "/Type /Page " (trailing space) excludes "/Type /Pages".
        pages = content.count(b"/Type /Page ")
        self.assertGreaterEqual(pages, 2, "test needs a multi-page report to prove per-page furniture")
        # ELECTRACOM appears exactly twice per page: header-left + footer-left
        # wordmark. Content streams are uncompressed so the literals are direct.
        self.assertEqual(content.count(b"ELECTRACOM"), 2 * pages)
        # The report run id sits in the footer-right on every page (and nowhere in
        # the body — the body carries source-run ids, not the report's own id).
        self.assertGreaterEqual(content.count(run_id.encode("ascii")), pages)

    def test_docx_branding_parts_and_references(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("docx", [run_id])
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            names = set(archive.namelist())
            header_xml = archive.read("word/header1.xml").decode("utf-8")
            footer_xml = archive.read("word/footer1.xml").decode("utf-8")
            rels_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
            content_types = archive.read("[Content_Types].xml").decode("utf-8")
            document_xml = archive.read("word/document.xml").decode("utf-8")

        self.assertIn("word/header1.xml", names)
        self.assertIn("word/footer1.xml", names)
        self.assertIn("word/_rels/document.xml.rels", names)

        self.assertIn("ELECTRACOM", header_xml)
        self.assertIn("ELECTRACOM", footer_xml)
        # Footer carries the PAGE/NUMPAGES fields and the report's OWN run id
        # (report_id == the report-generation run's run_id; the source validation
        # runs are listed in the report body, not the footer).
        self.assertIn(" PAGE ", footer_xml)
        self.assertIn(" NUMPAGES ", footer_xml)
        self.assertIn(report_id, footer_xml)

        # Relationships point header/footer types at the parts.
        self.assertIn(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header", rels_xml
        )
        self.assertIn(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer", rels_xml
        )
        self.assertIn('Target="header1.xml"', rels_xml)
        self.assertIn('Target="footer1.xml"', rels_xml)

        # Content types declare both parts.
        self.assertIn("wordprocessingml.header+xml", content_types)
        self.assertIn("wordprocessingml.footer+xml", content_types)

        # document.xml wires the references, declares r:, and no longer carries
        # the empty self-closing sectPr.
        self.assertIn("w:headerReference", document_xml)
        self.assertIn("w:footerReference", document_xml)
        self.assertIn(
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"',
            document_xml,
        )
        self.assertNotIn("<w:sectPr/>", document_xml)

    def test_xlsx_branding_on_every_sheet(self) -> None:
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        report_id = self._create_report("xlsx", [run_id])
        download = self._download(report_id)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            sheet_names = [
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            ]
            self.assertTrue(sheet_names, "expected at least one worksheet part")
            for name in sheet_names:
                sheet_xml = archive.read(name).decode("utf-8")
                self.assertIn("<headerFooter", sheet_xml, name)
                self.assertIn("oddHeader", sheet_xml, name)
                self.assertIn("oddFooter", sheet_xml, name)
                self.assertIn("ELECTRACOM", sheet_xml, name)
                # openpyxl serializes the friendly &[Page]/&N page tokens as the
                # compact Excel codes &P/&N, XML-escaped to &amp;P/&amp;N. If a
                # future openpyxl serializes differently, pin the real form here.
                self.assertIn("&amp;P", sheet_xml, name)
                self.assertIn("&amp;N", sheet_xml, name)
                self.assertIn(run_id, sheet_xml, name)

    def test_branded_artifacts_remain_reproducible_and_verifiable(self) -> None:
        # The branding changes every artifact's bytes; deliberately re-pin
        # byte-reproducibility AND verify over the new bytes, in the same commit
        # that changes them (extends the pdf-only coverage above to docx/xlsx).
        run_id = self._seed_validation_run(_UPGRADED_SUMMARY, [_POINT_ISSUE])
        for output_format in ("pdf", "docx", "xlsx"):
            report_id = self._create_report(output_format, [run_id])
            first = self._download(report_id).content
            second = self._download(report_id).content
            self.assertEqual(first, second, f"{output_format} artifact must be reproducible")

            verify = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
            self.assertEqual(verify.status_code, 200, verify.text)
            body = verify.json()
            self.assertTrue(body["hash_matches"], (output_format, body))
            self.assertTrue(body["signature_valid"], (output_format, body))


class PdfWriterUnitTests(unittest.TestCase):
    """Direct PdfDocument tests (no API): WinAnsi extras and long-token layout."""

    def test_degree_plus_minus_micro_render_as_winansi_bytes(self) -> None:
        from app.services.report_pdf import PdfDocument

        document = PdfDocument()
        document.add_paragraph("Temperature 21.5 °C ± 0.5, filter 5 µm")
        rendered = document.render()
        # WinAnsi bytes, not the '?' fallback for unmeasured glyphs.
        self.assertIn(b"21.5 \xb0C \xb1 0.5", rendered)
        self.assertIn(b"5 \xb5m", rendered)
        self.assertNotIn(b"?C", rendered)

    def test_winansi_extras_width_table_is_consistent(self) -> None:
        from app.services import report_pdf

        for char, (code, regular, bold) in report_pdf._WINANSI_EXTRAS.items():
            self.assertTrue(0x80 <= code <= 0xFF, char)
            self.assertEqual(report_pdf._char_width(char, False), regular, char)
            self.assertEqual(report_pdf._char_width(char, True), bold, char)

    def test_truncate_and_wrap_handle_multi_kb_unbroken_tokens(self) -> None:
        # Regression for the O(n^2)/O(n^3) shrink-and-remeasure layout: a
        # multi-KB unbroken token must lay out promptly and losslessly.
        from app.services.report_pdf import _ELLIPSIS, _truncate, _wrap

        token = "x" * 5000
        truncated = _truncate(token, 9.0, False, 100.0)
        self.assertTrue(truncated.endswith(_ELLIPSIS))
        self.assertLess(len(truncated), 40)
        lines = _wrap(token, 9.0, False, 495.0)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), token)

        self.assertEqual(_wrap("short line", 10.0, False, 495.0), ["short line"])
        self.assertEqual(_truncate("short", 10.0, False, 495.0), "short")

    def test_no_furniture_means_no_header(self) -> None:
        # The writer's default (furniture-less) output shape is unchanged: no
        # branding band, and the footer is exactly the single page-number line.
        from app.services.report_pdf import PdfDocument

        document = PdfDocument()
        document.add_paragraph("A single line of body text.")
        rendered = document.render()
        self.assertNotIn(b"ELECTRACOM", rendered)
        self.assertEqual(rendered.count(b"Page 1 of 1"), 1)


if __name__ == "__main__":
    unittest.main()
