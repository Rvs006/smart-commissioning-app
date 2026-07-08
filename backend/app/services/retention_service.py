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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import Run
from sqlalchemy import select
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_REPORT_JOB_TYPE = "report_generation"


@dataclass
class RetentionCandidate:
    """One run that is eligible (or not) for deletion under the policy."""

    run_id: str
    job_type: str
    created_at: str
    evidence_linked: bool
    reason: str


@dataclass
class RetentionResult:
    """Outcome of a preview or apply pass."""

    cutoff: str
    dry_run: bool
    candidates: list[RetentionCandidate] = field(default_factory=list)
    deleted_run_ids: list[str] = field(default_factory=list)
    skipped_evidence_run_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "candidate_count": len(self.candidates),
            "deleted_count": len(self.deleted_run_ids),
            "skipped_evidence_count": len(self.skipped_evidence_run_ids),
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

        with self._session_factory.begin() as session:
            evidence_ids = self._evidence_linked_run_ids(session)
            old_runs = session.scalars(
                select(Run).where(Run.created_at < before).order_by(Run.created_at)
            ).all()

            for run in old_runs:
                evidence_linked = run.id in evidence_ids or run.job_type == _REPORT_JOB_TYPE
                reason = (
                    "retained: evidence pack / report or referenced by one"
                    if evidence_linked
                    else "eligible: older than cutoff and not evidence-linked"
                )
                result.candidates.append(
                    RetentionCandidate(
                        run_id=run.id,
                        job_type=run.job_type,
                        created_at=run.created_at.astimezone(UTC).isoformat(),
                        evidence_linked=evidence_linked,
                        reason=reason,
                    )
                )
                if evidence_linked:
                    result.skipped_evidence_run_ids.append(run.id)
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
        report_runs = session.scalars(
            select(Run).where(Run.job_type == _REPORT_JOB_TYPE)
        ).all()
        for report in report_runs:
            parameters = report.parameters if isinstance(report.parameters, dict) else {}
            source_ids = parameters.get("source_run_ids")
            if isinstance(source_ids, (list, tuple)):
                linked.update(str(item) for item in source_ids)
        return linked
