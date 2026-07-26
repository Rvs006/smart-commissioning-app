import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import func, select


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
        self.assertFalse(
            self.repository.update_progress(
                run_id,
                lease.owner_token,
                stage="late-owner",
                progress_percent=99,
            )
        )


if __name__ == "__main__":
    unittest.main()
