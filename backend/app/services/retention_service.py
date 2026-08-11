"""Data retention: purge old runs (cascading issues + discovery rows) safely.

Safety model:
  * DRY-RUN by default: :meth:`preview` reports what WOULD be deleted and
    deletes nothing.
  * :meth:`apply` requires an explicit ``confirm=True`` and only then deletes.
  * NEVER deletes an evidence-linked run. A run is evidence-linked when it is a
    report/evidence run (``job_type == "report_generation"``) OR when it is
    referenced by any report run's ``parameters["source_run_ids"]`` (the report
    is the persisted evidence the audit depends on). Deleting such a run would
    orphan its evidence pack.

Cascade: RunIssue, DiscoveredDevice/Point/Topic all declare
``ondelete="CASCADE"`` on their run FK, and the ORM Run.issues relationship is
``delete-orphan``; deleting the Run row removes its children. Every deletion is
logged via the module logger.

Pure DB work against the shared engine — fully unit-testable on tmp SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from smart_commissioning_core.db.engine import query_session_factory, session_factory
from smart_commissioning_core.db.models import (
    OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH,
    RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH,
    RUN_RETENTION_HOLD_EVIDENCE_SET_ID_MAX_LENGTH,
    RUN_RETENTION_HOLD_REASON_MAX_LENGTH,
    ActiveProtocolSlot,
    DiscoveredDevice,
    DiscoveredPoint,
    DiscoveredTopic,
    ObservationRetentionBatch,
    ObservationRetentionCandidate,
    ObservationRetentionJob,
    Run,
    RunDiscoveryObservation,
    RunExecutionContext,
    RunIssue,
    RunResult,
    RunRetentionHold,
    RunSeal,
    Site,
)
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationViewV1,
    ObservationEvidenceV1,
    fold_discovery_observations,
)
from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.sealed_run_integrity import (
    SealedRunIntegrityError,
    verify_sealed_run,
)
from sqlalchemy import and_, delete, exists, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

logger = logging.getLogger(__name__)

_REPORT_JOB_TYPE = "report_generation"
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
_OBSERVATION_ACKNOWLEDGEMENT = "DELETE PROVISIONAL OBSERVATIONS"
_MINIMUM_OBSERVATION_KEEP_DAYS = 30
_MAXIMUM_OBSERVATION_KEEP_DAYS = 3650
_MAXIMUM_OBSERVATION_BATCH_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class _CandidateBinding:
    """Immutable copy of one frozen candidate used across transaction boundaries."""

    job_id: str
    run_id: str
    project_id: str
    site_id: str
    attempt: int
    terminal_status: str
    context_sha256: str
    result_sha256: str
    seal_sha256: str
    sealed_at: datetime
    terminal_cursor: int
    observation_count: int
    observation_stream_sha256: str
    verified_at: datetime | None
    deleted_count: int


@dataclass(frozen=True, slots=True)
class _CandidateVerificationProof:
    """CPU verification result bound to the exact read-side snapshot."""

    job_id: str
    job_binding_sha256: str
    candidate_attestation_sha256: str
    run_id: str
    attempt: int
    run_binding_sha256: str
    context_binding_sha256: str
    result_binding_sha256: str
    seal_binding_sha256: str
    observation_count: int
    prior_attested_deleted_count: int
    terminal_cursor: int
    observation_stream_sha256: str


@dataclass(frozen=True, slots=True)
class _RetentionVerificationPlan:
    """Immutable proof set handed from read-side verification to mutation."""

    job_id: str
    job_binding_sha256: str
    window_rows: tuple[tuple[int, str], ...]
    candidate_bindings: tuple[_CandidateBinding, ...]
    candidate_proofs: tuple[_CandidateVerificationProof, ...]


class ObservationRetentionValidationError(ValueError):
    """The requested retention or hold operation is malformed."""


class ObservationRetentionNotFoundError(LookupError):
    """The requested scope, job, run, or hold does not exist."""


class ObservationRetentionConflictError(ValueError):
    """The requested operation conflicts with durable retention state."""


@dataclass
class RetentionCandidate:
    """One run that is eligible (or not) for deletion under the policy."""

    run_id: str
    job_type: str
    created_at: str
    evidence_linked: bool
    reason: str
    held: bool = False
    active_observation_retention: bool = False


@dataclass
class RetentionResult:
    """Outcome of a preview or apply pass."""

    cutoff: str
    dry_run: bool
    candidates: list[RetentionCandidate] = field(default_factory=list)
    deleted_run_ids: list[str] = field(default_factory=list)
    skipped_evidence_run_ids: list[str] = field(default_factory=list)
    skipped_held_run_ids: list[str] = field(default_factory=list)
    skipped_active_observation_retention_run_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "candidate_count": len(self.candidates),
            "deleted_count": len(self.deleted_run_ids),
            "skipped_evidence_count": len(self.skipped_evidence_run_ids),
            "skipped_held_count": len(self.skipped_held_run_ids),
            "skipped_active_observation_retention_count": len(self.skipped_active_observation_retention_run_ids),
        }


def cutoff_from_keep_days(keep_days: int, *, now: datetime | None = None) -> datetime:
    """Return the cutoff instant: runs created strictly before it are eligible."""
    if keep_days < 0:
        raise ValueError("keep_days must be >= 0.")
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    return reference - timedelta(days=keep_days)


class RetentionService:
    """Run retention with a mandatory dry-run / confirm gate and evidence guard."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)
        self._query_session_factory = query_session_factory(engine)

    def preview(self, *, before: datetime) -> RetentionResult:
        """Report what WOULD be deleted for runs created before ``before``.

        Deletes NOTHING. Evidence-linked runs are listed but flagged so the
        operator can see why they are retained.
        """
        return self._evaluate(before=before, dry_run=True)

    def apply(self, *, before: datetime, confirm: bool) -> RetentionResult:
        """Delete eligible (non-evidence) runs created before ``before``.

        Requires ``confirm=True`` — without it this is a dry-run, never a
        deletion (defense in depth alongside the API's explicit confirmation).
        """
        if not confirm:
            logger.info("Retention apply called without confirm=True; running as dry-run.")
            return self._evaluate(before=before, dry_run=True)
        return self._evaluate(before=before, dry_run=False)

    def _evaluate(self, *, before: datetime, dry_run: bool) -> RetentionResult:
        before = before.astimezone(UTC)
        result = RetentionResult(cutoff=before.isoformat(), dry_run=dry_run)

        factory = self._query_session_factory if dry_run else self._session_factory
        with factory.begin() as session:
            evidence_ids = self._evidence_linked_run_ids(session)
            old_runs_statement = select(Run).where(Run.created_at < before)
            if dry_run:
                old_runs_statement = old_runs_statement.order_by(Run.created_at, Run.id)
            else:
                # Hold placement takes the same row lock.  Whichever transaction
                # wins the lock is authoritative, and the loser rechecks state.
                old_runs_statement = old_runs_statement.order_by(Run.id).with_for_update()
            old_runs = session.scalars(old_runs_statement).all()
            held_ids = set(
                session.scalars(select(RunRetentionHold.run_id).where(RunRetentionHold.active_marker.is_(True))).all()
            )
            active_observation_retention_ids = set(
                session.scalars(
                    select(ObservationRetentionCandidate.run_id)
                    .join(
                        ObservationRetentionJob,
                        ObservationRetentionJob.job_id == ObservationRetentionCandidate.job_id,
                    )
                    .where(ObservationRetentionJob.active_marker.is_(True))
                ).all()
            )

            for run in old_runs:
                evidence_linked = run.id in evidence_ids or run.job_type == _REPORT_JOB_TYPE
                held = run.id in held_ids
                active_observation_retention = run.id in active_observation_retention_ids
                reason = (
                    "retained: evidence pack / report or referenced by one"
                    if evidence_linked
                    else (
                        "retained: active legal or evidence hold"
                        if held
                        else (
                            "retained: active provisional-observation retention job"
                            if active_observation_retention
                            else "eligible: older than cutoff and not evidence-linked or held"
                        )
                    )
                )
                result.candidates.append(
                    RetentionCandidate(
                        run_id=run.id,
                        job_type=run.job_type,
                        created_at=run.created_at.astimezone(UTC).isoformat(),
                        evidence_linked=evidence_linked,
                        reason=reason,
                        held=held,
                        active_observation_retention=active_observation_retention,
                    )
                )
                if evidence_linked:
                    result.skipped_evidence_run_ids.append(run.id)
                if held:
                    result.skipped_held_run_ids.append(run.id)
                if active_observation_retention:
                    result.skipped_active_observation_retention_run_ids.append(run.id)
                if evidence_linked or held or active_observation_retention:
                    continue
                if dry_run:
                    continue
                logger.info(
                    "Retention deleting run %s (job_type=%s, created_at=%s, cutoff=%s)",
                    run.id,
                    run.job_type,
                    run.created_at.isoformat(),
                    before.isoformat(),
                )
                # ORM delete triggers the Run.issues delete-orphan cascade and
                # the DB-level ON DELETE CASCADE for discovery rows.
                session.delete(run)
                result.deleted_run_ids.append(run.id)

        return result

    def _evidence_linked_run_ids(self, session) -> set[str]:  # noqa: ANN001
        """Run ids referenced by any report run's source_run_ids."""
        linked: set[str] = set()
        report_runs = session.scalars(select(Run).where(Run.job_type == _REPORT_JOB_TYPE)).all()
        for report in report_runs:
            parameters = report.parameters if isinstance(report.parameters, dict) else {}
            source_ids = parameters.get("source_run_ids")
            if isinstance(source_ids, (list, tuple)):
                linked.update(str(item) for item in source_ids)
        return linked


def _utc(value: datetime) -> datetime:
    """Return one timezone-aware instant normalized to UTC."""
    if value.tzinfo is None:
        raise ObservationRetentionValidationError("Retention timestamps must include a timezone.")
    return value.astimezone(UTC)


def _required_text(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ObservationRetentionValidationError(f"{field_name} must contain between 1 and {maximum} characters.")
    return normalized


def _job_to_dict(job: ObservationRetentionJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "site_id": job.site_id,
        "keep_days": job.keep_days,
        "cutoff_sealed_at": job.cutoff_sealed_at.astimezone(UTC).isoformat(),
        "high_water_observation_id": int(job.high_water_observation_id),
        "candidate_run_count": int(job.candidate_run_count),
        "candidate_count": int(job.candidate_observation_count),
        "candidate_manifest_sha256": job.candidate_manifest_sha256,
        "next_cursor": int(job.next_cursor),
        "batch_limit": job.batch_limit,
        "status": job.status,
        "requested_by": job.requested_by,
        "requested_at": job.requested_at.astimezone(UTC).isoformat(),
        "confirmed_by": job.confirmed_by,
        "confirmed_at": (job.confirmed_at.astimezone(UTC).isoformat() if job.confirmed_at is not None else None),
        "deleted_count": int(job.deleted_count),
        "batch_count": job.batch_count,
        "completed_at": (job.completed_at.astimezone(UTC).isoformat() if job.completed_at is not None else None),
        "error_code": job.error_code,
        "active": job.active_marker is True,
    }


def _seal_sha256(
    *,
    run_id: str,
    terminal_status: str,
    context_sha256: str,
    result_sha256: str,
    sealed_at: datetime,
) -> str:
    """Hash the complete persisted seal row for a stable preview binding."""
    return canonical_sha256(
        {
            "run_id": run_id,
            "terminal_status": terminal_status,
            "context_sha256": context_sha256,
            "result_sha256": result_sha256,
            "sealed_at": _utc(sealed_at).isoformat(),
        }
    )


def _candidate_binding(candidate: ObservationRetentionCandidate) -> _CandidateBinding:
    return _CandidateBinding(
        job_id=candidate.job_id,
        run_id=candidate.run_id,
        project_id=candidate.project_id,
        site_id=candidate.site_id,
        attempt=int(candidate.attempt),
        terminal_status=candidate.terminal_status,
        context_sha256=candidate.context_sha256,
        result_sha256=candidate.result_sha256,
        seal_sha256=candidate.seal_sha256,
        sealed_at=_utc(candidate.sealed_at),
        terminal_cursor=int(candidate.terminal_cursor),
        observation_count=int(candidate.observation_count),
        observation_stream_sha256=candidate.observation_stream_sha256,
        verified_at=(_utc(candidate.verified_at) if candidate.verified_at is not None else None),
        deleted_count=int(candidate.deleted_count),
    )


def _candidate_attestation(
    candidate: ObservationRetentionCandidate | _CandidateBinding,
) -> dict[str, object]:
    """Return only immutable preview fields used by the job manifest."""
    return {
        "run_id": candidate.run_id,
        "project_id": candidate.project_id,
        "site_id": candidate.site_id,
        "attempt": int(candidate.attempt),
        "terminal_status": candidate.terminal_status,
        "context_sha256": candidate.context_sha256,
        "result_sha256": candidate.result_sha256,
        "seal_sha256": candidate.seal_sha256,
        "sealed_at": _utc(candidate.sealed_at).isoformat(),
        "terminal_cursor": int(candidate.terminal_cursor),
        "observation_count": int(candidate.observation_count),
        "observation_stream_sha256": candidate.observation_stream_sha256,
    }


def _candidate_manifest_sha256(
    candidates: Sequence[ObservationRetentionCandidate | _CandidateBinding],
) -> str:
    return canonical_sha256(
        [_candidate_attestation(candidate) for candidate in sorted(candidates, key=lambda item: item.run_id)]
    )


def _json_binding_value(value: Any) -> Any:
    """Normalize datetimes before hashing an in-memory retention binding."""
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_binding_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_binding_value(child) for child in value]
    return value


def _binding_sha256(value: Mapping[str, object]) -> str:
    return canonical_sha256(_json_binding_value(value))


def _job_apply_binding(job: ObservationRetentionJob) -> dict[str, object]:
    """Fields that must remain stable between verification and mutation."""
    return {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "site_id": job.site_id,
        "keep_days": int(job.keep_days),
        "cutoff_sealed_at": _utc(job.cutoff_sealed_at),
        "high_water_observation_id": int(job.high_water_observation_id),
        "candidate_run_count": int(job.candidate_run_count),
        "candidate_observation_count": int(job.candidate_observation_count),
        "candidate_manifest_sha256": job.candidate_manifest_sha256,
        "next_cursor": int(job.next_cursor),
        "batch_limit": int(job.batch_limit),
        "status": job.status,
        "confirmed_by": job.confirmed_by,
        "confirmed_at": job.confirmed_at,
        "deleted_count": int(job.deleted_count),
        "batch_count": int(job.batch_count),
        "active_marker": job.active_marker,
    }


def _run_verification_payload(run: Run) -> dict[str, object]:
    return {
        "job_type": run.job_type,
        "project_id": run.project_id,
        "site_id": run.site_id,
        "parameters": run.parameters,
        "status": run.status,
        "stage": run.stage,
        "result_summary": run.result_summary,
        "error_message": run.error_message,
        "result_sha256": run.result_sha256,
        "terminal_at": run.terminal_at,
        "owner_token": run.owner_token,
        "attempt": run.attempt,
        "claimed_at": run.claimed_at,
        "heartbeat_at": run.heartbeat_at,
        "lease_expires_at": run.lease_expires_at,
        "state_version": run.state_version,
        "execution_mode": run.execution_mode,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _context_verification_payload(context: RunExecutionContext) -> dict[str, object]:
    return {
        "context_json": context.context_json,
        "context_sha256": context.context_sha256,
    }


def _result_verification_payload(result: RunResult) -> dict[str, object]:
    return {
        "schema_version": result.schema_version,
        "terminal_status": result.terminal_status,
        "terminal_stage": result.terminal_stage,
        "summary": result.summary,
        "result_payload": result.result_payload,
        "result_sha256": result.result_sha256,
    }


def _seal_verification_payload(seal: RunSeal) -> dict[str, object]:
    return {
        "terminal_status": seal.terminal_status,
        "context_sha256": seal.context_sha256,
        "result_sha256": seal.result_sha256,
        "sealed_at": seal.sealed_at,
    }


def _observation_evidence_payload(
    payload: Mapping[str, object] | object,
) -> ObservationEvidenceV1 | None:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    raw_evidence = summary.get("observation_evidence_v1") if isinstance(summary, dict) else None
    if raw_evidence is None:
        return None
    try:
        return ObservationEvidenceV1.model_validate(raw_evidence)
    except ValidationError as error:
        raise ObservationRetentionConflictError("Sealed discovery observation evidence is malformed.") from error


def _observation_evidence(result: RunResult) -> ObservationEvidenceV1 | None:
    return _observation_evidence_payload(result.result_payload)


def _hold_to_dict(hold: RunRetentionHold) -> dict[str, object]:
    return {
        "hold_id": hold.hold_id,
        "run_id": hold.run_id,
        "project_id": hold.project_id,
        "site_id": hold.site_id,
        "hold_type": hold.hold_type,
        "evidence_set_id": hold.evidence_set_id,
        "active": hold.active_marker is True,
        "placed_by": hold.placed_by,
        "reason": hold.reason,
        "placed_at": hold.placed_at.astimezone(UTC).isoformat(),
        "released_by": hold.released_by,
        "release_reason": hold.release_reason,
        "released_at": (hold.released_at.astimezone(UTC).isoformat() if hold.released_at is not None else None),
    }


class ObservationRetentionService:
    """Restart-safe cleanup of provisional rows after immutable sealing."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)
        self._query_session_factory = query_session_factory(engine)

    @staticmethod
    def _validate_policy(*, keep_days: int, batch_limit: int) -> None:
        if not _MINIMUM_OBSERVATION_KEEP_DAYS <= keep_days <= _MAXIMUM_OBSERVATION_KEEP_DAYS:
            raise ObservationRetentionValidationError("keep_days must be between 30 and 3650.")
        if not 1 <= batch_limit <= _MAXIMUM_OBSERVATION_BATCH_LIMIT:
            raise ObservationRetentionValidationError("batch_limit must be between 1 and 1000.")

    @staticmethod
    def _preview_candidate_statement(*, project_id: str, site_id: str, cutoff: datetime):
        active_slot = exists(select(ActiveProtocolSlot.protocol_key).where(ActiveProtocolSlot.run_id == Run.id))
        active_hold = exists(
            select(RunRetentionHold.hold_id).where(
                RunRetentionHold.run_id == Run.id,
                RunRetentionHold.active_marker.is_(True),
            )
        )
        return (
            select(Run, RunExecutionContext, RunResult, RunSeal)
            .join(RunExecutionContext, RunExecutionContext.run_id == Run.id)
            .join(RunResult, RunResult.run_id == Run.id)
            .join(RunSeal, RunSeal.run_id == Run.id)
            .where(
                Run.project_id == project_id,
                Run.site_id == site_id,
                Run.job_type.in_(("ip_discovery", "bacnet_discovery")),
                Run.status.in_(_TERMINAL_STATUSES),
                Run.terminal_at.is_not(None),
                RunSeal.sealed_at < cutoff,
                ~active_slot,
                ~active_hold,
            )
            .order_by(Run.id)
            .with_for_update()
        )

    def _scope_exists(self, *, project_id: str, site_id: str) -> bool:
        with self._query_session_factory() as session:
            return (
                session.scalar(select(Site.id).where(Site.id == site_id, Site.project_id == project_id).limit(1))
                is not None
            )

    def preview(
        self,
        *,
        project_id: str,
        site_id: str,
        keep_days: int,
        batch_limit: int,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Atomically persist a frozen candidate set and its attestations."""
        self._validate_policy(keep_days=keep_days, batch_limit=batch_limit)
        actor = _required_text(
            actor,
            field_name="actor",
            maximum=OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH,
        )
        requested_at = _utc(now or datetime.now(UTC))
        cutoff = requested_at - timedelta(days=keep_days)
        try:
            with self._session_factory.begin() as session:
                if (
                    session.scalar(
                        select(Site.id).where(
                            Site.id == site_id,
                            Site.project_id == project_id,
                        )
                    )
                    is None
                ):
                    raise ObservationRetentionNotFoundError("Project/site not found.")
                active_job = session.scalar(
                    select(ObservationRetentionJob)
                    .where(
                        ObservationRetentionJob.project_id == project_id,
                        ObservationRetentionJob.site_id == site_id,
                        ObservationRetentionJob.active_marker.is_(True),
                    )
                    .with_for_update()
                )
                if active_job is not None:
                    if (
                        active_job.keep_days == keep_days
                        and active_job.batch_limit == batch_limit
                        and active_job.requested_by == actor
                    ):
                        return {**_job_to_dict(active_job), "idempotent": True}
                    raise ObservationRetentionConflictError(
                        "That project/site already has an active observation retention job."
                    )

                job_id = f"observation-retention-{uuid4()}"
                candidates: list[ObservationRetentionCandidate] = []
                rows = session.execute(
                    self._preview_candidate_statement(
                        project_id=project_id,
                        site_id=site_id,
                        cutoff=cutoff,
                    )
                ).all()
                for run, context, result, seal in rows:
                    evidence = _observation_evidence(result)
                    if evidence is None or evidence.observation_count == 0:
                        continue
                    if evidence.attempt != run.attempt or evidence.terminal_cursor <= 0:
                        raise ObservationRetentionConflictError(
                            "Sealed discovery observation evidence does not match its run."
                        )
                    candidate = ObservationRetentionCandidate(
                        job_id=job_id,
                        run_id=run.id,
                        project_id=run.project_id,
                        site_id=run.site_id,
                        attempt=run.attempt,
                        terminal_status=run.status,
                        context_sha256=context.context_sha256,
                        result_sha256=result.result_sha256,
                        seal_sha256=_seal_sha256(
                            run_id=run.id,
                            terminal_status=seal.terminal_status,
                            context_sha256=seal.context_sha256,
                            result_sha256=seal.result_sha256,
                            sealed_at=seal.sealed_at,
                        ),
                        sealed_at=seal.sealed_at,
                        terminal_cursor=evidence.terminal_cursor,
                        observation_count=evidence.observation_count,
                        observation_stream_sha256=evidence.observation_stream_sha256,
                        deleted_count=0,
                    )
                    prior_attestations = self._attested_candidates_for_binding(
                        session,
                        _candidate_binding(candidate),
                    )
                    prior_deleted_count = sum(
                        int(attestation.deleted_count)
                        for attestation in prior_attestations
                    )
                    if prior_deleted_count > int(evidence.observation_count):
                        raise ObservationRetentionConflictError(
                            "Observation retention deletion audit failed integrity verification."
                        )
                    if prior_deleted_count == int(evidence.observation_count):
                        continue
                    candidates.append(candidate)

                job = ObservationRetentionJob(
                    job_id=job_id,
                    project_id=project_id,
                    site_id=site_id,
                    keep_days=keep_days,
                    cutoff_sealed_at=cutoff,
                    high_water_observation_id=max(
                        (candidate.terminal_cursor for candidate in candidates),
                        default=0,
                    ),
                    candidate_run_count=len(candidates),
                    candidate_observation_count=sum(candidate.observation_count for candidate in candidates),
                    candidate_manifest_sha256=_candidate_manifest_sha256(candidates),
                    next_cursor=0,
                    batch_limit=batch_limit,
                    status="preview",
                    requested_by=actor,
                    requested_at=requested_at,
                    deleted_count=0,
                    batch_count=0,
                    active_marker=True,
                )
                session.add(job)
                session.flush()
                session.add_all(candidates)
                session.flush()
                response = {**_job_to_dict(job), "idempotent": False}
        except IntegrityError as error:
            # A concurrent identical preview may have committed after our
            # preflight.  Recover that durable response instead of stranding
            # the caller that lost the original HTTP response.
            with self._query_session_factory() as session:
                active_job = session.scalar(
                    select(ObservationRetentionJob).where(
                        ObservationRetentionJob.project_id == project_id,
                        ObservationRetentionJob.site_id == site_id,
                        ObservationRetentionJob.active_marker.is_(True),
                    )
                )
            if active_job is not None and (
                active_job.keep_days == keep_days
                and active_job.batch_limit == batch_limit
                and active_job.requested_by == actor
            ):
                return {**_job_to_dict(active_job), "idempotent": True}
            if active_job is not None:
                raise ObservationRetentionConflictError(
                    "That project/site already has an active observation retention job."
                ) from error
            raise ObservationRetentionConflictError(
                "Retention preview could not persist a canonical frozen candidate set."
            ) from error
        return response

    @staticmethod
    def _verify_candidate_manifest(
        job: ObservationRetentionJob,
        candidates: Sequence[ObservationRetentionCandidate | _CandidateBinding],
    ) -> None:
        high_water = max(
            (candidate.terminal_cursor for candidate in candidates),
            default=0,
        )
        coherent = (
            len(candidates) == job.candidate_run_count
            and sum(candidate.observation_count for candidate in candidates) == job.candidate_observation_count
            and high_water == job.high_water_observation_id
            and all(
                candidate.project_id == job.project_id
                and candidate.site_id == job.site_id
                and _utc(candidate.sealed_at) < _utc(job.cutoff_sealed_at)
                for candidate in candidates
            )
            and _candidate_manifest_sha256(candidates) == job.candidate_manifest_sha256
        )
        if not coherent:
            raise ObservationRetentionConflictError(
                "Frozen retention candidate manifest failed integrity verification."
            )

    @staticmethod
    def _verify_candidate_summary(session, job: ObservationRetentionJob) -> None:  # noqa: ANN001
        """Check frozen candidate metadata without materializing every candidate."""

        candidate_count, observation_count, high_water = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(ObservationRetentionCandidate.observation_count), 0),
                func.coalesce(func.max(ObservationRetentionCandidate.terminal_cursor), 0),
            ).where(ObservationRetentionCandidate.job_id == job.job_id)
        ).one()
        if (
            int(candidate_count) != int(job.candidate_run_count)
            or int(observation_count) != int(job.candidate_observation_count)
            or int(high_water) != int(job.high_water_observation_id)
        ):
            raise ObservationRetentionConflictError(
                "Frozen retention candidate manifest failed integrity verification."
            )

    def _verify_attested_candidate_jobs(
        self,
        session,  # noqa: ANN001
        candidates: Sequence[ObservationRetentionCandidate],
    ) -> None:
        """Verify every retention job supplying the candidate attestations."""

        job_ids = sorted({candidate.job_id for candidate in candidates})
        if not job_ids:
            return
        jobs = list(
            session.scalars(
                select(ObservationRetentionJob)
                .where(ObservationRetentionJob.job_id.in_(job_ids))
                .order_by(ObservationRetentionJob.job_id)
            ).all()
        )
        if len(jobs) != len(job_ids):
            raise ObservationRetentionConflictError(
                "Observation retention deletion audit failed integrity verification."
            )
        candidates_by_job_id: dict[str, list[ObservationRetentionCandidate]] = {
            job_id: [] for job_id in job_ids
        }
        for candidate in session.scalars(
            select(ObservationRetentionCandidate)
            .where(ObservationRetentionCandidate.job_id.in_(job_ids))
            .order_by(
                ObservationRetentionCandidate.job_id,
                ObservationRetentionCandidate.run_id,
            )
        ):
            candidates_by_job_id[candidate.job_id].append(candidate)
        for attested_job in jobs:
            self._verify_candidate_manifest(
                attested_job,
                candidates_by_job_id[attested_job.job_id],
            )
            self._verify_deletion_audit_conservation(session, attested_job)

    def _attested_candidates_for_binding(
        self,
        session,  # noqa: ANN001
        candidate: _CandidateBinding,
    ) -> list[ObservationRetentionCandidate]:
        matching_candidates = list(
            session.scalars(
                select(ObservationRetentionCandidate)
                .where(
                    ObservationRetentionCandidate.run_id == candidate.run_id,
                    ObservationRetentionCandidate.project_id == candidate.project_id,
                    ObservationRetentionCandidate.site_id == candidate.site_id,
                    ObservationRetentionCandidate.attempt == candidate.attempt,
                    ObservationRetentionCandidate.terminal_status
                    == candidate.terminal_status,
                    ObservationRetentionCandidate.context_sha256
                    == candidate.context_sha256,
                    ObservationRetentionCandidate.result_sha256
                    == candidate.result_sha256,
                    ObservationRetentionCandidate.seal_sha256
                    == candidate.seal_sha256,
                    ObservationRetentionCandidate.sealed_at == candidate.sealed_at,
                    ObservationRetentionCandidate.terminal_cursor
                    == candidate.terminal_cursor,
                    ObservationRetentionCandidate.observation_count
                    == candidate.observation_count,
                    ObservationRetentionCandidate.observation_stream_sha256
                    == candidate.observation_stream_sha256,
                    ObservationRetentionCandidate.verified_at.is_not(None),
                )
                .order_by(ObservationRetentionCandidate.job_id)
            ).all()
        )
        self._verify_attested_candidate_jobs(session, matching_candidates)
        return matching_candidates

    def _verify_physical_observation_count(
        self,
        session,  # noqa: ANN001
        job: ObservationRetentionJob,
    ) -> None:
        """Check the whole frozen row count during query-only preflight."""

        current_candidate = aliased(ObservationRetentionCandidate)
        attested_candidate = aliased(ObservationRetentionCandidate)
        candidate_pairs = session.execute(
            select(current_candidate, attested_candidate)
            .select_from(current_candidate)
            .outerjoin(
                attested_candidate,
                and_(
                    attested_candidate.run_id == current_candidate.run_id,
                    attested_candidate.project_id == current_candidate.project_id,
                    attested_candidate.site_id == current_candidate.site_id,
                    attested_candidate.attempt == current_candidate.attempt,
                    attested_candidate.terminal_status
                    == current_candidate.terminal_status,
                    attested_candidate.context_sha256
                    == current_candidate.context_sha256,
                    attested_candidate.result_sha256
                    == current_candidate.result_sha256,
                    attested_candidate.seal_sha256 == current_candidate.seal_sha256,
                    attested_candidate.sealed_at == current_candidate.sealed_at,
                    attested_candidate.terminal_cursor
                    == current_candidate.terminal_cursor,
                    attested_candidate.observation_count
                    == current_candidate.observation_count,
                    attested_candidate.observation_stream_sha256
                    == current_candidate.observation_stream_sha256,
                    attested_candidate.verified_at.is_not(None),
                ),
            )
            .where(current_candidate.job_id == job.job_id)
        ).all()
        expected_by_run_id: dict[str, int] = {}
        attested_candidates: list[ObservationRetentionCandidate] = []
        for candidate, attestation in candidate_pairs:
            expected_by_run_id.setdefault(
                candidate.run_id,
                int(candidate.observation_count),
            )
            if attestation is not None:
                expected_by_run_id[candidate.run_id] -= int(attestation.deleted_count)
                attested_candidates.append(attestation)
        self._verify_attested_candidate_jobs(session, attested_candidates)
        if any(count < 0 for count in expected_by_run_id.values()):
            raise ObservationRetentionConflictError(
                "Observation retention deletion audit failed integrity verification."
            )

        physical_observation_count = session.scalar(
            select(func.count())
            .select_from(RunDiscoveryObservation)
            .join(
                ObservationRetentionCandidate,
                and_(
                    ObservationRetentionCandidate.job_id == job.job_id,
                    ObservationRetentionCandidate.run_id == RunDiscoveryObservation.run_id,
                    ObservationRetentionCandidate.attempt == RunDiscoveryObservation.attempt,
                ),
            )
        )
        expected_observation_count = sum(expected_by_run_id.values())
        if int(physical_observation_count or 0) != expected_observation_count:
            raise ObservationRetentionConflictError(
                "Frozen retention observations changed outside an audited batch."
            )

    @staticmethod
    def _verify_deletion_audit_conservation(session, job: ObservationRetentionJob) -> None:  # noqa: ANN001
        """Require candidate, batch, and job deletion totals to agree exactly."""

        invalid_candidate_count = int(
            session.scalar(
                select(func.count()).where(
                    ObservationRetentionCandidate.job_id == job.job_id,
                    or_(
                        ObservationRetentionCandidate.deleted_count < 0,
                        ObservationRetentionCandidate.deleted_count
                        > ObservationRetentionCandidate.observation_count,
                    ),
                )
            )
            or 0
        )
        candidate_deleted_count = int(
            session.scalar(
                select(func.coalesce(func.sum(ObservationRetentionCandidate.deleted_count), 0)).where(
                    ObservationRetentionCandidate.job_id == job.job_id
                )
            )
            or 0
        )
        batch_count, batch_deleted_count = session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(ObservationRetentionBatch.deleted_count), 0),
            ).where(ObservationRetentionBatch.job_id == job.job_id)
        ).one()
        if (
            invalid_candidate_count != 0
            or candidate_deleted_count != int(job.deleted_count)
            or int(batch_deleted_count) != int(job.deleted_count)
            or int(batch_count) != int(job.batch_count)
        ):
            raise ObservationRetentionConflictError(
                "Observation retention deletion audit failed integrity verification."
            )

    @staticmethod
    def _next_window_statement(
        *,
        job_id: str,
        next_cursor: int,
        batch_limit: int,
    ):
        """Select only the durable observation rows one apply may process."""

        return (
            select(
                RunDiscoveryObservation.id,
                RunDiscoveryObservation.run_id,
            )
            .join(
                ObservationRetentionCandidate,
                and_(
                    ObservationRetentionCandidate.job_id == job_id,
                    ObservationRetentionCandidate.run_id == RunDiscoveryObservation.run_id,
                    ObservationRetentionCandidate.attempt == RunDiscoveryObservation.attempt,
                ),
            )
            .where(
                RunDiscoveryObservation.id > next_cursor,
                RunDiscoveryObservation.id <= ObservationRetentionCandidate.terminal_cursor,
            )
            .order_by(RunDiscoveryObservation.id)
            .limit(batch_limit)
        )

    @staticmethod
    def _require_active_job(job: ObservationRetentionJob) -> None:
        if job.active_marker is not True or job.status in {"complete", "failed"}:
            raise ObservationRetentionConflictError("Observation retention job is no longer active.")
        if job.status not in {"preview", "ready", "running"}:
            raise ObservationRetentionConflictError(
                "Observation retention job cannot be applied from its current state."
            )

    @staticmethod
    def _row_payload(row: object) -> dict[str, object]:
        return {
            column.key: getattr(row, column.key)
            for column in row.__table__.columns  # type: ignore[attr-defined]
        }

    @classmethod
    def _projection_rows(
        cls,
        session,  # noqa: ANN001
        *,
        run_id: str,
    ) -> dict[str, list[dict[str, object]]]:
        projections: dict[str, list[dict[str, object]]] = {}
        for label, model in (
            ("issues", RunIssue),
            ("devices", DiscoveredDevice),
            ("points", DiscoveredPoint),
            ("topics", DiscoveredTopic),
        ):
            rows = session.scalars(select(model).where(model.run_id == run_id).order_by(model.position, model.id)).all()
            projections[label] = [cls._row_payload(row) for row in rows]
        return projections

    @staticmethod
    def _verify_frozen_bindings(
        *,
        candidate: _CandidateBinding,
        run_payload: Mapping[str, object],
        context_payload: Mapping[str, object],
        result_payload: Mapping[str, object],
        seal_payload: Mapping[str, object],
    ) -> None:
        raw_sealed_at = seal_payload.get("sealed_at")
        if not isinstance(raw_sealed_at, datetime):
            raise ObservationRetentionConflictError("Frozen retention candidate no longer has a valid seal timestamp.")
        evidence = _observation_evidence_payload(result_payload.get("result_payload"))
        seal_digest = _seal_sha256(
            run_id=candidate.run_id,
            terminal_status=str(seal_payload.get("terminal_status") or ""),
            context_sha256=str(seal_payload.get("context_sha256") or ""),
            result_sha256=str(seal_payload.get("result_sha256") or ""),
            sealed_at=raw_sealed_at,
        )
        if evidence is None or (
            run_payload.get("job_type") not in {"ip_discovery", "bacnet_discovery"}
            or run_payload.get("project_id") != candidate.project_id
            or run_payload.get("site_id") != candidate.site_id
            or run_payload.get("attempt") != candidate.attempt
            or run_payload.get("status") != candidate.terminal_status
            or run_payload.get("result_sha256") != candidate.result_sha256
            or context_payload.get("context_sha256") != candidate.context_sha256
            or result_payload.get("terminal_status") != candidate.terminal_status
            or result_payload.get("result_sha256") != candidate.result_sha256
            or seal_digest != candidate.seal_sha256
            or _utc(raw_sealed_at) != _utc(candidate.sealed_at)
            or evidence.attempt != candidate.attempt
            or evidence.terminal_cursor != candidate.terminal_cursor
            or evidence.observation_count != candidate.observation_count
            or evidence.observation_stream_sha256 != candidate.observation_stream_sha256
        ):
            raise ObservationRetentionConflictError(
                "Frozen retention candidate attestation no longer matches sealed evidence."
            )

    def _verify_candidate_read_only(
        self,
        *,
        job_binding_sha256: str,
        candidate: _CandidateBinding,
    ) -> _CandidateVerificationProof:
        # Copy every row into ordinary Python values, then release the database
        # read transaction before canonical projection verification and folding.
        with self._query_session_factory() as session:
            run = session.get(Run, candidate.run_id)
            context = session.get(RunExecutionContext, candidate.run_id)
            result = session.get(RunResult, candidate.run_id)
            seal = session.get(RunSeal, candidate.run_id)
            if run is None or context is None or result is None or seal is None:
                raise ObservationRetentionConflictError(
                    "Frozen retention candidate no longer has complete sealed evidence."
                )
            run_payload = _run_verification_payload(run)
            context_payload = _context_verification_payload(context)
            result_payload = _result_verification_payload(result)
            seal_payload = _seal_verification_payload(seal)
            projections = self._projection_rows(session, run_id=candidate.run_id)
            observation_rows = [
                self._row_payload(row)
                for row in session.scalars(
                    select(RunDiscoveryObservation)
                    .where(
                        RunDiscoveryObservation.run_id == candidate.run_id,
                        RunDiscoveryObservation.attempt == candidate.attempt,
                    )
                    .order_by(RunDiscoveryObservation.id)
                ).all()
            ]
            prior_attestations = self._attested_candidates_for_binding(
                session,
                candidate,
            )
            prior_attested_deleted_count = sum(
                int(attestation.deleted_count) for attestation in prior_attestations
            )

        self._verify_frozen_bindings(
            candidate=candidate,
            run_payload=run_payload,
            context_payload=context_payload,
            result_payload=result_payload,
            seal_payload=seal_payload,
        )
        try:
            verified = verify_sealed_run(
                run_id=candidate.run_id,
                run=run_payload,
                context=context_payload,
                result=result_payload,
                seal=seal_payload,
                **projections,
            )
        except (SealedRunIntegrityError, TypeError, ValueError) as error:
            raise ObservationRetentionConflictError(
                "Frozen retention candidate failed sealed-run integrity verification."
            ) from error
        if verified is None:
            raise ObservationRetentionConflictError("Frozen retention candidate is not a canonical sealed run.")
        observations = [
            DiscoveryObservationViewV1(
                cursor=int(row["id"]),
                run_id=str(row["run_id"]),
                attempt=int(row["attempt"]),
                protocol=str(row["protocol"]),
                entity_kind=str(row["entity_kind"]),
                entity_key=str(row["entity_key"]),
                entity_version=int(row["entity_version"]),
                event_key=str(row["event_key"]),
                phase=str(row["phase"]),
                outcome=str(row["outcome"]),
                payload_schema_version=str(row["payload_schema_version"]),
                payload=row["payload"],
                payload_sha256=str(row["payload_sha256"]),
                observed_at=row["observed_at"],
                created_at=row["created_at"],
            )
            for row in observation_rows
        ]
        expected_remaining_count = (
            candidate.observation_count - prior_attested_deleted_count
        )
        if expected_remaining_count < 0:
            raise ObservationRetentionConflictError(
                "Observation retention deletion audit failed integrity verification."
            )
        try:
            fold = fold_discovery_observations(
                observations,
                terminal_cursor=candidate.terminal_cursor,
                expected_count=expected_remaining_count,
                run_id=candidate.run_id,
                attempt=candidate.attempt,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise ObservationRetentionConflictError(
                "Frozen retention candidate observation prefix failed integrity verification."
            ) from error
        if (
            prior_attested_deleted_count == 0
            and fold.observation_stream_sha256 != candidate.observation_stream_sha256
        ):
            raise ObservationRetentionConflictError("Frozen retention candidate observation stream digest changed.")
        return _CandidateVerificationProof(
            job_id=candidate.job_id,
            job_binding_sha256=job_binding_sha256,
            candidate_attestation_sha256=canonical_sha256(_candidate_attestation(candidate)),
            run_id=candidate.run_id,
            attempt=candidate.attempt,
            run_binding_sha256=_binding_sha256(run_payload),
            context_binding_sha256=_binding_sha256(context_payload),
            result_binding_sha256=_binding_sha256(result_payload),
            seal_binding_sha256=_binding_sha256(seal_payload),
            observation_count=len(observations),
            prior_attested_deleted_count=prior_attested_deleted_count,
            terminal_cursor=fold.terminal_cursor,
            observation_stream_sha256=candidate.observation_stream_sha256,
        )

    def _prepare_apply_verification(
        self,
        *,
        job_id: str,
    ) -> _RetentionVerificationPlan:
        with self._query_session_factory() as session:
            job = session.get(ObservationRetentionJob, job_id)
            if job is None:
                raise ObservationRetentionNotFoundError("Observation retention job not found.")
            self._require_active_job(job)
            self._verify_candidate_summary(session, job)
            self._verify_deletion_audit_conservation(session, job)
            self._verify_physical_observation_count(session, job)
            window_rows = tuple(
                (int(row_id), str(run_id))
                for row_id, run_id in session.execute(
                    self._next_window_statement(
                        job_id=job.job_id,
                        next_cursor=int(job.next_cursor),
                        batch_limit=int(job.batch_limit),
                    )
                ).all()
            )
            window_run_ids = sorted({run_id for _row_id, run_id in window_rows})
            candidates = (
                list(
                    session.scalars(
                        select(ObservationRetentionCandidate)
                        .where(
                            ObservationRetentionCandidate.job_id == job.job_id,
                            ObservationRetentionCandidate.run_id.in_(window_run_ids),
                        )
                        .order_by(ObservationRetentionCandidate.run_id)
                    ).all()
                )
                if window_run_ids
                else []
            )
            if (
                len(candidates) != len(window_run_ids)
                or any(
                    candidate.project_id != job.project_id
                    or candidate.site_id != job.site_id
                    or _utc(candidate.sealed_at) >= _utc(job.cutoff_sealed_at)
                    for candidate in candidates
                )
            ):
                raise ObservationRetentionConflictError(
                    "Frozen retention candidate manifest failed integrity verification."
                )
            job_binding_sha256 = _binding_sha256(_job_apply_binding(job))
            candidate_bindings = tuple(_candidate_binding(candidate) for candidate in candidates)

        proofs = tuple(
            self._verify_candidate_read_only(
                job_binding_sha256=job_binding_sha256,
                candidate=candidate,
            )
            for candidate in candidate_bindings
            if candidate.verified_at is None
        )
        return _RetentionVerificationPlan(
            job_id=job_id,
            job_binding_sha256=job_binding_sha256,
            window_rows=window_rows,
            candidate_bindings=candidate_bindings,
            candidate_proofs=proofs,
        )

    def apply(
        self,
        *,
        job_id: str,
        acknowledge: str,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Delete one bounded batch and atomically advance the durable cursor."""
        if acknowledge != _OBSERVATION_ACKNOWLEDGEMENT:
            raise ObservationRetentionValidationError(f'acknowledge must equal "{_OBSERVATION_ACKNOWLEDGEMENT}".')
        actor = _required_text(
            actor,
            field_name="actor",
            maximum=OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH,
        )
        applied_at = _utc(now or datetime.now(UTC))
        deleted_this_batch = 0
        attempted_this_batch = 0
        verification_plan = self._prepare_apply_verification(job_id=job_id)
        with self._session_factory.begin() as session:
            if self._engine.dialect.name == "postgresql":
                # The proof is produced in an earlier read transaction. At
                # READ COMMITTED, a row lock that waited for another mutation
                # re-evaluates against that mutation's committed row version.
                session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            job = session.scalar(
                select(ObservationRetentionJob).where(ObservationRetentionJob.job_id == job_id).with_for_update()
            )
            if job is None:
                raise ObservationRetentionNotFoundError("Observation retention job not found.")
            self._require_active_job(job)

            if (
                verification_plan.job_id != job.job_id
                or _binding_sha256(_job_apply_binding(job)) != verification_plan.job_binding_sha256
            ):
                raise ObservationRetentionConflictError(
                    "Observation retention job changed after read-side verification."
                )

            self._verify_candidate_summary(session, job)
            self._verify_deletion_audit_conservation(session, job)
            run_ids = [candidate.run_id for candidate in verification_plan.candidate_bindings]
            candidates = (
                list(
                    session.scalars(
                        select(ObservationRetentionCandidate)
                        .where(
                            ObservationRetentionCandidate.job_id == job.job_id,
                            ObservationRetentionCandidate.run_id.in_(run_ids),
                        )
                        .order_by(ObservationRetentionCandidate.run_id)
                    ).all()
                )
                if run_ids
                else []
            )
            if tuple(_candidate_binding(candidate) for candidate in candidates) != verification_plan.candidate_bindings:
                raise ObservationRetentionConflictError(
                    "Frozen candidate changed after read-side verification."
                )
            locked_runs = (
                list(session.scalars(select(Run).where(Run.id.in_(run_ids)).order_by(Run.id).with_for_update()).all())
                if run_ids
                else []
            )
            runs_by_id = {run.id: run for run in locked_runs}
            if len(runs_by_id) != len(run_ids):
                raise ObservationRetentionConflictError("A frozen retention candidate run no longer exists.")
            if any(run.project_id != job.project_id or run.site_id != job.site_id for run in locked_runs):
                raise ObservationRetentionConflictError("A frozen retention candidate moved outside the job scope.")

            proofs_by_run_id = {proof.run_id: proof for proof in verification_plan.candidate_proofs}
            unverified_run_ids = {candidate.run_id for candidate in candidates if candidate.verified_at is None}
            if set(proofs_by_run_id) != unverified_run_ids:
                raise ObservationRetentionConflictError("Frozen candidate verification state changed before mutation.")

            proof_observation_stats = {
                (str(run_id), int(attempt)): (int(count), int(max_cursor or 0))
                for run_id, attempt, count, max_cursor in (
                    session.execute(
                        select(
                            RunDiscoveryObservation.run_id,
                            RunDiscoveryObservation.attempt,
                            func.count(),
                            func.max(RunDiscoveryObservation.id),
                        )
                        .where(RunDiscoveryObservation.run_id.in_(sorted(proofs_by_run_id)))
                        .group_by(
                            RunDiscoveryObservation.run_id,
                            RunDiscoveryObservation.attempt,
                        )
                    ).all()
                    if proofs_by_run_id
                    else []
                )
            }
            for candidate in candidates:
                binding = _candidate_binding(candidate)
                run = runs_by_id[candidate.run_id]
                context = session.get(RunExecutionContext, candidate.run_id)
                result_row = session.get(RunResult, candidate.run_id)
                seal = session.get(RunSeal, candidate.run_id)
                if context is None or result_row is None or seal is None:
                    raise ObservationRetentionConflictError(
                        "Frozen retention candidate no longer has complete sealed evidence."
                    )
                run_payload = _run_verification_payload(run)
                context_payload = _context_verification_payload(context)
                result_payload = _result_verification_payload(result_row)
                seal_payload = _seal_verification_payload(seal)
                self._verify_frozen_bindings(
                    candidate=binding,
                    run_payload=run_payload,
                    context_payload=context_payload,
                    result_payload=result_payload,
                    seal_payload=seal_payload,
                )

                proof = proofs_by_run_id.get(candidate.run_id)
                if proof is None:
                    continue
                current_count, current_cursor = proof_observation_stats.get(
                    (candidate.run_id, int(candidate.attempt)),
                    (0, 0),
                )
                proof_is_current = (
                    proof.job_id == job.job_id
                    and proof.job_binding_sha256 == verification_plan.job_binding_sha256
                    and proof.candidate_attestation_sha256 == canonical_sha256(_candidate_attestation(binding))
                    and proof.attempt == int(candidate.attempt)
                    and proof.run_binding_sha256 == _binding_sha256(run_payload)
                    and proof.context_binding_sha256 == _binding_sha256(context_payload)
                    and proof.result_binding_sha256 == _binding_sha256(result_payload)
                    and proof.seal_binding_sha256 == _binding_sha256(seal_payload)
                    and proof.observation_count == current_count
                    and proof.observation_count
                    + proof.prior_attested_deleted_count
                    == int(candidate.observation_count)
                    and proof.terminal_cursor == current_cursor
                    and proof.terminal_cursor == int(candidate.terminal_cursor)
                    and proof.observation_stream_sha256 == candidate.observation_stream_sha256
                )
                if not proof_is_current:
                    raise ObservationRetentionConflictError("Frozen candidate changed after read-side verification.")

            held_run_ids = (
                set(
                    session.scalars(
                        select(RunRetentionHold.run_id).where(
                            RunRetentionHold.run_id.in_(run_ids),
                            RunRetentionHold.active_marker.is_(True),
                        )
                    ).all()
                )
                if run_ids
                else set()
            )
            slotted_run_ids = (
                set(
                    session.scalars(
                        select(ActiveProtocolSlot.run_id).where(ActiveProtocolSlot.run_id.in_(run_ids))
                    ).all()
                )
                if run_ids
                else set()
            )
            blocked_run_ids = held_run_ids | slotted_run_ids

            for run_id in sorted(proofs_by_run_id):
                candidate = next(item for item in candidates if item.run_id == run_id)
                candidate.verified_at = applied_at

            if job.confirmed_by is None:
                job.confirmed_by = actor
                job.confirmed_at = applied_at

            cursor_before = int(job.next_cursor)
            scanned = tuple(
                (int(row_id), str(run_id))
                for row_id, run_id in session.execute(
                    self._next_window_statement(
                        job_id=job.job_id,
                        next_cursor=int(job.next_cursor),
                        batch_limit=int(job.batch_limit),
                    ).with_for_update()
                ).all()
            )
            if scanned != verification_plan.window_rows:
                raise ObservationRetentionConflictError(
                    "Frozen retention observations changed after read-side verification."
                )
            attempted_this_batch = len(scanned)
            scanned_ids = [int(row_id) for row_id, _run_id in scanned]
            eligible_ids = [int(row_id) for row_id, run_id in scanned if run_id not in blocked_run_ids]
            deleted_by_run_id: dict[str, int] = {}
            for _row_id, run_id in scanned:
                if run_id not in blocked_run_ids:
                    deleted_by_run_id[run_id] = deleted_by_run_id.get(run_id, 0) + 1
            if eligible_ids:
                deletion = session.execute(
                    delete(RunDiscoveryObservation).where(RunDiscoveryObservation.id.in_(eligible_ids))
                )
                if deletion.rowcount is None or deletion.rowcount < 0:
                    raise ObservationRetentionConflictError(
                        "The database did not report an authoritative retention delete count."
                    )
                deleted_this_batch = int(deletion.rowcount)
                if deleted_this_batch != len(eligible_ids):
                    raise ObservationRetentionConflictError(
                        "Observation rows changed while the retention batch was locked."
                    )
            candidates_by_run_id = {candidate.run_id: candidate for candidate in candidates}
            for run_id, deleted_count in deleted_by_run_id.items():
                candidates_by_run_id[run_id].deleted_count += deleted_count
            if scanned_ids:
                job.next_cursor = max(scanned_ids)
            elif job.next_cursor < job.high_water_observation_id:
                raise ObservationRetentionConflictError(
                    "Frozen retention observations disappeared before their cursor was processed."
                )

            job.deleted_count += deleted_this_batch
            job.batch_count += 1
            if not scanned_ids or job.next_cursor >= job.high_water_observation_id:
                job.status = "complete"
                job.completed_at = applied_at
                job.active_marker = None
            else:
                job.status = "running"
            session.add(
                ObservationRetentionBatch(
                    job_id=job.job_id,
                    batch_number=job.batch_count,
                    actor=actor,
                    cursor_before=cursor_before,
                    cursor_after=int(job.next_cursor),
                    attempted_count=attempted_this_batch,
                    deleted_count=deleted_this_batch,
                    applied_at=applied_at,
                )
            )
            session.flush()
            self._verify_deletion_audit_conservation(session, job)
            result = _job_to_dict(job)
        return {
            **result,
            "attempted_this_batch": attempted_this_batch,
            "deleted_this_batch": deleted_this_batch,
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        """Read one durable observation retention job without reserving a writer."""
        with self._query_session_factory() as session:
            job = session.get(ObservationRetentionJob, job_id)
            if job is None:
                raise ObservationRetentionNotFoundError("Observation retention job not found.")
            return _job_to_dict(job)

    def get_attested_observation_deletion_count(
        self,
        *,
        run_id: str,
        project_id: str,
        site_id: str,
        attempt: int,
        terminal_cursor: int,
        observation_count: int,
        observation_stream_sha256: str,
    ) -> int:
        """Return deletions bound to the exact sealed run evidence, or zero."""

        with self._query_session_factory() as session:
            matching_candidates = list(
                session.scalars(
                    select(ObservationRetentionCandidate)
                    .where(
                        ObservationRetentionCandidate.run_id == run_id,
                        ObservationRetentionCandidate.project_id == project_id,
                        ObservationRetentionCandidate.site_id == site_id,
                        ObservationRetentionCandidate.attempt == attempt,
                        ObservationRetentionCandidate.terminal_cursor == terminal_cursor,
                        ObservationRetentionCandidate.observation_count == observation_count,
                        ObservationRetentionCandidate.observation_stream_sha256
                        == observation_stream_sha256,
                        ObservationRetentionCandidate.verified_at.is_not(None),
                    )
                    .order_by(ObservationRetentionCandidate.job_id)
                ).all()
            )
            if not matching_candidates:
                return 0
            self._verify_attested_candidate_jobs(session, matching_candidates)
            return sum(int(candidate.deleted_count) for candidate in matching_candidates)

    def get_active_job(
        self,
        *,
        project_id: str,
        site_id: str,
    ) -> dict[str, object]:
        """Recover the one active job for an exact server-validated scope."""
        with self._query_session_factory() as session:
            if (
                session.scalar(
                    select(Site.id).where(
                        Site.id == site_id,
                        Site.project_id == project_id,
                    )
                )
                is None
            ):
                raise ObservationRetentionNotFoundError("Project/site not found.")
            job = session.scalar(
                select(ObservationRetentionJob).where(
                    ObservationRetentionJob.project_id == project_id,
                    ObservationRetentionJob.site_id == site_id,
                    ObservationRetentionJob.active_marker.is_(True),
                )
            )
            if job is None:
                raise ObservationRetentionNotFoundError("Active observation retention job not found.")
            return _job_to_dict(job)

    def place_hold(
        self,
        *,
        run_id: str,
        hold_type: str,
        reason: str,
        actor: str,
        evidence_set_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Place one active legal or evidence hold using server-derived run scope."""
        if hold_type not in {"legal", "evidence"}:
            raise ObservationRetentionValidationError("hold_type must be legal or evidence.")
        reason = _required_text(
            reason,
            field_name="reason",
            maximum=RUN_RETENTION_HOLD_REASON_MAX_LENGTH,
        )
        actor = _required_text(
            actor,
            field_name="actor",
            maximum=RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH,
        )
        normalized_evidence_set_id = (
            _required_text(
                evidence_set_id,
                field_name="evidence_set_id",
                maximum=RUN_RETENTION_HOLD_EVIDENCE_SET_ID_MAX_LENGTH,
            )
            if evidence_set_id is not None
            else None
        )
        if hold_type == "evidence" and normalized_evidence_set_id is None:
            raise ObservationRetentionValidationError("evidence_set_id is required for an evidence hold.")
        if hold_type == "legal" and normalized_evidence_set_id is not None:
            raise ObservationRetentionValidationError("evidence_set_id is only valid for an evidence hold.")
        placed_at = _utc(now or datetime.now(UTC))
        hold: RunRetentionHold | None = None
        try:
            with self._session_factory.begin() as session:
                run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                if run is None:
                    raise ObservationRetentionNotFoundError("Run not found.")
                existing = session.scalar(
                    select(RunRetentionHold.hold_id).where(
                        RunRetentionHold.run_id == run_id,
                        RunRetentionHold.hold_type == hold_type,
                        RunRetentionHold.active_marker.is_(True),
                    )
                )
                if existing is not None:
                    raise ObservationRetentionConflictError("That run already has an active hold of this type.")
                hold = RunRetentionHold(
                    hold_id=f"retention-hold-{uuid4()}",
                    run_id=run.id,
                    project_id=run.project_id,
                    site_id=run.site_id,
                    hold_type=hold_type,
                    evidence_set_id=normalized_evidence_set_id,
                    active_marker=True,
                    placed_by=actor,
                    reason=reason,
                    placed_at=placed_at,
                )
                session.add(hold)
                session.flush()
        except IntegrityError as error:
            raise ObservationRetentionConflictError("That run already has an active hold of this type.") from error
        if hold is None:  # pragma: no cover - all branches above assign or raise
            raise ObservationRetentionConflictError("Retention hold was not created.")
        return _hold_to_dict(hold)

    def list_holds(
        self,
        *,
        project_id: str,
        site_id: str,
        include_released: bool = False,
    ) -> list[dict[str, object]]:
        """List holds for one exact project/site through the query-only path."""
        if not self._scope_exists(project_id=project_id, site_id=site_id):
            raise ObservationRetentionNotFoundError("Project/site not found.")
        statement = select(RunRetentionHold).where(
            RunRetentionHold.project_id == project_id,
            RunRetentionHold.site_id == site_id,
        )
        if not include_released:
            statement = statement.where(RunRetentionHold.active_marker.is_(True))
        statement = statement.order_by(RunRetentionHold.placed_at.desc(), RunRetentionHold.hold_id)
        with self._query_session_factory() as session:
            return [_hold_to_dict(hold) for hold in session.scalars(statement).all()]

    def release_hold(
        self,
        *,
        hold_id: str,
        reason: str,
        actor: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Release an active hold while retaining its complete audit history."""
        reason = _required_text(
            reason,
            field_name="reason",
            maximum=RUN_RETENTION_HOLD_REASON_MAX_LENGTH,
        )
        actor = _required_text(
            actor,
            field_name="actor",
            maximum=RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH,
        )
        released_at = _utc(now or datetime.now(UTC))
        with self._session_factory.begin() as session:
            hold = session.scalar(select(RunRetentionHold).where(RunRetentionHold.hold_id == hold_id).with_for_update())
            if hold is None:
                raise ObservationRetentionNotFoundError("Retention hold not found.")
            if hold.active_marker is not True:
                raise ObservationRetentionConflictError("Retention hold is already released.")
            hold.active_marker = None
            hold.released_by = actor
            hold.release_reason = reason
            hold.released_at = released_at
            session.flush()
            return _hold_to_dict(hold)
