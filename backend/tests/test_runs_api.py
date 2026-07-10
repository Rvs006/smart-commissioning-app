"""API tests for the database-backed run/configuration persistence.

Runs the FastAPI app against a temporary SQLite database via the shared
harness.ApiTestCase: DATABASE_URL is set before app.main is imported, and the
TestClient is entered as a context manager so the startup lifespan applies
the Alembic migrations.

The app runs in api_key auth mode here, exercising the authenticated path on
every request (auth-specific behavior is covered in test_auth.py).

The database is shared per process (see harness.shared_test_database_url):
route modules instantiate their services -- and therefore the SQLAlchemy
engine -- at the first app.main import, so every test class in the test run
must point at the same database file.
"""

import unittest

from harness import ApiTestCase

_API_KEY = "test-runs-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


class RunsApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _create_udmi_run(self) -> dict:
        response = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": "udmi_validation",
                "parameters": {"requested_from": "test_runs_api"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_udmi_fixture_run_inline_then_poll_status(self) -> None:
        accepted = self._create_udmi_run()
        self.assertEqual(accepted["status"], "succeeded", "inline mode processes synchronously")

        run_response = self.client.get(f"/api/v1/validation/runs/{accepted['run_id']}")
        self.assertEqual(run_response.status_code, 200, run_response.text)
        run = run_response.json()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["progress_percent"], 100)
        self.assertEqual(run["project_id"], "demo-project")
        self.assertEqual(run["site_id"], "demo-site")
        self.assertEqual(run["result_summary"]["execution_mode"], "inline_local_fallback")
        self.assertGreater(run["result_summary"]["issue_count"], 0)
        self.assertEqual(len(run["issues"]), run["result_summary"]["issue_count"])

    def test_missing_run_returns_404(self) -> None:
        response = self.client.get("/api/v1/validation/runs/run_00000000000000_deadbeef")
        self.assertEqual(response.status_code, 404)

    def test_list_runs_endpoint_filters_and_paginates(self) -> None:
        run_id = self._create_udmi_run()["run_id"]

        listed = self.client.get("/api/v1/runs")
        self.assertEqual(listed.status_code, 200, listed.text)
        runs = listed.json()["runs"]
        self.assertIn(run_id, [run["run_id"] for run in runs])
        first = runs[0]
        self.assertEqual(
            set(first),
            {
                "run_id",
                "job_type",
                "status",
                "stage",
                "progress_percent",
                "created_at",
                "updated_at",
                # Additive edge attribution for the multi-project hub; null for a
                # locally created run.
                "edge_id",
            },
            "list endpoint returns run summaries",
        )

        filtered = self.client.get("/api/v1/runs", params={"job_type": "udmi_validation"})
        self.assertIn(run_id, [run["run_id"] for run in filtered.json()["runs"]])

        other_type = self.client.get("/api/v1/runs", params={"job_type": "ip_discovery"})
        self.assertNotIn(run_id, [run["run_id"] for run in other_type.json()["runs"]])

        other_project = self.client.get("/api/v1/runs", params={"project_id": "another-project"})
        self.assertEqual(other_project.json()["runs"], [])

        limited = self.client.get("/api/v1/runs", params={"limit": 1})
        self.assertEqual(len(limited.json()["runs"]), 1)

        self.assertEqual(self.client.get("/api/v1/runs", params={"limit": 300}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/runs", params={"offset": -1}).status_code, 422)

    def test_configuration_put_get_roundtrip_with_versioning(self) -> None:
        seeded = self.client.get("/api/v1/configuration")
        self.assertEqual(seeded.status_code, 200, seeded.text)
        configuration = seeded.json()
        self.assertEqual(configuration["mqtt"]["values"]["Port"], "8883")

        configuration["mqtt"]["values"]["Port"] = "1883"
        first_put = self.client.put("/api/v1/configuration", json=configuration)
        self.assertEqual(first_put.status_code, 200, first_put.text)

        configuration["mqtt"]["values"]["Port"] = "8884"
        second_put = self.client.put("/api/v1/configuration", json=configuration)
        self.assertEqual(second_put.status_code, 200, second_put.text)

        current = self.client.get("/api/v1/configuration").json()
        self.assertEqual(
            current["mqtt"]["values"]["Port"],
            "8884",
            "GET must return the highest configuration version",
        )

        other_site = self.client.get("/api/v1/configuration", params={"site_id": "another-site"}).json()
        self.assertEqual(
            other_site["mqtt"]["values"]["Port"],
            "8883",
            "another site seeds its own default configuration",
        )

        invalid = dict(configuration)
        invalid["mqtt"] = {"values": {**configuration["mqtt"]["values"], "Port": "not-a-port"}, "status": "Connected"}
        rejected = self.client.put("/api/v1/configuration", json=invalid)
        self.assertEqual(rejected.status_code, 400)

    def test_configuration_put_rejects_malformed_mqtt_use_tls(self) -> None:
        configuration = self.client.get("/api/v1/configuration").json()
        configuration["mqtt"]["values"]["Use TLS"] = "Maybe"

        rejected = self.client.put("/api/v1/configuration", json=configuration)

        self.assertEqual(rejected.status_code, 400)
        self.assertIn("MQTT Use TLS must be Enabled or Disabled.", rejected.json()["detail"])


if __name__ == "__main__":
    unittest.main()
