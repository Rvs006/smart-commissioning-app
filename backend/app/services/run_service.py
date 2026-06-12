from smart_commissioning_core.db.db_run_store import DbRunStore
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.schemas.jobs import (
    JobCreateRequest,
    JobStatus,
    JobSummary,
    JobType,
    ReportRequest,
    ReportSummary,
    RunRecord,
    ValidationIssueRecord,
)

DISCOVERY_JOB_TYPES: set[JobType] = {"ip_discovery", "bacnet_discovery", "mqtt_discovery"}
VALIDATION_JOB_TYPES: set[JobType] = {
    "udmi_validation",
    "mqtt_config_publish",
    "bacnet_validation",
    "mapping_validation",
}
REPORT_JOB_TYPES: set[JobType] = {"report_generation"}
REPORT_FORMAT_EXTENSIONS = {
    "docx": "docx",
    "xlsx": "xlsx",
    "zip": "zip",
}


class RunService:
    """Thin wrapper over the shared database-backed run store.

    The public API mirrors the previous file-backed implementation exactly:
    method names, RunRecord/JobSummary return models, and FileNotFoundError
    for missing run ids (routes translate that into 404 responses).
    """

    def __init__(self, engine: Engine | None = None) -> None:
        ensure_runtime_directories()
        self._engine = engine if engine is not None else get_engine()
        self._store = DbRunStore(self._engine)

    def create_job_run(
        self,
        request: JobCreateRequest,
        *,
        expected_job_type: JobType,
    ) -> RunRecord:
        if request.job_type != expected_job_type:
            raise ValueError(f"Endpoint expects job_type '{expected_job_type}'.")

        return self._create_run(
            project_id=request.project_id,
            site_id=request.site_id,
            job_type=expected_job_type,
            parameters=dict(request.parameters),
        )

    def create_report_run(self, request: ReportRequest) -> tuple[RunRecord, ReportSummary]:
        run = self._create_run(
            project_id=request.project_id,
            site_id=request.site_id,
            job_type="report_generation",
            parameters={
                "output_format": request.output_format,
                "report_type": request.report_type,
                "source_run_ids": request.source_run_ids,
            },
        )
        report = ReportSummary(
            report_id=run.run_id,
            report_type=request.report_type,
            output_format=request.output_format,
            status=run.status,
            file_name=self._report_file_name(request.report_type, run.run_id, request.output_format),
        )
        return run, report

    def get_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(self._store.get_run(run_id))

    def list_runs(
        self,
        *,
        job_types: set[JobType] | None = None,
        project_id: str | None = None,
        site_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobSummary]:
        records = self._store.list_runs(
            project_id,
            site_id,
            job_type=job_types,
            limit=limit,
            offset=offset,
        )
        return [JobSummary.model_validate(record) for record in records]

    def runtime_ready(self) -> tuple[bool, str]:
        try:
            ensure_runtime_directories()
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except (OSError, SQLAlchemyError) as error:
            return False, str(error)
        return True, "run store database is reachable"

    def update_run_status(
        self,
        run_id: str,
        *,
        status: JobStatus,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> RunRecord:
        record = self._store.update_run_status(
            run_id,
            status=status,
            stage=stage,
            progress_percent=progress_percent,
            error_message=error_message,
        )
        return RunRecord.model_validate(record)

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> RunRecord:
        record = self._store.update_result_summary(run_id, result_summary, merge=merge)
        return RunRecord.model_validate(record)

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> RunRecord:
        return RunRecord.model_validate(self._store.replace_issues(run_id, issues))

    def append_issue(
        self,
        run_id: str,
        issue: ValidationIssueRecord | dict[str, object],
    ) -> RunRecord:
        return RunRecord.model_validate(self._store.append_issue(run_id, issue))

    # -- cooperative cancellation (CancellableRunStore protocol) --------------

    def request_cancel(self, run_id: str) -> RunRecord:
        """Flag the run as cancellation-requested; returns the updated run.

        Cooperative: running engines poll :meth:`is_cancel_requested` and stop
        early, flipping the terminal status to ``cancelled``. Raises
        FileNotFoundError for a missing run (the route maps that to 404).
        """
        return RunRecord.model_validate(self._store.request_cancel(run_id))

    def is_cancel_requested(self, run_id: str) -> bool:
        """Return True if cancellation has been requested for the run.

        Exposed so the engine framework (and inline dispatch) can build a
        cancellation checker from this RunService directly. Never raises for a
        missing run (a vanished run cannot be cancelled).
        """
        return self._store.is_cancel_requested(run_id)

    @property
    def engine(self) -> Engine:
        """The shared SQLAlchemy engine backing this service.

        Exposed so route dispatch can build a DiscoveryRepository / loaders on
        the SAME engine/database the run store uses.
        """
        return self._engine

    def _create_run(
        self,
        *,
        project_id: str,
        site_id: str,
        job_type: JobType,
        parameters: dict[str, object],
    ) -> RunRecord:
        record = self._store.create_run(
            project_id=project_id,
            site_id=site_id,
            job_type=job_type,
            parameters=parameters,
        )
        return RunRecord.model_validate(record)

    def _report_file_name(self, report_type: str, run_id: str, output_format: str) -> str:
        extension = REPORT_FORMAT_EXTENSIONS.get(output_format, "zip")
        return f"{report_type}_{run_id}.{extension}"
