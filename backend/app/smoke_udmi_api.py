import os
import tempfile


def main() -> None:
    os.environ["JOB_EXECUTION_MODE"] = "inline"

    with tempfile.TemporaryDirectory(prefix="smart-commissioning-runs-") as runs_root:
        os.environ["SMART_COMMISSIONING_RUNS_ROOT"] = runs_root

        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        create_response = client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "smoke-project",
                "site_id": "smoke-site",
                "job_type": "udmi_validation",
                "parameters": {"requested_from": "api-smoke"},
            },
        )
        create_response.raise_for_status()
        accepted = create_response.json()
        run_id = accepted["run_id"]

        run_response = client.get(f"/api/v1/validation/runs/{run_id}")
        run_response.raise_for_status()
        run = run_response.json()
        assert run["status"] == "succeeded", run
        assert run["result_summary"]["expected_devices"] == 35, run
        assert run["result_summary"]["issue_count"] > 0, run

        issues_response = client.get(f"/api/v1/validation/runs/{run_id}/issues")
        issues_response.raise_for_status()
        issues = issues_response.json()["issues"]
        assert issues, "Expected normalized UDMI issues from fixture."
        assert {"issue_id", "asset_id", "issue_type", "severity", "description"} <= set(issues[0])

        print(
            f"UDMI API smoke passed: {run_id}, "
            f"{run['result_summary']['issue_count']} issues."
        )


if __name__ == "__main__":
    main()
