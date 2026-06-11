import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex

from app.core.runtime import RUNS_ROOT, ensure_runtime_directories
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
    def __init__(self, root: Path = RUNS_ROOT) -> None:
        self.root = root
        ensure_runtime_directories()

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
        return self._load(run_id)

    def list_runs(self, *, job_types: set[JobType] | None = None) -> list[JobSummary]:
        runs: list[RunRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                run = RunRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValueError):
                continue
            if job_types is None or run.job_type in job_types:
                runs.append(run)

        runs.sort(key=lambda run: run.created_at, reverse=True)
        return [self._summary(run) for run in runs]

    def runtime_ready(self) -> tuple[bool, str]:
        try:
            ensure_runtime_directories()
            probe_path = self.root / f".readiness_{token_hex(4)}"
            probe_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            probe_path.unlink(missing_ok=True)
        except OSError as error:
            return False, str(error)
        return True, "run store is writable"

    def update_run_status(
        self,
        run_id: str,
        *,
        status: JobStatus,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> RunRecord:
        def mutate(run: RunRecord) -> None:
            run.status = status
            if stage is not None:
                run.stage = stage
            if progress_percent is not None:
                run.progress_percent = max(0, min(100, progress_percent))
            run.error_message = error_message

        return self._update_run(run_id, mutate)

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> RunRecord:
        def mutate(run: RunRecord) -> None:
            if merge:
                run.result_summary = {**run.result_summary, **result_summary}
            else:
                run.result_summary = dict(result_summary)

        return self._update_run(run_id, mutate)

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> RunRecord:
        def mutate(run: RunRecord) -> None:
            run.issues = [ValidationIssueRecord.model_validate(issue) for issue in issues]

        return self._update_run(run_id, mutate)

    def append_issue(
        self,
        run_id: str,
        issue: ValidationIssueRecord | dict[str, object],
    ) -> RunRecord:
        def mutate(run: RunRecord) -> None:
            run.issues.append(ValidationIssueRecord.model_validate(issue))

        return self._update_run(run_id, mutate)

    def _create_run(
        self,
        *,
        project_id: str,
        site_id: str,
        job_type: JobType,
        parameters: dict[str, object],
    ) -> RunRecord:
        now = datetime.now(timezone.utc)
        run = RunRecord(
            run_id=f"run_{now.strftime('%Y%m%d%H%M%S')}_{token_hex(4)}",
            project_id=project_id,
            site_id=site_id,
            job_type=job_type,
            status="queued",
            stage="awaiting_worker",
            progress_percent=0,
            parameters=parameters,
            result_summary={
                "queued": True,
                "worker_required": True,
            },
            issues=[],
            created_at=now,
            updated_at=now,
        )
        self._save(run)
        return run

    def _load(self, run_id: str) -> RunRecord:
        payload = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        return RunRecord.model_validate(payload)

    def _update_run(self, run_id: str, mutate: Callable[[RunRecord], None]) -> RunRecord:
        run = self._load(run_id)
        mutate(run)
        run.updated_at = datetime.now(timezone.utc)
        self._save(run)
        return run

    def _save(self, run: RunRecord) -> None:
        path = self._path(run.run_id)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _path(self, run_id: str) -> Path:
        if "/" in run_id or "\\" in run_id or ".." in run_id:
            raise FileNotFoundError(run_id)
        return self.root / f"{run_id}.json"

    def _summary(self, run: RunRecord) -> JobSummary:
        return JobSummary(
            run_id=run.run_id,
            job_type=run.job_type,
            status=run.status,
            stage=run.stage,
            progress_percent=run.progress_percent,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def _report_file_name(self, report_type: str, run_id: str, output_format: str) -> str:
        extension = REPORT_FORMAT_EXTENSIONS.get(output_format, "zip")
        return f"{report_type}_{run_id}.{extension}"
