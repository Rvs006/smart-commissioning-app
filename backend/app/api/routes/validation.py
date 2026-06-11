from fastapi import APIRouter, HTTPException

from app.core.config import get_settings
from app.schemas.jobs import (
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunRecord,
    RunListResponse,
    ValidationIssueRecord,
    ValidationIssuesResponse,
)
from app.services.job_queue import JobQueueService, JobQueueUnavailable
from app.services.mqtt_config_publish_processor import process_mqtt_config_publish_run
from app.services.run_service import RunService, VALIDATION_JOB_TYPES
from app.services.udmi_run_processor import process_udmi_validation_run

router = APIRouter()
service = RunService()
queue_service = JobQueueService()
settings = get_settings()


def _create_run(
    request: JobCreateRequest,
    expected_job_type: JobType,
    message: str,
) -> JobAcceptedResponse:
    try:
        run = service.create_job_run(request, expected_job_type=expected_job_type)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return JobAcceptedResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        message=message,
    )


@router.post("/udmi/runs", response_model=JobAcceptedResponse)
def create_udmi_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    try:
        run = service.create_job_run(request, expected_job_type="udmi_validation")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if settings.job_execution_mode == "inline":
        processed_run = process_udmi_validation_run(
            run.run_id,
            dict(run.parameters),
            run_service=service,
            execution_mode="inline_local_fallback",
            fallback_reason="JOB_EXECUTION_MODE is set to inline for local development.",
        )
        return JobAcceptedResponse(
            run_id=processed_run.run_id,
            job_type=processed_run.job_type,
            status=processed_run.status,
            message="UDMI validation processed with labelled local inline fallback.",
        )

    try:
        dispatch = queue_service.enqueue_udmi_validation(run)
        service.update_result_summary(
            run.run_id,
            {
                "queued": True,
                "worker_required": True,
                "execution_mode": "dramatiq_redis",
                "queue_name": dispatch.queue_name,
                "actor_name": dispatch.actor_name,
            },
        )
        return JobAcceptedResponse(
            run_id=run.run_id,
            job_type=run.job_type,
            status=run.status,
            message="UDMI validation job queued for worker execution.",
        )
    except JobQueueUnavailable as error:
        if settings.job_execution_mode == "queue" or not settings.allow_inline_worker_fallback:
            service.update_run_status(
                run.run_id,
                status="failed",
                stage="queue_unavailable",
                progress_percent=100,
                error_message=str(error),
            )
            raise HTTPException(status_code=503, detail=str(error)) from error

        processed_run = process_udmi_validation_run(
            run.run_id,
            dict(run.parameters),
            run_service=service,
            execution_mode="inline_local_fallback",
            fallback_reason=str(error),
        )
        return JobAcceptedResponse(
            run_id=processed_run.run_id,
            job_type=processed_run.job_type,
            status=processed_run.status,
            message=(
                "UDMI validation processed with labelled local inline fallback "
                "because Redis/Dramatiq was unavailable."
            ),
        )


@router.post("/mqtt-config/runs", response_model=JobAcceptedResponse)
def create_mqtt_config_publish_run(request: JobCreateRequest) -> JobAcceptedResponse:
    try:
        run = service.create_job_run(request, expected_job_type="mqtt_config_publish")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    processed_run = process_mqtt_config_publish_run(
        run.run_id,
        dict(run.parameters),
        run_service=service,
        execution_mode="inline_local_fallback",
    )
    return JobAcceptedResponse(
        run_id=processed_run.run_id,
        job_type=processed_run.job_type,
        status=processed_run.status,
        message="MQTT config publish processed with labelled local inline fallback.",
    )


@router.post("/bacnet/runs", response_model=JobAcceptedResponse)
def create_bacnet_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    return _create_run(request, "bacnet_validation", "BACnet validation job queued.")


@router.post("/mapping/runs", response_model=JobAcceptedResponse)
def create_mapping_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    return _create_run(request, "mapping_validation", "BACnet to MQTT mapping validation job queued.")


@router.get("/runs", response_model=RunListResponse)
def list_validation_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=VALIDATION_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_validation_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in VALIDATION_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Validation run '{run_id}' was not found.")
    return run


@router.get("/runs/{run_id}/issues", response_model=ValidationIssuesResponse)
def get_validation_issues(run_id: str) -> ValidationIssuesResponse:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in VALIDATION_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Validation run '{run_id}' was not found.")

    issues = run.issues
    if not issues:
        raw_issues = run.result_summary.get("issues", [])
        issues = raw_issues if isinstance(raw_issues, list) else []
    return ValidationIssuesResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        issues=[ValidationIssueRecord.model_validate(issue) for issue in issues],
    )
