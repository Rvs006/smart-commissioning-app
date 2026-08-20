"""M5 PR-A: the sealed one-message MQTT publish ceremony.

The safety-critical path: a dry preview seals the exact bytes (no broker I/O), an
admin approves that exact preview, and the live send replays ONLY the frozen
bytes. These tests prove un-approved bytes can never reach the broker: drift is
rejected, live parameters must be empty, the session must be owned by the sender
and connected to the sealed broker, and every failure is an honest failed run.

Broker settings are patched (no Configuration round-trip); the sidecar seams
(_get_json status gate, engine _post_json) are patched (no Node process). Public
values only (broker.example.local).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from harness import ApiTestCase

_API_KEY = "test-mqtt-publish-key"
_ENV = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "api_key", "API_KEY": _API_KEY}


class _StubSupervisor:
    def __init__(self, base_url: str) -> None:
        self._base = base_url

    def base_url_for(self, _name: str) -> str:
        return self._base


def _fake_settings(*_a, **_k):
    return SimpleNamespace(host="broker.example.local", port=8883, use_tls=True)


def _connected_status(*_a, **_k):
    return {"status": {"status": "connected", "host": "broker.example.local", "port": 8883, "tls": True}}


class MqttPublishApiTest(ApiTestCase):
    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        from app.api.routes import scanners_mqtt_live as live_routes
        from app.services.mqtt_live_session import service as live_service

        self.live_routes = live_routes
        self.live_service = live_service
        self.live_service.release(None)
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")
        self.username = self.client.get("/api/v1/me").json()["username"]
        # Broker resolves server-side; patch it so the preview can seal without a
        # Configuration round-trip.
        patcher = patch.object(live_routes, "build_mqtt_connection_settings", _fake_settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.live_service.release(None)
        if hasattr(self.app.state, "sidecar_supervisor"):
            del self.app.state.sidecar_supervisor

    # -- helpers --------------------------------------------------------------

    def _preview(self, topic="site/ahu-1/cmd", payload='{"cmd":1}', qos=0, retain=False) -> dict:
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/publish/runs",
            json={
                "project_id": "p",
                "site_id": "s",
                "job_type": "mqtt_publish",
                "parameters": {"dry_run": True, "topic": topic, "payload": payload, "qos": qos, "retain": retain},
            },
        )
        return response

    def _approve(self, preview_run_id: str) -> dict:
        now = datetime.now(UTC)
        return self.client.post(
            "/api/v1/discovery/scan-authorizations",
            json={
                "preview_run_id": preview_run_id,
                "ticket": "CHG-1",
                "purpose": "commissioning publish",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
        )

    def _send(self, preview_run_id: str, authorization_id: str) -> dict:
        return self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/publish/runs",
            json={
                "project_id": "p",
                "site_id": "s",
                "job_type": "mqtt_publish",
                "parameters": {},
                "preview_run_id": preview_run_id,
                "scan_authorization_id": authorization_id,
            },
        )

    def _hold_session(self, owner: str | None = None) -> object:
        with self.live_service.lock:
            return self.live_service.acquire(
                owner=owner or self.username, project_id="p", site_id="s", disconnect=lambda: None, take_over=False
            )

    # -- the sealed happy path ------------------------------------------------

    def test_full_ceremony_publishes_and_consumes_the_authorization(self) -> None:
        preview = self._preview()
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["status"], "succeeded", "inline dry preview seals synchronously")
        preview_id = preview.json()["run_id"]

        approval = self._approve(preview_id)
        self.assertEqual(approval.status_code, 201, approval.text)
        auth_id = approval.json()["authorization_id"]

        self._hold_session()
        with patch.object(self.live_routes, "_get_json", _connected_status), patch(
            "smart_commissioning_core.engines.mqtt_publish_sidecar._post_json", lambda *_a, **_k: {"ok": True}
        ):
            send = self._send(preview_id, auth_id)
        self.assertEqual(send.status_code, 200, send.text)
        self.assertEqual(send.json()["status"], "succeeded", send.text)
        run = self.client.get(f"/api/v1/validation/runs/{send.json()['run_id']}").json()
        publish = run["result_summary"]["publish"]
        self.assertTrue(publish["accepted_by_sidecar"])
        self.assertFalse(publish["delivery_confirmed"])
        self.assertNotIn("password", run["result_summary"].__str__().lower())

        # One use: a second send with the same authorization is rejected.
        self._hold_session()
        with patch.object(self.live_routes, "_get_json", _connected_status), patch(
            "smart_commissioning_core.engines.mqtt_publish_sidecar._post_json", lambda *_a, **_k: {"ok": True}
        ):
            again = self._send(preview_id, auth_id)
        self.assertEqual(again.status_code, 409, again.text)

    def test_preview_touches_no_broker_and_is_sealed(self) -> None:
        # If the engine's _post_json were called during a dry preview, this raises.
        with patch("smart_commissioning_core.engines.mqtt_publish_sidecar._post_json", side_effect=AssertionError("no I/O in a dry preview")):
            preview = self._preview()
        self.assertEqual(preview.status_code, 200, preview.text)
        # The preview is sealed if an admin can approve it.
        self.assertEqual(self._approve(preview.json()["run_id"]).status_code, 201)

    # -- drift + shape rejections --------------------------------------------

    def test_live_send_with_nonempty_parameters_is_400(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/publish/runs",
            json={
                "project_id": "p", "site_id": "s", "job_type": "mqtt_publish",
                "parameters": {"topic": "site/other"},  # smuggling a topic into the live send
                "preview_run_id": preview_id, "scan_authorization_id": auth_id,
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_dry_preview_carrying_ids_is_400(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/publish/runs",
            json={
                "project_id": "p", "site_id": "s", "job_type": "mqtt_publish",
                "parameters": {"dry_run": True, "topic": "t", "payload": "x"},
                "preview_run_id": "some-run", "scan_authorization_id": "some-auth",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_mismatched_authorization_is_rejected(self) -> None:
        preview_a = self._preview(topic="site/a/cmd").json()["run_id"]
        preview_b = self._preview(topic="site/b/cmd").json()["run_id"]
        auth_a = self._approve(preview_a).json()["authorization_id"]
        self._hold_session()
        # Authorization for preview A used to send preview B -> rejected.
        with patch.object(self.live_routes, "_get_json", _connected_status):
            response = self._send(preview_b, auth_a)
        self.assertIn(response.status_code, (400, 409), response.text)

    def test_wildcard_topic_is_400_at_preview(self) -> None:
        self.assertEqual(self._preview(topic="site/+/cmd").status_code, 400)
        self.assertEqual(self._preview(topic="site/#").status_code, 400)

    # -- session + broker gates ----------------------------------------------

    def test_send_without_a_session_is_409(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        # No live session held.
        self.assertEqual(self._send(preview_id, auth_id).status_code, 409)

    def test_send_by_non_owner_of_the_session_is_409(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        self._hold_session(owner="someone-else")
        self.assertEqual(self._send(preview_id, auth_id).status_code, 409)

    def test_send_to_a_different_broker_than_sealed_is_409(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        self._hold_session()

        def other_broker(*_a, **_k):
            return {"status": {"status": "connected", "host": "other.example.local", "port": 8883, "tls": True}}

        with patch.object(self.live_routes, "_get_json", other_broker):
            self.assertEqual(self._send(preview_id, auth_id).status_code, 409)

    def test_send_while_not_connected_is_409(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        self._hold_session()

        def disconnected(*_a, **_k):
            return {"status": {"status": "disconnected"}}

        with patch.object(self.live_routes, "_get_json", disconnected):
            self.assertEqual(self._send(preview_id, auth_id).status_code, 409)

    # -- honesty + listing ----------------------------------------------------

    def test_sidecar_not_accepting_records_a_failed_run(self) -> None:
        preview_id = self._preview().json()["run_id"]
        auth_id = self._approve(preview_id).json()["authorization_id"]
        self._hold_session()
        with patch.object(self.live_routes, "_get_json", _connected_status), patch(
            "smart_commissioning_core.engines.mqtt_publish_sidecar._post_json", lambda *_a, **_k: {"ok": False}
        ):
            send = self._send(preview_id, auth_id)
        self.assertEqual(send.status_code, 200, send.text)
        self.assertEqual(send.json()["status"], "failed", send.text)

    def test_publish_run_is_a_validation_run_not_a_discovery_run(self) -> None:
        preview_id = self._preview().json()["run_id"]
        detail = self.client.get(f"/api/v1/validation/runs/{preview_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        # A write must never surface as a discovery run.
        self.assertEqual(self.client.get(f"/api/v1/discovery/runs/{preview_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
