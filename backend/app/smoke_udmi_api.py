import os
import tempfile
from pathlib import Path

# Exercise the authenticated path: the smoke run uses api_key mode with a
# throwaway key presented via the X-API-Key header on every request.
SMOKE_API_KEY = "smoke-udmi-api-key"


def main() -> None:
    os.environ["JOB_EXECUTION_MODE"] = "inline"
    os.environ["AUTH_MODE"] = "api_key"
    os.environ["API_KEY"] = SMOKE_API_KEY

    with tempfile.TemporaryDirectory(prefix="smart-commissioning-db-") as runtime_root:
        database_path = (Path(runtime_root) / "smart_commissioning.db").as_posix()
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path}"

        from fastapi.testclient import TestClient

        from app.core.db import get_engine
        from app.main import app

        # Context manager so the startup lifespan applies the migrations.
        with TestClient(app, headers={"X-API-Key": SMOKE_API_KEY}) as client:
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

        # Release SQLite file handles so the temporary directory can be removed.
        get_engine().dispose()


if __name__ == "__main__":
    main()
