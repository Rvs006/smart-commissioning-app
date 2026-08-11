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
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
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
        from app.services.run_service import RunService
        from sqlalchemy import event

        run = self._seed_terminal_run()
        dml: list[str] = []

        def record_dml(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in {"DELETE", "INSERT", "UPDATE"}:
                dml.append(statement)

        engine = RunService().engine
        event.listen(engine, "before_cursor_execute", record_dml)
        try:
            observed = self.client.get(f"/api/v1/validation/runs/{run['run_id']}")
            self.assertEqual(observed.status_code, 200, observed.text)

            with self.client.stream("GET", f"/api/v1/runs/{run['run_id']}/events") as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    response.headers["content-type"].startswith("text/event-stream"),
                    response.headers.get("content-type"),
                )
                self.assertEqual(response.headers["cache-control"], "no-store")
                # Reading to completion must terminate (the stream closes itself on
                # the terminal status); a non-closing stream would hang here.
                body = "".join(response.iter_text())
        finally:
            event.remove(engine, "before_cursor_execute", record_dml)

        self.assertEqual(dml, [], "GET and SSE must execute no lifecycle DML")

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

    def _seed_running_discovery(self):  # noqa: ANN202
        from app.core.db import get_engine
        from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
        from smart_commissioning_core.run_context import RunContextV1

        repository = RunLifecycleRepository(get_engine())
        context = RunContextV1.model_validate(
            {
                "project_id": "demo-project",
                "site_id": "demo-site",
                "configuration_snapshot": {},
                "configuration_version": 1,
                "registers": [],
                "imports": [],
                "schema_versions": {},
                "engine_parameters": {"authorized": True, "dry_run": False},
                "network_interface": "192.0.2.10/24",
                "connection_settings": {},
                "secret_references": {},
                "requesting_principal": "test-sse",
                "application_version": "0.1.41",
            }
        )
        envelope = repository.create_run_with_context(
            job_type="ip_discovery",
            context=context,
            execution_mode="dramatiq_worker",
        )
        lease = repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token="sse-observation-owner",
            lease_seconds=60,
        )
        self.assertIsNotNone(lease)
        return repository, lease

    def test_discovery_stream_emits_only_cursor_and_count_hints(self) -> None:
        from unittest import mock

        import app.api.routes.events as events_module
        from smart_commissioning_core.discovery_observations import (
            DiscoveryObservationInputV1,
        )

        repository, lease = self._seed_running_discovery()
        appended = repository.append_discovery_observation(
            lease.run_id,
            lease.owner_token,
            lease.attempt,
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="host",
                entity_key="host:viewer-safe",
                entity_version=1,
                event_key="host-viewer-safe-v1",
                phase="reachability",
                outcome="observed",
                payload_schema_version="1.0",
                payload={"projection_v1": {"collection": "devices", "record": {}}},
            ),
        )

        with mock.patch.object(events_module, "MAX_STREAM_SECONDS", 0.0):
            with self.client.stream("GET", f"/api/v1/runs/{lease.run_id}/events") as response:
                body = "".join(response.iter_text())

        frames = _parse_sse(body)
        progress = frames[0]["data"]
        self.assertEqual(progress["observation_attempt"], lease.attempt)
        self.assertEqual(progress["latest_observation_cursor"], appended.cursor)
        self.assertEqual(progress["progressive_counts"], {"observations": 1})
        self.assertNotIn("observations", progress)
        self.assertNotIn("viewer-safe", body)

    def test_discovery_cursor_store_failure_closes_as_unavailable(self) -> None:
        from unittest import mock

        import app.api.routes.events as events_module

        _repository, lease = self._seed_running_discovery()
        with mock.patch.object(
            events_module.lifecycle_repository,
            "get_discovery_observation_cutoff",
            side_effect=RuntimeError("control store unavailable"),
        ):
            with self.client.stream("GET", f"/api/v1/runs/{lease.run_id}/events") as response:
                body = "".join(response.iter_text())

        frames = _parse_sse(body)
        self.assertEqual(
            frames,
            [
                {
                    "event": "unavailable",
                    "data": {
                        "run_id": lease.run_id,
                        "status": "unavailable",
                    },
                }
            ],
        )

    def test_discovery_progress_payload_runs_off_the_event_loop(self) -> None:
        from unittest import mock

        import app.api.routes.events as events_module
        from app.core.auth import AuthPrincipal
        from smart_commissioning_core.rbac import Role

        _repository, lease = self._seed_running_discovery()
        principal = AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")
        real_to_thread = asyncio.to_thread
        threaded_functions: list[object] = []

        async def recording_to_thread(function, /, *args, **kwargs):  # noqa: ANN001, ANN202
            threaded_functions.append(function)
            return await real_to_thread(function, *args, **kwargs)

        async def read_first_frame() -> None:
            stream = events_module._run_event_stream(lease.run_id, principal)
            try:
                await stream.__anext__()
            finally:
                await stream.aclose()

        with (
            mock.patch.object(
                events_module.asyncio,
                "to_thread",
                side_effect=recording_to_thread,
            ),
            mock.patch.object(events_module, "POLL_INTERVAL_SECONDS", 0.0),
        ):
            asyncio.run(read_first_frame())

        self.assertIn(events_module._progress_payload, threaded_functions)

    def test_terminal_discovery_frame_keeps_the_sealed_count_after_pruning(self) -> None:
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from unittest import mock

        import app.api.routes.events as events_module

        run = SimpleNamespace(
            run_id="run-terminal-observations",
            job_type="ip_discovery",
            status="succeeded",
            stage="completed",
            progress_percent=100,
            updated_at=datetime.now(UTC),
            error_message=None,
        )
        pruned_cutoff = SimpleNamespace(
            attempt=2,
            terminal_cursor=0,
            observation_count=0,
        )
        sealed_marker = {
            "attempt": 2,
            "terminal_cursor": 987,
            "observation_count": 42,
        }

        with (
            mock.patch.object(events_module.service, "get_run_attempt_read_only", return_value=2),
            mock.patch.object(
                events_module.lifecycle_repository,
                "get_discovery_observation_cutoff",
                return_value=pruned_cutoff,
            ),
            mock.patch.object(
                events_module.service,
                "get_terminal_observation_marker_read_only",
                return_value=sealed_marker,
            ),
        ):
            payload = events_module._progress_payload(run)

        self.assertEqual(payload["latest_observation_cursor"], 987)
        self.assertEqual(payload["progressive_counts"], {"observations": 42})

    def test_cursor_only_change_emits_a_new_progress_frame(self) -> None:
        from unittest import mock

        import app.api.routes.events as events_module
        from app.core.auth import AuthPrincipal
        from smart_commissioning_core.discovery_observations import (
            DiscoveryObservationInputV1,
        )
        from smart_commissioning_core.rbac import Role

        repository, lease = self._seed_running_discovery()
        principal = AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")

        async def observe_cursor_change() -> tuple[dict, dict]:
            stream = events_module._run_event_stream(lease.run_id, principal)
            initial = _parse_sse(await stream.__anext__())[0]
            appended = repository.append_discovery_observation(
                lease.run_id,
                lease.owner_token,
                lease.attempt,
                DiscoveryObservationInputV1(
                    protocol="ip",
                    entity_kind="host",
                    entity_key="host:cursor-only",
                    entity_version=1,
                    event_key="host-cursor-only-v1",
                    phase="reachability",
                    outcome="observed",
                    payload_schema_version="1.0",
                    payload={},
                ),
            )
            changed = _parse_sse(await stream.__anext__())[0]
            self.assertEqual(
                changed["data"]["latest_observation_cursor"],
                appended.cursor,
            )
            await stream.aclose()
            return initial, changed

        with mock.patch.object(events_module, "POLL_INTERVAL_SECONDS", 0.0):
            initial, changed = asyncio.run(observe_cursor_change())

        self.assertEqual(initial["data"]["latest_observation_cursor"], 0)
        self.assertEqual(initial["data"]["stage"], changed["data"]["stage"])
        self.assertEqual(
            initial["data"]["progress_percent"],
            changed["data"]["progress_percent"],
        )

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
        from app.core.auth import AuthPrincipal
        from smart_commissioning_core.rbac import Role

        run_id = self._seed_nonterminal_run()
        principal = AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")

        async def close_after_first_frame() -> None:
            stream = events_module._run_event_stream(run_id, principal)
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
