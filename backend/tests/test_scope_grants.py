"""Project/site grant lifecycle and central scoped-loader contracts."""

from __future__ import annotations

import uuid

from app.core.auth import AuthPrincipal, hash_api_key
from app.core.db import get_engine
from app.core.scopes import (
    ScopeGrantRepository,
    load_scoped_import,
    load_scoped_report,
    load_scoped_run,
    require_project_site_access,
)
from fastapi import HTTPException
from harness import ApiTestCase
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.repositories import ImportRepository, UserRepository
from smart_commissioning_core.rbac import Role


def _suffix() -> str:
    return uuid.uuid4().hex[:10]


class ScopeGrantApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "local",
        "API_KEY": None,
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
    }

    def setUp(self) -> None:
        suffix = _suffix()
        self.project_id = f"grant-project-{suffix}"
        self.site_id = f"grant-site-{suffix}"
        self.foreign_project_id = f"foreign-project-{suffix}"
        self.foreign_site_id = f"foreign-site-{suffix}"
        store = DbRunStore(get_engine())
        self.run = store.create_run(
            project_id=self.project_id,
            site_id=self.site_id,
            job_type="ip_discovery",
            parameters={},
        )
        self.foreign_run = store.create_run(
            project_id=self.foreign_project_id,
            site_id=self.foreign_site_id,
            job_type="ip_discovery",
            parameters={},
        )
        report = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "report_type": "evidence_pack",
                "output_format": "zip",
                "source_run_ids": [],
            },
        )
        self.assertEqual(report.status_code, 200, report.text)
        self.report = {"run_id": report.json()["report_id"]}
        self.import_record = ImportRepository(get_engine()).create(
            import_id=f"imp_{suffix}",
            import_type="ip_register",
            original_filename="register.csv",
            stored_file_path="protected/register.csv",
            summary={"import_id": f"imp_{suffix}", "accepted": 0, "rejected": 0},
            project_id=self.project_id,
            site_id=self.site_id,
        )
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"viewer-{suffix}", "role": "viewer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.user = created.json()["user"]
        self.user_key = created.json()["api_key"]

    @property
    def user_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.user_key}

    def test_grant_is_explicit_visible_in_me_and_revocable(self) -> None:
        before = self.client.get("/api/v1/me", headers=self.user_headers)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertFalse(before.json()["global_scope"])
        self.assertEqual(before.json()["effective_scopes"], [])

        granted = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Commissioning shift assignment",
            },
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        body = granted.json()
        self.assertTrue(body["active"])
        self.assertEqual(body["granted_by"], "local")
        self.assertEqual(body["reason"], "Commissioning shift assignment")

        duplicate = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Duplicate",
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        after = self.client.get("/api/v1/me", headers=self.user_headers)
        self.assertEqual(
            after.json()["effective_scopes"],
            [{"project_id": self.project_id, "site_id": self.site_id}],
        )

        revoked = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants/{body['grant_id']}/revoke",
            json={"reason": "Shift completed"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertFalse(revoked.json()["active"])
        self.assertEqual(revoked.json()["revoked_by"], "local")
        self.assertEqual(revoked.json()["revoke_reason"], "Shift completed")
        self.assertEqual(
            self.client.get("/api/v1/me", headers=self.user_headers).json()["effective_scopes"],
            [],
        )

        regranted = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Second commissioning shift",
            },
        )
        self.assertEqual(regranted.status_code, 201, regranted.text)
        self.assertNotEqual(regranted.json()["grant_id"], body["grant_id"])

        history = self.client.get(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            params={"include_revoked": True},
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertCountEqual(
            [item["grant_id"] for item in history.json()],
            [body["grant_id"], regranted.json()["grant_id"]],
        )

    def test_non_admin_cannot_manage_grants_and_unknown_scope_is_404(self) -> None:
        denied = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            headers=self.user_headers,
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Self grant",
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        missing = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            json={
                "project_id": "missing-project",
                "site_id": "missing-site",
                "reason": "Invalid target",
            },
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_scope_activation_preflight_reports_unscoped_active_user(self) -> None:
        denied = self.client.get(
            "/api/v1/users/scope-activation-preflight",
            headers=self.user_headers,
        )
        self.assertEqual(denied.status_code, 403, denied.text)

        preflight = self.client.get("/api/v1/users/scope-activation-preflight")
        self.assertEqual(preflight.status_code, 200, preflight.text)
        body = preflight.json()
        self.assertFalse(body["ready"])
        self.assertIn(
            self.user["id"],
            {user["id"] for user in body["unscoped_active_non_admin_users"]},
        )

    def test_scope_activation_preflight_becomes_ready_without_mutable_flag(self) -> None:
        suffix = _suffix()
        initial = self.client.get("/api/v1/users/scope-activation-preflight")
        self.assertEqual(initial.status_code, 200, initial.text)

        for user in initial.json()["unscoped_active_non_admin_users"]:
            grant = self.client.post(
                f"/api/v1/users/{user['id']}/scope-grants",
                json={
                    "project_id": self.project_id,
                    "site_id": self.site_id,
                    "reason": "Scope-activation preflight fixture",
                },
            )
            self.assertEqual(grant.status_code, 201, grant.text)

        named_admin = self.client.post(
            "/api/v1/users",
            json={"username": f"preflight-admin-{suffix}", "role": "admin"},
        )
        self.assertEqual(named_admin.status_code, 201, named_admin.text)
        named_headers = {"X-API-Key": named_admin.json()["api_key"]}

        ready = self.client.get(
            "/api/v1/users/scope-activation-preflight",
            headers=named_headers,
        )
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertTrue(ready.json()["ready"])
        self.assertGreaterEqual(ready.json()["active_named_admin_count"], 1)
        self.assertEqual(ready.json()["unscoped_active_non_admin_users"], [])

    def test_preflight_requires_an_active_named_admin_after_grants_are_complete(self) -> None:
        from smart_commissioning_core.db.base import Base
        from smart_commissioning_core.db.engine import create_engine_from_url

        engine = create_engine_from_url("sqlite://")
        try:
            Base.metadata.create_all(engine)
            store = DbRunStore(engine)
            run = store.create_run(
                project_id="isolated-preflight-project",
                site_id="isolated-preflight-site",
                job_type="ip_discovery",
                parameters={},
            )
            users = UserRepository(engine)
            viewer_id = str(uuid.uuid4())
            users.create_user(
                user_id=viewer_id,
                username="isolated-preflight-viewer",
                role=Role.VIEWER.value,
                api_key_hash=hash_api_key("isolated-preflight-viewer-key"),
            )
            repository = ScopeGrantRepository(engine)
            blocked = repository.activation_preflight()
            self.assertFalse(blocked["ready"])
            self.assertEqual(blocked["active_named_admin_count"], 0)
            self.assertEqual(
                [user["id"] for user in blocked["unscoped_active_non_admin_users"]],
                [viewer_id],
            )

            repository.create(
                user_id=viewer_id,
                project_id=run["project_id"],
                site_id=run["site_id"],
                reason="Isolated preflight fixture",
                principal=AuthPrincipal(None, "local", Role.ADMIN, "local"),
            )
            missing_admin = repository.activation_preflight()
            self.assertFalse(missing_admin["ready"])
            self.assertEqual(missing_admin["unscoped_active_non_admin_users"], [])

            users.create_user(
                user_id=str(uuid.uuid4()),
                username="isolated-preflight-admin",
                role=Role.ADMIN.value,
                api_key_hash=hash_api_key("isolated-preflight-admin-key"),
            )
            ready = repository.activation_preflight()
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["active_named_admin_count"], 1)
        finally:
            engine.dispose()

    def test_scoped_loaders_make_missing_and_foreign_ids_indistinguishable(self) -> None:
        grant = self.client.post(
            f"/api/v1/users/{self.user['id']}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Loader contract",
            },
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        principal = AuthPrincipal(
            user_id=self.user["id"],
            username=self.user["username"],
            role=Role.VIEWER,
            source="user_key",
        )

        self.assertEqual(
            load_scoped_run(self.run["run_id"], principal).project_id,
            self.project_id,
        )
        self.assertEqual(
            load_scoped_import(self.import_record["import_id"], principal).site_id,
            self.site_id,
        )
        self.assertEqual(
            load_scoped_report(self.report["run_id"], principal).resource_id,
            self.report["run_id"],
        )

        details: list[str] = []
        for run_id in (self.foreign_run["run_id"], "run_missing"):
            with self.assertRaises(HTTPException) as caught:
                load_scoped_run(run_id, principal)
            self.assertEqual(caught.exception.status_code, 404)
            details.append(str(caught.exception.detail))
        self.assertEqual(details[0], details[1])

        with self.assertRaises(HTTPException) as wrong_kind:
            load_scoped_report(self.run["run_id"], principal)
        self.assertEqual(wrong_kind.exception.status_code, 404)

        deactivated = self.client.post(f"/api/v1/users/{self.user['id']}/deactivate")
        self.assertEqual(deactivated.status_code, 200, deactivated.text)
        with self.assertRaises(HTTPException) as inactive:
            load_scoped_run(self.run["run_id"], principal)
        self.assertEqual(inactive.exception.status_code, 404)


class HubSyntheticScopeTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": "hub-bootstrap-key",
        "DEPLOYMENT_ROLE": "hub",
        "JOB_EXECUTION_MODE": "inline",
    }
    client_headers = {"X-API-Key": "hub-bootstrap-key"}

    def test_synthetic_admin_has_no_hub_scope_bypass(self) -> None:
        suffix = _suffix()
        run = DbRunStore(get_engine()).create_run(
            project_id=f"hub-project-{suffix}",
            site_id=f"hub-site-{suffix}",
            job_type="ip_discovery",
            parameters={},
        )
        principal = AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")
        with self.assertRaises(HTTPException) as denied:
            require_project_site_access(
                principal,
                run["project_id"],
                run["site_id"],
            )
        self.assertEqual(denied.exception.status_code, 404)

        me = self.client.get("/api/v1/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertFalse(me.json()["global_scope"])
        self.assertEqual(me.json()["effective_scopes"], [])

        repository = UserRepository(get_engine())
        target_id = str(uuid.uuid4())
        target_key = f"hub-viewer-key-{suffix}"
        repository.create_user(
            user_id=target_id,
            username=f"hub-viewer-{suffix}",
            role=Role.VIEWER.value,
            api_key_hash=hash_api_key(target_key),
        )
        named_admin_id = str(uuid.uuid4())
        named_admin_key = f"hub-admin-key-{suffix}"
        repository.create_user(
            user_id=named_admin_id,
            username=f"hub-admin-{suffix}",
            role=Role.ADMIN.value,
            api_key_hash=hash_api_key(named_admin_key),
        )

        denied_requests = (
            self.client.post(
                "/api/v1/users",
                json={"username": f"blocked-{suffix}", "role": "viewer"},
            ),
            self.client.get("/api/v1/users"),
            self.client.get("/api/v1/users/scope-activation-preflight"),
            self.client.post(f"/api/v1/users/{target_id}/deactivate"),
            self.client.post(f"/api/v1/users/{target_id}/key"),
            self.client.post(
                f"/api/v1/users/{target_id}/role",
                json={"role": "reviewer"},
            ),
            self.client.post(
                f"/api/v1/users/{target_id}/scope-grants",
                json={
                    "project_id": run["project_id"],
                    "site_id": run["site_id"],
                    "reason": "Synthetic bypass attempt",
                },
            ),
            self.client.get(f"/api/v1/users/{target_id}/scope-grants"),
            self.client.post(
                f"/api/v1/users/{target_id}/scope-grants/missing-grant/revoke",
                json={"reason": "Synthetic bypass attempt"},
            ),
        )
        for response in denied_requests:
            self.assertEqual(response.status_code, 403, response.text)

        named_headers = {"X-API-Key": named_admin_key}
        named_me = self.client.get("/api/v1/me", headers=named_headers)
        self.assertEqual(named_me.status_code, 200, named_me.text)
        self.assertTrue(named_me.json()["global_scope"])
        named_users = self.client.get("/api/v1/users", headers=named_headers)
        self.assertEqual(named_users.status_code, 200, named_users.text)

        named_grant = self.client.post(
            f"/api/v1/users/{target_id}/scope-grants",
            headers=named_headers,
            json={
                "project_id": run["project_id"],
                "site_id": run["site_id"],
                "reason": "Named global admin assignment",
            },
        )
        self.assertEqual(named_grant.status_code, 201, named_grant.text)
        self.assertEqual(named_grant.json()["granted_by"], named_admin_id)
        named_preflight = self.client.get(
            "/api/v1/users/scope-activation-preflight",
            headers=named_headers,
        )
        self.assertEqual(named_preflight.status_code, 200, named_preflight.text)
        preflight = named_preflight.json()
        self.assertGreaterEqual(preflight["active_named_admin_count"], 1)
        unscoped_ids = {
            item["id"] for item in preflight["unscoped_active_non_admin_users"]
        }
        self.assertNotIn(target_id, unscoped_ids)


if __name__ == "__main__":
    import unittest

    unittest.main()
