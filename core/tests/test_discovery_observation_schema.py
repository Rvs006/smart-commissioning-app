import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import create_engine_from_url, session_factory
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.models import (
    ObservationRetentionBatch,
    ObservationRetentionCandidate,
    ObservationRetentionJob,
    Run,
    RunDiscoveryObservation,
    RunDiscoveryObservationState,
    RunRetentionHold,
)
from sqlalchemy import BigInteger, Integer, delete, func, inspect, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable


class RunDiscoveryObservationSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine_from_url("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
        self.run_id = DbRunStore(self.engine).create_run(
            project_id="project-observations",
            site_id="site-observations",
            job_type="ip_discovery",
        )["run_id"]

    def _observation(self, *, event_key: str, entity_version: int) -> RunDiscoveryObservation:
        return RunDiscoveryObservation(
            run_id=self.run_id,
            attempt=1,
            protocol="ip",
            entity_kind="host",
            entity_key="192.0.2.10",
            entity_version=entity_version,
            event_key=event_key,
            phase="reachability",
            outcome="responded",
            payload_schema_version="1.0",
            payload={},
            payload_sha256=("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
        )

    def _values(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "run_id": self.run_id,
            "attempt": 1,
            "protocol": "ip",
            "entity_kind": "host",
            "entity_key": "192.0.2.10",
            "entity_version": 1,
            "event_key": "host-seen",
            "phase": "reachability",
            "outcome": "responded",
            "payload_schema_version": "1.0",
            "payload": {},
            "payload_sha256": ("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
        }
        values.update(overrides)
        return values

    def _assert_rejected(self, **overrides: object) -> None:
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    RunDiscoveryObservation.__table__.insert().values(
                        **self._values(**overrides)
                    )
                )

    def test_cursor_is_64_bit_cross_database_and_sqlite_autoincrements(self) -> None:
        id_column = RunDiscoveryObservation.__table__.c.id
        self.assertIsInstance(id_column.type, BigInteger)
        self.assertIsInstance(id_column.type.dialect_impl(self.engine.dialect), Integer)
        postgresql_ddl = str(
            CreateTable(RunDiscoveryObservation.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("id BIGSERIAL NOT NULL", postgresql_ddl)

        columns = {
            column["name"]: column
            for column in inspect(self.engine).get_columns(
                "run_discovery_observations"
            )
        }
        required_columns = set(columns) - {"observed_at"}
        self.assertTrue(
            all(not columns[name]["nullable"] for name in required_columns)
        )
        self.assertTrue(columns["observed_at"]["nullable"])

        first = self._observation(event_key="host-seen", entity_version=1)
        second = self._observation(event_key="host-enriched", entity_version=2)
        with session_factory(self.engine).begin() as session:
            session.add_all([first, second])

        self.assertEqual((first.id, second.id), (1, 2))

        with session_factory(self.engine).begin() as session:
            session.execute(
                delete(RunDiscoveryObservation).where(
                    RunDiscoveryObservation.id == second.id
                )
            )
        third = self._observation(event_key="host-final", entity_version=3)
        with session_factory(self.engine).begin() as session:
            session.add(third)
        self.assertEqual(third.id, 3)

        with session_factory(self.engine).begin() as session:
            session.execute(delete(Run).where(Run.id == self.run_id))
        with session_factory(self.engine)() as session:
            remaining = session.scalar(
                select(func.count()).select_from(RunDiscoveryObservation)
            )
        self.assertEqual(remaining, 0)

    def test_database_enforces_bounded_canonical_observation_fields(self) -> None:
        invalid_cases = {
            "zero attempt": {"attempt": 0},
            "zero entity version": {"entity_version": 0},
            "unknown protocol": {"protocol": "mqtt"},
            "unknown entity kind": {"entity_kind": "gateway"},
            "unknown phase": {"phase": "scanning"},
            "empty entity key": {"entity_key": ""},
            "long entity key": {"entity_key": "x" * 256},
            "empty event key": {"event_key": ""},
            "long event key": {"event_key": "x" * 256},
            "empty outcome": {"outcome": ""},
            "long outcome": {"outcome": "x" * 65},
            "empty payload schema": {"payload_schema_version": ""},
            "long payload schema": {"payload_schema_version": "x" * 33},
            "uppercase digest": {"payload_sha256": "A" * 64},
            "non-hex digest": {"payload_sha256": "g" * 64},
            "oversized payload": {"payload": {"data": "x" * 65_536}},
        }
        for label, overrides in invalid_cases.items():
            with self.subTest(label):
                self._assert_rejected(**overrides)

    def test_database_enforces_idempotency_and_supports_page_fold_retention_queries(
        self,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                RunDiscoveryObservation.__table__.insert().values(**self._values())
            )

        self._assert_rejected(
            entity_kind="port",
            entity_key="192.0.2.10:443",
            event_key="host-seen",
        )
        self._assert_rejected(event_key="different-event")

        with self.engine.begin() as connection:
            connection.execute(
                RunDiscoveryObservation.__table__.insert().values(
                    **self._values(
                        entity_version=2,
                        event_key="host-enriched",
                    )
                )
            )

        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspect(self.engine).get_indexes("run_discovery_observations")
        }
        self.assertEqual(
            indexes["ix_run_discovery_observations_page"],
            ("run_id", "attempt", "id"),
        )
        self.assertEqual(
            indexes["ix_run_discovery_observations_fold"],
            (
                "run_id",
                "attempt",
                "entity_kind",
                "entity_key",
                "entity_version",
                "id",
            ),
        )
        self.assertEqual(
            indexes["ix_run_discovery_observations_retention"],
            ("created_at", "id"),
        )
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspect(self.engine).get_unique_constraints(
                "run_discovery_observations"
            )
        }
        self.assertEqual(
            unique_constraints["uq_run_discovery_observations_event"],
            ("run_id", "attempt", "event_key"),
        )
        self.assertEqual(
            unique_constraints["uq_run_discovery_observations_entity_version"],
            ("run_id", "attempt", "entity_kind", "entity_key", "entity_version"),
        )

    def test_attempt_scoped_lifecycle_state_is_bounded_and_run_owned(self) -> None:
        inspector = inspect(self.engine)

        self.assertIn(
            "run_discovery_observation_states",
            inspector.get_table_names(),
        )
        columns = {
            column["name"]: column
            for column in inspector.get_columns(
                "run_discovery_observation_states"
            )
        }
        self.assertEqual(
            set(columns),
            {
                "run_id",
                "attempt",
                "observation_count",
                "canonical_payload_bytes",
                "terminal_cursor",
                "observation_stream_sha256",
            },
        )
        self.assertTrue(all(not column["nullable"] for column in columns.values()))
        self.assertEqual(
            tuple(
                inspector.get_pk_constraint(
                    "run_discovery_observation_states"
                )["constrained_columns"]
            ),
            ("run_id", "attempt"),
        )
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(
                "run_discovery_observation_states"
            )
        }
        self.assertTrue(
            {
                "ck_run_discovery_observation_states_attempt_positive",
                "ck_run_discovery_observation_states_count_bounded",
                "ck_run_discovery_observation_states_payload_bytes_bounded",
                "ck_run_discovery_observation_states_terminal_cursor_positive",
                "ck_run_discovery_observation_states_stream_sha256",
            }.issubset(checks)
        )


class RetentionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine_from_url("sqlite://")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
        self.project_id = "project-retention"
        self.site_id = "site-retention"
        self.run_id = DbRunStore(self.engine).create_run(
            project_id=self.project_id,
            site_id=self.site_id,
            job_type="ip_discovery",
        )["run_id"]

    def _hold_values(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "hold_id": "hold-1",
            "run_id": self.run_id,
            "project_id": self.project_id,
            "site_id": self.site_id,
            "hold_type": "legal",
            "evidence_set_id": None,
            "active_marker": True,
            "placed_by": "operator@example.com",
            "reason": "Preserve discovery evidence for an open investigation.",
            "placed_at": self.now,
            "released_by": None,
            "release_reason": None,
            "released_at": None,
        }
        values.update(overrides)
        return values

    def _job_values(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "job_id": "retention-job-1",
            "project_id": self.project_id,
            "site_id": self.site_id,
            "keep_days": 90,
            "cutoff_sealed_at": self.now,
            "high_water_observation_id": 100,
            "candidate_run_count": 1,
            "candidate_observation_count": 2,
            "candidate_manifest_sha256": "f" * 64,
            "next_cursor": 0,
            "batch_limit": 250,
            "status": "preview",
            "requested_by": "operator@example.com",
            "requested_at": self.now,
            "confirmed_by": None,
            "confirmed_at": None,
            "deleted_count": 0,
            "batch_count": 0,
            "completed_at": None,
            "error_code": None,
            "active_marker": True,
        }
        values.update(overrides)
        return values

    def _assert_hold_rejected(self, **overrides: object) -> None:
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                with self.assertRaises(IntegrityError):
                    connection.execute(
                        RunRetentionHold.__table__.insert().values(
                            **self._hold_values(**overrides)
                        )
                    )
            finally:
                transaction.rollback()

    def _assert_job_rejected(self, **overrides: object) -> None:
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                with self.assertRaises(IntegrityError):
                    connection.execute(
                        ObservationRetentionJob.__table__.insert().values(
                            **self._job_values(**overrides)
                        )
                    )
            finally:
                transaction.rollback()

    def test_hold_fields_and_release_state_are_bounded_and_consistent(self) -> None:
        invalid_cases = {
            "empty hold id": {"hold_id": ""},
            "long hold id": {"hold_id": "x" * 65},
            "empty project": {"project_id": ""},
            "long project": {"project_id": "x" * 256},
            "empty site": {"site_id": ""},
            "long site": {"site_id": "x" * 256},
            "unknown hold type": {"hold_type": "audit"},
            "evidence hold without evidence set": {
                "hold_type": "evidence",
                "evidence_set_id": None,
            },
            "legal hold with evidence set": {
                "hold_type": "legal",
                "evidence_set_id": "evidence-set-1",
            },
            "empty evidence set": {"evidence_set_id": ""},
            "long evidence set": {"evidence_set_id": "x" * 129},
            "false active marker": {"active_marker": False},
            "empty placed by": {"placed_by": ""},
            "long placed by": {"placed_by": "x" * 256},
            "empty reason": {"reason": ""},
            "long reason": {"reason": "x" * 4097},
            "active release actor": {"released_by": "operator@example.com"},
            "released without actor": {
                "active_marker": None,
                "release_reason": "Case closed.",
                "released_at": self.now,
            },
            "empty release actor": {
                "active_marker": None,
                "released_by": "",
                "release_reason": "Case closed.",
                "released_at": self.now,
            },
            "empty release reason": {
                "active_marker": None,
                "released_by": "operator@example.com",
                "release_reason": "",
                "released_at": self.now,
            },
            "long release reason": {
                "active_marker": None,
                "released_by": "operator@example.com",
                "release_reason": "x" * 4097,
                "released_at": self.now,
            },
        }
        for label, overrides in invalid_cases.items():
            with self.subTest(label):
                self._assert_hold_rejected(**overrides)

        with self.engine.begin() as connection:
            connection.execute(
                RunRetentionHold.__table__.insert().values(**self._hold_values())
            )
        self._assert_hold_rejected(hold_id="hold-duplicate-active")

        released = {
            "active_marker": None,
            "released_by": "operator@example.com",
            "release_reason": "Case closed.",
            "released_at": self.now,
        }
        with self.engine.begin() as connection:
            connection.execute(
                RunRetentionHold.__table__.insert().values(
                    **self._hold_values(hold_id="hold-history-1", **released)
                )
            )
            connection.execute(
                RunRetentionHold.__table__.insert().values(
                    **self._hold_values(hold_id="hold-history-2", **released)
                )
            )

    def test_released_hold_audit_survives_run_deletion(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                RunRetentionHold.__table__.insert().values(
                    **self._hold_values(
                        active_marker=None,
                        released_by="operator@example.com",
                        release_reason="Case closed.",
                        released_at=self.now,
                    )
                )
            )
            connection.execute(delete(Run).where(Run.id == self.run_id))

        with self.engine.connect() as connection:
            surviving = connection.scalar(
                select(func.count()).select_from(RunRetentionHold)
            )
        self.assertEqual(surviving, 1)

    def test_candidate_and_batch_audit_rows_are_job_bound_not_run_bound(self) -> None:
        job = ObservationRetentionJob(**self._job_values())
        candidate = ObservationRetentionCandidate(
            job_id=job.job_id,
            run_id=self.run_id,
            project_id=self.project_id,
            site_id=self.site_id,
            attempt=1,
            terminal_status="succeeded",
            context_sha256="a" * 64,
            result_sha256="b" * 64,
            seal_sha256="c" * 64,
            sealed_at=self.now,
            terminal_cursor=10,
            observation_count=2,
            observation_stream_sha256="d" * 64,
        )
        batch = ObservationRetentionBatch(
            job_id=job.job_id,
            batch_number=1,
            actor="operator@example.com",
            cursor_before=0,
            cursor_after=10,
            attempted_count=2,
            deleted_count=2,
            applied_at=self.now,
        )
        with session_factory(self.engine).begin() as session:
            session.add(job)
            session.flush()
            session.add_all([candidate, batch])
        with self.engine.begin() as connection:
            connection.execute(delete(Run).where(Run.id == self.run_id))

        with session_factory(self.engine)() as session:
            self.assertIsNotNone(
                session.get(
                    ObservationRetentionCandidate,
                    (job.job_id, self.run_id),
                )
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ObservationRetentionBatch)
                ),
                1,
            )

    def test_hold_indexes_support_active_scope_and_run_queries(self) -> None:
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspect(self.engine).get_indexes("run_retention_holds")
        }
        self.assertEqual(
            indexes["ix_run_retention_holds_scope_active"],
            ("project_id", "site_id", "active_marker"),
        )
        self.assertEqual(
            indexes["ix_run_retention_holds_run_active"],
            ("run_id", "active_marker"),
        )
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspect(self.engine).get_unique_constraints(
                "run_retention_holds"
            )
        }
        self.assertEqual(
            unique_constraints["uq_run_retention_holds_one_active"],
            ("run_id", "hold_type", "active_marker"),
        )

    def test_retention_job_fields_are_bounded_and_database_checked(self) -> None:
        invalid_cases = {
            "empty job id": {"job_id": ""},
            "long job id": {"job_id": "x" * 65},
            "empty project": {"project_id": ""},
            "long project": {"project_id": "x" * 256},
            "empty site": {"site_id": ""},
            "long site": {"site_id": "x" * 256},
            "keep days below floor": {"keep_days": 29},
            "keep days above ceiling": {"keep_days": 3651},
            "negative high water": {"high_water_observation_id": -1},
            "negative next cursor": {"next_cursor": -1},
            "zero batch limit": {"batch_limit": 0},
            "large batch limit": {"batch_limit": 1001},
            "unknown status": {"status": "cancelled"},
            "empty requested by": {"requested_by": ""},
            "long requested by": {"requested_by": "x" * 256},
            "confirmation actor only": {"confirmed_by": "operator@example.com"},
            "confirmation time only": {"confirmed_at": self.now},
            "empty confirmation actor": {
                "confirmed_by": "",
                "confirmed_at": self.now,
            },
            "long confirmation actor": {
                "confirmed_by": "x" * 256,
                "confirmed_at": self.now,
            },
            "negative deleted count": {"deleted_count": -1},
            "negative batch count": {"batch_count": -1},
            "empty error code": {"error_code": ""},
            "long error code": {"error_code": "x" * 129},
            "false active marker": {"active_marker": False},
        }
        for label, overrides in invalid_cases.items():
            with self.subTest(label):
                self._assert_job_rejected(**overrides)

        with self.engine.begin() as connection:
            connection.execute(
                ObservationRetentionJob.__table__.insert().values(
                    **self._job_values()
                )
            )
        self._assert_job_rejected(job_id="retention-job-duplicate-active")

        completed = {
            "status": "complete",
            "confirmed_by": "operator@example.com",
            "confirmed_at": self.now,
            "deleted_count": 40,
            "batch_count": 2,
            "completed_at": self.now,
            "active_marker": None,
        }
        with self.engine.begin() as connection:
            connection.execute(
                ObservationRetentionJob.__table__.insert().values(
                    **self._job_values(job_id="retention-job-history-1", **completed)
                )
            )
            connection.execute(
                ObservationRetentionJob.__table__.insert().values(
                    **self._job_values(job_id="retention-job-history-2", **completed)
                )
            )

    def test_retention_job_index_supports_one_active_job_per_exact_scope(self) -> None:
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspect(self.engine).get_indexes(
                "observation_retention_jobs"
            )
        }
        self.assertEqual(
            indexes["ix_observation_retention_jobs_scope_active_status"],
            ("project_id", "site_id", "active_marker", "status"),
        )
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspect(self.engine).get_unique_constraints(
                "observation_retention_jobs"
            )
        }
        self.assertEqual(
            unique_constraints["uq_observation_retention_jobs_one_active_scope"],
            ("project_id", "site_id", "active_marker"),
        )


class RunDiscoveryObservationMigrationTests(unittest.TestCase):
    def test_downgrade_refuses_nonempty_observation_table(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            url = f"sqlite:///{(Path(temp_dir) / 'observations.db').as_posix()}"
            command.upgrade(build_alembic_config(url), "e1f2a3b4c5d6")
            engine = create_engine_from_url(url)
            try:
                run_id = DbRunStore(engine).create_run(
                    project_id="project-downgrade",
                    site_id="site-downgrade",
                    job_type="bacnet_discovery",
                )["run_id"]
            finally:
                engine.dispose()

            upgrade_to_head(url)
            engine = create_engine_from_url(url)
            try:
                with engine.begin() as connection:
                    tables = set(inspect(connection).get_table_names())
                    self.assertTrue(
                        {
                            "run_discovery_observations",
                            "run_discovery_observation_states",
                            "run_retention_holds",
                            "observation_retention_jobs",
                            "observation_retention_candidates",
                            "observation_retention_batches",
                        }.issubset(tables)
                    )
                    for table_name in (
                        "run_discovery_observations",
                        "run_discovery_observation_states",
                        "run_retention_holds",
                        "observation_retention_jobs",
                        "observation_retention_candidates",
                        "observation_retention_batches",
                    ):
                        self.assertEqual(
                            connection.scalar(
                                text(f"SELECT COUNT(*) FROM {table_name}")
                            ),
                            0,
                        )
                    connection.execute(
                        RunDiscoveryObservation.__table__.insert().values(
                            run_id=run_id,
                            attempt=1,
                            protocol="bacnet",
                            entity_kind="device",
                            entity_key="1001",
                            entity_version=1,
                            event_key="device-1001",
                            phase="enrichment",
                            outcome="responded",
                            payload_schema_version="1.0",
                            payload={},
                            payload_sha256=("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
                        )
                    )
                    connection.execute(
                        RunDiscoveryObservationState.__table__.insert().values(
                            run_id=run_id,
                            attempt=1,
                            observation_count=1,
                            canonical_payload_bytes=2,
                            terminal_cursor=1,
                            observation_stream_sha256="d" * 64,
                        )
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "observation table is empty"):
                command.downgrade(build_alembic_config(url), "e1f2a3b4c5d6")

            engine = create_engine_from_url(url)
            try:
                self.assertIn(
                    "run_discovery_observations", inspect(engine).get_table_names()
                )
                with engine.connect() as connection:
                    revision = connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                self.assertEqual(revision, "f2a3b4c5d6e7")
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM run_discovery_observations")
                    )
                    connection.execute(
                        update(Run).where(Run.id == run_id).values(status="succeeded")
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "state table is empty"):
                command.downgrade(build_alembic_config(url), "e1f2a3b4c5d6")

            engine = create_engine_from_url(url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM run_discovery_observation_states")
                    )
                    connection.execute(
                        RunRetentionHold.__table__.insert().values(
                            hold_id="released-hold",
                            run_id=run_id,
                            project_id="project-downgrade",
                            site_id="site-downgrade",
                            hold_type="legal",
                            evidence_set_id=None,
                            active_marker=None,
                            placed_by="operator@example.com",
                            reason="Preserve while the case is open.",
                            placed_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
                            released_by="operator@example.com",
                            release_reason="Case closed.",
                            released_at=datetime(2026, 8, 11, 11, 0, tzinfo=UTC),
                        )
                    )
                    connection.execute(
                        ObservationRetentionJob.__table__.insert().values(
                            job_id="completed-retention-job",
                            project_id="project-downgrade",
                            site_id="site-downgrade",
                            keep_days=90,
                            cutoff_sealed_at=datetime(
                                2026, 5, 13, 10, 0, tzinfo=UTC
                            ),
                            high_water_observation_id=0,
                            candidate_run_count=0,
                            candidate_observation_count=0,
                            candidate_manifest_sha256=(
                                "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
                            ),
                            next_cursor=0,
                            batch_limit=250,
                            status="complete",
                            requested_by="operator@example.com",
                            requested_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
                            confirmed_by="operator@example.com",
                            confirmed_at=datetime(2026, 8, 11, 10, 5, tzinfo=UTC),
                            deleted_count=0,
                            batch_count=0,
                            completed_at=datetime(2026, 8, 11, 10, 6, tzinfo=UTC),
                            error_code=None,
                            active_marker=None,
                        )
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "hold audit table is empty"):
                command.downgrade(build_alembic_config(url), "e1f2a3b4c5d6")
            engine = create_engine_from_url(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text("DELETE FROM run_retention_holds"))
            finally:
                engine.dispose()
            with self.assertRaisesRegex(RuntimeError, "job audit table is empty"):
                command.downgrade(build_alembic_config(url), "e1f2a3b4c5d6")
            engine = create_engine_from_url(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text("DELETE FROM observation_retention_jobs"))
            finally:
                engine.dispose()

            command.downgrade(build_alembic_config(url), "e1f2a3b4c5d6")
            engine = create_engine_from_url(url)
            try:
                self.assertTrue(
                    {
                        "run_discovery_observations",
                        "run_discovery_observation_states",
                        "run_retention_holds",
                        "observation_retention_jobs",
                        "observation_retention_candidates",
                        "observation_retention_batches",
                    }.isdisjoint(inspect(engine).get_table_names())
                )
            finally:
                engine.dispose()

    def test_downgrade_refuses_retention_audit_or_active_discovery_work(self) -> None:
        cases = {
            "candidate audit": "candidate audit table is empty",
            "batch audit": "batch audit table is empty",
            "active discovery run": "IP/BACnet discovery runs are terminal",
        }
        for blocker, error_pattern in cases.items():
            with self.subTest(blocker):
                with tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True
                ) as temp_dir:
                    url = (
                        "sqlite:///"
                        f"{(Path(temp_dir) / f'{blocker.replace(' ', '-')}.db').as_posix()}"
                    )
                    upgrade_to_head(url)
                    engine = create_engine_from_url(url)
                    try:
                        run_id = DbRunStore(engine).create_run(
                            project_id="project-downgrade-blocker",
                            site_id="site-downgrade-blocker",
                            job_type="bacnet_discovery",
                        )["run_id"]
                        with engine.begin() as connection:
                            if blocker != "active discovery run":
                                connection.execute(
                                    update(Run)
                                    .where(Run.id == run_id)
                                    .values(status="succeeded")
                                )
                                connection.execute(
                                    ObservationRetentionJob.__table__.insert().values(
                                        job_id="audit-retention-job",
                                        project_id="project-downgrade-blocker",
                                        site_id="site-downgrade-blocker",
                                        keep_days=90,
                                        cutoff_sealed_at=datetime(
                                            2026, 5, 13, 10, 0, tzinfo=UTC
                                        ),
                                        high_water_observation_id=1,
                                        candidate_run_count=(
                                            1 if blocker == "candidate audit" else 0
                                        ),
                                        candidate_observation_count=(
                                            1 if blocker == "candidate audit" else 0
                                        ),
                                        candidate_manifest_sha256="f" * 64,
                                        next_cursor=0,
                                        batch_limit=250,
                                        status="complete",
                                        requested_by="operator@example.com",
                                        requested_at=datetime(
                                            2026, 8, 11, 10, 0, tzinfo=UTC
                                        ),
                                        confirmed_by="operator@example.com",
                                        confirmed_at=datetime(
                                            2026, 8, 11, 10, 1, tzinfo=UTC
                                        ),
                                        deleted_count=0,
                                        batch_count=(
                                            1 if blocker == "batch audit" else 0
                                        ),
                                        completed_at=datetime(
                                            2026, 8, 11, 10, 2, tzinfo=UTC
                                        ),
                                        error_code=None,
                                        active_marker=None,
                                    )
                                )
                            if blocker == "candidate audit":
                                connection.execute(
                                    ObservationRetentionCandidate.__table__.insert().values(
                                        job_id="audit-retention-job",
                                        run_id=run_id,
                                        project_id="project-downgrade-blocker",
                                        site_id="site-downgrade-blocker",
                                        attempt=1,
                                        terminal_status="succeeded",
                                        context_sha256="a" * 64,
                                        result_sha256="b" * 64,
                                        seal_sha256="c" * 64,
                                        sealed_at=datetime(2026, 5, 1, tzinfo=UTC),
                                        terminal_cursor=1,
                                        observation_count=1,
                                        observation_stream_sha256="d" * 64,
                                        verified_at=None,
                                    )
                                )
                            elif blocker == "batch audit":
                                connection.execute(
                                    ObservationRetentionBatch.__table__.insert().values(
                                        job_id="audit-retention-job",
                                        batch_number=1,
                                        actor="operator@example.com",
                                        cursor_before=0,
                                        cursor_after=0,
                                        attempted_count=0,
                                        deleted_count=0,
                                        applied_at=datetime(
                                            2026, 8, 11, 10, 1, tzinfo=UTC
                                        ),
                                    )
                                )
                    finally:
                        engine.dispose()

                    with self.assertRaisesRegex(RuntimeError, error_pattern):
                        command.downgrade(
                            build_alembic_config(url), "e1f2a3b4c5d6"
                        )

                    engine = create_engine_from_url(url)
                    try:
                        with engine.connect() as connection:
                            self.assertEqual(
                                connection.scalar(
                                    text("SELECT version_num FROM alembic_version")
                                ),
                                "f2a3b4c5d6e7",
                            )
                    finally:
                        engine.dispose()


if __name__ == "__main__":
    unittest.main()
