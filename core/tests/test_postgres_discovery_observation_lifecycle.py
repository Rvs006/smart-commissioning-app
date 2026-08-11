import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import uuid4

from alembic.script import ScriptDirectory
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    query_session_factory,
    session_factory,
)
from smart_commissioning_core.db.migrate import (
    build_alembic_config,
    upgrade_to_head,
)
from smart_commissioning_core.db.models import (
    Run,
    RunDiscoveryObservation,
    RunLifecycleConflict,
    RunResult,
    RunSeal,
)
from smart_commissioning_core.db.run_lifecycle import (
    DiscoveryObservationConflictError,
    RunLifecycleRepository,
)
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import func, make_url, select, text
from sqlalchemy.schema import CreateSchema, DropSchema

POSTGRES_URL = os.environ.get("SCT_TEST_POSTGRES_URL", "").strip()
BASE_TIME = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


def _schema_url(database_url: str, schema: str) -> str:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("SCT_TEST_POSTGRES_URL must be a PostgreSQL URL")
    existing_options = parsed.query.get("options")
    if isinstance(existing_options, tuple):
        option_parts = [str(value) for value in existing_options]
    elif existing_options:
        option_parts = [str(existing_options)]
    else:
        option_parts = []
    option_parts.append(f"-csearch_path={schema}")
    isolated = parsed.update_query_dict({"options": " ".join(option_parts)})
    return isolated.render_as_string(hide_password=False)


def _context() -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": "postgres-project",
            "site_id": "postgres-site",
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [],
            "schema_versions": {},
            "engine_parameters": {"authorized": True, "dry_run": True},
            "network_interface": "192.0.2.10/24",
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "postgres-test-principal",
            "application_version": "postgres-live-test",
            "protocol_key": "ip:postgres-live-test",
        }
    )


def _observation(
    event_key: str,
    *,
    entity_version: int = 1,
    payload: dict | None = None,
) -> DiscoveryObservationInputV1:
    return DiscoveryObservationInputV1(
        protocol="ip",
        entity_kind="host",
        entity_key="192.0.2.20",
        entity_version=entity_version,
        event_key=event_key,
        phase="reachability",
        outcome="observed",
        payload_schema_version="1.0",
        payload=payload or {"reachable": False, "response_ms": 0},
        observed_at=BASE_TIME,
    )


@unittest.skipUnless(
    POSTGRES_URL,
    "set SCT_TEST_POSTGRES_URL to run the live PostgreSQL lifecycle tests",
)
class PostgreSQLDiscoveryObservationLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = f"sct_test_{uuid4().hex}"
        cls.admin_engine = create_engine_from_url(POSTGRES_URL)
        cls.engine = None
        cls.addClassCleanup(cls._drop_isolated_schema)

        with cls.admin_engine.begin() as connection:
            connection.execute(CreateSchema(cls.schema))
            cls.tables_before_upgrade = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": cls.schema},
                )
                or 0
            )

        cls.database_url = _schema_url(POSTGRES_URL, cls.schema)
        upgrade_to_head(cls.database_url)
        cls.engine = create_engine_from_url(cls.database_url)
        cls.expected_heads = set(
            ScriptDirectory.from_config(
                build_alembic_config(cls.database_url)
            ).get_heads()
        )

    @classmethod
    def _drop_isolated_schema(cls) -> None:
        if cls.engine is not None:
            cls.engine.dispose()
        try:
            with cls.admin_engine.begin() as connection:
                connection.execute(
                    DropSchema(cls.schema, cascade=True, if_exists=True)
                )
        finally:
            cls.admin_engine.dispose()

    def setUp(self) -> None:
        self.clock_now = BASE_TIME + timedelta(seconds=10)
        self.repository = RunLifecycleRepository(
            self.engine,
            clock=lambda: self.clock_now,
        )

    def create_claimed_run(self):
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=_context(),
            execution_mode="inline",
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            lease_seconds=60,
            now=BASE_TIME,
            owner_token=f"owner-{uuid4().hex}",
        )
        self.assertIsNotNone(lease)
        return envelope.run_id, lease

    def test_fresh_schema_migrates_to_the_current_alembic_heads(self) -> None:
        self.assertEqual(self.tables_before_upgrade, 0)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_schema()")), self.schema)
            database_heads = set(
                connection.scalars(text("SELECT version_num FROM alembic_version"))
            )
            migrated_tables = set(
                connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": self.schema},
                )
            )

        self.assertEqual(database_heads, self.expected_heads)
        self.assertIn("runs", migrated_tables)
        self.assertIn("run_discovery_observations", migrated_tables)
        self.assertIn("run_lifecycle_conflicts", migrated_tables)

    def test_concurrent_replay_is_idempotent_and_conflict_is_audited(self) -> None:
        run_id, lease = self.create_claimed_run()
        observation = _observation("postgres-concurrent-v1")
        start = threading.Barrier(2)

        def append_once():
            start.wait(timeout=10)
            return self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                observation,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append_once) for _ in range(2)]
            outcomes = [future.result(timeout=15) for future in futures]

        self.assertEqual(len({outcome.cursor for outcome in outcomes}), 1)
        self.assertEqual(sum(outcome.idempotent for outcome in outcomes), 1)
        with self.assertRaises(DiscoveryObservationConflictError) as rejected:
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(
                    "postgres-concurrent-v1",
                    payload={"reachable": True, "response_ms": 1},
                ),
            )

        self.assertEqual(rejected.exception.reason, "observation_identity_conflict")
        cutoff = self.repository.get_discovery_observation_cutoff(
            run_id,
            lease.attempt,
            project_id="postgres-project",
            site_id="postgres-site",
        )
        self.assertEqual(cutoff.observation_count, 1)
        with session_factory(self.engine)() as session:
            conflicts = list(
                session.scalars(
                    select(RunLifecycleConflict).where(
                        RunLifecycleConflict.run_id == run_id,
                        RunLifecycleConflict.operation
                        == "append_discovery_observation",
                    )
                )
            )
        self.assertEqual([row.reason for row in conflicts], ["observation_identity_conflict"])
        self.assertIsNotNone(conflicts[0].owner_token_fingerprint)
        self.assertIsNotNone(conflicts[0].attempted_sha256)
        self.assertIsNotNone(conflicts[0].observed_at.tzinfo)

    def test_expired_lease_rejects_append_at_the_repository_clock(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.clock_now = lease.lease_expires_at + timedelta(microseconds=1)

        with self.assertRaises(DiscoveryObservationConflictError) as rejected:
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation("postgres-after-expiry"),
            )

        self.assertEqual(rejected.exception.reason, "stale_owner_attempt_or_terminal")
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            0,
        )
        with session_factory(self.engine)() as session:
            conflict = session.scalar(
                select(RunLifecycleConflict).where(
                    RunLifecycleConflict.run_id == run_id
                )
            )
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.reason, "stale_owner_attempt_or_terminal")
        self.assertIsNotNone(conflict.observed_at.tzinfo)

    def test_cursor_fence_refolds_and_commits_one_atomic_terminal_tuple(self) -> None:
        run_id, lease = self.create_claimed_run()
        first = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation("postgres-cursor-v2", entity_version=2),
        )
        fold_started = threading.Event()
        release_fold = threading.Event()
        finalizer_result: dict[str, object] = {}
        finalizer_errors: list[BaseException] = []
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_fold = lifecycle_module.fold_discovery_observations
        fold_calls = 0

        def block_first_fold(*args, **kwargs):
            nonlocal fold_calls
            fold_calls += 1
            if fold_calls == 1:
                fold_started.set()
                if not release_fold.wait(10):
                    raise TimeoutError("PostgreSQL discovery fold was not released")
            return real_fold(*args, **kwargs)

        def finalize() -> None:
            try:
                finalizer_result["outcome"] = self.repository.finalize_discovery_run(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    TerminalResultV1(
                        status="succeeded",
                        stage="postgres_discovery_complete",
                        summary={"source": "postgres-live-test"},
                    ),
                    now=BASE_TIME + timedelta(seconds=20),
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                finalizer_errors.append(error)

        with mock.patch.object(
            lifecycle_module,
            "fold_discovery_observations",
            side_effect=block_first_fold,
        ):
            finalizer = threading.Thread(target=finalize)
            finalizer.start()
            try:
                self.assertTrue(fold_started.wait(10))
                self.clock_now = BASE_TIME + timedelta(seconds=11)
                second = self.repository.append_discovery_observation(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    _observation("postgres-cursor-v1", entity_version=1),
                )
            finally:
                release_fold.set()
                finalizer.join(15)

        self.assertFalse(finalizer.is_alive())
        if finalizer_errors:
            raise finalizer_errors[0]
        outcome = finalizer_result["outcome"]
        self.assertTrue(outcome.applied)
        self.assertGreaterEqual(fold_calls, 2)

        first_page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            limit=1,
            project_id="postgres-project",
            site_id="postgres-site",
        )
        second_page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            after_cursor=first_page[0].cursor,
            limit=1,
            project_id="postgres-project",
            site_id="postgres-site",
        )
        self.assertEqual(first_page[0].cursor, first.cursor)
        self.assertEqual(second_page[0].cursor, second.cursor)
        self.assertEqual(first_page[0].entity_version, 2)
        self.assertEqual(second_page[0].entity_version, 1)

        with query_session_factory(self.engine)() as session:
            observation_count = session.scalar(
                select(func.count())
                .select_from(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == run_id)
            )
        self.assertEqual(observation_count, 2)

        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            seal = session.get(RunSeal, run_id)
        self.assertEqual(run.status, "succeeded")
        self.assertIsNone(run.lease_expires_at)
        self.assertIsNotNone(result)
        self.assertIsNotNone(seal)
        evidence = result.summary["observation_evidence_v1"]
        self.assertEqual(evidence["terminal_cursor"], second.cursor)
        self.assertEqual(evidence["observation_count"], 2)
        self.assertEqual(run.result_sha256, result.result_sha256)
        self.assertEqual(seal.result_sha256, result.result_sha256)
        self.assertEqual(outcome.result_sha256, result.result_sha256)

        self.clock_now = BASE_TIME + timedelta(seconds=21)
        with self.assertRaises(DiscoveryObservationConflictError):
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation("postgres-after-seal", entity_version=3),
            )
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            2,
        )

    def test_injected_and_stale_finalization_leave_no_terminal_tuple(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation("postgres-before-fault"),
        )

        def fail_after_result(stage: str) -> None:
            if stage == "after_result":
                raise RuntimeError("injected PostgreSQL finalization fault")

        faulting_repository = RunLifecycleRepository(
            self.engine,
            fault_injector=fail_after_result,
        )
        with self.assertRaisesRegex(RuntimeError, "injected PostgreSQL"):
            faulting_repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(
                    status="succeeded",
                    stage="postgres_fault",
                ),
                now=BASE_TIME + timedelta(seconds=20),
            )

        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            seal = session.get(RunSeal, run_id)
        self.assertEqual(run.status, "running")
        self.assertIsNone(run.terminal_at)
        self.assertIsNone(run.result_sha256)
        self.assertIsNone(result)
        self.assertIsNone(seal)

        stale_outcome = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            TerminalResultV1(
                status="succeeded",
                stage="postgres_stale",
            ),
            now=lease.lease_expires_at + timedelta(microseconds=1),
        )
        self.assertTrue(stale_outcome.conflict)
        self.assertEqual(
            stale_outcome.reason,
            "stale_owner_attempt_or_terminal",
        )
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            seal = session.get(RunSeal, run_id)
        self.assertEqual(run.status, "running")
        self.assertIsNone(run.terminal_at)
        self.assertIsNone(run.result_sha256)
        self.assertIsNone(result)
        self.assertIsNone(seal)
        self.assertEqual(self.repository.conflict_count(run_id), 1)
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            1,
        )


if __name__ == "__main__":
    unittest.main()
