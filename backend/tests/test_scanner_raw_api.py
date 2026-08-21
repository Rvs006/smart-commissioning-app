"""Advanced-panel reverse proxy route: forwarding, write-block, SSE relay, honesty.

Mirrors test_mqtt_live_session_api: one ApiTestCase, the sidecar HTTP seam
(_proxy_client) monkeypatched with an httpx.MockTransport so no Node process is
spawned. Public-repo values only.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from harness import ApiTestCase

_API_KEY = "test-scanner-raw-key"
_ENV = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "api_key", "API_KEY": _API_KEY}


class _StubSupervisor:
    def __init__(self, base_url: str) -> None:
        self._base = base_url

    def base_url_for(self, _name: str) -> str:
        return self._base


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/adapters":
        return httpx.Response(
            200, stream=_Bytes([b'{"adapters":["eth0"]}']), headers={"content-type": "application/json"}
        )
    if path == "/":
        return httpx.Response(200, stream=_Bytes([b"<html>ip</html>"]), headers={"content-type": "text/html"})
    if path == "/api/scan":
        return httpx.Response(
            200,
            stream=_Bytes([b'data: {"type":"start"}\n\n', b'data: {"type":"complete"}\n\n']),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(404, text="not found")


def _mock_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class ScannerRawApiTest(ApiTestCase):
    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        from app.api.routes import scanner_raw
        from app.services import scanner_raw_session

        self.scanner_raw = scanner_raw
        self.sessions = scanner_raw_session
        self.sessions.clear()
        self.client.cookies.clear()
        self._orig = scanner_raw._proxy_client
        scanner_raw._proxy_client = _mock_client
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")

    def tearDown(self) -> None:
        self.scanner_raw._proxy_client = self._orig
        self.sessions.clear()
        self.client.cookies.clear()
        if hasattr(self.app.state, "sidecar_supervisor"):
            del self.app.state.sidecar_supervisor

    def test_forwards_a_read_and_returns_the_body(self) -> None:
        resp = self.client.get("/api/v1/scanners/ip/raw/api/adapters")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"adapters": ["eth0"]})

    def test_serves_the_index(self) -> None:
        resp = self.client.get("/api/v1/scanners/ip/raw/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<html>ip</html>", resp.text)

    def test_relays_sse_frames_with_event_stream_content_type(self) -> None:
        resp = self.client.get("/api/v1/scanners/ip/raw/api/scan?start=192.0.2.1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        self.assertIn('"type":"start"', resp.text)
        self.assertIn('"type":"complete"', resp.text)

    def test_blocks_a_device_write_without_touching_the_sidecar(self) -> None:
        # publish is a write; 403 before any forward. Flip the seam to a bomb to
        # prove the sidecar is never called.
        self.scanner_raw._proxy_client = lambda: (_ for _ in ()).throw(AssertionError("forwarded a write"))
        resp = self.client.post("/api/v1/scanners/mqtt/raw/api/publish", json={"topic": "t", "payload": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_serves_the_bridge_stub_without_forwarding(self) -> None:
        self.scanner_raw._proxy_client = lambda: (_ for _ in ()).throw(AssertionError("forwarded bridge"))
        resp = self.client.get("/api/v1/scanners/ip/raw/sct-bridge.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp.headers["content-type"])

    def test_unknown_protocol_is_404(self) -> None:
        resp = self.client.get("/api/v1/scanners/nope/raw/api/health")
        self.assertEqual(resp.status_code, 404)

    def test_missing_sidecar_is_503(self) -> None:
        del self.app.state.sidecar_supervisor
        resp = self.client.get("/api/v1/scanners/ip/raw/api/adapters")
        self.assertEqual(resp.status_code, 503)

    def test_session_route_sets_the_attribution_cookie(self) -> None:
        with patch.object(self.scanner_raw, "require_project_site_access", lambda *a, **k: None):
            resp = self.client.post(
                "/api/v1/scanners/ip/raw/session", json={"project_id": "p", "site_id": "s"}
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["project_id"], "p")
        self.assertIn("sct_panel", self.client.cookies)

    def test_recorded_read_creates_one_evidence_run(self) -> None:
        from app.api.routes.discovery import service

        session = self.sessions.create(
            owner="tester", project_id="demo-project", site_id="demo-site", proto="ip"
        )
        self.client.cookies.set("sct_panel", session.session_id)
        before = len(service.list_runs(job_types={"scanner_raw_action"}))
        resp = self.client.get("/api/v1/scanners/ip/raw/api/scan?start=192.0.2.1")
        self.assertEqual(resp.status_code, 200)
        runs = service.list_runs(job_types={"scanner_raw_action"})
        self.assertEqual(len(runs), before + 1)
        self.assertEqual(runs[0].status, "succeeded")

    def test_read_without_session_records_nothing(self) -> None:
        from app.api.routes.discovery import service

        before = len(service.list_runs(job_types={"scanner_raw_action"}))
        self.client.get("/api/v1/scanners/ip/raw/api/scan?start=192.0.2.1")
        self.assertEqual(len(service.list_runs(job_types={"scanner_raw_action"})), before)


if __name__ == "__main__":
    unittest.main()
