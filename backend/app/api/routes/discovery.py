"""Discovery routes: IP / BACnet / MQTT device discovery.

Each create route builds a run, enforces scan authorization at the API
boundary for real (non-dry-run) scans, then dispatches the matching engine
processor through the shared queue-or-inline path
(:func:`app.services.run_dispatch.dispatch_run`). The inline path runs the
engine in-process against the backend's RunService (which is a
CancellableRunStore) and persists structured discovery records via
DiscoveryRepository; the queue path enqueues the worker actor.

HONESTY: the real network probes live inside the engines and are gated again by
``safety.require_scan_authorization`` as defense in depth. Dry-runs perform NO
I/O and are allowed without authorization (a side-effect-free preview), matching
the safety module's stated convention.
"""

from fastapi import APIRouter, HTTPException
from smart_commissioning_core.db.repositories import DiscoveryRepository
from smart_commissioning_core.engines.bacnet_discovery import process_bacnet_discovery_run
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.safety import is_authorized

from app.core.config import get_settings
from app.schemas.jobs import (
    DiscoveryPointsResponse,
    DiscoveryResultsResponse,
    DiscoveryTopicsResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunListResponse,
    RunRecord,
)
from app.services.engine_dispatch import (
    build_throttle,
    is_dry_run,
    make_cancel_checker,
    make_device_persister,
    make_device_point_persister,
    make_topic_persister,
)
from app.services.job_queue import JobQueueService, JobQueueUnavailable
from app.services.run_dispatch import dispatch_run
from app.services.run_service import DISCOVERY_JOB_TYPES, RunService

router = APIRouter()
service = RunService()
queue_service = JobQueueService()


# Actionable message returned when a real scan lacks authorization. Mirrors the
# safety module's contract so the operator knows exactly how to authorize.
_SCAN_AUTH_DETAIL = (
    "Active network scan requires authorization. Provide parameters.authorized = true, "
    "or the audit-friendly parameters.scan_authorization = "
    "{\"authorized\": true, \"authorized_by\": \"<who>\"}. "
    "A dry_run = true request previews the plan without scanning and needs no authorization."
)


def _settings_throttle(parameters: dict) -> object:
    settings = get_settings()
    return build_throttle(
        parameters,
        max_concurrency=settings.scan_max_concurrency,
        rate_limit_per_sec=settings.scan_rate_limit_per_sec,
        connect_timeout_s=settings.scan_connect_timeout_s,
    )


def _require_scan_authorization(parameters: dict) -> None:
    """Reject a real (non-dry-run) scan that lacks the authorization contract."""
    if is_dry_run(parameters):
        return
    if not is_authorized(parameters):
        raise HTTPException(status_code=403, detail=_SCAN_AUTH_DETAIL)


def _create_run(request: JobCreateRequest, expected_job_type: JobType) -> RunRecord:
    try:
        return service.create_job_run(request, expected_job_type=expected_job_type)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _discovery_repository() -> DiscoveryRepository:
    return DiscoveryRepository(service.engine)


@router.post("/ip/runs", response_model=JobAcceptedResponse)
def create_ip_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "ip_discovery")
    parameters = dict(run.parameters)
    _require_scan_authorization(parameters)

    def run_inline() -> RunRecord:
        persist = make_device_persister(_discovery_repository())
        return process_ip_discovery_run(
            run.run_id,
            parameters,
            run_store=service,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(parameters),
            dry_run=is_dry_run(parameters),
            persist_records=persist,
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_ip_discovery,
        run_inline=run_inline,
        label="IP discovery",
    )


@router.post("/bacnet/runs", response_model=JobAcceptedResponse)
def create_bacnet_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "bacnet_discovery")
    parameters = dict(run.parameters)
    _require_scan_authorization(parameters)

    def run_inline() -> RunRecord:
        persist = make_device_point_persister(_discovery_repository())
        return process_bacnet_discovery_run(
            run.run_id,
            parameters,
            run_store=service,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(parameters),
            dry_run=is_dry_run(parameters),
            persist_records=persist,
            is_cancelled=make_cancel_checker(service, run.run_id),
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_bacnet_discovery,
        run_inline=run_inline,
        label="BACnet discovery",
    )


@router.post("/mqtt/runs", response_model=JobAcceptedResponse)
def create_mqtt_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "mqtt_discovery")
    parameters = dict(run.parameters)
    _require_scan_authorization(parameters)

    def run_inline() -> RunRecord:
        persist = make_topic_persister(_discovery_repository())
        # live_capture defaults to the real raw-socket subscribe_and_capture; in
        # the API process broker egress may be absent, but the engine honestly
        # records 'broker_unreachable'/'live_capture_unavailable' rather than
        # faking success, so we keep the real default here.
        return process_mqtt_discovery_run(
            run.run_id,
            parameters,
            run_store=service,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(parameters),
            dry_run=is_dry_run(parameters),
            persist_records=persist,
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_mqtt_discovery,
        run_inline=run_inline,
        label="MQTT discovery",
    )


def _dispatch(run: RunRecord, *, enqueue, run_inline, label: str) -> JobAcceptedResponse:
    try:
        return dispatch_run(
            run,
            service=service,
            enqueue=enqueue,
            run_inline=run_inline,
            inline_message=f"{label} processed with labelled local inline fallback.",
            queued_message=f"{label} job queued for worker execution.",
            fallback_message=(
                f"{label} processed with labelled local inline fallback "
                "because Redis/Dramatiq was unavailable."
            ),
        )
    except JobQueueUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/runs", response_model=RunListResponse)
def list_discovery_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=DISCOVERY_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord)
def get_discovery_run(run_id: str) -> RunRecord:
    run = _load_discovery_run(run_id)
    return run


@router.get("/runs/{run_id}/results", response_model=DiscoveryResultsResponse)
def get_discovery_results(run_id: str) -> DiscoveryResultsResponse:
    run = _load_discovery_run(run_id)

    # Back-compat: discovered_assets still come from result_summary (the engines
    # write them there). Structured rows additionally come from the repository
    # so consumers see persisted devices/points/topics, not just the summary.
    discovered_assets = run.result_summary.get("discovered_assets", [])
    if not isinstance(discovered_assets, list):
        discovered_assets = []

    repository = _discovery_repository()
    return DiscoveryResultsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        result_summary=run.result_summary,
        discovered_assets=discovered_assets,
        devices=repository.list_devices(run_id),
        points=repository.list_points(run_id),
        topics=repository.list_topics(run_id),
    )


@router.get("/runs/{run_id}/points", response_model=DiscoveryPointsResponse)
def get_discovery_points(run_id: str) -> DiscoveryPointsResponse:
    run = _load_discovery_run(run_id)
    return DiscoveryPointsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        points=_discovery_repository().list_points(run_id),
    )


@router.get("/runs/{run_id}/topics", response_model=DiscoveryTopicsResponse)
def get_discovery_topics(run_id: str) -> DiscoveryTopicsResponse:
    run = _load_discovery_run(run_id)
    return DiscoveryTopicsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        topics=_discovery_repository().list_topics(run_id),
    )


def _load_discovery_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in DISCOVERY_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' was not found.")
    return run
