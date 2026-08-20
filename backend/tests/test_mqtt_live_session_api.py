"""M4a: MQTT live session lease + lifecycle routes + SSE relay.

One ApiTestCase class so the app (and its temp DB) is booted in setUpClass before
any app-module import, avoiding the collection-order engine-binding poisoning the
other API suites document. The sidecar HTTP seams (_post_json/_get_json/
_stream_client) are monkeypatched per test, so no Node process is spawned and no
broker is contacted. Public-repo values only (broker.example.local / 192.0.2.x).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from harness import ApiTestCase

_API_KEY = "test-mqtt-live-key"
_ENV = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "api_key", "API_KEY": _API_KEY}


def _parse_sse(body: str) -> list[dict]:
    frames: list[dict] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        parsed: dict = {"event": event}
        if data_lines:
            parsed["data"] = json.loads("".join(data_lines))
        frames.append(parsed)
    return frames


class _StubSupervisor:
    def __init__(self, base_url: str) -> None:
        self._base = base_url

    def base_url_for(self, _name: str) -> str:
        return self._base


class _ScriptedByteStream(httpx.AsyncByteStream):
    """Yields scripted SSE chunks, optionally raising a transport error at the end."""

    def __init__(self, chunks: list[bytes], *, raise_at_end: bool = False) -> None:
        self._chunks = chunks
        self._raise = raise_at_end

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._raise:
            raise httpx.ReadError("upstream died")

    async def aclose(self) -> None:
        return None


def _scripted_stream_client(chunks: list[bytes], *, raise_at_end: bool = False):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ScriptedByteStream(chunks, raise_at_end=raise_at_end))

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakeBody:
    """A tiny read()-able for urllib.error.HTTPError(fp=...)."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None


class MqttLiveSessionApiTest(ApiTestCase):
    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        from app.api.routes import scanners_mqtt_live as live_routes
        from app.services.mqtt_live_session import service as live_service

        self.live_routes = live_routes
        self.live_service = live_service
        self.live_service.release(None)  # clean lease between tests
        self.app.state.sidecar_supervisor = _StubSupervisor("http://127.0.0.1:9")

    def tearDown(self) -> None:
        self.live_service.release(None)
        if hasattr(self.app.state, "sidecar_supervisor"):
            del self.app.state.sidecar_supervisor

    # -- lease + reaper units -------------------------------------------------

    def _acquire(self, owner: str = "tester", **kw) -> object:
        with self.live_service.lock:
            return self.live_service.acquire(
                owner=owner, project_id="proj", site_id="site", disconnect=lambda: None, take_over=kw.get("take_over", False)
            )

    def test_reaper_idle_reaps_only_with_no_attached_stream(self) -> None:
        session = self._acquire()
        self.assertIsNone(self.live_service.reap_if_stale(now_mono=session.acquired_mono + 10))
        # An attached stream defers the idle reap past the grace window.
        self.live_service.stream_attach(session.session_id)
        self.assertIsNone(self.live_service.reap_if_stale(now_mono=session.acquired_mono + 10_000))
        self.live_service.stream_detach(session.session_id)
        self.assertEqual(self.live_service.reap_if_stale(now_mono=session.acquired_mono + 200), "idle")
        self.assertIsNone(self.live_service.current())

    def test_reaper_absolute_cap_reaps_even_with_a_stream(self) -> None:
        session = self._acquire()
        self.live_service.stream_attach(session.session_id)
        self.assertEqual(self.live_service.reap_if_stale(now_mono=session.acquired_mono + 20_000), "cap")

    def test_stream_attach_is_bounded(self) -> None:
        session = self._acquire()
        for _ in range(self.live_service.MAX_ATTACHED_STREAMS):
            self.assertTrue(self.live_service.stream_attach(session.session_id))
        self.assertFalse(self.live_service.stream_attach(session.session_id))  # cap
        self.assertFalse(self.live_service.stream_attach("not-the-session"))

    # -- connect --------------------------------------------------------------

    def test_connect_without_broker_configured_is_400(self) -> None:
        # No broker configured -> _connect_config (build_mqtt_connection_settings)
        # raises MqttSettingsError, a ValueError; the route maps it to an honest
        # 400 with the Configuration hint, and leaves no lease behind.
        def no_broker(_params, _root):
            raise ValueError("Live broker mode requires an MQTT broker FQDN or IP address.")

        with patch.object(self.live_routes, "_connect_config", no_broker):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True},
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("broker", response.text.lower())
        self.assertIsNone(self.live_service.current(), "no lease left after a 400")

    def test_connect_requires_authorization(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/connect",
            json={"project_id": "p", "site_id": "s", "authorized": False},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_connect_happy_path_sends_no_broker_fields_from_the_client(self) -> None:
        captured: dict = {}

        def fake_connect_config(params, root):
            captured["params"] = dict(params)
            captured["root"] = root
            return {"host": "broker.example.local", "port": 8883, "tls": True, "rootFilter": root}

        def fake_post_json(base, path, body):
            captured["post"] = (path, dict(body))
            return {"ok": True, "status": {"status": "connected", "host": "broker.example.local"}}

        with patch.object(self.live_routes, "_connect_config", fake_connect_config), patch.object(
            self.live_routes, "_post_json", fake_post_json
        ):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True, "root_filter": "udmi/site/example/#"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["session"]["owner"], "the acquiring principal's username")
        self.assertEqual(body["connection"]["status"], "connected")
        # The browser sent no broker host/credentials; the route built params from
        # project/site/root only, and _connect_config resolved the rest server-side.
        self.assertNotIn("host", captured["params"])
        self.assertNotIn("password", captured["params"])
        self.assertEqual(captured["root"], "udmi/site/example/#")
        self.assertEqual(captured["post"][0], "/api/connect")

    def test_connect_while_held_is_409_then_takeover_replaces(self) -> None:
        first = self._acquire(owner="alice")

        def ok_config(_p, _r):
            return {"host": "broker.example.local"}

        def ok_post(_b, _p, _body):
            return {"ok": True, "status": {"status": "connected"}}

        with patch.object(self.live_routes, "_connect_config", ok_config), patch.object(
            self.live_routes, "_post_json", ok_post
        ):
            blocked = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True},
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertIn("alice", blocked.text)
            took = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True, "take_over": True},
            )
            self.assertEqual(took.status_code, 200, took.text)
        self.assertNotEqual(self.live_service.current().session_id, first.session_id)

    def test_connect_refused_by_broker_is_502_and_releases_the_lease(self) -> None:
        def ok_config(_p, _r):
            return {"host": "broker.example.local"}

        # The real code catches urllib.error.HTTPError; simulate that exact type.
        import urllib.error

        def http_error(_b, _p, _body):
            raise urllib.error.HTTPError(
                "http://x/api/connect", 502, "Bad Gateway", {}, _FakeBody(b'{"error":"Connection refused"}')
            )

        with patch.object(self.live_routes, "_connect_config", ok_config), patch.object(
            self.live_routes, "_post_json", http_error
        ):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True},
            )
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("Connection refused", response.text)
        self.assertIsNone(self.live_service.current(), "a refused connect must not leave a lease")

    def test_connect_blocked_by_a_running_capture_run_is_409(self) -> None:
        running = SimpleNamespace(run_id="run-mqtt-x")
        with patch.object(self.live_routes.service, "list_runs", return_value=[running]), patch.object(
            self.live_routes, "_connect_config", lambda _p, _r: {"host": "broker.example.local"}
        ):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/connect",
                json={"project_id": "p", "site_id": "s", "authorized": True},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("run-mqtt-x", response.text)

    # -- create-run reverse 409 ----------------------------------------------

    def test_create_capture_run_is_blocked_while_a_live_session_is_held(self) -> None:
        self._acquire(owner="bob")
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/runs",
            json={"project_id": "p", "site_id": "s", "job_type": "mqtt_scanner", "parameters": {"authorized": True}},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("live session", response.text.lower())

    # -- status + disconnect --------------------------------------------------

    def test_status_reports_unavailable_when_the_sidecar_is_gone(self) -> None:
        del self.app.state.sidecar_supervisor  # no supervisor at all
        response = self.client.get("/api/v1/discovery/mqtt_sidecar/live/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["sidecar_available"])
        self.assertIsNone(body["session"])

    def test_disconnect_releases_then_is_a_safe_noop(self) -> None:
        session = self._acquire()
        first = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/disconnect", json={"session_id": session.session_id}
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["released"])
        second = self.client.post("/api/v1/discovery/mqtt_sidecar/live/disconnect", json={})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["released"])

    # -- focus ----------------------------------------------------------------

    def test_focus_proxies_to_the_sidecar_for_the_current_session(self) -> None:
        session = self._acquire()

        def fake_post(_base, _path, body):
            return {"ok": True, "focused": {"asset": body["asset"], "livePoints": []}}

        with patch.object(self.live_routes, "_post_json", fake_post):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/focus",
                json={"session_id": session.session_id, "asset": "AHU-1"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["focused"]["asset"], "AHU-1")

    def test_focus_with_a_stale_session_is_409(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/mqtt_sidecar/live/focus",
            json={"session_id": "not-current", "asset": "AHU-1"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    # -- subscribe + search ---------------------------------------------------

    def test_subscribe_change_is_proxied(self) -> None:
        session = self._acquire()
        with patch.object(self.live_routes, "_post_json", lambda *_a: {"ok": True}):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/subscribe",
                json={"session_id": session.session_id, "root_filter": "site/#", "qos": 1},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_subscribe_when_not_connected_is_409(self) -> None:
        import urllib.error

        session = self._acquire()

        def not_connected(*_a):
            raise urllib.error.HTTPError(
                "http://x/api/subscribe", 409, "Conflict", {}, _FakeBody(b'{"ok":false,"error":"Not connected"}')
            )

        with patch.object(self.live_routes, "_post_json", not_connected):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/subscribe",
                json={"session_id": session.session_id, "root_filter": "site/#"},
            )
        self.assertEqual(response.status_code, 409, response.text)

    def test_search_is_proxied_with_snake_to_camel_keys(self) -> None:
        session = self._acquire()
        captured: dict = {}

        def fake_post(_base, _path, body):
            captured["body"] = body
            return {"type": "snapshot"}

        with patch.object(self.live_routes, "_post_json", fake_post):
            response = self.client.post(
                "/api/v1/discovery/mqtt_sidecar/live/search",
                json={"session_id": session.session_id, "q": "supply_air_temp", "matched_only": True},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured["body"], {"q": "supply_air_temp", "matchedOnly": True})

    # -- stream relay ---------------------------------------------------------

    def test_stream_open_with_a_stale_session_is_409(self) -> None:
        response = self.client.get("/api/v1/discovery/mqtt_sidecar/live/stream?session_id=not-current")
        self.assertEqual(response.status_code, 409, response.text)

    def _drive_relay(self, session_id: str, chunks: list[bytes], *, raise_at_end: bool = False) -> list[dict]:
        principal = SimpleNamespace(username="tester", user_id=None, role=None)

        async def run() -> list[str]:
            out: list[str] = []
            async for frame in self.live_routes._live_relay(
                "http://127.0.0.1:9", session_id, principal, "proj", "site"
            ):
                out.append(frame)
            return out

        with patch.object(self.live_routes, "_stream_client", _scripted_stream_client(chunks, raise_at_end=raise_at_end)), \
                patch.object(self.live_routes, "require_project_site_access", lambda *a, **k: None):
            return _parse_sse("".join(asyncio.run(run())))

    def test_relay_forwards_frames_then_emits_unavailable_on_upstream_death(self) -> None:
        session = self._acquire()
        frames = self._drive_relay(
            session.session_id,
            [b'data: {"type":"snapshot","stats":{}}\n\n', b'data: {"type":"activity","paths":["a"]}\n\n'],
            raise_at_end=True,
        )
        kinds = [f.get("data", {}).get("type") or f["event"] for f in frames]
        self.assertEqual(kinds[:2], ["snapshot", "activity"], frames)
        self.assertEqual(frames[-1]["event"], "unavailable")
        # Nothing fabricated after the upstream failure.
        self.assertEqual(len(frames), 3)

    def test_relay_emits_closed_when_the_lease_is_lost(self) -> None:
        session = self._acquire()
        self.live_service.release(session.session_id)  # lease gone before the first frame is checked
        frames = self._drive_relay(session.session_id, [b'data: {"type":"snapshot"}\n\n'])
        self.assertEqual(frames[-1]["event"], "closed")


if __name__ == "__main__":
    unittest.main()
