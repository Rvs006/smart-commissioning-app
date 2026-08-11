"""Real-database regression for portable inline lease renewal."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import run_dispatch
from app.services.inline_heartbeat import InlineRunHeartbeat
from app.services.run_service import RunService
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    Run,
    RunExecutionContext,
    RunResult,
    RunSeal,
)
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.mqtt_transport import MqttCaptureOutcome
from smart_commissioning_core.owned_run_store import (
    OwnedRunStore,
    OwnershipLostError,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run
from sqlalchemy import event, func, select, update

_WAIT = 5.0


class EventControlledCapture:
    """Quiet broker stand-in that blocks until the test releases the window."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[dict[str, object]] = []

    def __call__(self, _settings: object, **kwargs: object) -> MqttCaptureOutcome:
        self.calls.append(kwargs)
        self.started.set()
        if not self.release.wait(_WAIT):
            raise TimeoutError("test capture was not released")
        return MqttCaptureOutcome(
            messages=[],
            termination="deadline_elapsed",
            deadline_requested=True,
            deadline_elapsed=True,
            cancelled=False,
            primary_cap_reached=False,
            primary_byte_cap_reached=False,
            secondary_truncated=False,
            secondary_count_truncated=False,
            secondary_byte_truncated=False,
            primary_retained_count=0,
            secondary_retained_count=0,
            primary_retained_bytes=0,
            secondary_retained_bytes=0,
        )


def _context(parameters: dict[str, object]) -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": "heartbeat-project",
            "site_id": "heartbeat-site",
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [],
            "schema_versions": {"udmi": "1.5.2"},
            "engine_parameters": parameters,
            "network_interface": None,
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "integration-test",
            "application_version": "0.1.28",
            "protocol_key": None,
        }
    )


class RealInlineHeartbeatIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.engine = create_engine_from_url(
            default_sqlite_url(Path(temp_dir.name))
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.lifecycle = RunLifecycleRepository(self.engine)
        self.service = RunService(self.engine)

    def _create_run(
        self,
        *,
        job_type: str,
        parameters: dict[str, object],
    ):
        envelope = self.lifecycle.create_run_with_context(
            job_type=job_type,
            context=_context(parameters),
            execution_mode="inline",
            edge_id="edge-heartbeat-test",
        )
        return self.service.get_run(envelope.run_id)

    def test_tampered_context_is_rejected_before_inline_processor(self) -> None:
        run = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 60},
        )
        with self.engine.begin() as connection:
            stored = connection.execute(
                RunExecutionContext.__table__.select().where(
                    RunExecutionContext.run_id == run.run_id
                )
            ).mappings().one()
            tampered = dict(stored["context_json"])
            tampered["engine_parameters"] = {"capture_seconds": 61}
            connection.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == run.run_id)
                .values(context_json=tampered)
            )
        processor = mock.Mock()
        settings = SimpleNamespace(
            inline_run_async=False,
            job_execution_mode="inline",
            run_lease_seconds=15,
            run_heartbeat_seconds=2,
            deployment_id="smart-commissioning-local",
        )

        with mock.patch.object(run_dispatch, "get_settings", return_value=settings):
            response = run_dispatch.dispatch_run(
                run,
                service=self.service,
                enqueue=None,
                run_inline=processor,
                inline_message="inline",
                queued_message="queued",
            )

        processor.assert_not_called()
        self.assertEqual(response.status, "failed")
        self.assertEqual(
            self.service.get_run(run.run_id).stage,
            "execution_context_unavailable",
        )

    def test_udmi_nine_hour_quiet_capture_renews_and_finalizes_once(self) -> None:
        parameters = {
            "use_live_broker": True,
            "broker_host": "192.0.2.44",
            "capture_seconds": 32_400,
            "state_topic": "sample/device/state",
        }
        run = self._create_run(
            job_type="udmi_validation",
            parameters=parameters,
        )
        capture = EventControlledCapture()
        self.addCleanup(capture.release.set)
        heartbeat_count = 0
        heartbeat_lock = threading.Lock()
        multiple_heartbeats = threading.Event()
        original_heartbeat = OwnedRunStore.heartbeat

        def observed_heartbeat(
            store: OwnedRunStore, *, lease_seconds: int = 60
        ) -> bool:
            nonlocal heartbeat_count
            renewed = original_heartbeat(
                store,
                lease_seconds=lease_seconds,
            )
            with heartbeat_lock:
                heartbeat_count += 1
                if heartbeat_count >= 2:
                    multiple_heartbeats.set()
            return renewed

        def run_inline(
            owned_store: OwnedRunStore,
            frozen_parameters: dict[str, object],
        ) -> object:
            self.assertEqual(frozen_parameters["capture_seconds"], 32_400)
            return process_udmi_validation_run(
                run.run_id,
                frozen_parameters,
                run_store=owned_store,
                execution_mode="inline_local_fallback",
                live_capture=capture,
                run_is_backgrounded=True,
            )

        settings = SimpleNamespace(
            inline_run_async=True,
            job_execution_mode="inline",
            run_lease_seconds=1,
            run_heartbeat_seconds=0.02,
            deployment_id="smart-commissioning-local",
        )
        with (
            mock.patch.object(run_dispatch, "get_settings", return_value=settings),
            mock.patch.object(
                OwnedRunStore,
                "heartbeat",
                observed_heartbeat,
            ),
        ):
            accepted = run_dispatch.dispatch_run(
                run,
                service=self.service,
                enqueue=None,
                run_inline=run_inline,
                inline_message="UDMI validation run started.",
                queued_message="queued",
            )

            self.assertEqual(accepted.run_id, run.run_id)
            self.assertTrue(
                capture.started.wait(_WAIT),
                "event-controlled capture never started",
            )
            self.assertTrue(
                multiple_heartbeats.wait(_WAIT),
                "real repository did not receive multiple renewals",
            )
            active = self.service.get_run(run.run_id)
            self.assertEqual(active.status, "running")
            self.assertEqual(active.stage, "capturing_live_mqtt")

            with session_factory(self.engine)() as session:
                row = session.get(Run, run.run_id)
                original_boundary = row.claimed_at + timedelta(seconds=1)
                renewed_expiry = row.lease_expires_at
            self.assertGreater(renewed_expiry, original_boundary)
            self.assertEqual(
                self.lifecycle.recover_expired_leases(
                    now=original_boundary + timedelta(milliseconds=1)
                ),
                [],
                "maintenance must not reap a live inline owner",
            )

            capture.release.set()
            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                terminal = self.service.get_run(run.run_id)
                if terminal.status in {"succeeded", "failed", "cancelled"}:
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("UDMI inline run did not reach a terminal state")

            self.assertEqual(terminal.status, "succeeded")
            self.assertEqual(
                terminal.stage,
                "udmi_validation_complete_with_silent_devices",
            )
            self.assertEqual(len(capture.calls), 1)
            self.assertEqual(
                capture.calls[0]["timeout_seconds"],
                32_400.0,
            )

            with session_factory(self.engine)() as session:
                result_count = session.scalar(
                    select(func.count())
                    .select_from(RunResult)
                    .where(RunResult.run_id == run.run_id)
                )
                seal_count = session.scalar(
                    select(func.count())
                    .select_from(RunSeal)
                    .where(RunSeal.run_id == run.run_id)
                )
            self.assertEqual(result_count, 1)
            self.assertEqual(seal_count, 1)

            deadline = time.monotonic() + _WAIT
            heartbeat_name = f"inline-heartbeat-{run.run_id}"
            while time.monotonic() < deadline:
                if not any(
                    thread.name == heartbeat_name
                    for thread in threading.enumerate()
                ):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("inline heartbeat thread leaked after terminal finalization")
            with heartbeat_lock:
                stopped_count = heartbeat_count
            threading.Event().wait(0.08)
            with heartbeat_lock:
                self.assertEqual(heartbeat_count, stopped_count)

    def test_recovered_owner_cannot_overwrite_terminal_evidence(self) -> None:
        run = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 32_400},
        )
        processor_started = threading.Event()
        release_processor = threading.Event()
        self.addCleanup(release_processor.set)
        processor_done = threading.Event()
        ownership_lost = threading.Event()
        stale_write_errors: list[Exception] = []
        original_mark_lost = OwnedRunStore.mark_ownership_lost

        def observed_mark_lost(store: OwnedRunStore) -> None:
            original_mark_lost(store)
            ownership_lost.set()

        def run_inline(
            owned_store: OwnedRunStore,
            frozen_parameters: dict[str, object],
        ) -> None:
            self.assertEqual(frozen_parameters["capture_seconds"], 32_400)
            processor_started.set()
            if not release_processor.wait(_WAIT):
                stale_write_errors.append(
                    TimeoutError("stale processor was not released")
                )
                processor_done.set()
                return
            try:
                owned_store.update_result_summary(
                    run.run_id,
                    {"stale_executor": True},
                )
            except Exception as error:
                stale_write_errors.append(error)
            finally:
                processor_done.set()

        settings = SimpleNamespace(
            inline_run_async=True,
            job_execution_mode="inline",
            run_lease_seconds=1,
            run_heartbeat_seconds=0.05,
            deployment_id="smart-commissioning-local",
        )
        with (
            mock.patch.object(run_dispatch, "get_settings", return_value=settings),
            mock.patch.object(
                OwnedRunStore,
                "mark_ownership_lost",
                observed_mark_lost,
            ),
        ):
            run_dispatch.dispatch_run(
                run,
                service=self.service,
                enqueue=None,
                run_inline=run_inline,
                inline_message="MQTT discovery run started.",
                queued_message="queued",
            )
            self.assertTrue(processor_started.wait(_WAIT))

            with session_factory(self.engine)() as session:
                row = session.get(Run, run.run_id)
                forced_recovery_time = row.lease_expires_at + timedelta(
                    milliseconds=1
                )
            self.assertEqual(
                self.lifecycle.recover_expired_leases(
                    now=forced_recovery_time
                ),
                [run.run_id],
            )
            recovered_seal = self.lifecycle.get_seal(run.run_id)
            self.assertEqual(recovered_seal.terminal_status, "failed")
            self.assertTrue(
                ownership_lost.wait(_WAIT),
                "heartbeat did not confirm the recovered ownership loss",
            )

            release_processor.set()
            self.assertTrue(processor_done.wait(_WAIT))
            self.assertEqual(len(stale_write_errors), 1)
            self.assertIsInstance(stale_write_errors[0], OwnershipLostError)

            executor_name = f"inline-run-{run.run_id}"
            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                if not any(
                    thread.name == executor_name
                    for thread in threading.enumerate()
                ):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("stale inline executor did not stop")

            record = self.service.get_run(run.run_id)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.stage, "lease_expired")
            self.assertNotIn("stale_executor", record.result_summary)
            preserved_seal = self.lifecycle.get_seal(run.run_id)
            self.assertEqual(
                preserved_seal.result_sha256,
                recovered_seal.result_sha256,
            )
            with session_factory(self.engine)() as session:
                result_count = session.scalar(
                    select(func.count())
                    .select_from(RunResult)
                    .where(RunResult.run_id == run.run_id)
                )
                seal_count = session.scalar(
                    select(func.count())
                    .select_from(RunSeal)
                    .where(RunSeal.run_id == run.run_id)
                )
            self.assertEqual(result_count, 1)
            self.assertEqual(seal_count, 1)

    def test_terminal_conflict_before_first_heartbeat_logs_ownership_loss(self) -> None:
        run = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 60},
        )
        processor_started = threading.Event()
        release_processor = threading.Event()
        self.addCleanup(release_processor.set)
        processor_returned = threading.Event()
        terminal_responses: list[dict[str, object]] = []

        def run_inline(
            owned_store: OwnedRunStore,
            _frozen_parameters: dict[str, object],
        ) -> None:
            processor_started.set()
            if not release_processor.wait(_WAIT):
                raise TimeoutError("terminal-conflict processor was not released")
            terminal_responses.append(
                owned_store.update_run_status(
                    run.run_id,
                    status="succeeded",
                    stage="mqtt_discovery_complete",
                    progress_percent=100,
                )
            )
            processor_returned.set()

        settings = SimpleNamespace(
            inline_run_async=True,
            job_execution_mode="inline",
            run_lease_seconds=60,
            run_heartbeat_seconds=15,
            deployment_id="smart-commissioning-local",
        )
        with (
            mock.patch.object(run_dispatch, "get_settings", return_value=settings),
            self.assertLogs("app.services.run_dispatch", level="WARNING") as captured,
        ):
            run_dispatch.dispatch_run(
                run,
                service=self.service,
                enqueue=None,
                run_inline=run_inline,
                inline_message="MQTT discovery run started.",
                queued_message="queued",
            )
            self.assertTrue(processor_started.wait(_WAIT))
            with session_factory(self.engine)() as session:
                row = session.get(Run, run.run_id)
                forced_recovery_time = row.lease_expires_at + timedelta(
                    milliseconds=1
                )
            self.assertEqual(
                self.lifecycle.recover_expired_leases(now=forced_recovery_time),
                [run.run_id],
            )
            recovered_seal = self.lifecycle.get_seal(run.run_id)
            release_processor.set()
            self.assertTrue(processor_returned.wait(_WAIT))

            executor_name = f"inline-run-{run.run_id}"
            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                if not any(
                    thread.name == executor_name
                    for thread in threading.enumerate()
                ):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("terminal-conflict executor did not stop")

        self.assertEqual(len(terminal_responses), 1)
        self.assertEqual(terminal_responses[0]["status"], "ownership_lost")
        self.assertIn(
            "inline processor observed ownership loss",
            "\n".join(captured.output),
        )
        preserved_seal = self.lifecycle.get_seal(run.run_id)
        self.assertEqual(preserved_seal.result_sha256, recovered_seal.result_sha256)
        with session_factory(self.engine)() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(RunResult)
                    .where(RunResult.run_id == run.run_id)
                ),
                1,
            )

    def test_stop_request_keeps_heartbeat_until_cancelled_finalization(self) -> None:
        run = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 32_400},
        )
        processor_started = threading.Event()
        release_processor = threading.Event()
        self.addCleanup(release_processor.set)
        stop_requested = threading.Event()
        heartbeat_after_stop = threading.Event()
        original_heartbeat = OwnedRunStore.heartbeat

        def observed_heartbeat(
            store: OwnedRunStore, *, lease_seconds: int = 60
        ) -> bool:
            renewed = original_heartbeat(
                store,
                lease_seconds=lease_seconds,
            )
            if stop_requested.is_set() and renewed:
                heartbeat_after_stop.set()
            return renewed

        def run_inline(
            owned_store: OwnedRunStore,
            _frozen_parameters: dict[str, object],
        ) -> None:
            processor_started.set()
            if not release_processor.wait(_WAIT):
                raise TimeoutError("cancelled processor was not released")
            if owned_store.is_cancel_requested(run.run_id):
                owned_store.update_run_status(
                    run.run_id,
                    status="cancelled",
                    stage="mqtt_discovery_cancelled",
                    progress_percent=100,
                )

        settings = SimpleNamespace(
            inline_run_async=True,
            job_execution_mode="inline",
            run_lease_seconds=1,
            run_heartbeat_seconds=0.02,
            deployment_id="smart-commissioning-local",
        )
        with (
            mock.patch.object(run_dispatch, "get_settings", return_value=settings),
            mock.patch.object(
                OwnedRunStore,
                "heartbeat",
                observed_heartbeat,
            ),
        ):
            run_dispatch.dispatch_run(
                run,
                service=self.service,
                enqueue=None,
                run_inline=run_inline,
                inline_message="MQTT discovery run started.",
                queued_message="queued",
            )
            self.assertTrue(processor_started.wait(_WAIT))

            requested = self.service.request_cancel(run.run_id)
            stop_requested.set()
            self.assertEqual(requested.status, "running")
            with session_factory(self.engine)() as session:
                row = session.get(Run, run.run_id)
                self.assertTrue(row.cancel_requested)
                self.assertEqual(row.status, "running")
            self.assertTrue(
                heartbeat_after_stop.wait(_WAIT),
                "heartbeat stopped before cancelled finalization completed",
            )

            release_processor.set()
            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                terminal = self.service.get_run(run.run_id)
                if terminal.status == "cancelled":
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("stop request did not produce cancelled finalization")

            self.assertEqual(terminal.stage, "mqtt_discovery_cancelled")
            heartbeat_name = f"inline-heartbeat-{run.run_id}"
            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                if not any(
                    thread.name == heartbeat_name
                    for thread in threading.enumerate()
                ):
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("heartbeat thread survived cancelled finalization")

            with session_factory(self.engine)() as session:
                result_count = session.scalar(
                    select(func.count())
                    .select_from(RunResult)
                    .where(RunResult.run_id == run.run_id)
                )
                seal_count = session.scalar(
                    select(func.count())
                    .select_from(RunSeal)
                    .where(RunSeal.run_id == run.run_id)
                )
            self.assertEqual(result_count, 1)
            self.assertEqual(seal_count, 1)

    def test_stopping_one_persisted_run_does_not_cancel_a_second_run(self) -> None:
        """Cancellation stays attached to the owned run ID through dispatch."""

        first = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 32_400},
        )
        second = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 32_400},
        )
        started = {first.run_id: threading.Event(), second.run_id: threading.Event()}
        release = {first.run_id: threading.Event(), second.run_id: threading.Event()}
        self.addCleanup(release[first.run_id].set)
        self.addCleanup(release[second.run_id].set)

        def run_inline(
            owned_store: OwnedRunStore,
            _frozen_parameters: dict[str, object],
        ) -> None:
            run_id = owned_store.lease.run_id
            started[run_id].set()
            if not release[run_id].wait(_WAIT):
                raise TimeoutError(f"run {run_id} was not released")
            if owned_store.is_cancel_requested(run_id):
                owned_store.update_run_status(
                    run_id,
                    status="cancelled",
                    stage="mqtt_discovery_cancelled",
                    progress_percent=100,
                )
            else:
                owned_store.update_run_status(
                    run_id,
                    status="succeeded",
                    stage="mqtt_discovery_complete",
                    progress_percent=100,
                )

        settings = SimpleNamespace(
            inline_run_async=True,
            job_execution_mode="inline",
            run_lease_seconds=1,
            run_heartbeat_seconds=0.02,
            deployment_id="smart-commissioning-local",
        )
        with mock.patch.object(run_dispatch, "get_settings", return_value=settings):
            for run in (first, second):
                run_dispatch.dispatch_run(
                    run,
                    service=self.service,
                    enqueue=None,
                    run_inline=run_inline,
                    inline_message="MQTT discovery run started.",
                    queued_message="queued",
                )
            self.assertTrue(started[first.run_id].wait(_WAIT))
            self.assertTrue(started[second.run_id].wait(_WAIT))

            self.service.request_cancel(first.run_id)
            self.assertTrue(self.service.is_cancel_requested(first.run_id))
            self.assertFalse(self.service.is_cancel_requested(second.run_id))
            release[first.run_id].set()

            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                first_terminal = self.service.get_run(first.run_id)
                if first_terminal.status == "cancelled":
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("first run did not finalise as cancelled")

            # The second run is still live with an untouched persisted flag
            # while the cancelled run seals its terminal result.
            second_live = self.service.get_run(second.run_id)
            self.assertEqual(second_live.status, "running")
            with session_factory(self.engine)() as session:
                second_row = session.get(Run, second.run_id)
                assert second_row is not None
                self.assertFalse(second_row.cancel_requested)
            release[second.run_id].set()

            deadline = time.monotonic() + _WAIT
            while time.monotonic() < deadline:
                second_terminal = self.service.get_run(second.run_id)
                if second_terminal.status == "succeeded":
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("second run did not finish independently")

        self.assertEqual(first_terminal.stage, "mqtt_discovery_cancelled")
        self.assertEqual(second_terminal.stage, "mqtt_discovery_complete")
        with session_factory(self.engine)() as session:
            second_row = session.get(Run, second.run_id)
            assert second_row is not None
            self.assertFalse(second_row.cancel_requested)

    def test_brief_real_sqlite_write_lock_is_retried(self) -> None:
        run = self._create_run(
            job_type="mqtt_discovery",
            parameters={"capture_seconds": 32_400},
        )
        owned_store = self.service.claim_owned_run(
            run.run_id,
            lease_seconds=15,
        )
        self.assertIsNotNone(owned_store)
        heartbeat_attempted = threading.Event()
        heartbeat_failed = threading.Event()
        heartbeat_succeeded = threading.Event()
        original_heartbeat = OwnedRunStore.heartbeat

        def configure_test_busy_timeout(
            dbapi_connection: object,
            _connection_record: object,
            _connection_proxy: object,
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute("PRAGMA busy_timeout=250")
            finally:
                cursor.close()

        event.listen(self.engine.pool, "checkout", configure_test_busy_timeout)
        self.addCleanup(
            event.remove,
            self.engine.pool,
            "checkout",
            configure_test_busy_timeout,
        )

        def observed_heartbeat(
            store: OwnedRunStore, *, lease_seconds: int = 60
        ) -> bool:
            heartbeat_attempted.set()
            try:
                renewed = original_heartbeat(
                    store,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                heartbeat_failed.set()
                raise
            if renewed:
                heartbeat_succeeded.set()
            return renewed

        lock_connection = self.engine.connect()
        self.addCleanup(lock_connection.close)
        lock_transaction = lock_connection.begin()
        self.addCleanup(
            lambda: lock_transaction.rollback()
            if lock_transaction.is_active
            else None
        )
        heartbeat = InlineRunHeartbeat(
            owned_store,
            lease_seconds=15,
            interval_seconds=0.02,
        )
        self.addCleanup(heartbeat.stop_and_join)

        with mock.patch.object(
            OwnedRunStore,
            "heartbeat",
            observed_heartbeat,
        ):
            heartbeat.start()
            self.assertTrue(
                heartbeat_attempted.wait(_WAIT),
                "heartbeat never attempted while SQLite was locked",
            )
            self.assertTrue(heartbeat.is_alive)
            self.assertTrue(
                heartbeat_failed.wait(_WAIT + 2.0),
                "SQLite busy timeout did not force a heartbeat failure",
            )
            self.assertFalse(
                heartbeat_succeeded.is_set(),
                "heartbeat succeeded before the SQLite lock was released",
            )
            lock_transaction.rollback()
            self.assertTrue(
                heartbeat_succeeded.wait(_WAIT),
                "heartbeat did not recover after SQLite lock release",
            )
            owned_store.update_run_status(
                run.run_id,
                status="succeeded",
                stage="lock_retry_complete",
                progress_percent=100,
            )
            heartbeat.stop_and_join()

        self.assertFalse(heartbeat.is_alive)
        self.assertFalse(heartbeat.ownership_lost)
        record = self.service.get_run(run.run_id)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.stage, "lock_retry_complete")


if __name__ == "__main__":
    unittest.main()
