"""Cross-scope contracts for runs, imports, configuration, and SSE."""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import UTC, datetime
from unittest import mock

from app.core.auth import AuthPrincipal
from app.core.db import get_engine
from app.core.scopes import ScopeGrantRepository
from harness import ApiTestCase
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.rbac import Role

_ROOT_KEY = "scope-route-root-key"
_REGISTER_HEADER = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol"
)
_REGISTER_ROW = (
    "Site A,BMS,FCU-04,site/b1/fcu-04/#,1.5.2,supply_air_temp,"
    "degrees-celsius,60,MQTT"
)


class ScopeRouteEnforcementTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _ROOT_KEY,
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
    }
    client_headers = {"X-API-Key": _ROOT_KEY}

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.scope_a = (f"scope-a-project-{suffix}", f"scope-a-site-{suffix}")
        self.scope_b = (f"scope-b-project-{suffix}", f"scope-b-site-{suffix}")
        store = DbRunStore(get_engine())
        self.run_a = store.create_run(
            project_id=self.scope_a[0],
            site_id=self.scope_a[1],
            job_type="ip_discovery",
            parameters={},
        )
        self.run_b = store.create_run(
            project_id=self.scope_b[0],
            site_id=self.scope_b[1],
            job_type="ip_discovery",
            parameters={},
        )
        self.viewer = self._create_user(f"scope-viewer-{suffix}", "viewer")
        self.engineer = self._create_user(f"scope-engineer-{suffix}", "engineer")
        self.viewer_grant = self._grant(self.viewer["user"]["id"], self.scope_a)
        self.engineer_grant = self._grant(self.engineer["user"]["id"], self.scope_a)

    def _create_user(self, username: str, role: str) -> dict:
        response = self.client.post(
            "/api/v1/users",
            json={"username": username, "role": role},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _grant(self, user_id: str, scope: tuple[str, str]) -> dict:
        response = self.client.post(
            f"/api/v1/users/{user_id}/scope-grants",
            json={
                "project_id": scope[0],
                "site_id": scope[1],
                "reason": "Focused scope-route contract",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def _headers(created: dict) -> dict[str, str]:
        return {"X-API-Key": created["api_key"]}

    def _upload(self, actor: dict, scope: tuple[str, str] | None) -> object:
        data = {"import_type": "mqtt_register"}
        if scope is not None:
            data.update({"project_id": scope[0], "site_id": scope[1]})
        return self.client.post(
            "/api/v1/imports",
            headers=self._headers(actor),
            data=data,
            files={
                "file": (
                    "register.csv",
                    io.BytesIO(f"{_REGISTER_HEADER}\n{_REGISTER_ROW}\n".encode()),
                    "text/csv",
                )
            },
        )

    def test_run_lists_are_database_scoped_and_foreign_cancel_is_concealed(self) -> None:
        viewer_headers = self._headers(self.viewer)
        owned = self.client.get(
            "/api/v1/runs",
            headers=viewer_headers,
            params={"project_id": self.scope_a[0], "site_id": self.scope_a[1]},
        )
        self.assertEqual(owned.status_code, 200, owned.text)
        self.assertIn(self.run_a["run_id"], [item["run_id"] for item in owned.json()["runs"]])

        foreign_list = self.client.get(
            "/api/v1/runs",
            headers=viewer_headers,
            params={"project_id": self.scope_b[0], "site_id": self.scope_b[1]},
        )
        self.assertEqual(foreign_list.status_code, 200, foreign_list.text)
        self.assertEqual(foreign_list.json()["runs"], [])

        engineer_headers = self._headers(self.engineer)
        owned_cancel = self.client.post(
            f"/api/v1/runs/{self.run_a['run_id']}/cancel",
            headers=engineer_headers,
        )
        self.assertEqual(owned_cancel.status_code, 200, owned_cancel.text)

        details: list[str] = []
        for run_id in (self.run_b["run_id"], "run_missing"):
            denied = self.client.post(
                f"/api/v1/runs/{run_id}/cancel",
                headers=engineer_headers,
            )
            self.assertEqual(denied.status_code, 404, denied.text)
            details.append(denied.json()["detail"])
        self.assertEqual(details[0], details[1])

    def test_configuration_reads_and_writes_require_the_exact_scope(self) -> None:
        viewer_headers = self._headers(self.viewer)
        owned = self.client.get(
            "/api/v1/configuration",
            headers=viewer_headers,
            params={"project_id": self.scope_a[0], "site_id": self.scope_a[1]},
        )
        self.assertEqual(owned.status_code, 200, owned.text)

        foreign = self.client.get(
            "/api/v1/configuration",
            headers=viewer_headers,
            params={"project_id": self.scope_b[0], "site_id": self.scope_b[1]},
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        configuration = owned.json()
        configuration["mqtt"]["values"]["Port"] = "1883"
        saved = self.client.put(
            "/api/v1/configuration",
            headers=self._headers(self.engineer),
            params={"project_id": self.scope_a[0], "site_id": self.scope_a[1]},
            json=configuration,
        )
        self.assertEqual(saved.status_code, 200, saved.text)

        denied = self.client.put(
            "/api/v1/configuration",
            headers=self._headers(self.engineer),
            params={"project_id": self.scope_b[0], "site_id": self.scope_b[1]},
            json=configuration,
        )
        self.assertEqual(denied.status_code, 404, denied.text)

    def test_import_create_latest_and_resource_reads_are_scope_bound(self) -> None:
        owned = self._upload(self.engineer, self.scope_a)
        self.assertEqual(owned.status_code, 200, owned.text)
        import_id = owned.json()["import_id"]

        denied_create = self._upload(self.engineer, self.scope_b)
        self.assertEqual(denied_create.status_code, 404, denied_create.text)
        denied_unscoped = self._upload(self.engineer, None)
        self.assertEqual(denied_unscoped.status_code, 400, denied_unscoped.text)
        self.assertEqual(
            denied_unscoped.json()["detail"],
            "project_id and site_id are required.",
        )

        viewer_headers = self._headers(self.viewer)
        read = self.client.get(f"/api/v1/imports/{import_id}", headers=viewer_headers)
        self.assertEqual(read.status_code, 200, read.text)
        errors = self.client.get(
            f"/api/v1/imports/{import_id}/errors",
            headers=viewer_headers,
        )
        self.assertEqual(errors.status_code, 200, errors.text)

        foreign = self._upload(
            {"api_key": _ROOT_KEY},
            self.scope_b,
        )
        self.assertEqual(foreign.status_code, 200, foreign.text)
        details: list[str] = []
        for candidate in (foreign.json()["import_id"], "imp_missing"):
            response = self.client.get(
                f"/api/v1/imports/{candidate}",
                headers=viewer_headers,
            )
            self.assertEqual(response.status_code, 404, response.text)
            details.append(response.json()["detail"])
        self.assertEqual(details[0], details[1])

        latest = self.client.get(
            "/api/v1/imports/latest",
            headers=viewer_headers,
            params={
                "import_type": "mqtt_register",
                "project_id": self.scope_a[0],
                "site_id": self.scope_a[1],
            },
        )
        self.assertEqual(latest.status_code, 200, latest.text)
        foreign_latest = self.client.get(
            "/api/v1/imports/latest",
            headers=viewer_headers,
            params={
                "import_type": "mqtt_register",
                "project_id": self.scope_b[0],
                "site_id": self.scope_b[1],
            },
        )
        self.assertEqual(foreign_latest.status_code, 404, foreign_latest.text)

    def test_new_imports_require_nonblank_project_and_site_for_every_admin(self) -> None:
        admin = self._create_user(f"scope-admin-{uuid.uuid4().hex[:10]}", "admin")

        for actor in (admin, {"api_key": _ROOT_KEY}):
            unscoped = self._upload(actor, None)
            self.assertEqual(unscoped.status_code, 400, unscoped.text)
            self.assertEqual(
                unscoped.json()["detail"],
                "project_id and site_id are required.",
            )

        for ownership in (
            {"project_id": self.scope_a[0]},
            {"site_id": self.scope_a[1]},
            {"project_id": " ", "site_id": "\t"},
        ):
            response = self.client.post(
                "/api/v1/imports",
                headers=self._headers(admin),
                data={"import_type": "mqtt_register", **ownership},
                files={
                    "file": (
                        "register.csv",
                        io.BytesIO(f"{_REGISTER_HEADER}\n{_REGISTER_ROW}\n".encode()),
                        "text/csv",
                    )
                },
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(
                response.json()["detail"],
                "project_id and site_id are required.",
            )

        scoped = self._upload(admin, self.scope_a)
        self.assertEqual(scoped.status_code, 200, scoped.text)
        import_id = scoped.json()["import_id"]
        for path in ("", "/errors"):
            response = self.client.get(
                f"/api/v1/imports/{import_id}{path}",
                headers=self._headers(admin),
            )
            self.assertEqual(response.status_code, 200, response.text)

    def test_global_admin_can_read_legacy_unscoped_import_without_revealing_it(
        self,
    ) -> None:
        admin = self._create_user(f"legacy-admin-{uuid.uuid4().hex[:10]}", "admin")
        import_id = f"imp_legacy_{uuid.uuid4().hex[:10]}"
        created_at = datetime.now(UTC)
        ImportRepository(get_engine()).create(
            import_id=import_id,
            import_type="mqtt_register",
            original_filename="legacy-register.csv",
            stored_file_path="protected/legacy-register.csv",
            summary={
                "import_id": import_id,
                "import_type": "mqtt_register",
                "file_name": "legacy-register.csv",
                "file_type": "csv",
                "project_id": None,
                "site_id": None,
                "total_rows": 0,
                "accepted_rows": 0,
                "rejected_rows": 0,
                "status": "rejected",
                "missing_columns": [],
                "warnings": [],
                "stored_file_name": "legacy-register.csv",
                "created_at": created_at.isoformat(),
            },
            errors=[],
            created_at=created_at,
        )

        for headers in (self._headers(admin), self.client_headers):
            summary = self.client.get(
                f"/api/v1/imports/{import_id}",
                headers=headers,
            )
            self.assertEqual(summary.status_code, 200, summary.text)
            errors = self.client.get(
                f"/api/v1/imports/{import_id}/errors",
                headers=headers,
            )
            self.assertEqual(errors.status_code, 200, errors.text)

        viewer_headers = self._headers(self.viewer)
        for path in ("", "/errors"):
            details: list[str] = []
            for candidate in (import_id, "imp_missing"):
                response = self.client.get(
                    f"/api/v1/imports/{candidate}{path}",
                    headers=viewer_headers,
                )
                self.assertEqual(response.status_code, 404, response.text)
                details.append(response.json()["detail"])
            self.assertEqual(details[0], details[1])

    def test_sse_rechecks_grant_each_poll_and_closes_on_control_store_error(self) -> None:
        import app.api.routes.events as events_module

        viewer_headers = self._headers(self.viewer)
        denied_details: list[str] = []
        for run_id in (self.run_b["run_id"], "run_missing"):
            denied = self.client.get(
                f"/api/v1/runs/{run_id}/events",
                headers=viewer_headers,
            )
            self.assertEqual(denied.status_code, 404, denied.text)
            denied_details.append(denied.json()["detail"])
        self.assertEqual(denied_details[0], denied_details[1])

        principal = AuthPrincipal(
            user_id=self.viewer["user"]["id"],
            username=self.viewer["user"]["username"],
            role=Role.VIEWER,
            source="user_key",
        )

        async def revoke_between_polls() -> None:
            stream = events_module._run_event_stream(self.run_a["run_id"], principal)
            first = await stream.__anext__()
            self.assertIn(f'"run_id":"{self.run_a["run_id"]}"', first)
            ScopeGrantRepository(get_engine()).revoke(
                user_id=self.viewer["user"]["id"],
                grant_id=self.viewer_grant["grant_id"],
                reason="SSE revocation test",
                principal=AuthPrincipal(None, "local", Role.ADMIN, "local"),
            )
            closed = await stream.__anext__()
            self.assertIn("event: closed", closed)
            with self.assertRaises(StopAsyncIteration):
                await stream.__anext__()

        with mock.patch.object(events_module, "POLL_INTERVAL_SECONDS", 0.001):
            asyncio.run(revoke_between_polls())

        async def fail_closed_on_store_error() -> None:
            stream = events_module._run_event_stream(self.run_a["run_id"], principal)
            unavailable = await stream.__anext__()
            self.assertIn("event: unavailable", unavailable)
            with self.assertRaises(StopAsyncIteration):
                await stream.__anext__()

        with mock.patch.object(
            events_module,
            "load_scoped_run",
            side_effect=RuntimeError("control store unavailable"),
        ):
            asyncio.run(fail_closed_on_store_error())


if __name__ == "__main__":
    import unittest

    unittest.main()
