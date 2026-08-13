"""Preview-bound scan authorization API and discovery start tests."""

from __future__ import annotations

import socket
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from app.core.db import get_engine
from harness import ApiTestCase
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import Project, Site

_API_KEY = "test-scan-authorization-api-key"


class ScanAuthorizationApiTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def _preview(self, port: int, *, retry_parent_run_id: str | None = None):
        parameters: dict[str, object] = {
            "dry_run": True,
            "cidr": "127.0.0.1/32",
            "ports": [port],
        }
        if retry_parent_run_id is not None:
            parameters["retry_parent_run_id"] = retry_parent_run_id
        return self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "parameters": parameters,
            },
        )

    def _authorize(self, preview_run_id: str):
        now = datetime.now(UTC)
        return self.client.post(
            "/api/v1/discovery/scan-authorizations",
            json={
                "preview_run_id": preview_run_id,
                "ticket": "CHG-1042",
                "purpose": "Controlled loopback integration test",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
        )

    def _preview_in_scope(self, project_id: str, site_id: str):
        return self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": project_id,
                "site_id": site_id,
                "job_type": "ip_discovery",
                "parameters": {
                    "dry_run": True,
                    "cidr": "127.0.0.1/32",
                    "ports": [9],
                },
            },
        )

    def _seed_scope(self, project_id: str, site_id: str) -> None:
        with session_factory(get_engine()).begin() as session:
            session.add(Project(id=project_id, name=f"Project {project_id}"))
            session.add(Site(id=site_id, project_id=project_id, name=f"Site {site_id}"))

    def _create_scoped_user(
        self,
        *,
        role: str,
        project_id: str,
        site_id: str,
    ) -> dict[str, str]:
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"scan-auth-{role}-{uuid.uuid4().hex[:10]}", "role": role},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        granted = self.client.post(
            f"/api/v1/users/{body['user']['id']}/scope-grants",
            json={
                "project_id": project_id,
                "site_id": site_id,
                "reason": "Review scan authorization state.",
            },
        )
        self.assertEqual(granted.status_code, 201, granted.text)
        return {"X-API-Key": body["api_key"]}

    def test_viewer_can_list_and_get_authorizations_in_an_exact_granted_scope(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        project_id = f"scan-auth-project-{suffix}"
        site_id = f"scan-auth-site-{suffix}"
        self._seed_scope(project_id, site_id)
        preview = self._preview_in_scope(project_id, site_id)
        self.assertEqual(preview.status_code, 200, preview.text)
        authorization = self._authorize(preview.json()["run_id"])
        self.assertEqual(authorization.status_code, 201, authorization.text)
        authorization_id = authorization.json()["authorization_id"]
        viewer_headers = self._create_scoped_user(
            role="viewer",
            project_id=project_id,
            site_id=site_id,
        )

        listed = self.client.get(
            "/api/v1/discovery/scan-authorizations",
            params={"project_id": project_id, "site_id": site_id},
            headers=viewer_headers,
        )
        listed_for_preview = self.client.get(
            "/api/v1/discovery/scan-authorizations",
            params={
                "project_id": project_id,
                "site_id": site_id,
                "preview_run_id": preview.json()["run_id"],
            },
            headers=viewer_headers,
        )
        fetched = self.client.get(
            f"/api/v1/discovery/scan-authorizations/{authorization_id}",
            headers=viewer_headers,
        )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [item["authorization_id"] for item in listed.json()],
            [authorization_id],
        )
        self.assertEqual(listed_for_preview.status_code, 200, listed_for_preview.text)
        self.assertEqual(
            [item["authorization_id"] for item in listed_for_preview.json()],
            [authorization_id],
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["authorization_id"], authorization_id)

    def test_authorization_list_requires_an_exact_project_and_site(self) -> None:
        unscoped = self.client.get("/api/v1/discovery/scan-authorizations")
        project_only = self.client.get(
            "/api/v1/discovery/scan-authorizations",
            params={"project_id": "project-a"},
        )

        self.assertEqual(unscoped.status_code, 422, unscoped.text)
        self.assertEqual(project_only.status_code, 422, project_only.text)

    def test_foreign_and_missing_authorization_ids_are_equally_concealed(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        project_id = f"scan-auth-project-{suffix}"
        site_id = f"scan-auth-site-{suffix}"
        foreign_project_id = f"scan-auth-foreign-project-{suffix}"
        foreign_site_id = f"scan-auth-foreign-site-{suffix}"
        self._seed_scope(project_id, site_id)
        self._seed_scope(foreign_project_id, foreign_site_id)
        foreign_preview = self._preview_in_scope(foreign_project_id, foreign_site_id)
        self.assertEqual(foreign_preview.status_code, 200, foreign_preview.text)
        foreign_authorization = self._authorize(foreign_preview.json()["run_id"])
        self.assertEqual(foreign_authorization.status_code, 201, foreign_authorization.text)
        viewer_headers = self._create_scoped_user(
            role="viewer",
            project_id=project_id,
            site_id=site_id,
        )

        foreign = self.client.get(
            "/api/v1/discovery/scan-authorizations/"
            f"{foreign_authorization.json()['authorization_id']}",
            headers=viewer_headers,
        )
        missing = self.client.get(
            "/api/v1/discovery/scan-authorizations/auth_missing",
            headers=viewer_headers,
        )

        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(foreign.json(), missing.json())

    def test_create_and_revoke_remain_global_admin_actions(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        project_id = f"scan-auth-project-{suffix}"
        site_id = f"scan-auth-site-{suffix}"
        self._seed_scope(project_id, site_id)
        preview = self._preview_in_scope(project_id, site_id)
        self.assertEqual(preview.status_code, 200, preview.text)
        authorization = self._authorize(preview.json()["run_id"])
        self.assertEqual(authorization.status_code, 201, authorization.text)
        engineer_headers = self._create_scoped_user(
            role="engineer",
            project_id=project_id,
            site_id=site_id,
        )
        now = datetime.now(UTC)

        create_denied = self.client.post(
            "/api/v1/discovery/scan-authorizations",
            headers=engineer_headers,
            json={
                "preview_run_id": preview.json()["run_id"],
                "ticket": "CHG-1043",
                "purpose": "Engineer cannot self-approve",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
        )
        revoke_denied = self.client.post(
            "/api/v1/discovery/scan-authorizations/"
            f"{authorization.json()['authorization_id']}/revoke",
            headers=engineer_headers,
            json={"reason": "Engineer cannot revoke"},
        )

        self.assertEqual(create_denied.status_code, 403, create_denied.text)
        self.assertEqual(revoke_denied.status_code, 403, revoke_denied.text)

    def test_sealed_preview_authorization_is_consumed_once_by_live_start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        try:
            preview = self._preview(port)
            self.assertEqual(preview.status_code, 200, preview.text)
            preview_run_id = preview.json()["run_id"]
            authorization = self._authorize(preview_run_id)
            self.assertEqual(authorization.status_code, 201, authorization.text)
            approval = authorization.json()

            preview_record = self.client.get(
                f"/api/v1/discovery/runs/{preview_run_id}"
            ).json()
            effective_throttle = preview_record["parameters"]["scan_contract_v1"][
                "effective_throttle"
            ]
            self.assertEqual(
                set(effective_throttle),
                {"max_concurrency", "rate_limit_per_sec", "connect_timeout_s"},
            )
            self.assertEqual(
                approval["packet_plan_sha256"],
                preview_record["parameters"]["scan_contract_v1"]["packet_plan_sha256"],
            )

            live_payload = {
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "preview_run_id": preview_run_id,
                "scan_authorization_id": approval["authorization_id"],
                "parameters": {},
            }
            live = self.client.post(
                "/api/v1/discovery/ip/runs",
                json=live_payload,
            )
            self.assertEqual(live.status_code, 200, live.text)
            self.assertEqual(live.json()["status"], "succeeded")
            live_record = self.client.get(
                f"/api/v1/discovery/runs/{live.json()['run_id']}"
            ).json()
            self.assertEqual(
                live_record["parameters"]["scan_contract_v1"]["effective_throttle"],
                effective_throttle,
            )
            self.assertEqual(
                live_record["parameters"]["scan_contract_v1"],
                preview_record["parameters"]["scan_contract_v1"],
            )

            replay = self.client.post(
                "/api/v1/discovery/ip/runs",
                json=live_payload,
            )
            self.assertEqual(replay.status_code, 409, replay.text)
            self.assertIn("already been used", replay.json()["detail"])
        finally:
            listener.close()

    def test_legacy_boolean_authorization_cannot_start_a_live_discovery(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "parameters": {
                    "authorized": True,
                    "cidr": "127.0.0.1/32",
                    "ports": [9],
                },
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("sealed preview", response.json()["detail"])

    def test_authorization_requires_a_sealed_dry_preview_in_the_same_scope(self) -> None:
        response = self._authorize("run_missing")

        self.assertEqual(response.status_code, 404, response.text)

    def test_retry_is_a_new_preview_authorization_and_relational_run(self) -> None:
        from app.core.db import get_engine
        from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository

        first_preview = self._preview(9)
        self.assertEqual(first_preview.status_code, 200, first_preview.text)
        first_authorization = self._authorize(first_preview.json()["run_id"])
        self.assertEqual(first_authorization.status_code, 201, first_authorization.text)
        first_live = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "preview_run_id": first_preview.json()["run_id"],
                "scan_authorization_id": first_authorization.json()["authorization_id"],
                "parameters": {},
            },
        )
        self.assertEqual(first_live.status_code, 200, first_live.text)

        retry_preview = self._preview(
            9,
            retry_parent_run_id=first_live.json()["run_id"],
        )
        self.assertEqual(retry_preview.status_code, 200, retry_preview.text)
        retry_preview_id = retry_preview.json()["run_id"]
        retry_record = self.client.get(
            f"/api/v1/discovery/runs/{retry_preview_id}"
        ).json()
        self.assertEqual(
            retry_record["parameters"]["scan_contract_v1"]["relation_snapshot"],
            {
                "relation": "retry",
                "parent_run_id": first_live.json()["run_id"],
            },
        )

        retry_authorization = self._authorize(retry_preview_id)
        self.assertEqual(retry_authorization.status_code, 201, retry_authorization.text)
        retry_live = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "preview_run_id": retry_preview_id,
                "scan_authorization_id": retry_authorization.json()["authorization_id"],
                "parameters": {},
            },
        )
        self.assertEqual(retry_live.status_code, 200, retry_live.text)

        links = RunLifecycleRepository(get_engine()).list_run_links(
            retry_live.json()["run_id"]
        )
        self.assertCountEqual(
            [(item["relation"], item["parent_run_id"]) for item in links],
            [
                ("preview", retry_preview_id),
                ("retry", first_live.json()["run_id"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
