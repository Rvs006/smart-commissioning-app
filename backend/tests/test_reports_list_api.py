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

    def _create_report(self, source_run_ids: list[str]) -> dict:
        """POST a report. Report creation does not validate that the source runs
        exist (verified in create_report_run), so these ids need no seeded runs
        -- the projection under test copies them through verbatim."""
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
        body = self._create_report(["run-a", "run-b"])

        self.assertEqual(body["source_run_ids"], ["run-a", "run-b"])
        # Parseable ISO 8601 (what the frontend's Date.parse consumes).
        self.assertIsInstance(
            datetime.fromisoformat(body["created_at"]),
            datetime,
        )

    def test_list_projection_matches_the_creation_projection(self) -> None:
        created = self._create_report(["run-a", "run-b"])

        listed = self._find(self._list_reports(), created["report_id"])

        # The two construction sites must agree: a report's creation instant and
        # scoped runs cannot change between the POST response and the list.
        self.assertEqual(listed["created_at"], created["created_at"])
        self.assertEqual(listed["source_run_ids"], created["source_run_ids"])

    def test_single_get_projection_matches_the_creation_projection(self) -> None:
        created = self._create_report(["run-a"])

        response = self.client.get(f"/api/v1/reports/{created['report_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["created_at"], created["created_at"])
        self.assertEqual(body["source_run_ids"], ["run-a"])

    def test_report_with_no_source_runs_lists_an_empty_list(self) -> None:
        created = self._create_report([])

        listed = self._find(self._list_reports(), created["report_id"])

        # Empty, not absent and not null: the UI renders "—" off an empty list.
        self.assertEqual(created["source_run_ids"], [])
        self.assertEqual(listed["source_run_ids"], [])
        self.assertTrue(listed["created_at"])

    def test_order_of_source_run_ids_is_preserved(self) -> None:
        created = self._create_report(["run-z", "run-a", "run-m"])

        listed = self._find(self._list_reports(), created["report_id"])

        # Scoping order is the operator's; the projection must not sort it.
        self.assertEqual(listed["source_run_ids"], ["run-z", "run-a", "run-m"])


if __name__ == "__main__":
    unittest.main()
</content>
