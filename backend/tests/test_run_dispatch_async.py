"""Background inline execution contract (ITEM-4).

``dispatch_run`` runs an inline (portable-exe) run on a daemon thread when
``inline_run_async`` is set, so the POST returns immediately and the run monitor
can render while the run is live. These tests exercise the dispatcher directly
with a fake run store and a patched settings object -- no app, no database -- and
stay deterministic by coordinating the background thread with events (no sleeps
beyond tiny bounded waits).
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import run_dispatch

_WAIT = 3.0  # generous upper bound; the events fire well within this


class _FakeService:
    def __init__(self, *, heartbeat_plan: list[object] | None = None) -> None:
        self.status_calls: list[dict] = []
        self.status_written = threading.Event()
        self.status = "running"
        self.lease = SimpleNamespace(run_id="run_abc")
        self.claimed_lease_seconds: int | None = None
        self.heartbeat_calls = 0
        self.heartbeat_plan = list(heartbeat_plan or [])
        self.heartbeat_returned_false = threading.Event()
        self._heartbeat_lock = threading.Lock()
        self._terminal_outcome = None
        self.ownership_lost = False

    def update_run_status(self, run_id, *, status, stage=None, progress_percent=None, error_message=None):
        self.status_calls.append({"run_id": run_id, "status": status, "stage": stage, "error_message": error_message})
        self.status = status
        if status in {"succeeded", "failed", "cancelled"}:
            self._terminal_outcome = SimpleNamespace(conflict=False)
        self.status_written.set()
        return SimpleNamespace(run_id=run_id, job_type="mqtt_discovery", status=status)

    @property
    def terminal_outcome(self):
        return self._terminal_outcome

    def heartbeat(self, *, lease_seconds):
        with self._heartbeat_lock:
            self.heartbeat_calls += 1
            action = self.heartbeat_plan.pop(0) if self.heartbeat_plan else True
        if isinstance(action, BaseException):
            raise action
        if action is False:
            self.heartbeat_returned_false.set()
        return action

    def mark_ownership_lost(self):
        self.ownership_lost = True

    def get_run(self, run_id):  # only the sync None-guard path uses this
        return SimpleNamespace(run_id=run_id, job_type="mqtt_discovery", status=self.status)

    def get_dispatch_for_run(self, run_id):
        return SimpleNamespace(run_id=run_id, dispatch_id="dispatch_abc")

    def get_execution_context(self, run_id):
        return SimpleNamespace(context=SimpleNamespace(engine_parameters={}))

    def claim_owned_run(self, run_id, *, lease_seconds=60):
        self.claimed_lease_seconds = lease_seconds
        return self

    def mark_dispatch_published(self, dispatch_id):
        return True


def _run() -> SimpleNamespace:
    return SimpleNamespace(run_id="run_abc", job_type="mqtt_discovery", status="queued")


def _dispatch(
    case,
    service,
    run_inline,
    *,
    inline_run_async: bool,
    resolver=None,
):
    settings = SimpleNamespace(
        inline_run_async=inline_run_async,
        job_execution_mode="inline",
    )
    case.enterContext(mock.patch.object(run_dispatch, "get_settings", return_value=settings))
    case.enterContext(mock.patch.object(run_dispatch, "_INLINE_LEASE_SECONDS", 1))
    case.enterContext(mock.patch.object(run_dispatch, "_INLINE_HEARTBEAT_SECONDS", 0.01))
    if resolver is None:
        case.enterContext(
            mock.patch.object(run_dispatch, "resolve_context_parameters", return_value={})
        )
    else:
        case.enterContext(
            mock.patch.object(
                run_dispatch,
                "resolve_context_parameters",
                side_effect=resolver,
            )
        )
    return run_dispatch.dispatch_run(
        _run(),
        service=service,
        enqueue=None,  # None forces the inline path
        run_inline=run_inline,
        inline_message="MQTT discovery run started.",
        queued_message="queued",
    )


def _wait_for_heartbeat_calls(service: _FakeService, expected: int) -> None:
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        with service._heartbeat_lock:
            if service.heartbeat_calls >= expected:
                return
        threading.Event().wait(0.01)
    raise AssertionError(
        f"observed {service.heartbeat_calls} heartbeat(s), expected {expected}"
    )


def _wait_for_thread_exit(prefix: str) -> None:
    deadline = time.monotonic() + _WAIT
    while time.monotonic() < deadline:
        if not any(thread.name.startswith(prefix) for thread in threading.enumerate()):
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"thread with prefix {prefix!r} did not stop")


class BackgroundInlineDispatchTests(unittest.TestCase):
    def test_exhausted_lease_retry_never_becomes_a_tight_loop(self) -> None:
        heartbeat = run_dispatch.InlineRunHeartbeat(
            _FakeService(),
            lease_seconds=60,
            interval_seconds=15,
        )

        self.assertEqual(heartbeat._retry_delay(0), 5.0)
        self.assertGreaterEqual(heartbeat._retry_delay(-1), 1.0)
        self.assertEqual(heartbeat._retry_delay(10), 1.0)

    def test_heartbeat_constructor_rejects_non_finite_or_excessive_timing(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive and below"):
            run_dispatch.InlineRunHeartbeat(
                _FakeService(),
                lease_seconds=60,
                interval_seconds=float("nan"),
            )
        with self.assertRaisesRegex(ValueError, "no more than 300"):
            run_dispatch.InlineRunHeartbeat(
                _FakeService(),
                lease_seconds=32_400,
                interval_seconds=15,
            )

    def test_heartbeat_start_failure_clears_unstarted_thread(self) -> None:
        heartbeat = run_dispatch.InlineRunHeartbeat(
            _FakeService(),
            lease_seconds=60,
            interval_seconds=15,
        )
        with mock.patch.object(
            run_dispatch.threading.Thread,
            "start",
            side_effect=RuntimeError("heartbeat start failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "heartbeat start failed"):
                heartbeat.start()

        self.assertFalse(heartbeat.is_alive)
        heartbeat.stop_and_join()

    def test_heartbeat_construction_failure_seals_claimed_run(self) -> None:
        service = _FakeService()
        with mock.patch.object(
            run_dispatch,
            "InlineRunHeartbeat",
            side_effect=ValueError("invalid heartbeat timing"),
        ):
            response = _dispatch(
                self,
                service,
                lambda _store, _parameters: None,
                inline_run_async=True,
            )

        self.assertEqual(response.status, "failed")
        self.assertEqual(
            service.status_calls[-1]["stage"],
            "inline_heartbeat_start_failed",
        )

    def test_async_returns_before_run_finishes_then_writes_terminal(self) -> None:
        service = _FakeService()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_run_inline(_run_store, _parameters):
            started.set()
            self.assertTrue(release.wait(_WAIT), "release never signalled")
            service.update_run_status("run_abc", status="succeeded", stage="engine_complete", progress_percent=100)
            finished.set()
            return None

        response = _dispatch(self, service, slow_run_inline, inline_run_async=True)

        # POST returns immediately with the run's current (non-terminal) status.
        self.assertEqual(response.run_id, "run_abc")
        self.assertEqual(response.status, "queued")
        self.assertEqual(response.message, "MQTT discovery run started.")
        # The run really started on a background thread but has NOT finished.
        self.assertTrue(started.wait(_WAIT), "background run never started")
        self.assertFalse(finished.is_set())
        self.assertEqual(service.status_calls, [])
        _wait_for_heartbeat_calls(service, 2)
        self.assertEqual(service.claimed_lease_seconds, 1)

        release.set()
        self.assertTrue(finished.wait(_WAIT), "background run never finished")
        _wait_for_thread_exit("inline-heartbeat-run_abc")
        self.assertEqual(
            [call["status"] for call in service.status_calls],
            ["succeeded"],
        )

    def test_async_crash_before_terminal_write_marks_run_failed(self) -> None:
        service = _FakeService()

        def crashing_run_inline(_run_store, _parameters):
            raise RuntimeError("boom before the engine wrapper could run")

        _dispatch(self, service, crashing_run_inline, inline_run_async=True)

        self.assertTrue(service.status_written.wait(_WAIT), "crash guard never wrote a terminal status")
        crash = service.status_calls[-1]
        self.assertEqual(crash["status"], "failed")
        self.assertEqual(crash["stage"], "inline_run_crashed")
        self.assertIn("run it again", crash["error_message"])
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_processor_terminal_success_then_cleanup_error_stays_successful(self) -> None:
        service = _FakeService()

        def successful_then_crashing(_run_store, _parameters):
            service.update_run_status(
                "run_abc",
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )
            raise RuntimeError("cleanup failed after commit")

        _dispatch(
            self,
            service,
            successful_then_crashing,
            inline_run_async=True,
        )

        self.assertTrue(service.status_written.wait(_WAIT))
        _wait_for_thread_exit("inline-run-run_abc")
        _wait_for_thread_exit("inline-heartbeat-run_abc")
        self.assertEqual(
            [call["status"] for call in service.status_calls],
            ["succeeded"],
        )
        self.assertFalse(service.terminal_outcome.conflict)

    def test_sync_mode_renews_while_caller_is_blocked(self) -> None:
        service = _FakeService()
        ran_on = {}
        started = threading.Event()
        release = threading.Event()
        response: dict[str, object] = {}

        def run_inline(_run_store, _parameters):
            ran_on["thread"] = threading.current_thread().name
            started.set()
            self.assertTrue(release.wait(_WAIT), "release never signalled")
            service.update_run_status("run_abc", status="succeeded", stage="engine_complete", progress_percent=100)

        def call_dispatch():
            response["value"] = _dispatch(
                self,
                service,
                run_inline,
                inline_run_async=False,
            )

        caller = threading.Thread(target=call_dispatch, name="sync-inline-caller")
        caller.start()
        self.assertTrue(started.wait(_WAIT), "sync processor never started")
        _wait_for_heartbeat_calls(service, 2)
        self.assertTrue(caller.is_alive(), "caller should remain blocked by sync execution")
        release.set()
        caller.join(_WAIT)
        self.assertFalse(caller.is_alive())

        # Synchronous: executed on the calling thread and the terminal status is
        # reported straight back on the accepted response.
        self.assertEqual(ran_on["thread"], "sync-inline-caller")
        self.assertEqual(response["value"].status, "succeeded")
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_heartbeat_protects_slow_context_resolution(self) -> None:
        service = _FakeService()
        resolving = threading.Event()
        release_resolution = threading.Event()
        finished = threading.Event()

        def slow_resolver(*_args, **_kwargs):
            resolving.set()
            self.assertTrue(
                release_resolution.wait(_WAIT),
                "context-resolution release never signalled",
            )
            return {}

        def run_inline(_run_store, _parameters):
            service.update_run_status(
                "run_abc",
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )
            finished.set()

        _dispatch(
            self,
            service,
            run_inline,
            inline_run_async=True,
            resolver=slow_resolver,
        )
        self.assertTrue(resolving.wait(_WAIT), "context resolution never started")
        _wait_for_heartbeat_calls(service, 2)
        self.assertEqual(service.status_calls, [])

        release_resolution.set()
        self.assertTrue(finished.wait(_WAIT), "processor did not finish")
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_context_resolution_failure_seals_and_stops_heartbeat(self) -> None:
        service = _FakeService()
        processor_called = threading.Event()

        def run_inline(_run_store, _parameters):
            processor_called.set()

        _dispatch(
            self,
            service,
            run_inline,
            inline_run_async=True,
            resolver=FileNotFoundError("missing reference"),
        )

        self.assertTrue(service.status_written.wait(_WAIT))
        self.assertFalse(processor_called.is_set())
        self.assertEqual(service.status_calls[-1]["stage"], "execution_context_unavailable")
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_cancellation_finalization_stops_heartbeat(self) -> None:
        service = _FakeService()
        finished = threading.Event()

        def run_inline(_run_store, _parameters):
            service.update_run_status(
                "run_abc",
                status="cancelled",
                stage="engine_cancelled",
                progress_percent=100,
            )
            finished.set()

        _dispatch(self, service, run_inline, inline_run_async=True)

        self.assertTrue(finished.wait(_WAIT))
        self.assertEqual(service.status_calls[-1]["status"], "cancelled")
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_normal_failure_finalization_stops_heartbeat(self) -> None:
        service = _FakeService()
        finished = threading.Event()

        def run_inline(_run_store, _parameters):
            service.update_run_status(
                "run_abc",
                status="failed",
                stage="engine_failed",
                progress_percent=100,
                error_message="sanitized failure",
            )
            finished.set()

        _dispatch(self, service, run_inline, inline_run_async=True)

        self.assertTrue(finished.wait(_WAIT))
        self.assertEqual(service.status_calls[-1]["status"], "failed")
        _wait_for_thread_exit("inline-heartbeat-run_abc")

    def test_confirmed_ownership_loss_fences_crash_finalization(self) -> None:
        service = _FakeService(heartbeat_plan=[False])
        started = threading.Event()
        release = threading.Event()
        processor_done = threading.Event()

        def run_inline(_run_store, _parameters):
            started.set()
            self.assertTrue(release.wait(_WAIT), "release never signalled")
            processor_done.set()
            raise RuntimeError("stale executor must not finalize")

        _dispatch(self, service, run_inline, inline_run_async=True)

        self.assertTrue(started.wait(_WAIT))
        self.assertTrue(service.heartbeat_returned_false.wait(_WAIT))
        release.set()
        self.assertTrue(processor_done.wait(_WAIT))
        _wait_for_thread_exit("inline-run-run_abc")
        _wait_for_thread_exit("inline-heartbeat-run_abc")
        self.assertEqual(service.status_calls, [])

    def test_transient_heartbeat_exception_is_redacted_and_retried(self) -> None:
        canary = "credential-canary-must-not-reach-logs"
        service = _FakeService(
            heartbeat_plan=[RuntimeError(canary), True, True]
        )
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def run_inline(_run_store, _parameters):
            started.set()
            self.assertTrue(release.wait(_WAIT), "release never signalled")
            service.update_run_status(
                "run_abc",
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )
            finished.set()

        with self.assertLogs("app.services.inline_heartbeat", level="INFO") as captured:
            _dispatch(self, service, run_inline, inline_run_async=True)
            self.assertTrue(started.wait(_WAIT))
            _wait_for_heartbeat_calls(service, 3)
            self.assertEqual(service.status_calls, [])
            release.set()
            self.assertTrue(finished.wait(_WAIT))
            _wait_for_thread_exit("inline-heartbeat-run_abc")

        logs = "\n".join(captured.output)
        self.assertNotIn(canary, logs)
        self.assertIn("retrying", logs)
        self.assertIn("recovered", logs)

    def test_executor_start_failure_finalizes_before_heartbeat_stops(self) -> None:
        service = _FakeService()
        order: list[str] = []
        original_update = service.update_run_status

        class TrackingHeartbeat:
            ownership_lost = False

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                order.append("heartbeat-start")

            def stop_and_join(self):
                order.append("heartbeat-stop")

        def tracked_update(*args, **kwargs):
            order.append("finalize")
            return original_update(*args, **kwargs)

        service.update_run_status = tracked_update
        with (
            mock.patch.object(
                run_dispatch,
                "InlineRunHeartbeat",
                TrackingHeartbeat,
            ),
            mock.patch.object(
                run_dispatch.threading.Thread,
                "start",
                side_effect=RuntimeError("thread start failed"),
            ),
        ):
            response = _dispatch(
                self,
                service,
                lambda _store, _parameters: None,
                inline_run_async=True,
            )

        self.assertEqual(response.status, "failed")
        self.assertEqual(
            order,
            ["heartbeat-start", "finalize", "heartbeat-stop"],
        )
        self.assertEqual(
            service.status_calls[-1]["stage"],
            "inline_executor_start_failed",
        )

    def test_executor_construction_failure_finalizes_and_stops_heartbeat(self) -> None:
        service = _FakeService()
        order: list[str] = []
        original_update = service.update_run_status

        class TrackingHeartbeat:
            ownership_lost = False

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                order.append("heartbeat-start")

            def stop_and_join(self):
                order.append("heartbeat-stop")

        def tracked_update(*args, **kwargs):
            order.append("finalize")
            return original_update(*args, **kwargs)

        service.update_run_status = tracked_update
        with (
            mock.patch.object(
                run_dispatch,
                "InlineRunHeartbeat",
                TrackingHeartbeat,
            ),
            mock.patch.object(
                run_dispatch.threading,
                "Thread",
                side_effect=RuntimeError("thread construction failed"),
            ),
        ):
            response = _dispatch(
                self,
                service,
                lambda _store, _parameters: None,
                inline_run_async=True,
            )

        self.assertEqual(response.status, "failed")
        self.assertEqual(
            order,
            ["heartbeat-start", "finalize", "heartbeat-stop"],
        )
        self.assertEqual(
            service.status_calls[-1]["stage"],
            "inline_executor_start_failed",
        )


if __name__ == "__main__":
    unittest.main()
