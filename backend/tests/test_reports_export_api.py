"""Reports 'Export selected' bundle endpoint (field review 2026-07-20, item 13).

Several ticked reports must download as ONE zip containing every selected report
file; a browser's multiple-download throttle otherwise keeps only one file (the
field-observed "whatever file you choose last" bug). A single ticked report
keeps its direct /{id}/download. Covered here:

  * POST /reports/export {"report_ids": [A, B]} returns application/zip whose
    members are exactly the two reports' file_names, each byte-identical to that
    report's own /{id}/download;
  * a duplicate id yields one member (order-preserving dedupe);
  * an unknown id among valid ones 404s the whole request (never a partial
    archive), naming the missing id;
  * a real non-report run id (a validation run) 404s — the wrong-job-type
    guard, so a discovery/validation record is never bundled as a "report";
  * an empty report_ids list yields 422 — the non-empty pydantic constraint.

The ids ride in a JSON body (not repeated query params) so an unbounded
selection never exceeds the request-line limits uvicorn/h11 and proxies cap.

In-process against the shared temporary SQLite DB (no live infra). The evidence
signing key is pointed at a temp secrets dir because export persists integrity.
"""

import io
import zipfile

from harness import ApiTestCase

_API_KEY = "test-reports-export-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_SUMMARY = {
    "expected_devices": 3,
    "publishing_seen": 3,
    "not_publishing": 0,
    "issue_count": 0,
}


class ReportsExportApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        import atexit
        import shutil
        import tempfile
        from pathlib import Path
        from unittest import mock

        # Export persists integrity per member, so point the signing key at a
        # temp secrets dir (same pattern as test_reports_validation.py).
        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-reports-export-")
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

    def _seed_validation_run(self) -> str:
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
        run_service.update_result_summary(run.run_id, _SUMMARY)
        return run.run_id

    def _create_report(self, source_run_ids: list[str], output_format: str = "zip") -> dict:
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
        return response.json()

    def _download(self, report_id: str) -> bytes:
        response = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        return response.content

    # -- tests -----------------------------------------------------------------

    def test_multiple_reports_export_as_one_zip_of_both_files(self) -> None:
        run_id = self._seed_validation_run()
        report_a = self._create_report([run_id])
        report_b = self._create_report([run_id])

        response = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [report_a["report_id"], report_b["report_id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("reports_export.zip", response.headers["content-disposition"])

        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {report_a["file_name"], report_b["file_name"]})
            # Each member is byte-identical to that report's own direct download.
            self.assertEqual(
                archive.read(report_a["file_name"]), self._download(report_a["report_id"])
            )
            self.assertEqual(
                archive.read(report_b["file_name"]), self._download(report_b["report_id"])
            )

    def test_duplicate_report_id_yields_one_member(self) -> None:
        run_id = self._seed_validation_run()
        report = self._create_report([run_id])
        response = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [report["report_id"], report["report_id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), [report["file_name"]])

    def test_unknown_id_among_valid_ones_404s_the_whole_request(self) -> None:
        run_id = self._seed_validation_run()
        report = self._create_report([run_id])
        response = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [report["report_id"], "no-such-report"]},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("no-such-report", response.json()["detail"])

    def test_non_report_run_id_404s_and_is_never_bundled(self) -> None:
        # A real run id whose job_type is NOT report_generation (here a
        # validation run) must 404 the export, never bundle a fabricated
        # "report" built from a validation/discovery record (honesty rule).
        run_id = self._seed_validation_run()
        response = self.client.post(
            "/api/v1/reports/export",
            json={"report_ids": [run_id]},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn(run_id, response.json()["detail"])

    def test_empty_report_ids_is_422(self) -> None:
        # An empty selection must 422 on the non-empty constraint, never build an
        # empty zip. A 404/405 here would mean the /export route did not resolve.
        response = self.client.post("/api/v1/reports/export", json={"report_ids": []})
        self.assertEqual(response.status_code, 422, response.text)
