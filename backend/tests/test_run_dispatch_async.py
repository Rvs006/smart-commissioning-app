"""Background inline execution contract (ITEM-4).

``dispatch_run`` runs an inline (portable-exe) run on a daemon thread when
``inline_run_async`` is set, so the POST returns immediately and the run monitor
can render while the run is live. These tests exercise the dispatcher directly
with a fake run store and a patched settings object -- no app, no database -- and
stay deterministic by coordinating the background thread with events (no sleeps
beyond tiny bounded waits).
"""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import run_dispatch

_WAIT = 3.0  # generous upper bound; the events fire well within this


class _FakeService:
    def __init__(self) -> None:
        self.status_calls: list[dict] = []
        self.status_written = threading.Event()

    def update_run_status(self, run_id, *, status, stage=None, progress_percent=None, error_message=None):
        self.status_calls.append(
            {"run_id": run_id, "status": status, "stage": stage, "error_message": error_message}
        )
        self.status_written.set()
        return SimpleNamespace(run_id=run_id, job_type="mqtt_discovery", status=status)

    def get_run(self, run_id):  # only the sync None-guard path uses this
        return SimpleNamespace(run_id=run_id, job_type="mqtt_discovery", status="running")


def _run() -> SimpleNamespace:
    return SimpleNamespace(run_id="run_abc", job_type="mqtt_discovery", status="queued")


def _dispatch(service, run_inline, *, inline_run_async: bool):
    settings = SimpleNamespace(
        inline_run_async=inline_run_async,
        job_execution_mode="inline",
        allow_inline_worker_fallback=True,
    )
    with mock.patch.object(run_dispatch, "get_settings", return_value=settings):
        return run_dispatch.dispatch_run(
            _run(),
            service=service,
            enqueue=None,  # None forces the inline path
            run_inline=run_inline,
            inline_message="MQTT discovery run started.",
            queued_message="queued",
            fallback_message="fallback",
        )


class BackgroundInlineDispatchTests(unittest.TestCase):
    def test_async_returns_before_run_finishes_then_writes_terminal(self) -> None:
        service = _FakeService()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_run_inline():
            started.set()
            self.assertTrue(release.wait(_WAIT), "release never signalled")
            service.update_run_status("run_abc", status="succeeded", stage="engine_complete", progress_percent=100)
            finished.set()
            return None

        response = _dispatch(service, slow_run_inline, inline_run_async=True)

        # POST returns immediately with the run's current (non-terminal) status.
        self.assertEqual(response.run_id, "run_abc")
        self.assertEqual(response.status, "queued")
        self.assertEqual(response.message, "MQTT discovery run started.")
        # The run really started on a background thread but has NOT finished.
        self.assertTrue(started.wait(_WAIT), "background run never started")
        self.assertFalse(finished.is_set())
        self.assertEqual(service.status_calls, [])

        release.set()
        self.assertTrue(finished.wait(_WAIT), "background run never finished")
        self.assertEqual(service.status_calls[-1]["status"], "succeeded")

    def test_async_crash_before_terminal_write_marks_run_failed(self) -> None:
        service = _FakeService()

        def crashing_run_inline():
            raise RuntimeError("boom before the engine wrapper could run")

        _dispatch(service, crashing_run_inline, inline_run_async=True)

        self.assertTrue(service.status_written.wait(_WAIT), "crash guard never wrote a terminal status")
        crash = service.status_calls[-1]
        self.assertEqual(crash["status"], "failed")
        self.assertEqual(crash["stage"], "inline_run_crashed")
        self.assertIn("run it again", crash["error_message"])

    def test_sync_mode_runs_in_thread_of_caller_and_reports_terminal(self) -> None:
        service = _FakeService()
        ran_on = {}

        def run_inline():
            ran_on["thread"] = threading.current_thread().name
            return SimpleNamespace(run_id="run_abc", job_type="mqtt_discovery", status="succeeded")

        response = _dispatch(service, run_inline, inline_run_async=False)

        # Synchronous: executed on the calling thread and the terminal status is
        # reported straight back on the accepted response.
        self.assertEqual(ran_on["thread"], threading.current_thread().name)
        self.assertEqual(response.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
