"""Tests for the SSE run-progress endpoint (GET /api/v1/runs/{run_id}/events).

Runs the FastAPI app against a temporary SQLite database in inline execution
mode, so a created validation run reaches a terminal status synchronously. The
stream therefore emits a progress frame and a terminal frame, then CLOSES — no
real worker, broker, or network is involved.

Auth runs in api_key mode here: the SSE route is mounted on the same protected
router as every other /api/v1 route, so the streaming auth wrinkle (EventSource
cannot send headers, hence the frontend uses fetch()+X-API-Key) is exercised by
asserting 401 without a key and a 200 stream with one.

Database sharing follows the established pattern (see test_runs_api.py): the
process-wide SCT_TEST_DATABASE_URL is reused so the engine instantiated at the
first app.main import points at the same file across modules.
"""

import asyncio
import json
import unittest

from harness import ApiTestCase

_API_KEY = "test-sse-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


def _parse_sse(body: str) -> list[dict]:
    """Parse an SSE response body into a list of {event, data} dicts.

    Frames are separated by a blank line. ``data:`` lines carry JSON.
    """
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


class SseEventsApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _seed_terminal_run(self) -> dict:
        """Create a UDMI validation run (terminal synchronously in inline mode)."""
        response = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": "udmi_validation",
                "parameters": {"requested_from": "test_sse"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        accepted = response.json()
        self.assertEqual(accepted["status"], "succeeded", "inline mode processes synchronously")
        return accepted

    def test_stream_for_terminal_run_emits_final_event_and_closes(self) -> None:
        run = self._seed_terminal_run()

        with self.client.stream("GET", f"/api/v1/runs/{run['run_id']}/events") as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-type"].startswith("text/event-stream"),
                response.headers.get("content-type"),
            )
            # Reading to completion must terminate (the stream closes itself on
            # the terminal status); a non-closing stream would hang here.
            body = "".join(response.iter_text())

        frames = _parse_sse(body)
        self.assertTrue(frames, "stream produced no frames")

        # A progress frame carries the run's status/stage/progress.
        progress_frames = [f for f in frames if f["event"] == "message"]
        self.assertTrue(progress_frames, "no progress frame emitted")
        self.assertEqual(progress_frames[0]["data"]["run_id"], run["run_id"])
        self.assertEqual(progress_frames[0]["data"]["status"], "succeeded")
        self.assertEqual(progress_frames[0]["data"]["progress_percent"], 100)

        # The final frame is the explicit terminal marker, then the stream ends.
        terminal_frames = [f for f in frames if f["event"] == "terminal"]
        self.assertEqual(len(terminal_frames), 1, "exactly one terminal frame expected")
        self.assertEqual(terminal_frames[-1]["data"]["status"], "succeeded")
        self.assertEqual(frames[-1]["event"], "terminal", "terminal frame must be last")

    def test_missing_run_returns_404(self) -> None:
        response = self.client.get("/api/v1/runs/run_00000000000000_deadbeef/events")
        self.assertEqual(response.status_code, 404, response.text)

    def test_stream_requires_api_key(self) -> None:
        run = self._seed_terminal_run()

        from fastapi.testclient import TestClient

        # A client with no key must be rejected before the stream opens.
        unauth = TestClient(self.app)
        no_key = unauth.get(f"/api/v1/runs/{run['run_id']}/events")
        self.assertEqual(no_key.status_code, 401, no_key.text)

        # The wrong key is also rejected, and never echoes key material.
        wrong = unauth.get(
            f"/api/v1/runs/{run['run_id']}/events",
            headers={"X-API-Key": "wrong-key"},
        )
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(_API_KEY, wrong.text)

        # With the valid key the stream opens and yields event-stream data.
        with unauth.stream(
            "GET",
            f"/api/v1/runs/{run['run_id']}/events",
            headers={"X-API-Key": _API_KEY},
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            body = "".join(response.iter_text())
        self.assertIn("data:", body)

    def _seed_nonterminal_run(self) -> str:
        """Insert a queued (non-terminal) run directly so the stream cannot reach
        a terminal status first; returns its run_id."""
        from app.core.db import get_engine
        from smart_commissioning_core.db.db_run_store import DbRunStore

        store = DbRunStore(get_engine())
        record = store.create_run(
            project_id="demo-project",
            site_id="demo-site",
            job_type="udmi_validation",
            parameters={"requested_from": "test_sse_nonterminal"},
        )
        run_id = record["run_id"]
        self.assertNotIn(record["status"], {"succeeded", "failed", "cancelled"})
        return run_id

    def test_nonterminal_stream_emits_timeout_then_closes(self) -> None:
        from unittest import mock

        import app.api.routes.events as events_module

        run_id = self._seed_nonterminal_run()

        # Force the wall-clock cap to fire immediately so a non-terminal run hits
        # the timeout branch instead of spinning. Keep the poll interval tiny too.
        with (
            mock.patch.object(events_module, "MAX_STREAM_SECONDS", 0.0),
            mock.patch.object(events_module, "POLL_INTERVAL_SECONDS", 0.001),
        ):
            with self.client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
                self.assertEqual(response.status_code, 200)
                # Must terminate (a non-closing stream would hang here).
                body = "".join(response.iter_text())

        frames = _parse_sse(body)
        self.assertTrue(frames, "stream produced no frames")
        # No terminal frame (the run never terminated); a timeout frame closes it.
        self.assertNotIn("terminal", {f["event"] for f in frames})
        timeout_frames = [f for f in frames if f["event"] == "timeout"]
        self.assertEqual(len(timeout_frames), 1, frames)
        self.assertEqual(frames[-1]["event"], "timeout", "timeout frame must be last")
        self.assertEqual(timeout_frames[0]["data"]["run_id"], run_id)

    def test_client_abort_mid_stream_does_not_raise(self) -> None:
        import app.api.routes.events as events_module

        run_id = self._seed_nonterminal_run()

        async def close_after_first_frame() -> None:
            stream = events_module._run_event_stream(run_id)
            await stream.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await stream.athrow(asyncio.CancelledError)

        asyncio.run(close_after_first_frame())

        # The app stays usable after an aborted stream (no leaked/raised state).
        followup = self.client.get("/api/v1/runs/run_00000000000000_deadbeef/events")
        self.assertEqual(followup.status_code, 404, followup.text)

    def test_stream_bearer_authorization_also_accepted(self) -> None:
        run = self._seed_terminal_run()

        from fastapi.testclient import TestClient

        unauth = TestClient(self.app)
        with unauth.stream(
            "GET",
            f"/api/v1/runs/{run['run_id']}/events",
            headers={"Authorization": f"Bearer {_API_KEY}"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        frames = _parse_sse(body)
        self.assertEqual(frames[-1]["event"], "terminal")


if __name__ == "__main__":
    unittest.main()
