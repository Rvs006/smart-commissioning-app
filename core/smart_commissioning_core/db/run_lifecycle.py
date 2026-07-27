"""Transactional lifecycle-v2 repository for SQLite and Postgres."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smart_commissioning_core.db.db_run_store import (
    get_or_create_project_and_site,
    new_run_id,
)
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    ActiveProtocolSlot,
    DiscoveredDevice,
    DiscoveredPoint,
    DiscoveredTopic,
    Run,
    RunDispatch,
    RunExecutionContext,
    RunIssue,
    RunLifecycleConflict,
    RunResult,
    RunSeal,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import (
    RunContextV1,
    canonical_context_sha256,
    json_safe_value,
)
from smart_commissioning_core.run_lifecycle import (
    CancelOutcomeV1,
    DispatchEnvelopeV1,
    FinalizeOutcome,
    RunDispatchV1,
    RunLeaseV1,
    RunSealViewV1,
    StoredRunContextV1,
    TerminalResultV1,
)

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ProtocolConflictError(RuntimeError):
    """Raised before dispatch when a canonical protocol key is already reserved."""

    def __init__(self, protocol_key: str, active_run_id: str) -> None:
        self.protocol_key = protocol_key
        self.active_run_id = active_run_id
        super().__init__(f"protocol key is active for run {active_run_id}")


class RunLifecycleRepository:
    """Own context, outbox, lease, protocol slot, finalization, and recovery."""

    def __init__(
        self,
        engine: Engine,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)
        self._fault_injector = fault_injector

    # -- run/context/outbox -------------------------------------------------

    def create_run_with_context(
        self,
        *,
        job_type: str,
        context: RunContextV1 | Mapping[str, Any],
        execution_mode: str | None = None,
        edge_id: str | None = None,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        now: datetime | None = None,
    ) -> DispatchEnvelopeV1:
        captured = (
            context
            if isinstance(context, RunContextV1)
            else RunContextV1.model_validate(context)
        )
        created_at = now or _utcnow()
        actual_run_id = run_id or new_run_id(created_at)
        actual_dispatch_id = dispatch_id or f"dispatch_{uuid4().hex}"
        context_sha256 = canonical_context_sha256(captured)
        protocol_key = captured.protocol_key
        try:
            with self._session_factory.begin() as session:
                get_or_create_project_and_site(
                    session, captured.project_id, captured.site_id
                )
                session.add(
                    Run(
                        id=actual_run_id,
                        project_id=captured.project_id,
                        site_id=captured.site_id,
                        job_type=job_type,
                        status="queued",
                        stage="awaiting_dispatch",
                        progress_percent=0,
                        parameters=dict(captured.engine_parameters),
                        result_summary={},
                        execution_mode=execution_mode,
                        edge_id=edge_id,
                        error_message=None,
                        cancel_requested=False,
                        attempt=0,
                        state_version=0,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
                session.flush()
                session.add(
                    RunExecutionContext(
                        run_id=actual_run_id,
                        schema_version=captured.schema_version,
                        context_json=captured.model_dump(mode="json"),
                        context_sha256=context_sha256,
                        created_at=created_at,
                    )
                )
                session.add(
                    RunDispatch(
                        dispatch_id=actual_dispatch_id,
                        run_id=actual_run_id,
                        state="pending",
                        publish_attempts=0,
                        created_at=created_at,
                    )
                )
                if protocol_key is not None:
                    session.add(
                        ActiveProtocolSlot(
                            protocol_key=protocol_key,
                            run_id=actual_run_id,
                            owner_token=None,
                            acquired_at=created_at,
                        )
                    )
                session.flush()
        except IntegrityError as error:
            if protocol_key is not None:
                active_run_id = self.get_protocol_conflict(protocol_key)
                if active_run_id is not None:
                    raise ProtocolConflictError(protocol_key, active_run_id) from error
            raise
        return DispatchEnvelopeV1(
            run_id=actual_run_id,
            dispatch_id=actual_dispatch_id,
            context_sha256=context_sha256,
        )

    def get_context(self, run_id: str) -> StoredRunContextV1:
        with self._session_factory() as session:
            row = session.get(RunExecutionContext, run_id)
            if row is None:
                raise FileNotFoundError(run_id)
            return StoredRunContextV1(
                run_id=run_id,
                context=RunContextV1.model_validate(row.context_json),
                context_sha256=row.context_sha256,
                created_at=row.created_at,
            )

    def get_dispatch(self, dispatch_id: str) -> RunDispatchV1:
        with self._session_factory() as session:
            row = session.get(RunDispatch, dispatch_id)
            if row is None:
                raise FileNotFoundError(dispatch_id)
            return self._dispatch_view(row)

    def get_dispatch_for_run(self, run_id: str) -> RunDispatchV1:
        """Return the one durable dispatch identity committed with ``run_id``."""
        with self._session_factory() as session:
            row = session.scalar(
                select(RunDispatch).where(RunDispatch.run_id == run_id)
            )
            if row is None:
                raise FileNotFoundError(run_id)
            return self._dispatch_view(row)

    def list_pending_dispatches(self, *, limit: int = 100) -> list[RunDispatchV1]:
        statement = (
            select(RunDispatch)
            .where(RunDispatch.state == "pending")
            .order_by(RunDispatch.created_at, RunDispatch.dispatch_id)
            .limit(max(1, limit))
        )
        with self._session_factory() as session:
            return [self._dispatch_view(row) for row in session.scalars(statement)]

    def mark_dispatch_published(
        self, dispatch_id: str, *, now: datetime | None = None
    ) -> bool:
        published_at = now or _utcnow()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(RunDispatch)
                .where(RunDispatch.dispatch_id == dispatch_id)
                .values(
                    state="published",
                    published_at=published_at,
                    publish_attempts=RunDispatch.publish_attempts + 1,
                )
            )
            return result.rowcount == 1

    # -- claim/heartbeat/progress ------------------------------------------

    def claim_run(
        self,
        run_id: str,
        dispatch_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
        owner_token: str | None = None,
    ) -> RunLeaseV1 | None:
        lease_seconds = max(1, int(lease_seconds))
        token = owner_token or token_urlsafe(32)
        with self._session_factory.begin() as session:
            # A new owner must receive the full requested lease after any row
            # lock wait. Sampling before the lock could create a lease that was
            # already partly, or completely, spent when the claim committed.
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update()
            )
            dispatch = session.get(RunDispatch, dispatch_id)
            context = session.get(RunExecutionContext, run_id)
            if (
                run is None
                or dispatch is None
                or dispatch.run_id != run_id
                or context is None
            ):
                self._add_conflict(
                    session,
                    run_id,
                    operation="claim",
                    reason="invalid_dispatch_or_context",
                    owner_token=token,
                )
                return None
            claimed_at = now or _utcnow()
            expires_at = claimed_at + timedelta(seconds=lease_seconds)
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "queued",
                    Run.owner_token.is_(None),
                    Run.terminal_at.is_(None),
                )
                .values(
                    status="running",
                    stage="worker_claimed",
                    progress_percent=0,
                    owner_token=token,
                    attempt=Run.attempt + 1,
                    claimed_at=claimed_at,
                    heartbeat_at=claimed_at,
                    lease_expires_at=expires_at,
                    state_version=Run.state_version + 1,
                    updated_at=claimed_at,
                )
            )
            if result.rowcount != 1:
                return None
            session.execute(
                update(ActiveProtocolSlot)
                .where(ActiveProtocolSlot.run_id == run_id)
                .values(owner_token=token)
            )
            session.expire(run)
            run = self._load_run(session, run_id)
            return RunLeaseV1(
                run_id=run_id,
                dispatch_id=dispatch_id,
                owner_token=token,
                attempt=run.attempt,
                claimed_at=claimed_at,
                heartbeat_at=claimed_at,
                lease_expires_at=expires_at,
                context_sha256=context.context_sha256,
            )

    def heartbeat(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        with self._session_factory.begin() as session:
            # Acquire the lifecycle row before sampling wall time. SQLite's
            # BEGIN IMMEDIATE and PostgreSQL's FOR UPDATE can both wait behind
            # another writer. A timestamp captured before that wait could let
            # an already-expired owner renew after the lease deadline.
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update()
            )
            if run is None:
                return False
            heartbeat_at = now or _utcnow()
            expires_at = heartbeat_at + timedelta(
                seconds=max(1, int(lease_seconds))
            )
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.terminal_at.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > heartbeat_at,
                )
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=expires_at,
                    state_version=Run.state_version + 1,
                    updated_at=heartbeat_at,
                )
            )
            if result.rowcount == 1:
                return True
            self._add_conflict(
                session,
                run_id,
                operation="heartbeat",
                reason="stale_owner_or_terminal",
                owner_token=owner_token,
            )
            return False

    def update_progress(
        self,
        run_id: str,
        owner_token: str,
        *,
        stage: str | None = None,
        progress_percent: int | None = None,
        summary: Mapping[str, Any] | None = None,
        merge_summary: bool = True,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        with self._session_factory.begin() as session:
            # Lock first, then decide whether the lease is still valid. This
            # prevents a progress write begun before expiry from committing
            # after a database-lock wait carried it across the deadline.
            run = self._load_run(session, run_id, for_update=True)
            updated_at = now or _utcnow()
            if (
                run.status != "running"
                or run.owner_token != owner_token
                or run.terminal_at is not None
            ):
                self._add_conflict(
                    session,
                    run_id,
                    operation="progress",
                    reason="stale_owner_or_terminal",
                    owner_token=owner_token,
                )
                return False
            values: dict[str, Any] = {
                "error_message": error_message,
                "state_version": Run.state_version + 1,
                "updated_at": updated_at,
            }
            if stage is not None:
                values["stage"] = stage
            if progress_percent is not None:
                values["progress_percent"] = max(
                    0, min(100, int(progress_percent))
                )
            if summary is not None:
                current = run.result_summary if isinstance(run.result_summary, dict) else {}
                values["result_summary"] = (
                    {**current, **dict(summary)} if merge_summary else dict(summary)
                )
            guarded = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.terminal_at.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > updated_at,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount == 1:
                return True
            self._add_conflict(
                session,
                run_id,
                operation="progress",
                reason="stale_owner_or_terminal",
                owner_token=owner_token,
            )
            return False

    def is_cancel_requested(self, run_id: str, owner_token: str | None = None) -> bool:
        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return True
            if owner_token is not None and (
                run.owner_token != owner_token or run.status != "running"
            ):
                return True
            return bool(run.cancel_requested)

    # -- cancellation/finalization -----------------------------------------

    def request_cancel(
        self, run_id: str, *, now: datetime | None = None
    ) -> CancelOutcomeV1:
        requested_at = now or _utcnow()
        with self._session_factory.begin() as session:
            run = self._load_run(session, run_id)
            if run.status in _TERMINAL_STATUSES:
                return CancelOutcomeV1(run_id=run_id, state="terminal", changed=False)
            if run.status == "queued":
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "queued",
                        Run.owner_token.is_(None),
                        Run.terminal_at.is_(None),
                        Run.state_version == run.state_version,
                    )
                    .values(
                        status="cancelled",
                        cancel_requested=True,
                        updated_at=requested_at,
                    )
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:
                    session.expire(run)
                    run = self._load_run(session, run_id)
                else:
                    session.expire(run)
                    run = self._load_run(session, run_id)
            if run.status == "cancelled" and run.terminal_at is None:
                result = TerminalResultV1(
                    status="cancelled",
                    stage="cancelled_before_start",
                    summary={"cancelled_before_start": True},
                )
                context = session.get(RunExecutionContext, run_id)
                if context is None:
                    raise RuntimeError(f"run {run_id} has no execution context")
                self._apply_finalization(
                    session,
                    run,
                    result,
                    result.sha256(),
                    context.context_sha256,
                    requested_at,
                )
                return CancelOutcomeV1(run_id=run_id, state="cancelled", changed=True)
            if run.status in _TERMINAL_STATUSES:
                return CancelOutcomeV1(run_id=run_id, state="terminal", changed=False)
            if run.cancel_requested:
                return CancelOutcomeV1(
                    run_id=run_id,
                    state="cancellation_requested",
                    changed=False,
                )
            guarded = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.terminal_at.is_(None),
                    Run.cancel_requested.is_(False),
                )
                .values(
                    cancel_requested=True,
                    state_version=Run.state_version + 1,
                    updated_at=requested_at,
                )
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount == 1:
                return CancelOutcomeV1(
                    run_id=run_id,
                    state="cancellation_requested",
                    changed=True,
                )
            session.expire(run)
            run = self._load_run(session, run_id)
            state = "terminal" if run.status in _TERMINAL_STATUSES else "cancellation_requested"
            return CancelOutcomeV1(run_id=run_id, state=state, changed=False)

    def finalize_run(
        self,
        run_id: str,
        owner_token: str,
        result: TerminalResultV1 | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> FinalizeOutcome:
        terminal = (
            result
            if isinstance(result, TerminalResultV1)
            else TerminalResultV1.model_validate(result)
        )
        result_sha256 = terminal.sha256()
        with self._session_factory.begin() as session:
            # First finalization is lease-fenced at the instant the lifecycle
            # row becomes writable, not when the caller began waiting for it.
            # Identical terminal replay remains idempotent below.
            self._load_run(session, run_id, for_update=True)
            terminal_at = now or _utcnow()
            existing = session.get(RunResult, run_id)
            if existing is None:
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "running",
                        Run.owner_token == owner_token,
                        Run.terminal_at.is_(None),
                        Run.result_sha256.is_(None),
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at > terminal_at,
                    )
                    .values(status=terminal.status)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount == 1:
                    run = self._load_run(session, run_id)
                    context = session.get(RunExecutionContext, run_id)
                    if context is None:
                        raise RuntimeError(f"run {run_id} has no execution context")
                    self._apply_finalization(
                        session,
                        run,
                        terminal,
                        result_sha256,
                        context.context_sha256,
                        terminal_at,
                    )
                    return FinalizeOutcome(applied=True, result_sha256=result_sha256)
            run = self._load_run(session, run_id)
            existing = session.get(RunResult, run_id)
            if run.status in _TERMINAL_STATUSES or existing is not None:
                if (
                    run.owner_token == owner_token
                    and existing is not None
                    and existing.terminal_status == terminal.status
                    and existing.result_sha256 == result_sha256
                ):
                    return FinalizeOutcome(
                        idempotent=True, result_sha256=result_sha256
                    )
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize",
                    reason="terminal_result_conflict",
                    owner_token=owner_token,
                    attempted_status=terminal.status,
                    attempted_sha256=result_sha256,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason="terminal_result_conflict",
                    result_sha256=existing.result_sha256 if existing else run.result_sha256,
                )
            self._add_conflict(
                session,
                run_id,
                operation="finalize",
                reason="stale_owner",
                owner_token=owner_token,
                attempted_status=terminal.status,
                attempted_sha256=result_sha256,
            )
            return FinalizeOutcome(
                conflict=True,
                reason="stale_owner",
                result_sha256=None,
            )

    # -- recovery/protocol/audit -------------------------------------------

    def recover_expired_leases(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[str]:
        observed_at = now or _utcnow()
        with self._session_factory() as session:
            candidates = list(
                session.scalars(
                    select(Run.id)
                    .where(
                        Run.status == "running",
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at <= observed_at,
                    )
                    .order_by(Run.lease_expires_at, Run.id)
                    .limit(max(1, limit))
                )
            )
        recovered: list[str] = []
        for run_id in candidates:
            with self._session_factory.begin() as session:
                run = self._load_run(session, run_id, for_update=True)
                if (
                    run.status != "running"
                    or run.lease_expires_at is None
                    or run.lease_expires_at > observed_at
                ):
                    continue
                status = "cancelled" if run.cancel_requested else "failed"
                stage = "lease_expired_cancelled" if run.cancel_requested else "lease_expired"
                summary = dict(run.result_summary or {})
                summary.update(
                    {
                        "lease_recovered": True,
                        "expired_attempt": run.attempt,
                    }
                )
                terminal = self._terminal_snapshot(
                    session,
                    run,
                    status=status,
                    stage=stage,
                    summary=summary,
                    error_message=(
                        None if status == "cancelled" else "execution owner lease expired"
                    ),
                )
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "running",
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at <= observed_at,
                        Run.state_version == run.state_version,
                    )
                    .values(status=terminal.status)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:
                    continue
                session.expire(run)
                run = self._load_run(session, run_id)
                context = session.get(RunExecutionContext, run_id)
                if context is None:
                    raise RuntimeError(f"run {run_id} has no execution context")
                self._apply_finalization(
                    session,
                    run,
                    terminal,
                    terminal.sha256(),
                    context.context_sha256,
                    observed_at,
                )
                recovered.append(run_id)
        return recovered

    def get_protocol_conflict(self, protocol_key: str) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ActiveProtocolSlot.run_id).where(
                    ActiveProtocolSlot.protocol_key == protocol_key
                )
            )

    def acquire_protocol_slot(
        self,
        run_id: str,
        protocol_key: str,
        *,
        owner_token: str | None = None,
        now: datetime | None = None,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                self._load_run(session, run_id)
                session.add(
                    ActiveProtocolSlot(
                        protocol_key=protocol_key,
                        run_id=run_id,
                        owner_token=owner_token,
                        acquired_at=now or _utcnow(),
                    )
                )
                session.flush()
        except IntegrityError as error:
            active_run_id = self.get_protocol_conflict(protocol_key)
            if active_run_id is not None:
                raise ProtocolConflictError(protocol_key, active_run_id) from error
            raise

    def get_seal(self, run_id: str) -> RunSealViewV1:
        with self._session_factory() as session:
            seal = session.get(RunSeal, run_id)
            if seal is None:
                raise FileNotFoundError(run_id)
            return RunSealViewV1(
                run_id=run_id,
                terminal_status=seal.terminal_status,
                context_sha256=seal.context_sha256,
                result_sha256=seal.result_sha256,
                sealed_at=seal.sealed_at,
            )

    def conflict_count(self, run_id: str) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(RunLifecycleConflict)
                    .where(RunLifecycleConflict.run_id == run_id)
                )
                or 0
            )

    # -- internal transaction helpers --------------------------------------

    def _apply_finalization(
        self,
        session: Session,
        run: Run,
        terminal: TerminalResultV1,
        result_sha256: str,
        context_sha256: str,
        terminal_at: datetime,
    ) -> None:
        payload = json_safe_value(terminal)
        summary = dict(payload["summary"])
        issues = list(payload["issues"])
        devices = list(payload["devices"])
        points = list(payload["points"])
        topics = list(payload["topics"])
        terminal_stage = str(payload["stage"])
        error_message = payload.get("error_message")
        session.add(
            RunResult(
                run_id=run.id,
                schema_version=terminal.schema_version,
                terminal_status=terminal.status,
                terminal_stage=terminal_stage,
                summary=summary,
                result_payload=payload,
                result_sha256=result_sha256,
                created_at=terminal_at,
            )
        )
        session.flush()
        self._inject_fault("after_result")

        for model in (RunIssue, DiscoveredDevice, DiscoveredPoint, DiscoveredTopic):
            session.execute(delete(model).where(model.run_id == run.id))
        session.flush()
        for position, issue in enumerate(issues):
            record = ValidationIssueRecord.model_validate(issue)
            session.add(
                RunIssue(
                    run_id=run.id,
                    position=position,
                    **record.model_dump(),
                )
            )
        for position, device in enumerate(devices):
            session.add(self._device_row(run.id, position, device, terminal_at))
        for position, point in enumerate(points):
            session.add(self._point_row(run.id, position, point, terminal_at))
        for position, topic in enumerate(topics):
            session.add(self._topic_row(run.id, position, topic, terminal_at))
        session.flush()
        self._inject_fault("after_evidence")

        run.status = terminal.status
        run.stage = terminal_stage
        run.progress_percent = 100
        run.result_summary = summary
        run.error_message = error_message
        run.terminal_at = terminal_at
        run.result_sha256 = result_sha256
        run.heartbeat_at = terminal_at
        run.lease_expires_at = None
        run.state_version += 1
        run.updated_at = terminal_at
        session.add(
            RunSeal(
                run_id=run.id,
                terminal_status=terminal.status,
                context_sha256=context_sha256,
                result_sha256=result_sha256,
                sealed_at=terminal_at,
            )
        )
        session.execute(
            delete(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run.id)
        )
        session.flush()
        self._inject_fault("before_commit")

    def _terminal_snapshot(
        self,
        session: Session,
        run: Run,
        *,
        status: str,
        stage: str,
        summary: dict[str, Any],
        error_message: str | None,
    ) -> TerminalResultV1:
        issues = [
            ValidationIssueRecord.model_validate(
                {
                    field: getattr(issue, field)
                    for field in ValidationIssueRecord.model_fields
                }
            ).model_dump(mode="json")
            for issue in session.scalars(
                select(RunIssue)
                .where(RunIssue.run_id == run.id)
                .order_by(RunIssue.position, RunIssue.id)
            )
        ]
        devices = [
            self._device_payload(row)
            for row in session.scalars(
                select(DiscoveredDevice)
                .where(DiscoveredDevice.run_id == run.id)
                .order_by(DiscoveredDevice.position, DiscoveredDevice.id)
            )
        ]
        points = [
            self._point_payload(row)
            for row in session.scalars(
                select(DiscoveredPoint)
                .where(DiscoveredPoint.run_id == run.id)
                .order_by(DiscoveredPoint.position, DiscoveredPoint.id)
            )
        ]
        topics = [
            self._topic_payload(row)
            for row in session.scalars(
                select(DiscoveredTopic)
                .where(DiscoveredTopic.run_id == run.id)
                .order_by(DiscoveredTopic.position, DiscoveredTopic.id)
            )
        ]
        return TerminalResultV1.model_validate(
            {
                "status": status,
                "stage": stage,
                "summary": summary,
                "issues": issues,
                "devices": devices,
                "points": points,
                "topics": topics,
                "error_message": error_message,
            }
        )

    @staticmethod
    def _device_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredDevice:
        columns = ("project_id", "site_id", "address", "device_type", "name", "vendor", "model")
        return DiscoveredDevice(
            run_id=run_id,
            position=position,
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
            **{key: payload.get(key) for key in columns},
        )

    @staticmethod
    def _point_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredPoint:
        return DiscoveredPoint(
            run_id=run_id,
            position=position,
            device_ref=payload.get("device_ref"),
            point_id=payload.get("point_id"),
            point_name=payload.get("point_name"),
            observed_value=dict(payload.get("observed_value") or {}),
            units=payload.get("units"),
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
        )

    @staticmethod
    def _topic_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredTopic:
        return DiscoveredTopic(
            run_id=run_id,
            position=position,
            topic=str(payload.get("topic") or ""),
            last_payload=dict(payload.get("last_payload") or {}),
            message_count=int(payload.get("message_count") or 0),
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
        )

    @staticmethod
    def _device_payload(row: DiscoveredDevice) -> dict[str, Any]:
        return {
            "project_id": row.project_id,
            "site_id": row.site_id,
            "address": row.address,
            "device_type": row.device_type,
            "name": row.name,
            "vendor": row.vendor,
            "model": row.model,
            "attributes": dict(row.attributes or {}),
        }

    @staticmethod
    def _point_payload(row: DiscoveredPoint) -> dict[str, Any]:
        return {
            "device_ref": row.device_ref,
            "point_id": row.point_id,
            "point_name": row.point_name,
            "observed_value": dict(row.observed_value or {}),
            "units": row.units,
            "attributes": dict(row.attributes or {}),
        }

    @staticmethod
    def _topic_payload(row: DiscoveredTopic) -> dict[str, Any]:
        return {
            "topic": row.topic,
            "last_payload": dict(row.last_payload or {}),
            "message_count": row.message_count,
            "attributes": dict(row.attributes or {}),
        }

    def _add_conflict(
        self,
        session: Session,
        run_id: str,
        *,
        operation: str,
        reason: str,
        owner_token: str | None = None,
        attempted_status: str | None = None,
        attempted_sha256: str | None = None,
    ) -> None:
        if session.get(Run, run_id) is None:
            return
        fingerprint = (
            hashlib.sha256(owner_token.encode("utf-8")).hexdigest()[:16]
            if owner_token
            else None
        )
        session.add(
            RunLifecycleConflict(
                run_id=run_id,
                operation=operation,
                reason=reason,
                owner_token_fingerprint=fingerprint,
                attempted_status=attempted_status,
                attempted_sha256=attempted_sha256,
                observed_at=_utcnow(),
            )
        )

    def _load_run(
        self, session: Session, run_id: str, *, for_update: bool = False
    ) -> Run:
        statement = select(Run).where(Run.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        run = session.scalar(statement)
        if run is None:
            raise FileNotFoundError(run_id)
        return run

    def _inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    @staticmethod
    def _dispatch_view(row: RunDispatch) -> RunDispatchV1:
        return RunDispatchV1(
            dispatch_id=row.dispatch_id,
            run_id=row.run_id,
            state=row.state,
            publish_attempts=row.publish_attempts,
            created_at=row.created_at,
            published_at=row.published_at,
        )
