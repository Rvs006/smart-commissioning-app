"""RBAC enforcement across the data/mutation routes + runs edge attribution.

Boots the FastAPI app in api_key auth mode against a temporary SQLite database,
using the same harness as test_rbac.py / test_runs_api.py: the env vars (incl.
the shared SCT_TEST_DATABASE_URL) are set and the settings/engine caches cleared
in setUpClass BEFORE app.main is imported, and the TestClient is entered as a
context manager so the startup lifespan applies the Alembic migrations.

The shared bootstrap API key authenticates as a synthetic ADMIN principal, so it
is used both to provision per-role users (POST /users) and as the admin actor in
the back-compat assertions. Per-role users get a one-time plaintext key from the
create response, which is then presented via X-API-Key.

What is covered here (HONESTY: no live Postgres/Redis/network — inline mode, tmp
SQLite, in-process TestClient):

  * viewer can GET /runs but is 403 creating a validation run;
  * reviewer behaves like viewer for now (read yes, mutate no);
  * engineer can create a run and cancel it, but is 403 on retention APPLY and on
    POST /users (user management is admin-only);
  * admin can apply retention and manage users;
  * the legacy shared key (ADMIN) still does everything (back-compat), so the
    existing admin-keyed tests keep passing;
  * GET /runs returns each summary's edge_id and filters by edge_id and status.
"""

import unittest

from harness import ApiTestCase

_SHARED_KEY = "test-rbac-enforcement-shared-admin-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _SHARED_KEY,
}

# A scan/publish authorization consent is orthogonal to RBAC; supply it so a
# permitted (engineer/admin) caller's run is not separately blocked by the
# safety gate. RBAC is WHO may act; this is the safety consent — both must hold.
_AUTH_PARAMS = {"authorized": True}


class RbacEnforcementTests(ApiTestCase):
    # No default header: each request chooses its own actor (shared admin key
    # or a per-user role key) explicitly.
    env = _ENV_OVERRIDES

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        # Per-role user keys, provisioned once via the shared admin key.
        cls._role_keys = {
            role: cls._provision_user(f"enf-{role}", role)
            for role in ("viewer", "reviewer", "engineer", "admin")
        }

    # -- helpers --------------------------------------------------------------

    @classmethod
    def _admin_headers(cls) -> dict[str, str]:
        return {"X-API-Key": _SHARED_KEY}

    @classmethod
    def _provision_user(cls, username: str, role: str) -> str:
        response = cls.client.post(
            "/api/v1/users",
            headers=cls._admin_headers(),
            json={"username": username, "role": role},
        )
        assert response.status_code == 201, response.text
        return response.json()["api_key"]

    def _headers(self, role: str) -> dict[str, str]:
        return {"X-API-Key": self._role_keys[role]}

    def _create_validation_run(self, headers: dict[str, str]):
        return self.client.post(
            "/api/v1/validation/udmi/runs",
            headers=headers,
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": "udmi_validation",
                "parameters": {"requested_from": "test_rbac_enforcement", **_AUTH_PARAMS},
            },
        )

    # -- viewer: read yes, mutate no ------------------------------------------

    def test_viewer_can_list_runs(self) -> None:
        response = self.client.get("/api/v1/runs", headers=self._headers("viewer"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("runs", response.json())

    def test_viewer_can_read_configuration(self) -> None:
        response = self.client.get("/api/v1/configuration", headers=self._headers("viewer"))
        self.assertEqual(response.status_code, 200, response.text)

    def test_viewer_is_403_creating_validation_run(self) -> None:
        response = self._create_validation_run(self._headers("viewer"))
        self.assertEqual(response.status_code, 403, response.text)
        detail = response.json()["detail"]
        self.assertIn("engineer", detail)
        # The 403 leaks neither the caller's own role nor their key.
        self.assertNotIn("viewer", detail)
        self.assertNotIn(self._role_keys["viewer"], response.text)

    def test_viewer_is_403_putting_configuration(self) -> None:
        snapshot = self.client.get("/api/v1/configuration", headers=self._headers("viewer")).json()
        response = self.client.put(
            "/api/v1/configuration", headers=self._headers("viewer"), json=snapshot
        )
        self.assertEqual(response.status_code, 403, response.text)

    # -- reviewer: same as viewer for now -------------------------------------

    def test_reviewer_can_list_runs_but_not_create(self) -> None:
        listed = self.client.get("/api/v1/runs", headers=self._headers("reviewer"))
        self.assertEqual(listed.status_code, 200, listed.text)
        created = self._create_validation_run(self._headers("reviewer"))
        self.assertEqual(created.status_code, 403, created.text)

    # -- engineer: create + cancel a run; blocked on admin-only ---------------

    def test_engineer_can_create_and_cancel_run(self) -> None:
        created = self._create_validation_run(self._headers("engineer"))
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run_id"]

        cancel = self.client.post(
            f"/api/v1/runs/{run_id}/cancel", headers=self._headers("engineer")
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["run_id"], run_id)

    def test_engineer_can_generate_report(self) -> None:
        response = self.client.post(
            "/api/v1/reports",
            headers=self._headers("engineer"),
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "evidence_pack",
                "output_format": "zip",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_engineer_is_403_on_retention_apply(self) -> None:
        response = self.client.post(
            "/api/v1/evidence/retention/apply",
            headers=self._headers("engineer"),
            json={"keep_days": 30, "confirm": True, "acknowledge": "DELETE"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("admin", response.json()["detail"])

    def test_engineer_can_preview_retention(self) -> None:
        response = self.client.post(
            "/api/v1/evidence/retention/preview",
            headers=self._headers("engineer"),
            json={"keep_days": 30},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_engineer_is_403_creating_users(self) -> None:
        response = self.client.post(
            "/api/v1/users",
            headers=self._headers("engineer"),
            json={"username": "eng-should-not-create", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    # -- configuration export-with-secrets / import (ITEM-1) ------------------

    _TRANSFER_PARAMS = {"project_id": "rbac-transfer-project", "site_id": "rbac-transfer-site"}

    def test_viewer_is_403_on_export_with_secrets(self) -> None:
        response = self.client.get(
            "/api/v1/configuration/export-with-secrets",
            headers=self._headers("viewer"),
            params=self._TRANSFER_PARAMS,
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_engineer_can_export_with_secrets_and_reimport(self) -> None:
        exported = self.client.get(
            "/api/v1/configuration/export-with-secrets",
            headers=self._headers("engineer"),
            params=self._TRANSFER_PARAMS,
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        envelope = exported.json()
        self.assertEqual(envelope["version"], 2)
        self.assertTrue(envelope["secrets_included"])

        imported = self.client.post(
            "/api/v1/configuration/import",
            headers=self._headers("engineer"),
            params=self._TRANSFER_PARAMS,
            json={
                "configuration": envelope["configuration"],
                "secret_material": envelope["secret_material"],
            },
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertIn("mqtt", imported.json())

    def test_viewer_is_403_on_import(self) -> None:
        envelope = self.client.get(
            "/api/v1/configuration/export-with-secrets",
            headers=self._headers("engineer"),
            params=self._TRANSFER_PARAMS,
        ).json()
        response = self.client.post(
            "/api/v1/configuration/import",
            headers=self._headers("viewer"),
            params=self._TRANSFER_PARAMS,
            json={"configuration": envelope["configuration"]},
        )
        self.assertEqual(response.status_code, 403, response.text)

    # -- admin: retention apply + user management -----------------------------

    def test_admin_user_can_apply_retention(self) -> None:
        response = self.client.post(
            "/api/v1/evidence/retention/apply",
            headers=self._headers("admin"),
            # keep_days far in the future cutoff is fine; nothing eligible is the
            # normal empty-purge outcome. We assert authorization, not deletion.
            json={"keep_days": 3650, "confirm": True, "acknowledge": "DELETE"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_admin_user_can_manage_users(self) -> None:
        response = self.client.post(
            "/api/v1/users",
            headers=self._headers("admin"),
            json={"username": "made-by-enf-admin", "role": "reviewer"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["user"]["role"], "reviewer")

    # -- back-compat: the legacy shared key (ADMIN) still does everything ------

    def test_shared_key_can_create_run_cancel_apply_retention_and_manage_users(self) -> None:
        created = self._create_validation_run(self._admin_headers())
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run_id"]

        cancel = self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=self._admin_headers())
        self.assertEqual(cancel.status_code, 200, cancel.text)

        retention = self.client.post(
            "/api/v1/evidence/retention/apply",
            headers=self._admin_headers(),
            json={"keep_days": 3650, "confirm": True, "acknowledge": "DELETE"},
        )
        self.assertEqual(retention.status_code, 200, retention.text)

        user = self.client.post(
            "/api/v1/users",
            headers=self._admin_headers(),
            json={"username": "made-by-shared-key", "role": "viewer"},
        )
        self.assertEqual(user.status_code, 201, user.text)

    # -- unauthenticated still 401 (gates do not mask the auth failure) --------

    def test_no_key_is_401_not_403(self) -> None:
        self.assertEqual(self.client.get("/api/v1/runs").status_code, 401)
        self.assertEqual(self._create_validation_run({}).status_code, 401)

    # -- runs list: edge attribution + filters --------------------------------

    def test_runs_list_exposes_edge_id_field(self) -> None:
        # Create a run so the list is non-empty, then assert every summary carries
        # the additive edge_id key (null for a locally created run is acceptable).
        created = self._create_validation_run(self._admin_headers())
        self.assertEqual(created.status_code, 200, created.text)
        listed = self.client.get("/api/v1/runs", headers=self._headers("viewer"))
        self.assertEqual(listed.status_code, 200, listed.text)
        runs = listed.json()["runs"]
        self.assertTrue(runs, "expected at least one run in the list")
        for summary in runs:
            self.assertIn("edge_id", summary)

    def test_runs_list_filters_by_edge_id_and_status(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.models import Run
        from sqlalchemy import update

        created = self._create_validation_run(self._admin_headers())
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run_id"]

        # Stamp a synthetic originating edge id directly on the run row so we can
        # assert the backend route's edge_id filter (no live hub/bundle needed).
        edge_id = "edge-test-filter-abc123"
        with get_engine().begin() as connection:
            connection.execute(update(Run).where(Run.id == run_id).values(edge_id=edge_id))

        # Filter by the synthetic edge_id: only the stamped run comes back.
        by_edge = self.client.get(
            "/api/v1/runs", headers=self._headers("viewer"), params={"edge_id": edge_id}
        )
        self.assertEqual(by_edge.status_code, 200, by_edge.text)
        returned = by_edge.json()["runs"]
        self.assertEqual([r["run_id"] for r in returned], [run_id])
        self.assertEqual(returned[0]["edge_id"], edge_id)

        # A non-matching edge_id yields no runs.
        none = self.client.get(
            "/api/v1/runs", headers=self._headers("viewer"), params={"edge_id": "no-such-edge"}
        )
        self.assertEqual(none.json()["runs"], [])

        # Status filter: the inline run succeeded, so status=succeeded includes it
        # and a status the run is not in (queued) excludes it.
        succeeded = self.client.get(
            "/api/v1/runs",
            headers=self._headers("viewer"),
            params={"edge_id": edge_id, "status": "succeeded"},
        )
        self.assertEqual([r["run_id"] for r in succeeded.json()["runs"]], [run_id])

        not_queued = self.client.get(
            "/api/v1/runs",
            headers=self._headers("viewer"),
            params={"edge_id": edge_id, "status": "queued"},
        )
        self.assertEqual(not_queued.json()["runs"], [])

    def test_runs_status_filter_rejects_unknown_value(self) -> None:
        # The status query param is the JobStatus literal, so an unknown value is
        # a 422 (validation) rather than silently returning everything.
        response = self.client.get(
            "/api/v1/runs", headers=self._headers("viewer"), params={"status": "not-a-status"}
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
