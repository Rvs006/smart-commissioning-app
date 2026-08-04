"""Report LIST/GET/POST projection tests for created_at + source_run_ids.

The Reports page has to show WHEN a report was generated and WHICH runs it was
scoped to, so a handover pack traces back to the evidence it came from. Both
values were already persisted on the report run record (Run.created_at, and
parameters["source_run_ids"] written by create_report_run) but neither reached
the API, so this is a projection, not a migration.

ReportSummary is constructed in two places -- run_service.create_report_run (the
POST response) and reports._to_report_summary (the list/get/download path) -- and
the two must not disagree; a report's created_at cannot change between the
response that created it and the list it later appears in. These tests pin both
sites and their agreement.

No SECRETS_ROOT patch here (unlike test_reports_validation): nothing is
downloaded, so the Ed25519 signing key is never touched.
"""

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from harness import ApiTestCase

_API_KEY = "test-reports-list-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


class ReportListProjectionTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    # -- helpers ---------------------------------------------------------------

    def _seed_source_runs(self, count: int) -> list[str]:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService
        from lifecycle_helpers import finish_run

        service = RunService()
        ids: list[str] = []
        for _index in range(count):
            run = service.create_job_run(
                JobCreateRequest(
                    project_id="demo-project",
                    site_id="demo-site",
                    job_type="udmi_validation",
                    parameters={},
                ),
                expected_job_type="udmi_validation",
            )
            finish_run(service, run.run_id)
            ids.append(run.run_id)
        return ids

    def _create_report(self, source_run_ids: list[str]) -> dict:
        """POST a report scoped to source runs that exist in this project/site."""
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "evidence_pack",
                "output_format": "zip",
                "source_run_ids": source_run_ids,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _list_reports(self) -> list[dict]:
        response = self.client.get("/api/v1/reports")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["reports"]

    def _find(self, reports: list[dict], report_id: str) -> dict:
        matches = [report for report in reports if report["report_id"] == report_id]
        self.assertEqual(len(matches), 1, f"{report_id} not found exactly once in {reports}")
        return matches[0]

    # -- tests -----------------------------------------------------------------

    def test_create_report_returns_created_at_and_source_run_ids(self) -> None:
        source_ids = self._seed_source_runs(2)
        body = self._create_report(source_ids)

        self.assertEqual(body["source_run_ids"], source_ids)
        # Parseable ISO 8601 (what the frontend's Date.parse consumes).
        self.assertIsInstance(
            datetime.fromisoformat(body["created_at"]),
            datetime,
        )

    def test_list_projection_matches_the_creation_projection(self) -> None:
        created = self._create_report(self._seed_source_runs(2))

        listed = self._find(self._list_reports(), created["report_id"])

        # The two construction sites must agree: a report's creation instant and
        # scoped runs cannot change between the POST response and the list.
        self.assertEqual(listed["created_at"], created["created_at"])
        self.assertEqual(listed["source_run_ids"], created["source_run_ids"])

    def test_single_get_projection_matches_the_creation_projection(self) -> None:
        source_ids = self._seed_source_runs(1)
        created = self._create_report(source_ids)

        response = self.client.get(f"/api/v1/reports/{created['report_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["created_at"], created["created_at"])
        self.assertEqual(body["source_run_ids"], source_ids)

    def test_report_with_no_source_runs_lists_an_empty_list(self) -> None:
        created = self._create_report([])

        listed = self._find(self._list_reports(), created["report_id"])

        # Empty, not absent and not null: the UI renders "—" off an empty list.
        self.assertEqual(created["source_run_ids"], [])
        self.assertEqual(listed["source_run_ids"], [])
        self.assertTrue(listed["created_at"])

    def test_order_of_source_run_ids_is_preserved(self) -> None:
        source_ids = self._seed_source_runs(3)
        requested_order = [source_ids[2], source_ids[0], source_ids[1]]
        created = self._create_report(requested_order)

        listed = self._find(self._list_reports(), created["report_id"])

        # Scoping order is the operator's; the projection must not sort it.
        self.assertEqual(listed["source_run_ids"], requested_order)

    def test_report_list_honours_a_bounded_page_limit_and_offset(self) -> None:
        baseline_total = self.client.get("/api/v1/reports").json()["total"]
        first = self._create_report([])
        second = self._create_report([])
        third = self._create_report([])

        limited = self.client.get("/api/v1/reports?limit=2&offset=1")
        self.assertEqual(limited.status_code, 200, limited.text)
        listed = limited.json()["reports"]
        self.assertEqual(len(listed), 2)
        self.assertEqual(
            [row["report_id"] for row in listed],
            [second["report_id"], first["report_id"]],
        )
        self.assertNotIn(third["report_id"], {row["report_id"] for row in listed})
        body = limited.json()
        self.assertEqual(body["total"], baseline_total + 3)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 1)
        self.assertEqual(body["has_more"], 1 + 2 < baseline_total + 3)

    def test_report_list_rejects_an_unbounded_limit(self) -> None:
        response = self.client.get("/api/v1/reports?limit=101")
        self.assertEqual(response.status_code, 422, response.text)

    def test_delete_one_report_removes_its_row_and_owned_local_artifact(self) -> None:
        from app.core.runtime import ARTIFACTS_ROOT
        from app.services.run_service import RunService

        created = self._create_report(self._seed_source_runs(1))
        stored = RunService().get_run(created["report_id"])
        manifest = stored.result_summary["artifact_manifest"]
        artifact = Path(ARTIFACTS_ROOT) / manifest["artifact_relpath"]
        self.assertTrue(artifact.is_file())

        response = self.client.post(
            "/api/v1/reports/delete",
            json={"report_ids": [created["report_id"]]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "deleted_report_ids": [created["report_id"]],
                "deleted_count": 1,
                "artifact_cleanup_warnings": [],
            },
        )
        self.assertFalse(artifact.exists())
        self.assertEqual(
            self.client.get(f"/api/v1/reports/{created['report_id']}").status_code,
            404,
        )

    def test_delete_reports_preserves_their_source_runs(self) -> None:
        source_id = self._seed_source_runs(1)[0]
        created = self._create_report([source_id])

        response = self.client.post(
            "/api/v1/reports/delete",
            json={"report_ids": [created["report_id"]]},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.client.get(f"/api/v1/validation/runs/{source_id}").status_code,
            200,
        )

    def test_delete_reports_deduplicates_and_removes_a_validated_batch(self) -> None:
        first = self._create_report([])
        second = self._create_report([])

        response = self.client.post(
            "/api/v1/reports/delete",
            json={
                "report_ids": [
                    second["report_id"],
                    first["report_id"],
                    second["report_id"],
                ]
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["deleted_report_ids"],
            [second["report_id"], first["report_id"]],
        )
        self.assertEqual(response.json()["deleted_count"], 2)
        remaining_ids = {report["report_id"] for report in self._list_reports()}
        self.assertNotIn(first["report_id"], remaining_ids)
        self.assertNotIn(second["report_id"], remaining_ids)

    def test_invalid_mixed_delete_batch_is_atomic_and_cannot_delete_a_source_run(self) -> None:
        source_id = self._seed_source_runs(1)[0]
        first = self._create_report([source_id])
        second = self._create_report([source_id])

        response = self.client.post(
            "/api/v1/reports/delete",
            json={"report_ids": [first["report_id"], source_id, second["report_id"]]},
        )

        self.assertEqual(response.status_code, 404, response.text)
        remaining_ids = {report["report_id"] for report in self._list_reports()}
        self.assertIn(first["report_id"], remaining_ids)
        self.assertIn(second["report_id"], remaining_ids)
        self.assertEqual(
            self.client.get(f"/api/v1/validation/runs/{source_id}").status_code,
            200,
        )

    def test_artifact_cleanup_rejects_nested_content_addressed_paths(self) -> None:
        from app.core.runtime import ARTIFACTS_ROOT
        from app.services.report_artifacts import delete_report_artifact

        shared = Path(ARTIFACTS_ROOT) / "sha256" / "aa" / ("a" * 64)
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_bytes(b"shared-report-evidence")

        with self.assertRaisesRegex(RuntimeError, "path is invalid"):
            delete_report_artifact(
                {
                    "report_id": "report-1",
                    "artifact_relpath": shared.relative_to(ARTIFACTS_ROOT).as_posix(),
                },
                report_id="report-1",
            )

        self.assertTrue(shared.is_file())

    def test_create_delete_interleaving_cleans_unpublished_artifact(self) -> None:
        """A delete between artifact write and manifest publish leaves no bytes."""

        from app.api.routes import reports as reports_route
        from app.core.runtime import ARTIFACTS_ROOT

        original_complete = reports_route.service.complete_report_run
        written_artifact: Path | None = None

        def delete_then_complete(run_id: str, manifest: dict[str, object]):
            nonlocal written_artifact
            written_artifact = Path(ARTIFACTS_ROOT) / str(manifest["artifact_relpath"])
            self.assertTrue(written_artifact.is_file())
            reports_route.service.delete_report_runs([run_id])
            return original_complete(run_id, manifest)

        with patch.object(
            reports_route.service,
            "complete_report_run",
            side_effect=delete_then_complete,
        ):
            response = self.client.post(
                "/api/v1/reports",
                json={
                    "project_id": "demo-project",
                    "site_id": "demo-site",
                    "report_type": "evidence_pack",
                    "output_format": "zip",
                    "source_run_ids": [],
                },
            )

        self.assertEqual(response.status_code, 500, response.text)
        self.assertIsNotNone(written_artifact)
        self.assertFalse(written_artifact.exists())

    def test_artifact_cleanup_unlinks_same_root_symlink_not_its_target(self) -> None:
        from app.core.runtime import ARTIFACTS_ROOT
        from app.services.report_artifacts import delete_report_artifact

        root = Path(ARTIFACTS_ROOT)
        target = root / "another-report-owned.zip"
        link = root / "report-link-owned.zip"
        target.write_bytes(b"other report bytes")
        try:
            link.symlink_to(target.name)
        except OSError as error:
            self.skipTest(f"File symlinks are unavailable: {error}")

        removed = delete_report_artifact(
            {
                "report_id": "report-link",
                "artifact_relpath": link.name,
            },
            report_id="report-link",
        )

        self.assertTrue(removed)
        self.assertFalse(link.is_symlink())
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"other report bytes")

    def test_artifact_cleanup_removes_dangling_same_root_symlink(self) -> None:
        from app.core.runtime import ARTIFACTS_ROOT
        from app.services.report_artifacts import delete_report_artifact

        root = Path(ARTIFACTS_ROOT)
        link = root / "report-dangling-owned.zip"
        try:
            link.symlink_to("missing-report-artifact.zip")
        except OSError as error:
            self.skipTest(f"File symlinks are unavailable: {error}")

        removed = delete_report_artifact(
            {
                "report_id": "report-dangling",
                "artifact_relpath": link.name,
            },
            report_id="report-dangling",
        )

        self.assertTrue(removed)
        self.assertFalse(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
