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


def _mqtt_export_zip() -> bytes:
    """Minimal MQTT export archive: _manifest.json + one topic payload, so
    _read_export_zip yields non-empty payloads and MQTT results-out fires."""
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(
            "_manifest.json",
            json.dumps(
                {
                    "assets": [{"asset": "AHU-1", "topics": ["site/ahu1"], "matched": True}],
                    "topicCount": 1,
                    "assetCount": 1,
                }
            ),
        )
        archive.writestr("site/ahu1.json", json.dumps({"present_value": 21.5}))
    return buf.getvalue()


_MQTT_EXPORT_ZIP = _mqtt_export_zip()

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
            stream=_Bytes(
                [
                    b'data: {"type":"start"}\n\n',
                    b'data: {"type":"result","rows":[{"ip":"192.0.2.10","register":"match",'
                    b'"rag":"green","status":"reachable","hostname":"h-a","openPorts":[80]}],'
                    b'"summary":{"reachable":1}}\n\n',
                    b'data: {"type":"complete"}\n\n',
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    if path == "/api/compare":
        # Finite JSON {rows, summary} - the sidecar's re-RAG output returned whole.
        # A placeholder "missing" row (ip "—") alongside a real one exercises the
        # start-IP derivation skip.
        return httpx.Response(
            200,
            stream=_Bytes(
                [
                    b'{"rows":[{"ip":"\xe2\x80\x94","register":"missing","rag":"red",'
                    b'"status":"unreachable","hostname":"h-x"},'
                    b'{"ip":"192.0.2.20","register":"match","rag":"green",'
                    b'"status":"reachable","hostname":"h-c","openPorts":[80]}],'
                    b'"summary":{"reachable":1,"matches":1}}'
                ]
            ),
            headers={"content-type": "application/json"},
        )
    if path == "/api/publish":
        return httpx.Response(200, stream=_Bytes([b'{"ok":true}']), headers={"content-type": "application/json"})
    if path == "/api/export-archive":
        return httpx.Response(200, stream=_Bytes([_MQTT_EXPORT_ZIP]), headers={"content-type": "application/zip"})
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

    def test_serves_the_theme_without_forwarding(self) -> None:
        self.scanner_raw._proxy_client = lambda: (_ for _ in ()).throw(AssertionError("forwarded theme"))
        resp = self.client.get("/api/v1/scanners/ip/raw/sct-theme.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("css", resp.headers["content-type"])

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

    def test_completed_scan_persists_a_real_ip_scanner_run(self) -> None:
        # Results-out (pipe 3): a completed panel scan lands a real ip_scanner run,
        # not just the thin evidence row, so the Results tab / history / reports fill.
        from app.api.routes.discovery import service

        session = self.sessions.create(
            owner="tester", project_id="demo-project", site_id="demo-site", proto="ip"
        )
        self.client.cookies.set("sct_panel", session.session_id)
        before = len(service.list_runs(job_types={"ip_scanner"}))
        resp = self.client.get("/api/v1/scanners/ip/raw/api/scan?start=192.0.2.1")
        self.assertEqual(resp.status_code, 200, resp.text)
        runs = service.list_runs(job_types={"ip_scanner"})
        self.assertEqual(len(runs), before + 1)
        self.assertEqual(runs[0].status, "succeeded")

    def test_completed_bacnet_scan_dispatches_a_results_out_run(self) -> None:
        # Results-out for BACnet: a completed panel scan hands the captured rows to
        # the bacnet dispatch helper (engine path + source_ip resolution live in that
        # helper, covered by the IP results-out test's real dispatch).
        from app.api.routes import scanners

        calls: list[dict] = []
        with patch.object(
            scanners, "dispatch_captured_bacnet_scanner_run", lambda **kw: calls.append(kw)
        ):
            session = self.sessions.create(
                owner="tester", project_id="demo-project", site_id="demo-site", proto="bacnet"
            )
            self.client.cookies.set("sct_panel", session.session_id)
            resp = self.client.get("/api/v1/scanners/bacnet/raw/api/scan?adapter=0")
            self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["captured"]["rows"])

    def test_completed_mqtt_export_dispatches_a_results_out_run(self) -> None:
        # Results-out for MQTT: the finite export-archive ZIP is captured, parsed to
        # manifest + payloads, and handed to the mqtt dispatch helper.
        from app.api.routes import scanners

        calls: list[dict] = []
        with patch.object(
            scanners, "dispatch_captured_mqtt_scanner_run", lambda **kw: calls.append(kw)
        ):
            session = self.sessions.create(
                owner="tester", project_id="demo-project", site_id="demo-site", proto="mqtt"
            )
            self.client.cookies.set("sct_panel", session.session_id)
            resp = self.client.get("/api/v1/scanners/mqtt/raw/api/export-archive")
            self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(calls), 1)
        self.assertIn("site/ahu1", calls[0]["captured"]["payloads"])

    def test_ip_recompare_persists_a_real_ip_scanner_run(self) -> None:
        # Results-out for a re-compare: after loading a register and re-RAGing, the
        # finite /api/compare JSON lands a real ip_scanner run (start IP derived from
        # the rows, the "—" placeholder skipped). Real dispatch, like the scan test.
        from app.api.routes.discovery import service

        session = self.sessions.create(
            owner="tester", project_id="demo-project", site_id="demo-site", proto="ip"
        )
        self.client.cookies.set("sct_panel", session.session_id)
        before = len(service.list_runs(job_types={"ip_scanner"}))
        resp = self.client.post(
            "/api/v1/scanners/ip/raw/api/compare", json={"devices": []}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        runs = service.list_runs(job_types={"ip_scanner"})
        self.assertEqual(len(runs), before + 1)
        self.assertEqual(runs[0].status, "succeeded")

    def test_bacnet_recompare_dispatches_a_results_out_run(self) -> None:
        # Results-out for a BACnet re-compare: the /api/compare JSON is captured and
        # handed to the bacnet dispatch helper (source_ip resolution lives there).
        from app.api.routes import scanners

        calls: list[dict] = []
        with patch.object(
            scanners, "dispatch_captured_bacnet_scanner_run", lambda **kw: calls.append(kw)
        ):
            session = self.sessions.create(
                owner="tester", project_id="demo-project", site_id="demo-site", proto="bacnet"
            )
            self.client.cookies.set("sct_panel", session.session_id)
            resp = self.client.post(
                "/api/v1/scanners/bacnet/raw/api/compare", json={"devices": []}
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["captured"]["rows"])

    def test_recompare_without_a_session_persists_nothing(self) -> None:
        # No panel session -> the compare still forwards, but nothing is captured.
        from app.api.routes.discovery import service

        before = len(service.list_runs(job_types={"ip_scanner"}))
        resp = self.client.post(
            "/api/v1/scanners/ip/raw/api/compare", json={"devices": []}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(service.list_runs(job_types={"ip_scanner"})), before)

    # -- M4: write guard ------------------------------------------------------

    def _open_session(self, proto: str = "mqtt") -> None:
        session = self.sessions.create(
            owner="tester", project_id="demo-project", site_id="demo-site", proto=proto
        )
        self.client.cookies.set("sct_panel", session.session_id)

    def _confirm(self, body_str: str) -> str:
        resp = self.client.post(
            "/api/v1/scanners/mqtt/raw/confirm-write",
            json={"method": "POST", "path": "api/publish", "body": body_str},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def test_write_without_a_token_is_403(self) -> None:
        self._open_session()
        resp = self.client.post("/api/v1/scanners/mqtt/raw/api/publish", json={"topic": "t", "payload": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_confirmed_write_passes_and_records_evidence(self) -> None:
        import json

        from app.api.routes.discovery import service

        self._open_session()
        body_str = json.dumps({"topic": "demo/x", "payload": "1"})
        token = self._confirm(body_str)
        before = len(service.list_runs(job_types={"scanner_raw_write"}))
        resp = self.client.post(
            "/api/v1/scanners/mqtt/raw/api/publish",
            content=body_str,
            headers={"content-type": "application/json", "X-SCT-Write-Confirm": token},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(service.list_runs(job_types={"scanner_raw_write"})), before + 1)

    def test_write_token_is_single_use(self) -> None:
        import json

        self._open_session()
        body_str = json.dumps({"topic": "t", "payload": "1"})
        token = self._confirm(body_str)
        headers = {"content-type": "application/json", "X-SCT-Write-Confirm": token}
        self.assertEqual(
            self.client.post("/api/v1/scanners/mqtt/raw/api/publish", content=body_str, headers=headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/v1/scanners/mqtt/raw/api/publish", content=body_str, headers=headers).status_code,
            403,
        )

    def test_write_token_rejects_a_tampered_body(self) -> None:
        import json

        self._open_session()
        token = self._confirm(json.dumps({"topic": "t", "payload": "1"}))
        tampered = json.dumps({"topic": "t", "payload": "TAMPERED"})
        resp = self.client.post(
            "/api/v1/scanners/mqtt/raw/api/publish",
            content=tampered,
            headers={"content-type": "application/json", "X-SCT-Write-Confirm": token},
        )
        self.assertEqual(resp.status_code, 403)

    # -- REV-7: a panel session is bound to its protocol --------------------------

    def test_confirm_write_rejects_a_cross_protocol_session(self) -> None:
        # REV-7: an IP-opened session must not mint a write token on the BACnet
        # route. The mismatch is rejected before any token is minted.
        self._open_session("ip")
        resp = self.client.post(
            "/api/v1/scanners/bacnet/raw/confirm-write",
            json={"method": "POST", "path": "api/publish", "body": "{}"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("different scanner", resp.text)

    def test_proxy_write_from_a_cross_protocol_session_fails_closed(self) -> None:
        # REV-7 defense in depth: even a token minted elsewhere cannot push a write
        # through a route whose protocol differs from the session's - the proxy drops
        # the mismatched session, so the write has no valid token and is refused.
        self._open_session("ip")  # IP session cookie, but write on the BACnet route
        resp = self.client.post(
            "/api/v1/scanners/bacnet/raw/api/publish",
            content=b'{"topic": "t", "payload": "1"}',
            headers={"content-type": "application/json", "X-SCT-Write-Confirm": "forged"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)


if __name__ == "__main__":
    unittest.main()
