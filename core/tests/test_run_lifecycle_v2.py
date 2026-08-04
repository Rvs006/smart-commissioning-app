import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    ActiveProtocolSlot,
    DiscoveredDevice,
    DiscoveredPoint,
    DiscoveredTopic,
    Run,
    RunIssue,
    RunLifecycleConflict,
    RunResult,
    RunSeal,
)
from smart_commissioning_core.db.run_lifecycle import (
    ProtocolConflictError,
    RunLifecycleRepository,
)
from smart_commissioning_core.owned_run_store import (
    OwnedRunStore,
    OwnershipLostError,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import event, func, select, update


def _context(protocol_key: str | None = None) -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": "project-north",
            "site_id": "site-17",
            "configuration_snapshot": {"mqtt": {"values": {"Port": 8883}}},
            "configuration_version": 7,
            "registers": [],
            "imports": [],
            "schema_versions": {"udmi": "1.5.2"},
            "engine_parameters": {"authorized": True, "dry_run": True},
            "network_interface": "192.0.2.10/24",
            "connection_settings": {"broker_host": "broker.example", "broker_port": 8883},
            "secret_references": {},
            "requesting_principal": "user-42",
            "application_version": "0.1.26",
            "protocol_key": protocol_key,
        }
    )


def _terminal(*, status: str = "succeeded", marker: str = "one") -> TerminalResultV1:
    return TerminalResultV1.model_validate(
        {
            "status": status,
            "stage": "engine_complete" if status == "succeeded" else "engine_failed",
            "summary": {"marker": marker, "issue_count": 1},
            "issues": [
                {
                    "issue_id": f"issue-{marker}",
                    "asset_id": "asset-1",
                    "issue_type": "test_issue",
                    "severity": "low",
                    "description": "A deterministic issue.",
                }
            ],
            "devices": [{"address": "192.0.2.20", "attributes": {"marker": marker}}],
            "points": [
                {
                    "device_ref": "192.0.2.20",
                    "point_id": "ai-1",
                    "observed_value": {"present_value": 21.0},
                }
            ],
            "topics": [
                {
                    "topic": "site-17/device/events",
                    "last_payload": {"marker": marker},
                    "message_count": 1,
                }
            ],
        }
    )


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temp_dir.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.repository = RunLifecycleRepository(self.engine)

    def create_run(self, *, protocol_key: str | None = None) -> tuple[str, str]:
        envelope = self.repository.create_run_with_context(
            job_type="mqtt_discovery",
            context=_context(protocol_key),
            execution_mode="dramatiq_worker",
        )
        return envelope.run_id, envelope.dispatch_id


class DispatchAndContextTests(LifecycleTestCase):
    def test_run_context_and_outbox_commit_together(self) -> None:
        run_id, dispatch_id = self.create_run()

        stored = self.repository.get_context(run_id)
        dispatch = self.repository.get_dispatch(dispatch_id)
        dispatch_for_run = self.repository.get_dispatch_for_run(run_id)
        self.assertEqual(stored.context.project_id, "project-north")
        self.assertEqual(stored.context.site_id, "site-17")
        self.assertEqual(stored.context_sha256, stored.context.sha256())
        self.assertEqual(dispatch.run_id, run_id)
        self.assertEqual(dispatch_for_run.dispatch_id, dispatch_id)
        self.assertEqual(dispatch.state, "pending")
        self.assertTrue(self.repository.mark_dispatch_published(dispatch_id))
        self.assertEqual(self.repository.get_dispatch(dispatch_id).state, "published")

    def test_protocol_slot_rejects_second_active_run_with_owner_id(self) -> None:
        protocol_key = "mqtt:" + "a" * 64
        first_run, _ = self.create_run(protocol_key=protocol_key)

        with self.assertRaises(ProtocolConflictError) as raised:
            self.create_run(protocol_key=protocol_key)

        self.assertEqual(raised.exception.active_run_id, first_run)


class ClaimAndFencingTests(LifecycleTestCase):
    def test_one_hundred_concurrent_claims_have_one_winner(self) -> None:
        run_id, dispatch_id = self.create_run()

        def claim(_: int):
            return self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)

        with ThreadPoolExecutor(max_workers=20) as pool:
            leases = list(pool.map(claim, range(100)))

        winners = [lease for lease in leases if lease is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt, 1)

    def test_claim_waiting_on_database_lock_receives_full_lease(self) -> None:
        run_id, dispatch_id = self.create_run()
        lock_connection = self.engine.connect()
        lock_transaction = lock_connection.begin()
        lock_connection.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(state_version=Run.state_version)
        )

        db_wait_started = threading.Event()
        worker_thread_id: list[int] = []

        def observe_sql(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if (
                worker_thread_id
                and threading.get_ident() == worker_thread_id[0]
                and statement.lstrip().upper().startswith("BEGIN IMMEDIATE")
            ):
                db_wait_started.set()

        event.listen(self.engine, "before_cursor_execute", observe_sql)
        result: dict[str, object] = {}
        errors: list[BaseException] = []

        def claim() -> None:
            worker_thread_id.append(threading.get_ident())
            try:
                result["lease"] = self.repository.claim_run(
                    run_id,
                    dispatch_id,
                    lease_seconds=2,
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                errors.append(error)

        claimant = threading.Thread(target=claim)
        try:
            claimant.start()
            self.assertTrue(
                db_wait_started.wait(2.0),
                "claim did not reach the SQLite lock",
            )
            threading.Event().wait(1.2)
            released_at = datetime.now(UTC)
        finally:
            if lock_transaction.is_active:
                lock_transaction.rollback()
            lock_connection.close()

        claimant.join(5.0)
        event.remove(self.engine, "before_cursor_execute", observe_sql)
        self.assertFalse(claimant.is_alive())
        if errors:
            raise errors[0]
        lease = result["lease"]
        self.assertIsNotNone(lease)
        self.assertGreaterEqual(
            lease.claimed_at,
            released_at - timedelta(milliseconds=100),
        )
        # The lease duration is anchored to the time ownership is acquired,
        # not to the time this assertion happens to run. Measuring from now
        # made the test race with scheduler and database-unlock overhead.
        self.assertGreaterEqual(
            (lease.lease_expires_at - lease.claimed_at).total_seconds(),
            1.99,
        )

    def test_every_stale_owner_write_changes_zero_lifecycle_rows(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)
        self.assertIsNotNone(lease)

        self.assertFalse(self.repository.heartbeat(run_id, "stale-owner", lease_seconds=60))
        self.assertFalse(
            self.repository.update_progress(
                run_id,
                "stale-owner",
                stage="should-not-stick",
                progress_percent=91,
                summary={"stale": True},
            )
        )
        outcome = self.repository.finalize_run(
            run_id, "stale-owner", _terminal(marker="stale")
        )
        self.assertTrue(outcome.conflict)

        with session_factory(self.engine)() as session:
            row = session.get(Run, run_id)
            self.assertEqual(row.status, "running")
            self.assertNotEqual(row.stage, "should-not-stick")
            self.assertIsNone(session.get(RunResult, run_id))
            conflict_count = session.scalar(
                select(func.count()).select_from(RunLifecycleConflict).where(
                    RunLifecycleConflict.run_id == run_id
                )
            )
        self.assertGreaterEqual(conflict_count, 1)

    def test_confirmed_loss_fences_store_before_any_more_evidence_write(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
        )
        self.assertIsNotNone(lease)
        store = OwnedRunStore(self.repository, lease)

        store.mark_ownership_lost()

        self.assertTrue(store.ownership_lost)
        self.assertTrue(store.is_cancel_requested(run_id))
        with self.assertRaises(OwnershipLostError):
            store.update_result_summary(run_id, {"stale": True})
        with self.assertRaises(OwnershipLostError):
            store.update_run_status(
                run_id,
                status="failed",
                stage="stale-finalizer",
                progress_percent=100,
            )
        with session_factory(self.engine)() as session:
            row = session.get(Run, run_id)
            self.assertEqual(row.status, "running")
            self.assertNotIn("stale", row.result_summary or {})
            self.assertIsNone(session.get(RunResult, run_id))

    def test_same_owner_heartbeat_version_change_does_not_reject_progress(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)
        self.assertIsNotNone(lease)
        original_load = self.repository._load_run
        injected = False

        def load_with_heartbeat_version_change(session, selected_run_id, **kwargs):
            nonlocal injected
            row = original_load(session, selected_run_id, **kwargs)
            if not injected:
                injected = True
                heartbeat_at = datetime.now(UTC)
                session.execute(
                    update(Run)
                    .where(Run.id == selected_run_id)
                    .values(
                        heartbeat_at=heartbeat_at,
                        lease_expires_at=heartbeat_at + timedelta(seconds=60),
                        state_version=Run.state_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
            return row

        with mock.patch.object(
            self.repository,
            "_load_run",
            side_effect=load_with_heartbeat_version_change,
        ):
            accepted = self.repository.update_progress(
                run_id,
                lease.owner_token,
                stage="capture_active",
                progress_percent=25,
            )

        self.assertTrue(accepted)
        with session_factory(self.engine)() as session:
            row = session.get(Run, run_id)
            self.assertEqual(row.stage, "capture_active")
            self.assertEqual(row.progress_percent, 25)

    def test_same_owner_heartbeat_version_change_does_not_drop_stop_request(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)
        self.assertIsNotNone(lease)
        original_load = self.repository._load_run
        injected = False

        def load_with_heartbeat_version_change(session, selected_run_id, **kwargs):
            nonlocal injected
            row = original_load(session, selected_run_id, **kwargs)
            if not injected:
                injected = True
                heartbeat_at = datetime.now(UTC)
                session.execute(
                    update(Run)
                    .where(Run.id == selected_run_id)
                    .values(
                        heartbeat_at=heartbeat_at,
                        lease_expires_at=heartbeat_at + timedelta(seconds=60),
                        state_version=Run.state_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
            return row

        with mock.patch.object(
            self.repository,
            "_load_run",
            side_effect=load_with_heartbeat_version_change,
        ):
            outcome = self.repository.request_cancel(run_id)

        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.state, "cancellation_requested")
        with session_factory(self.engine)() as session:
            self.assertTrue(session.get(Run, run_id).cancel_requested)

    def test_expired_owner_cannot_revive_lease_before_recovery_sweep(self) -> None:
        run_id, dispatch_id = self.create_run()
        claimed_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
            now=claimed_at,
        )
        self.assertIsNotNone(lease)
        after_expiry = claimed_at + timedelta(seconds=61)

        self.assertFalse(
            self.repository.heartbeat(
                run_id,
                lease.owner_token,
                lease_seconds=60,
                now=after_expiry,
            )
        )
        self.assertFalse(
            self.repository.update_progress(
                run_id,
                lease.owner_token,
                stage="late-progress",
                progress_percent=75,
                now=after_expiry,
            )
        )
        late_terminal = self.repository.finalize_run(
            run_id,
            lease.owner_token,
            _terminal(marker="late-before-recovery"),
            now=after_expiry,
        )
        self.assertTrue(late_terminal.conflict)
        with session_factory(self.engine)() as session:
            row = session.get(Run, run_id)
            self.assertEqual(row.status, "running")
            self.assertNotEqual(row.stage, "late-progress")
            self.assertIsNone(session.get(RunResult, run_id))

        self.assertEqual(
            self.repository.recover_expired_leases(now=after_expiry),
            [run_id],
        )
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")

    def test_lifecycle_writes_blocked_across_expiry_are_rejected(self) -> None:
        """A pre-expiry call cannot use a timestamp sampled before a DB wait."""

        def exercise(operation_name: str) -> None:
            run_id, dispatch_id = self.create_run()
            lease = self.repository.claim_run(
                run_id,
                dispatch_id,
                lease_seconds=1,
            )
            self.assertIsNotNone(lease)

            lock_connection = self.engine.connect()
            lock_transaction = lock_connection.begin()
            lock_connection.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(state_version=Run.state_version)
            )

            db_wait_started = threading.Event()
            worker_thread_id: list[int] = []

            def observe_sql(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
            ) -> None:
                if (
                    worker_thread_id
                    and threading.get_ident() == worker_thread_id[0]
                    and statement.lstrip().upper().startswith("BEGIN IMMEDIATE")
                ):
                    db_wait_started.set()

            event.listen(self.engine, "before_cursor_execute", observe_sql)
            result: dict[str, object] = {}
            errors: list[BaseException] = []

            def perform_write() -> None:
                worker_thread_id.append(threading.get_ident())
                try:
                    if operation_name == "heartbeat":
                        result["value"] = self.repository.heartbeat(
                            run_id,
                            lease.owner_token,
                            lease_seconds=60,
                        )
                    elif operation_name == "progress":
                        result["value"] = self.repository.update_progress(
                            run_id,
                            lease.owner_token,
                            stage="late-progress",
                            progress_percent=75,
                        )
                    else:
                        result["value"] = self.repository.finalize_run(
                            run_id,
                            lease.owner_token,
                            _terminal(marker="late-finalize"),
                        )
                except BaseException as error:  # pragma: no cover - surfaced below
                    errors.append(error)

            writer = threading.Thread(target=perform_write)
            try:
                writer.start()
                self.assertTrue(
                    db_wait_started.wait(2.0),
                    "lifecycle write did not reach the SQLite lock",
                )
                wait_seconds = max(
                    0.0,
                    (lease.lease_expires_at - datetime.now(UTC)).total_seconds(),
                )
                threading.Event().wait(wait_seconds + 0.2)
                self.assertGreater(datetime.now(UTC), lease.lease_expires_at)
            finally:
                if lock_transaction.is_active:
                    lock_transaction.rollback()
                lock_connection.close()

            writer.join(5.0)
            event.remove(self.engine, "before_cursor_execute", observe_sql)
            self.assertFalse(writer.is_alive())
            if errors:
                raise errors[0]

            if operation_name == "finalize":
                self.assertTrue(result["value"].conflict)
            else:
                self.assertFalse(result["value"])
            with session_factory(self.engine)() as session:
                row = session.get(Run, run_id)
                self.assertEqual(row.status, "running")
                self.assertNotEqual(row.stage, "late-progress")
                self.assertIsNone(session.get(RunResult, run_id))

        for operation_name in ("heartbeat", "progress", "finalize"):
            with self.subTest(operation=operation_name):
                exercise(operation_name)


class TerminalSealTests(LifecycleTestCase):
    def test_finalization_is_one_transaction_and_identical_replay_is_idempotent(self) -> None:
        run_id, dispatch_id = self.create_run(protocol_key="mqtt:" + "c" * 64)
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)
        result = _terminal(marker="sealed")

        applied = self.repository.finalize_run(run_id, lease.owner_token, result)
        replayed = self.repository.finalize_run(run_id, lease.owner_token, result)

        self.assertTrue(applied.applied)
        self.assertTrue(replayed.idempotent)
        self.assertEqual(applied.result_sha256, replayed.result_sha256)
        with session_factory(self.engine)() as session:
            self.assertIsNotNone(session.get(RunResult, run_id))
            self.assertIsNotNone(session.get(RunSeal, run_id))
            self.assertIsNone(
                session.scalar(
                    select(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run_id)
                )
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(RunIssue).where(RunIssue.run_id == run_id)),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(DiscoveredDevice).where(
                        DiscoveredDevice.run_id == run_id
                    )
                ),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(DiscoveredPoint).where(
                        DiscoveredPoint.run_id == run_id
                    )
                ),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(DiscoveredTopic).where(
                        DiscoveredTopic.run_id == run_id
                    )
                ),
                1,
            )

    def test_conflicting_terminal_replay_preserves_first_result(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)
        first = self.repository.finalize_run(run_id, lease.owner_token, _terminal(marker="first"))
        second = self.repository.finalize_run(run_id, lease.owner_token, _terminal(marker="second"))

        self.assertTrue(first.applied)
        self.assertTrue(second.conflict)
        self.assertEqual(self.repository.get_seal(run_id).result_sha256, first.result_sha256)

    def test_fault_injection_commits_no_terminal_or_normalized_rows(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)

        def fail_after_result(stage: str) -> None:
            if stage == "after_result":
                raise RuntimeError("injected finalization fault")

        repository = RunLifecycleRepository(self.engine, fault_injector=fail_after_result)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            repository.finalize_run(run_id, lease.owner_token, _terminal(marker="fault"))

        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "running")
            for model in (
                RunResult,
                RunSeal,
                RunIssue,
                DiscoveredDevice,
                DiscoveredPoint,
                DiscoveredTopic,
            ):
                count = session.scalar(
                    select(func.count()).select_from(model).where(model.run_id == run_id)
                )
                self.assertEqual(count, 0, model.__name__)

    def test_competing_finalizers_leave_one_terminal_result(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(run_id, dispatch_id, lease_seconds=60)

        def finalize(marker: str):
            return self.repository.finalize_run(
                run_id, lease.owner_token, _terminal(marker=marker)
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(finalize, ("left", "right")))

        self.assertEqual(sum(outcome.applied for outcome in outcomes), 1)
        self.assertEqual(sum(outcome.conflict for outcome in outcomes), 1)
        with session_factory(self.engine)() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(RunResult).where(RunResult.run_id == run_id)
                ),
                1,
            )

    def test_terminal_outcome_is_recorded_before_heartbeat_observes_terminal(self) -> None:
        run_id, dispatch_id = self.create_run()
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
        )
        terminal_committed = threading.Event()
        release_finalizer = threading.Event()
        heartbeat_entered = threading.Event()

        class PausingRepository:
            def finalize_run(inner_self, *args, **kwargs):
                outcome = self.repository.finalize_run(*args, **kwargs)
                terminal_committed.set()
                if not release_finalizer.wait(3.0):
                    raise TimeoutError("finalizer was not released")
                return outcome

            def heartbeat(inner_self, *args, **kwargs):
                heartbeat_entered.set()
                return self.repository.heartbeat(*args, **kwargs)

        store = OwnedRunStore(PausingRepository(), lease)
        finalizer_result: dict[str, object] = {}
        heartbeat_result: dict[str, object] = {}
        heartbeat_attempted = threading.Event()

        finalizer = threading.Thread(
            target=lambda: finalizer_result.setdefault(
                "value",
                store.update_run_status(
                    run_id,
                    status="succeeded",
                    stage="complete",
                    progress_percent=100,
                ),
            )
        )
        finalizer.start()
        self.assertTrue(terminal_committed.wait(3.0))

        def send_heartbeat() -> None:
            heartbeat_attempted.set()
            heartbeat_result["value"] = store.heartbeat(lease_seconds=60)

        heartbeat = threading.Thread(target=send_heartbeat)
        heartbeat.start()
        self.assertTrue(heartbeat_attempted.wait(3.0))
        self.assertFalse(
            heartbeat_entered.wait(0.05),
            "heartbeat crossed terminal finalization before its outcome was recorded",
        )

        release_finalizer.set()
        finalizer.join(3.0)
        heartbeat.join(3.0)
        self.assertFalse(finalizer.is_alive())
        self.assertFalse(heartbeat.is_alive())
        self.assertEqual(finalizer_result["value"]["status"], "succeeded")
        self.assertFalse(heartbeat_result["value"])
        self.assertTrue(store.terminal_outcome.applied)
        self.assertFalse(store.ownership_lost)


class CancellationAndRecoveryTests(LifecycleTestCase):
    def test_cancelling_queued_run_seals_without_claim(self) -> None:
        run_id, _ = self.create_run(protocol_key="mqtt:" + "e" * 64)

        outcome = self.repository.request_cancel(run_id)

        self.assertEqual(outcome.state, "cancelled")
        seal = self.repository.get_seal(run_id)
        self.assertEqual(seal.terminal_status, "cancelled")

    def test_dead_owner_is_recovered_after_lease_expiry(self) -> None:
        run_id, dispatch_id = self.create_run(protocol_key="mqtt:" + "f" * 64)
        claimed_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
            now=claimed_at,
        )
        self.assertIsNotNone(lease)

        recovered = self.repository.recover_expired_leases(
            now=claimed_at + timedelta(seconds=61)
        )

        self.assertEqual(recovered, [run_id])
        seal = self.repository.get_seal(run_id)
        self.assertEqual(seal.terminal_status, "failed")
        recovered_digest = seal.result_sha256
        self.assertFalse(
            self.repository.update_progress(
                run_id,
                lease.owner_token,
                stage="late-owner",
                progress_percent=99,
            )
        )
        late_finalization = self.repository.finalize_run(
            run_id,
            lease.owner_token,
            _terminal(marker="late-owner"),
        )
        self.assertTrue(late_finalization.conflict)
        preserved = self.repository.get_seal(run_id)
        self.assertEqual(preserved.terminal_status, "failed")
        self.assertEqual(preserved.result_sha256, recovered_digest)

    def test_conflicting_store_finalization_immediately_fences_late_writes(self) -> None:
        run_id, dispatch_id = self.create_run()
        claimed_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
            now=claimed_at,
        )
        store = OwnedRunStore(self.repository, lease)
        self.assertEqual(
            self.repository.recover_expired_leases(
                now=claimed_at + timedelta(seconds=61)
            ),
            [run_id],
        )

        late = store.update_run_status(
            run_id,
            status="succeeded",
            stage="late-success",
            progress_percent=100,
        )

        self.assertEqual(late["status"], "ownership_lost")
        self.assertTrue(store.terminal_outcome.conflict)
        self.assertTrue(store.ownership_lost)
        with self.assertRaises(OwnershipLostError):
            store.replace_devices(run_id, [{"address": "192.0.2.90"}])

    def test_heartbeat_survives_original_boundary_then_dead_owner_recovers(self) -> None:
        run_id, dispatch_id = self.create_run(protocol_key="mqtt:" + "1" * 64)
        claimed_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        lease = self.repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=60,
            now=claimed_at,
        )
        self.assertIsNotNone(lease)

        renewed_at = claimed_at + timedelta(seconds=15)
        self.assertTrue(
            self.repository.heartbeat(
                run_id,
                lease.owner_token,
                lease_seconds=60,
                now=renewed_at,
            )
        )

        # Maintenance runs after the original t0+60 boundary. The live owner
        # remains active because its independent heartbeat extended the lease.
        self.assertEqual(
            self.repository.recover_expired_leases(
                now=claimed_at + timedelta(seconds=61)
            ),
            [],
        )
        with session_factory(self.engine)() as session:
            self.assertEqual(session.get(Run, run_id).status, "running")

        # No later heartbeat arrives. Recovery remains real and deterministic
        # once the renewed t0+75 lease has expired.
        self.assertEqual(
            self.repository.recover_expired_leases(
                now=claimed_at + timedelta(seconds=76)
            ),
            [run_id],
        )
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")


if __name__ == "__main__":
    unittest.main()
