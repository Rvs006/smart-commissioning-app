"""Database-backed run store.

Implements the smart_commissioning_core.run_store.RunStore protocol used by the
shared run processors, plus the fuller create/get/list API the backend
RunService needs. Every method returns plain dicts whose shape is identical to
today's JSON file run records (backend RunService / worker FileRunStore), so
API responses do not change:

    run_id, job_type, status, stage, progress_percent, created_at, updated_at,
    project_id, site_id, parameters, result_summary, issues, error_message

Missing runs raise FileNotFoundError(run_id), matching the file-based store the
API routes already handle.
"""

from datetime import UTC, datetime
from secrets import token_hex

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import Project, Run, RunIssue, Site
from smart_commissioning_core.records import ValidationIssueRecord

_ISSUE_FIELDS = tuple(ValidationIssueRecord.model_fields)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run id in the existing run_YYYYMMDDHHMMSS_hex format."""
    now = now or _utcnow()
    return f"run_{now.strftime('%Y%m%d%H%M%S')}_{token_hex(4)}"


def get_or_create_project_and_site(
    session: Session,
    project_id: str,
    site_id: str,
) -> tuple[Project, Site]:
    """Ensure project/site rows exist (today's frontend always sends demo ids)."""
    project = session.get(Project, project_id)
    if project is None:
        project = Project(id=project_id, name=project_id, created_at=_utcnow())
        session.add(project)
    site = session.get(Site, site_id)
    if site is None:
        site = Site(id=site_id, project_id=project_id, name=site_id, created_at=_utcnow())
        session.add(site)
    # Flush now: Run has no relationship() to Project/Site, so the unit of work
    # would not otherwise guarantee these INSERTs run before the run INSERT.
    session.flush()
    return project, site


def _issue_to_dict(issue: RunIssue) -> dict[str, object]:
    record = ValidationIssueRecord.model_validate(
        {field: getattr(issue, field) for field in _ISSUE_FIELDS}
    )
    return record.model_dump(mode="json")


def _run_to_dict(run: Run) -> dict[str, object]:
    issues = sorted(run.issues, key=lambda issue: issue.position)
    return {
        "run_id": run.id,
        "job_type": run.job_type,
        "status": run.status,
        "stage": run.stage,
        "progress_percent": run.progress_percent,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "project_id": run.project_id,
        "site_id": run.site_id,
        "parameters": dict(run.parameters or {}),
        "result_summary": dict(run.result_summary or {}),
        "issues": [_issue_to_dict(issue) for issue in issues],
        "error_message": run.error_message,
    }


class DbRunStore:
    """SQLAlchemy-backed implementation of the shared RunStore protocol."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)

    # -- creation / retrieval -------------------------------------------------

    def create_run(
        self,
        *,
        project_id: str,
        site_id: str,
        job_type: str,
        parameters: dict[str, object] | None = None,
        run_id: str | None = None,
        execution_mode: str | None = None,
    ) -> dict[str, object]:
        now = _utcnow()
        run = Run(
            id=run_id or new_run_id(now),
            project_id=project_id,
            site_id=site_id,
            job_type=job_type,
            status="queued",
            stage="awaiting_worker",
            progress_percent=0,
            parameters=dict(parameters or {}),
            result_summary={"queued": True, "worker_required": True},
            execution_mode=execution_mode,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory.begin() as session:
            get_or_create_project_and_site(session, project_id, site_id)
            session.add(run)
            session.flush()
            return _run_to_dict(run)

    def get_run(self, run_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            run = self._load(session, run_id)
            return _run_to_dict(run)

    def list_runs(
        self,
        project_id: str | None = None,
        site_id: str | None = None,
        job_type: str | list[str] | set[str] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        """Return run records newest-first, optionally filtered and paginated."""
        statement = select(Run).order_by(Run.created_at.desc(), Run.id.desc())
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        if site_id is not None:
            statement = statement.where(Run.site_id == site_id)
        if job_type is not None:
            if isinstance(job_type, str):
                statement = statement.where(Run.job_type == job_type)
            else:
                statement = statement.where(Run.job_type.in_(sorted(job_type)))
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)

        with self._session_factory() as session:
            runs = session.scalars(statement).all()
            return [_run_to_dict(run) for run in runs]

    # -- RunStore protocol ----------------------------------------------------

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, object]:
        with self._session_factory.begin() as session:
            run = self._load(session, run_id, for_update=True)
            run.status = status
            if stage is not None:
                run.stage = stage
            if progress_percent is not None:
                run.progress_percent = max(0, min(100, progress_percent))
            run.error_message = error_message
            run.updated_at = _utcnow()
            session.flush()
            return _run_to_dict(run)

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> dict[str, object]:
        # Read-modify-write inside a single transaction. SELECT ... FOR UPDATE
        # locks the row on Postgres; on SQLite the engine issues BEGIN IMMEDIATE
        # at transaction start (see engine.create_engine_from_url), which takes
        # the write lock up front so concurrent mergers serialize.
        with self._session_factory.begin() as session:
            run = self._load(session, run_id, for_update=True)
            if merge:
                current = run.result_summary if isinstance(run.result_summary, dict) else {}
                run.result_summary = {**current, **result_summary}
            else:
                run.result_summary = dict(result_summary)
            run.updated_at = _utcnow()
            session.flush()
            return _run_to_dict(run)

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> dict[str, object]:
        records = [ValidationIssueRecord.model_validate(issue) for issue in issues]
        with self._session_factory.begin() as session:
            run = self._load(session, run_id, for_update=True)
            session.execute(delete(RunIssue).where(RunIssue.run_id == run_id))
            session.flush()
            for position, record in enumerate(records):
                session.add(self._issue_row(run_id, position, record))
            run.updated_at = _utcnow()
            session.flush()
            session.refresh(run, attribute_names=["issues"])
            return _run_to_dict(run)

    # -- internals ------------------------------------------------------------

    # -- cooperative cancellation --------------------------------------------

    def request_cancel(self, run_id: str) -> dict[str, object]:
        """Mark the run as cancellation-requested (cooperative).

        Sets the ``cancel_requested`` flag; running engines poll
        :meth:`is_cancel_requested` and stop early. Does NOT change ``status``
        on its own — the engine/run wrapper flips the terminal status to
        ``cancelled`` when it observes the request. Raises FileNotFoundError if
        the run does not exist.
        """
        with self._session_factory.begin() as session:
            run = self._load(session, run_id, for_update=True)
            run.cancel_requested = True
            run.updated_at = _utcnow()
            session.flush()
            return _run_to_dict(run)

    def is_cancel_requested(self, run_id: str) -> bool:
        """Return True if cancellation has been requested for the run.

        Returns False for a missing run (a vanished run cannot be cancelled),
        so engine cancellation polling never raises.
        """
        with self._session_factory() as session:
            run = session.scalars(select(Run).where(Run.id == run_id)).one_or_none()
            return bool(run is not None and run.cancel_requested)

    # -- edge->hub sync accessors --------------------------------------------
    # edge_id / synced_at are kept out of the public _run_to_dict contract (like
    # cancel_requested) and read here so the 13-key record shape never changes.

    def get_edge_id(self, run_id: str) -> str | None:
        """Return the originating edge id for the run (None for a local run).

        Raises FileNotFoundError if the run does not exist.
        """
        with self._session_factory() as session:
            return self._load(session, run_id).edge_id

    def get_synced_at(self, run_id: str) -> datetime | None:
        """Return the edge watermark (when last pushed from here), or None.

        Raises FileNotFoundError if the run does not exist.
        """
        with self._session_factory() as session:
            return self._load(session, run_id).synced_at

    def _load(self, session: Session, run_id: str, *, for_update: bool = False) -> Run:
        statement = select(Run).where(Run.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        run = session.scalars(statement).one_or_none()
        if run is None:
            raise FileNotFoundError(run_id)
        return run

    def _issue_row(self, run_id: str, position: int, record: ValidationIssueRecord) -> RunIssue:
        values = record.model_dump()
        return RunIssue(run_id=run_id, position=position, **values)
