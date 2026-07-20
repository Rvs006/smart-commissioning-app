"""Run-recovery resilience: the queue-vs-inline discriminator, the startup
orphan-run sweep, and persister session hygiene.

The field failure was a real BACnet run that 500'd during result persistence and
was fossilized at status "running" forever. Several backend defenses are covered
here: a mid-persist raise must leave the DB session usable so the framework's
terminal-"failed" write still lands; a run left at "running" by a restart is
reclaimed at startup (unless it was handed to the worker queue); the inline
dispatch path must not 500 when the engine framework returns None because even
its terminal write failed; and the queue-dispatch path must record its
worker-dispatch markers BEFORE enqueue so the sweep can never false-fail a live
worker run.
"""

import unittest

from harness import ApiTestCase

_API_KEY = "test-run-recovery-key"

_ENV = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

# Auto mode with inline fallback enabled, so dispatch_run exercises the real
# queue branch (and its Redis-down inline fallback) instead of the inline
# short-circuit.
_QUEUE_ENV = {
    "JOB_EXECUTION_MODE": "auto",
    "ALLOW_INLINE_WORKER_FALLBACK": "true",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


def _new_bacnet_run():
    """Create a fresh RunService and a queued bacnet_discovery run; return both."""
    from app.schemas.jobs import JobCreateRequest
    from app.services.run_service import RunService

    run_service = RunService()
    run = run_service.create_job_run(
        JobCreateRequest(
            project_id="demo-project",
            site_id="demo-site",
            job_type="bacnet_discovery",
            parameters={},
        ),
        expected_job_type="bacnet_discovery",
    )
    return run_service, run.run_id


class WasQueuedToWorkerTests(unittest.TestCase):
    """The discriminator that decides which stuck runs the sweep may touch."""

    def test_queue_dispatch_markers_are_detected(self) -> None:
        from app.services.run_service import _was_queued_to_worker

        # run_dispatch.dispatch_run is the only writer of these into result_summary.
        self.assertTrue(_was_queued_to_worker({"queue_name": "discovery"}))
        self.assertTrue(_was_queued_to_worker({"actor_name": "discover_bacnet"}))
        self.assertTrue(
            _was_queued_to_worker({"queue_name": "discovery", "actor_name": "discover_bacnet"})
        )

    def test_create_and_inline_defaults_are_not_queue_markers(self) -> None:
        from app.services.run_service import _was_queued_to_worker

        # The run store stamps queued/worker_required on EVERY freshly created run
        # (inline runs included), so they must not read as "went to the worker".
        self.assertFalse(_was_queued_to_worker({"queued": True, "worker_required": True}))
        self.assertFalse(_was_queued_to_worker({"execution_mode": "inline_local_fallback"}))
        self.assertFalse(_was_queued_to_worker({}))


class OrphanRunSweepTests(ApiTestCase):
    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def _new_run(self):
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        run_service = RunService()
        run = run_service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type="bacnet_discovery",
                parameters={},
            ),
            expected_job_type="bacnet_discovery",
        )
        return run_service, run.run_id

    def test_stuck_inline_run_is_swept_to_failed(self) -> None:
        from app.services.run_service import INTERRUPTED_RUN_MESSAGE, RunService

        run_service, run_id = self._new_run()
        # Fossilize the run at "running", as a restart mid-persist would leave it.
        run_service.update_run_status(
            run_id, status="running", stage="engine_running", progress_percent=15
        )

        swept = RunService().sweep_interrupted_runs()

        self.assertIn(run_id, swept)
        record = run_service.get_run(run_id)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.stage, "interrupted_by_restart")
        self.assertEqual(record.error_message, INTERRUPTED_RUN_MESSAGE)

    def test_queued_worker_run_is_left_alone(self) -> None:
        from app.services.run_service import RunService

        run_service, run_id = self._new_run()
        # A run handed to the worker queue carries the dispatch markers and may
        # still be executing on a worker, so the sweep must never touch it.
        run_service.update_result_summary(
            run_id, {"queue_name": "discovery", "actor_name": "discover_bacnet"}
        )
        run_service.update_run_status(
            run_id, status="running", stage="engine_running", progress_percent=15
        )
        # Leave the shared DB tidy for later class boots (the run is not swept, so
        # it would otherwise linger at "running").
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        swept = RunService().sweep_interrupted_runs()

        self.assertNotIn(run_id, swept)
        self.assertEqual(run_service.get_run(run_id).status, "running")

    def test_stuck_queued_inline_run_is_swept_to_failed(self) -> None:
        from app.services.run_service import INTERRUPTED_RUN_MESSAGE, RunService

        run_service, run_id = self._new_run()
        # A backgrounded inline run (ITEM-4) is committed at the default "queued"
        # status and only flips to "running" once its daemon thread starts. A
        # portable-exe process exit in that window strands the run at "queued"
        # with no worker markers; the sweep must reclaim it exactly like a stuck
        # "running" run, or the module head's Execute stays disabled across
        # restarts.
        self.assertEqual(run_service.get_run(run_id).status, "queued")

        swept = RunService().sweep_interrupted_runs()

        self.assertIn(run_id, swept)
        record = run_service.get_run(run_id)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.stage, "interrupted_by_restart")
        self.assertEqual(record.error_message, INTERRUPTED_RUN_MESSAGE)

    def test_queued_worker_run_at_queued_status_is_left_alone(self) -> None:
        from app.services.run_service import RunService

        run_service, run_id = self._new_run()
        # A run enqueued to the worker is left at "queued" until a worker picks it
        # up. It carries the dispatch markers, so the sweep must not touch it even
        # at "queued" — a worker across the fleet may still run it.
        run_service.update_result_summary(
            run_id, {"queue_name": "discovery", "actor_name": "discover_bacnet"}
        )
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        swept = RunService().sweep_interrupted_runs()

        self.assertNotIn(run_id, swept)
        self.assertEqual(run_service.get_run(run_id).status, "queued")


class PersisterSessionHygieneTests(ApiTestCase):
    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def test_failed_persist_leaves_session_usable_for_status_write(self) -> None:
        from app.schemas.jobs import JobCreateRequest
        from app.services.engine_dispatch import make_device_point_persister
        from app.services.run_service import RunService
        from smart_commissioning_core.db.repositories import DiscoveryRepository
        from sqlalchemy.exc import SQLAlchemyError

        run_service = RunService()
        run = run_service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id="demo-site",
                job_type="bacnet_discovery",
                parameters={},
            ),
            expected_job_type="bacnet_discovery",
        )
        run_service.update_run_status(
            run.run_id, status="running", stage="engine_running", progress_percent=15
        )

        persist = make_device_point_persister(DiscoveryRepository(run_service.engine))
        # A point row whose observed_value carries a non-JSON-serializable object
        # reproduces the field failure (a raw bacpypes3 present-value): JSON
        # serialization raises during the repository's flush.
        poisoned = [{"point_id": "bi-1", "device_ref": "device-1", "observed_value": {"value": object()}}]
        # The unserializable value raises during flush as a raw TypeError or, if
        # SQLAlchemy wraps the bind-processor failure, a StatementError.
        with self.assertRaises((TypeError, SQLAlchemyError)):
            persist(run.run_id, poisoned)

        # The mid-flush raise must have rolled back cleanly, so the framework's
        # subsequent terminal-"failed" write (a fresh session on the same engine)
        # still succeeds and the run is never fossilized at "running".
        record = run_service.update_run_status(
            run.run_id,
            status="failed",
            stage="engine_failed",
            progress_percent=100,
            error_message="engine failed",
        )
        self.assertEqual(record.status, "failed")
        # A follow-up read on the shared engine is also healthy.
        self.assertEqual(run_service.get_run(run.run_id).status, "failed")


class InlineDispatchStoreFailureTests(ApiTestCase):
    """The inline dispatch path must survive run_engine returning None.

    engines.base._safe_update_run_status returns None when even the terminal
    status write fails (a poisoned session / disk-full / locked DB), so the whole
    inline chain returns None. dispatch_run used to dereference that None as
    ``processed.run_id`` and AttributeError into a 500 while the run stayed
    'running' — the exact failure class the store-failure guard is meant to
    remove.
    """

    env = _ENV
    client_headers = {"X-API-Key": _API_KEY}

    def test_none_from_run_inline_does_not_500_and_reports_running(self) -> None:
        from app.services.run_dispatch import dispatch_run

        run_service, run_id = _new_bacnet_run()
        run = run_service.get_run(run_id)
        # run_engine writes 'running' first, then its terminal write fails and it
        # returns None, leaving the run fossilized at 'running'.
        run_service.update_run_status(
            run_id, status="running", stage="engine_running", progress_percent=15
        )
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        response = dispatch_run(
            run,
            service=run_service,
            enqueue=None,
            run_inline=lambda: None,
            inline_message="inline",
            queued_message="queued",
            fallback_message="fallback",
        )

        # No AttributeError/500: a clean accepted response carrying the run id, and
        # the re-read reports the real current state (still 'running'; the startup
        # sweep reclaims it on the next restart).
        self.assertEqual(response.run_id, run_id)
        self.assertEqual(response.job_type, "bacnet_discovery")
        self.assertEqual(response.status, "running")
        self.assertEqual(response.message, "inline")

    def test_none_and_unreadable_store_falls_back_to_created_run(self) -> None:
        from app.services.run_dispatch import dispatch_run

        run_service, run_id = _new_bacnet_run()
        run = run_service.get_run(run_id)
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        class _ReadFailsService:
            # Only get_run is reached on the inline path; make it raise to model a
            # store so broken that even the re-read fails.
            def get_run(self, _run_id: str):
                raise RuntimeError("run store is unreadable")

        response = dispatch_run(
            run,
            service=_ReadFailsService(),
            enqueue=None,
            run_inline=lambda: None,
            inline_message="inline",
            queued_message="queued",
            fallback_message="fallback",
        )

        # Still no 500: the response falls back to the run as created.
        self.assertEqual(response.run_id, run_id)
        self.assertEqual(response.status, run.status)


class QueueDispatchMarkerTimingTests(ApiTestCase):
    """The queue path must mark a run worker-bound BEFORE it enqueues it.

    Once enqueue() returns, a worker can pick the job up and flip the run to
    'running'. If the queue markers were written only after enqueue, a backend
    crash in that window left a live worker run marker-less, and the startup
    sweep would false-fail it. The markers must therefore be durable first, and
    cleared again if the dispatch falls back to inline.
    """

    env = _QUEUE_ENV
    client_headers = {"X-API-Key": _API_KEY}

    def test_worker_markers_written_before_enqueue(self) -> None:
        from app.services.job_queue import JobDispatch
        from app.services.run_dispatch import dispatch_run
        from app.services.run_service import _was_queued_to_worker

        run_service, run_id = _new_bacnet_run()
        run = run_service.get_run(run_id)
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        observed: dict[str, bool] = {}

        class _FakeEnqueuer:
            queue_name = "discovery"
            actor_name = "discover_bacnet"

            def __call__(self, _run) -> JobDispatch:
                # By the time the message is enqueued, the markers must already be
                # durable — that is the whole point.
                summary = run_service.get_run(run_id).result_summary
                observed["queued_before_enqueue"] = _was_queued_to_worker(summary)
                return JobDispatch(actor_name=self.actor_name, queue_name=self.queue_name)

        response = dispatch_run(
            run,
            service=run_service,
            enqueue=_FakeEnqueuer(),
            run_inline=lambda: run_service.get_run(run_id),
            inline_message="inline",
            queued_message="queued",
            fallback_message="fallback",
        )

        self.assertTrue(
            observed.get("queued_before_enqueue"),
            "worker markers must be durable before the message is enqueued",
        )
        self.assertEqual(response.message, "queued")
        self.assertTrue(_was_queued_to_worker(run_service.get_run(run_id).result_summary))

    def test_inline_fallback_clears_markers_so_sweep_can_reclaim(self) -> None:
        from app.services.job_queue import JobQueueUnavailable
        from app.services.run_dispatch import dispatch_run
        from app.services.run_service import RunService, _was_queued_to_worker

        run_service, run_id = _new_bacnet_run()
        run = run_service.get_run(run_id)
        self.addCleanup(
            run_service.update_run_status,
            run_id,
            status="cancelled",
            stage="test_cleanup",
            progress_percent=100,
        )

        class _FailingEnqueuer:
            queue_name = "discovery"
            actor_name = "discover_bacnet"

            def __call__(self, _run):
                raise JobQueueUnavailable("redis down")

        def run_inline():
            # Redis was down, so we fall back to inline; model that inline run
            # fossilizing at 'running' (its terminal write was lost).
            run_service.update_run_status(
                run_id, status="running", stage="engine_running", progress_percent=15
            )
            return None

        response = dispatch_run(
            run,
            service=run_service,
            enqueue=_FailingEnqueuer(),
            run_inline=run_inline,
            inline_message="inline",
            queued_message="queued",
            fallback_message="fallback",
        )

        self.assertEqual(response.message, "fallback")
        # The pre-enqueue markers must be cleared: no worker will ever run this, so
        # the sweep must be free to reclaim it.
        summary = run_service.get_run(run_id).result_summary
        self.assertFalse(
            _was_queued_to_worker(summary),
            "the inline fallback must clear the worker markers",
        )
        swept = RunService().sweep_interrupted_runs()
        self.assertIn(run_id, swept)


if __name__ == "__main__":
    unittest.main()
