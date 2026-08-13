"""Preview-bound scan authorization API and discovery start tests."""

from __future__ import annotations

import socket
import unittest
from datetime import UTC, datetime, timedelta

from harness import ApiTestCase

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
