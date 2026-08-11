"""Post-seal provisional observation retention and hold contracts."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from unittest import mock
from uuid import uuid4

from app.services.retention_service import (
    ObservationRetentionConflictError,
    ObservationRetentionService,
    ObservationRetentionValidationError,
    RetentionService,
    cutoff_from_keep_days,
)
from harness import ApiTestCase
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    ActiveProtocolSlot,
    DiscoveredDevice,
    ObservationRetentionBatch,
    ObservationRetentionCandidate,
    ObservationRetentionJob,
    Project,
    Run,
    RunDiscoveryObservation,
    RunExecutionContext,
    RunResult,
    RunRetentionHold,
    RunSeal,
    Site,
)
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationViewV1,
    fold_discovery_observations,
    observation_payload,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class ObservationRetentionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temporary.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = session_factory(self.engine)
        self.service = ObservationRetentionService(self.engine)
        with self.factory.begin() as session:
            for project_id, site_id in (
                ("project-a", "site-a"),
                ("project-b", "site-b"),
            ):
                session.add(Project(id=project_id, name=project_id))
                session.add(Site(id=site_id, project_id=project_id, name=site_id))

    def _seed_run(
        self,
        run_id: str,
        *,
        project_id: str = "project-a",
        site_id: str = "site-a",
        sealed_days_ago: int | None = 60,
        status: str = "succeeded",
        observation_count: int = 1,
        active_slot: bool = False,
    ) -> None:
        created_at = _NOW - timedelta(days=90)
        with self.factory.begin() as session:
            run = Run(
                id=run_id,
                project_id=project_id,
                site_id=site_id,
                job_type="ip_discovery",
                status=status,
                stage="done" if status in {"succeeded", "failed", "cancelled"} else "scan",
                progress_percent=100 if status in {"succeeded", "failed", "cancelled"} else 50,
                parameters={},
                result_summary={},
                attempt=1,
                terminal_at=(_NOW - timedelta(days=sealed_days_ago or 0) if sealed_days_ago is not None else None),
                result_sha256=None,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(run)
            session.flush()
            observation_rows: list[RunDiscoveryObservation] = []
            for index in range(observation_count):
                payload, _encoded, payload_sha256 = observation_payload(
                    {"reachable": True, "address": f"192.0.2.{index + 1}"}
                )
                row = RunDiscoveryObservation(
                    run_id=run_id,
                    attempt=1,
                    protocol="ip",
                    entity_kind="host",
                    entity_key=f"192.0.2.{index + 1}",
                    entity_version=1,
                    event_key=f"{run_id}:event:{index}",
                    phase="reachability",
                    outcome="observed",
                    payload_schema_version="1.0",
                    payload=payload,
                    payload_sha256=payload_sha256,
                    observed_at=created_at,
                    created_at=created_at,
                )
                session.add(row)
                observation_rows.append(row)
            session.flush()
            if sealed_days_ago is not None:
                sealed_at = _NOW - timedelta(days=sealed_days_ago)
                context = RunContextV1(
                    project_id=project_id,
                    site_id=site_id,
                    configuration_snapshot={},
                    configuration_version="retention-fixture-1",
                    engine_parameters={"dry_run": True},
                    requesting_principal="retention-test",
                    application_version="test",
                )
                views = [
                    DiscoveryObservationViewV1(
                        cursor=int(row.id),
                        run_id=run_id,
                        attempt=1,
                        protocol=row.protocol,
                        entity_kind=row.entity_kind,
                        entity_key=row.entity_key,
                        entity_version=row.entity_version,
                        event_key=row.event_key,
                        phase=row.phase,
                        outcome=row.outcome,
                        payload_schema_version=row.payload_schema_version,
                        payload=row.payload,
                        payload_sha256=row.payload_sha256,
                        observed_at=row.observed_at,
                        created_at=row.created_at,
                    )
                    for row in observation_rows
                ]
                terminal_cursor = int(observation_rows[-1].id) if observation_rows else 0
                fold = fold_discovery_observations(
                    views,
                    terminal_cursor=terminal_cursor,
                    expected_count=observation_count,
                    run_id=run_id,
                    attempt=1,
                )
                device = {
                    "project_id": project_id,
                    "site_id": site_id,
                    "address": "192.0.2.1",
                    "device_type": None,
                    "name": None,
                    "vendor": None,
                    "model": None,
                    "attributes": {},
                }
                terminal = TerminalResultV1(
                    status=status,
                    stage="done",
                    summary={"observation_evidence_v1": fold.evidence().model_dump(mode="json")},
                    devices=(device,),
                )
                result_sha256 = terminal.sha256()
                run.result_summary = dict(terminal.summary)
                run.result_sha256 = result_sha256
                session.add(
                    RunExecutionContext(
                        run_id=run_id,
                        schema_version=context.schema_version,
                        context_json=context.model_dump(mode="json"),
                        context_sha256=context.sha256(),
                        created_at=created_at,
                    )
                )
                session.add(
                    RunResult(
                        run_id=run_id,
                        schema_version=terminal.schema_version,
                        terminal_status=terminal.status,
                        terminal_stage=terminal.stage,
                        summary=dict(terminal.summary),
                        result_payload=terminal.model_dump(mode="json"),
                        result_sha256=result_sha256,
                        created_at=sealed_at,
                    )
                )
                session.add(
                    RunSeal(
                        run_id=run_id,
                        terminal_status=terminal.status,
                        context_sha256=context.sha256(),
                        result_sha256=result_sha256,
                        sealed_at=sealed_at,
                    )
                )
                session.add(
                    DiscoveredDevice(
                        run_id=run_id,
                        position=0,
                        project_id=project_id,
                        site_id=site_id,
                        address="192.0.2.1",
                        device_type=None,
                        name=None,
                        vendor=None,
                        model=None,
                        attributes={},
                    )
                )
            if active_slot:
                session.add(
                    ActiveProtocolSlot(
                        protocol_key=f"ip:{run_id}",
                        run_id=run_id,
                        owner_token="owner-a",
                    )
                )

    def _observation_count(self, run_id: str) -> int:
        with self.factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RunDiscoveryObservation)
                    .where(RunDiscoveryObservation.run_id == run_id)
                )
                or 0
            )

    def test_preview_enforces_minimum_and_freezes_candidates_in_one_transaction(self) -> None:
        self._seed_run("eligible")
        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture_statement)
        self.addCleanup(
            event.remove,
            self.engine,
            "before_cursor_execute",
            capture_statement,
        )

        with self.assertRaises(ObservationRetentionValidationError):
            self.service.preview(
                project_id="project-a",
                site_id="site-a",
                keep_days=29,
                batch_limit=100,
                actor="admin-1",
                now=_NOW,
            )
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=100,
            actor="admin-1",
            now=_NOW,
        )

        self.assertEqual(preview["candidate_count"], 1)
        self.assertEqual(preview["requested_by"], "admin-1")
        self.assertEqual(preview["status"], "preview")
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertLess(
            statements.index("BEGIN IMMEDIATE"),
            next(
                index
                for index, statement in enumerate(statements)
                if "FROM runs JOIN run_execution_contexts" in statement
            ),
        )
        with self.factory() as session:
            candidate = session.get(
                ObservationRetentionCandidate,
                (str(preview["job_id"]), "eligible"),
            )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.observation_count, 1)

    def test_preview_retry_and_exact_scope_lookup_recover_the_active_job(self) -> None:
        first = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=100,
            actor="admin-1",
            now=_NOW,
        )
        retry = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=100,
            actor="admin-1",
            now=_NOW + timedelta(minutes=1),
        )
        recovered = self.service.get_active_job(
            project_id="project-a",
            site_id="site-a",
        )

        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertIs(retry["idempotent"], True)
        self.assertEqual(recovered["job_id"], first["job_id"])
        self.assertEqual(recovered["candidate_count"], 0)

    def test_apply_is_exact_scope_bounded_restartable_and_preserves_sealed_authority(self) -> None:
        self._seed_run("eligible-a", observation_count=3)
        self._seed_run(
            "foreign-scope",
            project_id="project-b",
            site_id="site-b",
            observation_count=2,
        )
        self._seed_run("recent-seal", sealed_days_ago=5)
        self._seed_run("unsealed", sealed_days_ago=None, status="running")
        self._seed_run("active-slot", active_slot=True)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=2,
            actor="admin-preview",
            now=_NOW,
        )

        first = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="admin-apply",
            now=_NOW,
        )
        self.assertEqual(first["deleted_this_batch"], 2)
        self.assertEqual(first["status"], "running")
        second = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="admin-apply",
            now=_NOW,
        )

        self.assertEqual(second["deleted_this_batch"], 1)
        self.assertEqual(second["deleted_count"], 3)
        self.assertEqual(second["batch_count"], 2)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(self._observation_count("eligible-a"), 0)
        self.assertEqual(self._observation_count("foreign-scope"), 2)
        self.assertEqual(self._observation_count("recent-seal"), 1)
        self.assertEqual(self._observation_count("unsealed"), 1)
        self.assertEqual(self._observation_count("active-slot"), 1)
        with self.factory() as session:
            self.assertIsNotNone(session.get(RunResult, "eligible-a"))
            self.assertIsNotNone(session.get(RunSeal, "eligible-a"))
            self.assertIsNotNone(
                session.scalar(select(DiscoveredDevice).where(DiscoveredDevice.run_id == "eligible-a"))
            )
            job = session.get(ObservationRetentionJob, str(preview["job_id"]))
            self.assertEqual(job.confirmed_by, "admin-apply")
            self.assertIsNotNone(job.confirmed_at)
            self.assertIsNotNone(job.completed_at)
            self.assertIsNone(job.active_marker)

    def test_apply_fails_closed_when_the_previewed_seal_changes(self) -> None:
        self._seed_run("tampered-seal", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        with self.factory.begin() as session:
            session.get(RunSeal, "tampered-seal").result_sha256 = "0" * 64

        with self.assertRaises(ObservationRetentionConflictError):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )

        self.assertEqual(self._observation_count("tampered-seal"), 2)
        with self.factory() as session:
            candidate = session.get(
                ObservationRetentionCandidate,
                (str(preview["job_id"]), "tampered-seal"),
            )
            self.assertIsNone(candidate.verified_at)
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(ObservationRetentionBatch)
                    .where(ObservationRetentionBatch.job_id == preview["job_id"])
                ),
                0,
            )

    def test_first_delete_recomputes_the_complete_observation_prefix(self) -> None:
        self._seed_run("tampered-prefix", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        with self.factory.begin() as session:
            row = session.scalar(
                select(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == "tampered-prefix")
                .order_by(RunDiscoveryObservation.id)
                .limit(1)
            )
            row.payload = {"reachable": False, "address": "192.0.2.1"}

        with self.assertRaises(ObservationRetentionConflictError):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )

        self.assertEqual(self._observation_count("tampered-prefix"), 2)

    def test_later_batch_rechecks_frozen_bindings_without_recomputing_deleted_rows(self) -> None:
        self._seed_run("tampered-between-batches", observation_count=3)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=2,
            actor="previewer",
            now=_NOW,
        )
        first = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="applier",
            now=_NOW,
        )
        self.assertEqual(first["deleted_this_batch"], 2)
        with self.factory.begin() as session:
            seal = session.get(RunSeal, "tampered-between-batches")
            seal.sealed_at = seal.sealed_at + timedelta(seconds=1)

        with (
            mock.patch("app.services.retention_service.fold_discovery_observations") as fold_spy,
            mock.patch("app.services.retention_service.verify_sealed_run") as seal_spy,
            self.assertRaises(ObservationRetentionConflictError),
        ):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW + timedelta(minutes=1),
            )
        fold_spy.assert_not_called()
        seal_spy.assert_not_called()

        self.assertEqual(self._observation_count("tampered-between-batches"), 1)
        with self.factory() as session:
            batches = session.scalar(
                select(func.count())
                .select_from(ObservationRetentionBatch)
                .where(ObservationRetentionBatch.job_id == preview["job_id"])
            )
        self.assertEqual(batches, 1)

    def test_each_committed_batch_records_its_actual_actor_and_counts(self) -> None:
        self._seed_run("audited-batches", observation_count=3)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=2,
            actor="previewer",
            now=_NOW,
        )

        first = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="first-admin",
            now=_NOW,
        )
        second = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="second-admin",
            now=_NOW + timedelta(minutes=1),
        )

        self.assertEqual(first["attempted_this_batch"], 2)
        self.assertEqual(first["deleted_this_batch"], 2)
        self.assertEqual(second["attempted_this_batch"], 1)
        self.assertEqual(second["deleted_this_batch"], 1)
        with self.factory() as session:
            job = session.get(ObservationRetentionJob, str(preview["job_id"]))
            batches = list(
                session.scalars(
                    select(ObservationRetentionBatch)
                    .where(ObservationRetentionBatch.job_id == preview["job_id"])
                    .order_by(ObservationRetentionBatch.batch_number)
                )
            )
            candidate = session.get(
                ObservationRetentionCandidate,
                (str(preview["job_id"]), "audited-batches"),
            )
        self.assertEqual(job.confirmed_by, "first-admin")
        self.assertEqual([batch.actor for batch in batches], ["first-admin", "second-admin"])
        self.assertEqual([batch.attempted_count for batch in batches], [2, 1])
        self.assertEqual([batch.deleted_count for batch in batches], [2, 1])
        self.assertIsNotNone(candidate.verified_at)

    def test_apply_locks_the_job_before_sorted_candidate_runs(self) -> None:
        self._seed_run("lock-order-b", observation_count=1)
        self._seed_run("lock-order-a", observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        statements: list[object] = []

        def capture_statement(execute_state) -> None:  # noqa: ANN001
            statements.append(execute_state.statement)

        session_class = self.service._session_factory.class_
        event.listen(session_class, "do_orm_execute", capture_statement)
        self.addCleanup(
            event.remove,
            session_class,
            "do_orm_execute",
            capture_statement,
        )
        self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="applier",
            now=_NOW,
        )

        locked_sql = [
            str(statement.compile(dialect=postgresql.dialect()))
            for statement in statements
            if getattr(statement, "_for_update_arg", None) is not None
        ]
        job_lock_index = next(
            index for index, statement in enumerate(locked_sql) if "FROM observation_retention_jobs" in statement
        )
        run_lock_index = next(index for index, statement in enumerate(locked_sql) if "FROM runs" in statement)
        self.assertEqual(job_lock_index, 0)
        self.assertEqual(run_lock_index, 1)
        self.assertIn("ORDER BY runs.id", locked_sql[run_lock_index])
        self.assertIn("FOR UPDATE", locked_sql[run_lock_index])

    def test_apply_bounds_verification_and_run_locks_to_the_next_deletion_window(self) -> None:
        for run_id in ("window-a", "window-b", "window-c", "window-d"):
            self._seed_run(run_id, observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=2,
            actor="previewer",
            now=_NOW,
        )
        locked_sql: list[str] = []

        def capture_statement(execute_state) -> None:  # noqa: ANN001
            statement = execute_state.statement
            if getattr(statement, "_for_update_arg", None) is None:
                return
            compiled = str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            if "FROM runs" in compiled:
                locked_sql.append(compiled)

        session_class = self.service._session_factory.class_
        event.listen(session_class, "do_orm_execute", capture_statement)
        try:
            first = self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="first-applier",
                now=_NOW,
            )
        finally:
            event.remove(session_class, "do_orm_execute", capture_statement)

        self.assertEqual(first["deleted_this_batch"], 2)
        self.assertEqual(first["status"], "running")
        self.assertEqual(len(locked_sql), 1)
        self.assertIn("window-a", locked_sql[0])
        self.assertIn("window-b", locked_sql[0])
        self.assertNotIn("window-c", locked_sql[0])
        self.assertNotIn("window-d", locked_sql[0])
        with self.factory() as session:
            candidates = list(
                session.scalars(
                    select(ObservationRetentionCandidate)
                    .where(ObservationRetentionCandidate.job_id == preview["job_id"])
                    .order_by(ObservationRetentionCandidate.run_id)
                )
            )
        self.assertEqual(
            [candidate.run_id for candidate in candidates if candidate.verified_at is not None],
            ["window-a", "window-b"],
        )
        self.assertEqual(self._observation_count("window-c"), 1)
        self.assertEqual(self._observation_count("window-d"), 1)

        second = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="second-applier",
            now=_NOW + timedelta(minutes=1),
        )

        self.assertEqual(second["deleted_this_batch"], 2)
        self.assertEqual(second["deleted_count"], 4)
        self.assertEqual(second["status"], "complete")

    def test_apply_counts_the_whole_frozen_set_only_during_query_preflight(self) -> None:
        self._seed_run("count-preflight-a", observation_count=1)
        self._seed_run("count-preflight-b", observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=1,
            actor="previewer",
            now=_NOW,
        )
        whole_set_count_query_modes: list[bool] = []

        def capture_statement(
            connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:  # noqa: ANN001
            normalized = " ".join(statement.lower().split())
            if (
                "from run_discovery_observations join observation_retention_candidates"
                not in normalized
                or "count(" not in normalized
            ):
                return
            whole_set_count_query_modes.append(
                connection.get_execution_options().get("smart_commissioning_query_session") is True
            )

        event.listen(self.engine, "before_cursor_execute", capture_statement)
        try:
            applied = self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_statement)

        self.assertEqual(applied["deleted_this_batch"], 1)
        self.assertEqual(whole_set_count_query_modes, [True])

    def test_missing_earlier_candidate_row_blocks_a_later_window_without_delete(self) -> None:
        self._seed_run("drift-missing-a", observation_count=1)
        self._seed_run("drift-intact-b", observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=1,
            actor="previewer",
            now=_NOW,
        )
        with self.factory.begin() as session:
            missing_row = session.scalar(
                select(RunDiscoveryObservation).where(
                    RunDiscoveryObservation.run_id == "drift-missing-a"
                )
            )
            self.assertIsNotNone(missing_row)
            session.delete(missing_row)

        with self.assertRaises(ObservationRetentionConflictError):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )

        self.assertEqual(self._observation_count("drift-intact-b"), 1)
        with self.factory() as session:
            batch_count = session.scalar(
                select(func.count())
                .select_from(ObservationRetentionBatch)
                .where(ObservationRetentionBatch.job_id == preview["job_id"])
            )
        self.assertEqual(batch_count, 0)

    def test_tampered_later_candidate_is_rejected_only_when_its_window_is_reached(self) -> None:
        self._seed_run("window-safe", observation_count=1)
        self._seed_run("window-tampered", observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=1,
            actor="previewer",
            now=_NOW,
        )
        with self.factory.begin() as session:
            session.get(RunSeal, "window-tampered").result_sha256 = "0" * 64

        first = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="first-applier",
            now=_NOW,
        )

        self.assertEqual(first["deleted_this_batch"], 1)
        self.assertEqual(self._observation_count("window-safe"), 0)
        self.assertEqual(self._observation_count("window-tampered"), 1)
        with self.assertRaises(ObservationRetentionConflictError):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="second-applier",
                now=_NOW + timedelta(minutes=1),
            )
        with self.factory() as session:
            batches = list(
                session.scalars(
                    select(ObservationRetentionBatch).where(
                        ObservationRetentionBatch.job_id == preview["job_id"]
                    )
                )
            )
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].deleted_count, 1)

    def test_concurrent_apply_serializes_one_exact_batch_and_remains_restartable(self) -> None:
        self._seed_run("concurrent-window", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=1,
            actor="previewer",
            now=_NOW,
        )
        plans_ready = Barrier(2)
        real_prepare = self.service._prepare_apply_verification

        def synchronized_prepare(*, job_id: str):
            plan = real_prepare(job_id=job_id)
            plans_ready.wait(timeout=5)
            return plan

        def apply_once(actor: str):
            return self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor=actor,
                now=_NOW,
            )

        with (
            mock.patch.object(
                self.service,
                "_prepare_apply_verification",
                side_effect=synchronized_prepare,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(apply_once, actor) for actor in ("concurrent-a", "concurrent-b")]
            outcomes: list[dict[str, object]] = []
            errors: list[BaseException] = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except BaseException as error:  # noqa: BLE001 - the assertion checks the exact public error below
                    errors.append(error)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["deleted_this_batch"], 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ObservationRetentionConflictError)
        resumed = apply_once("resuming-applier")
        self.assertEqual(resumed["deleted_this_batch"], 1)
        self.assertEqual(resumed["deleted_count"], 2)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(self._observation_count("concurrent-window"), 0)
        with self.factory() as session:
            batches = list(
                session.scalars(
                    select(ObservationRetentionBatch)
                    .where(ObservationRetentionBatch.job_id == preview["job_id"])
                    .order_by(ObservationRetentionBatch.batch_number)
                )
            )
        self.assertEqual([batch.batch_number for batch in batches], [1, 2])
        self.assertEqual([batch.attempted_count for batch in batches], [1, 1])
        self.assertEqual([batch.deleted_count for batch in batches], [1, 1])

    def test_paused_verification_does_not_block_an_unrelated_sqlite_writer(self) -> None:
        self._seed_run("retention-candidate", observation_count=2)
        self._seed_run(
            "heartbeat-run",
            sealed_days_ago=None,
            status="running",
        )
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        verification_started = Event()
        release_verification = Event()
        heartbeat_write_started = Event()
        heartbeat_committed = Event()
        real_fold = fold_discovery_observations

        def paused_fold(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            verification_started.set()
            if not release_verification.wait(timeout=5):
                raise AssertionError("test did not release retention verification")
            return real_fold(*args, **kwargs)

        def write_heartbeat() -> None:
            heartbeat_write_started.set()
            with self.factory.begin() as session:
                run = session.get(Run, "heartbeat-run")
                run.heartbeat_at = _NOW + timedelta(seconds=1)
                run.updated_at = _NOW + timedelta(seconds=1)
            heartbeat_committed.set()

        with (
            mock.patch(
                "app.services.retention_service.fold_discovery_observations",
                side_effect=paused_fold,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            apply_future = pool.submit(
                self.service.apply,
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )
            self.assertTrue(verification_started.wait(timeout=2))
            heartbeat_future = pool.submit(write_heartbeat)
            self.assertTrue(heartbeat_write_started.wait(timeout=2))
            committed_while_fold_was_paused = heartbeat_committed.wait(timeout=2)
            release_verification.set()
            heartbeat_future.result(timeout=5)
            applied = apply_future.result(timeout=5)

        self.assertTrue(
            committed_while_fold_was_paused,
            "CPU-heavy retention verification held SQLite's writer reservation",
        )
        self.assertEqual(applied["deleted_this_batch"], 2)

    def test_hold_placed_during_read_side_verification_prevents_deletion(self) -> None:
        self._seed_run("held-during-verification", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        verification_started = Event()
        release_verification = Event()
        real_fold = fold_discovery_observations

        def paused_fold(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            verification_started.set()
            if not release_verification.wait(timeout=5):
                raise AssertionError("test did not release retention verification")
            return real_fold(*args, **kwargs)

        with (
            mock.patch(
                "app.services.retention_service.fold_discovery_observations",
                side_effect=paused_fold,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            apply_future = pool.submit(
                self.service.apply,
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )
            self.assertTrue(verification_started.wait(timeout=2))
            try:
                hold = self.service.place_hold(
                    run_id="held-during-verification",
                    hold_type="legal",
                    reason="Evidence review began during verification.",
                    actor="legal-admin",
                    now=_NOW,
                )
            finally:
                release_verification.set()
            applied = apply_future.result(timeout=5)

        self.assertTrue(hold["active"])
        self.assertEqual(applied["deleted_this_batch"], 0)
        self.assertEqual(applied["status"], "complete")
        self.assertEqual(self._observation_count("held-during-verification"), 2)

    def test_observation_drift_after_proof_rolls_back_delete_and_audit(self) -> None:
        self._seed_run("drift-after-proof", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        proof_finished = Event()
        release_proof = Event()
        real_fold = fold_discovery_observations

        def pause_after_fold(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            folded = real_fold(*args, **kwargs)
            proof_finished.set()
            if not release_proof.wait(timeout=5):
                raise AssertionError("test did not release retention proof")
            return folded

        with (
            mock.patch(
                "app.services.retention_service.fold_discovery_observations",
                side_effect=pause_after_fold,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            apply_future = pool.submit(
                self.service.apply,
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )
            self.assertTrue(proof_finished.wait(timeout=2))
            try:
                payload, _encoded, payload_sha256 = observation_payload({"reachable": True, "address": "192.0.2.99"})
                with self.factory.begin() as session:
                    session.add(
                        RunDiscoveryObservation(
                            run_id="drift-after-proof",
                            attempt=1,
                            protocol="ip",
                            entity_kind="host",
                            entity_key="192.0.2.99",
                            entity_version=1,
                            event_key="drift-after-proof:event:late",
                            phase="reachability",
                            outcome="observed",
                            payload_schema_version="1.0",
                            payload=payload,
                            payload_sha256=payload_sha256,
                            observed_at=_NOW,
                            created_at=_NOW,
                        )
                    )
            finally:
                release_proof.set()
            error = apply_future.exception(timeout=5)

        self.assertIsInstance(error, ObservationRetentionConflictError)
        self.assertEqual(self._observation_count("drift-after-proof"), 3)
        with self.factory() as session:
            candidate = session.get(
                ObservationRetentionCandidate,
                (str(preview["job_id"]), "drift-after-proof"),
            )
            batch_count = session.scalar(
                select(func.count())
                .select_from(ObservationRetentionBatch)
                .where(ObservationRetentionBatch.job_id == preview["job_id"])
            )
        self.assertIsNone(candidate.verified_at)
        self.assertEqual(batch_count, 0)

    def test_postgres_mutation_sets_read_committed_before_the_job_lock(self) -> None:
        self._seed_run("postgres-isolation", observation_count=1)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        postgres_engine = mock.MagicMock()
        postgres_engine.dialect.name = "postgresql"
        mutation_session = mock.MagicMock()
        mutation_session.execute.side_effect = RuntimeError("stop after isolation")
        mutation_factory = mock.MagicMock()
        mutation_factory.begin.return_value.__enter__.return_value = mutation_session
        self.service._engine = postgres_engine
        self.service._session_factory = mutation_factory

        with self.assertRaisesRegex(RuntimeError, "stop after isolation"):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE PROVISIONAL OBSERVATIONS",
                actor="applier",
                now=_NOW,
            )

        first_statement = mutation_session.execute.call_args_list[0].args[0]
        self.assertEqual(
            str(first_statement),
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        )
        mutation_session.scalar.assert_not_called()


class ObservationRetentionApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": "observation-retention-admin-key",
    }
    client_headers = {"X-API-Key": "observation-retention-admin-key"}

    def setUp(self) -> None:
        from app.core.db import get_engine

        suffix = uuid4().hex[:12]
        self.project_id = f"retention-project-{suffix}"
        self.site_id = f"retention-site-{suffix}"
        self.run_id = f"run-retention-{suffix}"
        with session_factory(get_engine()).begin() as session:
            session.add(Project(id=self.project_id, name=self.project_id))
            session.add(
                Site(
                    id=self.site_id,
                    project_id=self.project_id,
                    name=self.site_id,
                )
            )
            session.flush()
            session.add(
                Run(
                    id=self.run_id,
                    project_id=self.project_id,
                    site_id=self.site_id,
                    job_type="ip_discovery",
                    status="succeeded",
                    stage="done",
                    progress_percent=100,
                    parameters={},
                    result_summary={},
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )

    def test_job_endpoints_require_exact_acknowledgement_and_report_durable_state(self) -> None:
        preview = self.client.post(
            "/api/v1/evidence/retention/observations/preview",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "keep_days": 30,
                "batch_limit": 25,
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        job_id = preview.json()["job_id"]
        self.assertEqual(preview.json()["requested_by"], "shared_key")

        retried_preview = self.client.post(
            "/api/v1/evidence/retention/observations/preview",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "keep_days": 30,
                "batch_limit": 25,
            },
        )
        self.assertEqual(retried_preview.status_code, 200, retried_preview.text)
        self.assertEqual(retried_preview.json()["job_id"], job_id)
        self.assertIs(retried_preview.json()["idempotent"], True)
        active = self.client.get(
            "/api/v1/evidence/retention/observations/jobs/active",
            params={"project_id": self.project_id, "site_id": self.site_id},
        )
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(active.json()["job_id"], job_id)
        wrong_scope = self.client.get(
            "/api/v1/evidence/retention/observations/jobs/active",
            params={"project_id": "wrong-project", "site_id": self.site_id},
        )
        self.assertEqual(wrong_scope.status_code, 404, wrong_scope.text)

        status = self.client.get(f"/api/v1/evidence/retention/observations/jobs/{job_id}")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "preview")

        rejected = self.client.post(
            f"/api/v1/evidence/retention/observations/jobs/{job_id}/apply",
            json={"acknowledge": "DELETE"},
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        applied = self.client.post(
            f"/api/v1/evidence/retention/observations/jobs/{job_id}/apply",
            json={"acknowledge": "DELETE PROVISIONAL OBSERVATIONS"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["status"], "complete")
        self.assertEqual(applied.json()["confirmed_by"], "shared_key")

        repeated = self.client.post(
            f"/api/v1/evidence/retention/observations/jobs/{job_id}/apply",
            json={"acknowledge": "DELETE PROVISIONAL OBSERVATIONS"},
        )
        self.assertEqual(repeated.status_code, 409, repeated.text)
        missing = self.client.get("/api/v1/evidence/retention/observations/jobs/missing-job")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_hold_endpoints_preserve_release_history_and_stable_errors(self) -> None:
        placed = self.client.post(
            "/api/v1/evidence/retention/holds",
            json={
                "run_id": self.run_id,
                "hold_type": "legal",
                "reason": "Preserve for legal review.",
            },
        )
        self.assertEqual(placed.status_code, 200, placed.text)
        hold_id = placed.json()["hold_id"]
        duplicate = self.client.post(
            "/api/v1/evidence/retention/holds",
            json={
                "run_id": self.run_id,
                "hold_type": "legal",
                "reason": "Duplicate legal hold.",
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

        listed = self.client.get(
            "/api/v1/evidence/retention/holds",
            params={"project_id": self.project_id, "site_id": self.site_id},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["count"], 1)
        released = self.client.post(
            f"/api/v1/evidence/retention/holds/{hold_id}/release",
            json={"reason": "Legal review complete."},
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertFalse(released.json()["active"])
        repeated = self.client.post(
            f"/api/v1/evidence/retention/holds/{hold_id}/release",
            json={"reason": "Again."},
        )
        self.assertEqual(repeated.status_code, 409, repeated.text)

        history = self.client.get(
            "/api/v1/evidence/retention/holds",
            params={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "include_released": True,
            },
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["count"], 1)
        missing = self.client.post(
            "/api/v1/evidence/retention/holds",
            json={
                "run_id": "missing-run",
                "hold_type": "legal",
                "reason": "Missing.",
            },
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_observation_retention_preview_requires_authentication(self) -> None:
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as unauthenticated:
            response = unauthenticated.post(
                "/api/v1/evidence/retention/observations/preview",
                json={
                    "project_id": self.project_id,
                    "site_id": self.site_id,
                    "keep_days": 30,
                },
            )
        self.assertEqual(response.status_code, 401, response.text)


class ObservationRetentionHoldServiceTests(unittest.TestCase):
    setUp = ObservationRetentionServiceTests.setUp
    _seed_run = ObservationRetentionServiceTests._seed_run
    _observation_count = ObservationRetentionServiceTests._observation_count

    def test_hold_placed_after_preview_wins_and_can_be_released_with_audit(self) -> None:
        self._seed_run("held-after-preview", observation_count=2)
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        hold = self.service.place_hold(
            run_id="held-after-preview",
            hold_type="legal",
            reason="Preserve for incident 204.",
            actor="legal-admin",
            now=_NOW,
        )

        applied = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="retention-admin",
            now=_NOW,
        )

        self.assertEqual(applied["deleted_count"], 0)
        self.assertEqual(applied["status"], "complete")
        self.assertEqual(self._observation_count("held-after-preview"), 2)
        active = self.service.list_holds(project_id="project-a", site_id="site-a")
        self.assertEqual([item["hold_id"] for item in active], [hold["hold_id"]])
        released = self.service.release_hold(
            hold_id=str(hold["hold_id"]),
            reason="Incident evidence exported.",
            actor="legal-admin-2",
            now=_NOW + timedelta(minutes=1),
        )
        self.assertFalse(released["active"])
        self.assertEqual(released["released_by"], "legal-admin-2")
        self.assertEqual(
            self.service.list_holds(project_id="project-a", site_id="site-a"),
            [],
        )
        history = self.service.list_holds(
            project_id="project-a",
            site_id="site-a",
            include_released=True,
        )
        self.assertEqual(len(history), 1)

    def test_releasing_a_hold_after_preview_does_not_expand_frozen_membership(self) -> None:
        self._seed_run("held-at-preview", observation_count=2)
        hold = self.service.place_hold(
            run_id="held-at-preview",
            hold_type="legal",
            reason="Preserve during preview.",
            actor="legal-admin",
            now=_NOW,
        )
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="previewer",
            now=_NOW,
        )
        self.service.release_hold(
            hold_id=str(hold["hold_id"]),
            reason="Review complete.",
            actor="legal-admin",
            now=_NOW + timedelta(minutes=1),
        )

        applied = self.service.apply(
            job_id=str(preview["job_id"]),
            acknowledge="DELETE PROVISIONAL OBSERVATIONS",
            actor="retention-admin",
            now=_NOW + timedelta(minutes=2),
        )

        self.assertEqual(preview["candidate_run_count"], 0)
        self.assertEqual(applied["deleted_count"], 0)
        self.assertEqual(self._observation_count("held-at-preview"), 2)

    def test_invalid_acknowledgement_and_duplicate_hold_are_stable_conflicts(self) -> None:
        self._seed_run("conflict-run")
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=10,
            actor="admin",
            now=_NOW,
        )
        with self.assertRaises(ObservationRetentionValidationError):
            self.service.apply(
                job_id=str(preview["job_id"]),
                acknowledge="DELETE",
                actor="admin",
                now=_NOW,
            )
        self.service.place_hold(
            run_id="conflict-run",
            hold_type="evidence",
            evidence_set_id="evidence-1",
            reason="Release evidence.",
            actor="admin",
            now=_NOW,
        )
        with self.assertRaises(ObservationRetentionConflictError):
            self.service.place_hold(
                run_id="conflict-run",
                hold_type="evidence",
                evidence_set_id="evidence-2",
                reason="Duplicate.",
                actor="admin",
                now=_NOW,
            )

    def test_whole_run_retention_refuses_an_active_hold(self) -> None:
        self._seed_run("whole-run-held")
        self.service.place_hold(
            run_id="whole-run-held",
            hold_type="legal",
            reason="Legal preservation.",
            actor="legal-admin",
            now=_NOW,
        )

        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30, now=_NOW),
            confirm=True,
        )

        self.assertNotIn("whole-run-held", result.deleted_run_ids)
        self.assertIn("whole-run-held", result.skipped_held_run_ids)
        with self.factory() as session:
            self.assertIsNotNone(session.get(Run, "whole-run-held"))
            self.assertIsNotNone(
                session.scalar(
                    select(RunRetentionHold).where(
                        RunRetentionHold.run_id == "whole-run-held",
                        RunRetentionHold.active_marker.is_(True),
                    )
                )
            )

    def test_whole_run_retention_refuses_an_active_observation_job(self) -> None:
        self._seed_run("whole-run-observation-job")
        preview = self.service.preview(
            project_id="project-a",
            site_id="site-a",
            keep_days=30,
            batch_limit=100,
            actor="retention-admin",
            now=_NOW,
        )

        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30, now=_NOW),
            confirm=True,
        )

        self.assertNotIn("whole-run-observation-job", result.deleted_run_ids)
        self.assertIn(
            "whole-run-observation-job",
            result.skipped_active_observation_retention_run_ids,
        )
        with self.factory() as session:
            self.assertIsNotNone(session.get(Run, "whole-run-observation-job"))
            self.assertIsNotNone(session.get(ObservationRetentionJob, str(preview["job_id"])))

    def test_whole_run_delete_preserves_released_hold_history(self) -> None:
        self._seed_run("whole-run-released")
        hold = self.service.place_hold(
            run_id="whole-run-released",
            hold_type="legal",
            reason="Temporary legal preservation.",
            actor="legal-admin",
            now=_NOW,
        )
        self.service.release_hold(
            hold_id=str(hold["hold_id"]),
            reason="Case closed.",
            actor="legal-admin",
            now=_NOW + timedelta(minutes=1),
        )

        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30, now=_NOW),
            confirm=True,
        )

        self.assertIn("whole-run-released", result.deleted_run_ids)
        with self.factory() as session:
            self.assertIsNone(session.get(Run, "whole-run-released"))
            history = session.get(RunRetentionHold, str(hold["hold_id"]))
        self.assertIsNotNone(history)
        self.assertIsNone(history.active_marker)

    def test_hold_and_whole_run_delete_use_the_same_postgres_run_lock(self) -> None:
        run = Run(
            id="lock-protocol-run",
            project_id="project-a",
            site_id="site-a",
            job_type="ip_discovery",
            status="succeeded",
            stage="done",
            progress_percent=100,
            parameters={},
            result_summary={},
            created_at=_NOW - timedelta(days=90),
            updated_at=_NOW,
        )
        hold_session = mock.MagicMock()
        hold_session.scalar.side_effect = [run, None]
        hold_factory = mock.MagicMock()
        hold_factory.begin.return_value.__enter__.return_value = hold_session
        hold_service = ObservationRetentionService(self.engine)
        hold_service._session_factory = hold_factory
        hold_service.place_hold(
            run_id=run.id,
            hold_type="legal",
            reason="Lock protocol test.",
            actor="legal-admin",
            now=_NOW,
        )
        hold_statement = hold_session.scalar.call_args_list[0].args[0]

        def scalar_result(values: list[object]) -> mock.MagicMock:
            result = mock.MagicMock()
            result.all.return_value = values
            return result

        delete_session = mock.MagicMock()
        delete_session.scalars.side_effect = [
            scalar_result([]),
            scalar_result([run]),
            scalar_result([]),
            scalar_result([]),
        ]
        delete_factory = mock.MagicMock()
        delete_factory.begin.return_value.__enter__.return_value = delete_session
        delete_service = RetentionService(self.engine)
        delete_service._session_factory = delete_factory
        delete_service.apply(
            before=cutoff_from_keep_days(30, now=_NOW),
            confirm=True,
        )
        delete_statement = delete_session.scalars.call_args_list[1].args[0]

        hold_sql = str(hold_statement.compile(dialect=postgresql.dialect()))
        delete_sql = str(delete_statement.compile(dialect=postgresql.dialect()))
        self.assertIn("FROM runs", hold_sql)
        self.assertIn("FOR UPDATE", hold_sql)
        self.assertIn("FROM runs", delete_sql)
        self.assertIn("ORDER BY runs.id", delete_sql)
        self.assertIn("FOR UPDATE", delete_sql)


if __name__ == "__main__":
    unittest.main()
