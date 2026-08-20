"""Frictionless deployment mode (SCT_REQUIRE_SCAN_AUTHORIZATION=0).

When a deployment opts out, scans and device writes run with no "authorized"
checkbox and no sealed two-person approval. These tests prove: the live-direct
paths are accepted and reach a real terminal (NOT killed by the active-control
fence), evidence still records the initiator, and /me reports the mode. A second
class re-asserts that the default/enforced deployment still rejects the same
un-sealed requests, so the switch is the only thing that changed.

Public values only (loopback, broker.example.local).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from harness import ApiTestCase

_API_KEY = "test-frictionless-key"
_INLINE = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "api_key", "API_KEY": _API_KEY}


class _StubSupervisor:
    def __init__(self, base_url: str) -> None:
        self._base = base_url

    def base_url_for(self, _name: str) -> str:
        return self._base


def _fake_settings(*_a, **_k):
    return SimpleNamespace(host="broker.example.local", port=8883, use_tls=True)


def _connected_status(*_a, **_k):
    return {"status": {"status": "connected", "host": "broker.example.local", "port": 8883, "tls": True}}


class FrictionlessAuthorizationTests(ApiTestCase):
    env = {**_INLINE, "SCT_REQUIRE_SCAN_AUTHORIZATION": "0"}
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        self.username = self.client.get("/api/v1/me").json()["username"]

    def test_me_reports_authorization_not_enforced(self) -> None:
        body = self.client.get("/api/v1/me").json()
        self.assertFalse(body["authorization_enforced"])

    def test_live_ip_discovery_runs_direct_without_a_sealed_preview(self) -> None:
        # The exact request the enforced deployment 400s (see the enforced class)
        # is accepted here and reaches a real terminal, not active_control_lost.
        response = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "parameters": {"cidr": "127.0.0.1/32", "ports": [9]},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/discovery/runs/{response.json()['run_id']}").json()
        # "succeeded" is the proof: the ip success fence (_fence_success_terminal)
        # runs on the sealed contract and would flip this to failed
        # active_control_lost if the frozen initiator/contract were invalid.
        self.assertEqual(run["status"], "succeeded", run)

    def test_sidecar_mqtt_scan_needs_no_authorized_flag(self) -> None:
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")
        self.addCleanup(lambda: delattr(self.app.state, "sidecar_supervisor"))
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "mqtt_scanner",
                "parameters": {"capture_seconds": 1},
            },
        )
        # Accepted (not 403). The run may fail honestly on the unreachable stub;
        # the point is the authorization checkbox is not required.
        self.assertEqual(response.status_code, 200, response.text)

    def test_direct_publish_records_the_sender_without_admin_approval(self) -> None:
        from app.api.routes import scanners_mqtt_live as live_routes
        from app.services.mqtt_live_session import service as live_service

        live_service.release(None)
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")
        self.addCleanup(lambda: delattr(self.app.state, "sidecar_supervisor"))
        self.addCleanup(lambda: live_service.release(None))
        with live_service.lock:
            live_service.acquire(
                owner=self.username, project_id="project-a", site_id="site-a", disconnect=lambda: None, take_over=False
            )
        with patch.object(live_routes, "build_mqtt_connection_settings", _fake_settings), patch.object(
            live_routes, "_get_json", _connected_status
        ), patch(
            "smart_commissioning_core.engines.mqtt_publish_sidecar._post_json", lambda *_a, **_k: {"ok": True}
        ):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/publish/runs",
                json={
                    "project_id": "project-a",
                    "site_id": "site-a",
                    "job_type": "mqtt_publish",
                    "parameters": {"topic": "site/ahu-1/cmd", "payload": '{"cmd":1}', "qos": 0, "retain": False},
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded", response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()
        publish = run["result_summary"]["publish"]
        self.assertTrue(publish["accepted_by_sidecar"])
        self.assertEqual(publish["authorized_by"], self.username)


class EnforcedAuthorizationStillGatesTests(ApiTestCase):
    env = {**_INLINE, "SCT_REQUIRE_SCAN_AUTHORIZATION": "1"}
    client_headers = {"X-API-Key": _API_KEY}

    def test_me_reports_authorization_enforced(self) -> None:
        self.assertTrue(self.client.get("/api/v1/me").json()["authorization_enforced"])

    def test_live_ip_without_a_sealed_preview_is_400(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "ip_discovery",
                "parameters": {"authorized": True, "cidr": "127.0.0.1/32", "ports": [9]},
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("sealed preview", response.json()["detail"])

    def test_sidecar_mqtt_scan_without_authorized_is_403(self) -> None:
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")
        self.addCleanup(lambda: delattr(self.app.state, "sidecar_supervisor"))
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/runs",
            json={
                "project_id": "project-a",
                "site_id": "site-a",
                "job_type": "mqtt_scanner",
                "parameters": {"capture_seconds": 1},
            },
        )
        self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
