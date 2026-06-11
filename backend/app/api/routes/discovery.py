from fastapi import APIRouter, HTTPException

from app.schemas.jobs import (
    DiscoveryResultsResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunListResponse,
    RunRecord,
)
from app.services.run_service import DISCOVERY_JOB_TYPES, RunService

router = APIRouter()
service = RunService()


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


@router.post("/ip/runs", response_model=JobAcceptedResponse)
def create_ip_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    return _create_run(request, "ip_discovery", "IP discovery job queued.")


@router.post("/bacnet/runs", response_model=JobAcceptedResponse)
def create_bacnet_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    return _create_run(request, "bacnet_discovery", "BACnet discovery job queued.")


@router.post("/mqtt/runs", response_model=JobAcceptedResponse)
def create_mqtt_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    return _create_run(request, "mqtt_discovery", "MQTT discovery job queued.")


@router.get("/runs", response_model=RunListResponse)
def list_discovery_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=DISCOVERY_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_discovery_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in DISCOVERY_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' was not found.")
    return run


@router.get("/runs/{run_id}/results", response_model=DiscoveryResultsResponse)
def get_discovery_results(run_id: str) -> DiscoveryResultsResponse:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in DISCOVERY_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' was not found.")

    discovered_assets = run.result_summary.get("discovered_assets", [])
    if not isinstance(discovered_assets, list):
        discovered_assets = []
    return DiscoveryResultsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        result_summary=run.result_summary,
        discovered_assets=discovered_assets,
    )
