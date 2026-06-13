"""Validation routes: UDMI / MQTT config publish / BACnet point / mapping.

UDMI, BACnet point validation, and BACnet<->MQTT mapping comparison all route
through the shared queue-or-inline dispatcher. The validation/comparison engines
perform NO network I/O, so they require no scan authorization; they read their
expected register from an import (via ImportRepository) and observed values from
a discovery run (via DiscoveryRepository), or inline values supplied in
``parameters`` for testing.

MQTT config publish stays inline (its real broker publish path requires broker
egress) and additionally captures the prior retained config value for rollback;
see :mod:`smart_commissioning_core.mqtt_config_publish` and the ``rollback``
endpoint below.
"""

from fastapi import APIRouter, Depends, HTTPException
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.comparison import process_mapping_validation_run
from smart_commissioning_core.engines.point_validation import process_bacnet_validation_run
from smart_commissioning_core.mqtt_config_publish_processor import (
    process_mqtt_config_publish_run,
    process_mqtt_config_rollback_run,
)
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run

from app.core.auth import require_role
from app.core.config import get_settings
from app.schemas.jobs import (
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunListResponse,
    RunRecord,
    ValidationIssueRecord,
    ValidationIssuesResponse,
)
from app.services.engine_dispatch import (
    make_cancel_checker,
    make_discovery_loader,
    make_import_loader,
)
from app.services.job_queue import JobQueueService, JobQueueUnavailable
from app.services.run_dispatch import dispatch_run
from app.services.run_service import VALIDATION_JOB_TYPES, RunService

router = APIRouter()
service = RunService()
queue_service = JobQueueService()
settings = get_settings()

# RBAC: reading validation results/issues is viewer+; creating a validation run,
# publishing an MQTT config, or rolling one back is engineer+ (a publish/rollback
# is a live write, gated additionally by the scan/publish authorization consent).
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


def _create_run(request: JobCreateRequest, expected_job_type: JobType) -> RunRecord:
    try:
        return service.create_job_run(request, expected_job_type=expected_job_type)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _import_loader():
    return make_import_loader(ImportRepository(service.engine))


def _discovery_loader():
    return make_discovery_loader(DiscoveryRepository(service.engine))


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


@router.post("/udmi/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_udmi_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "udmi_validation")

    def run_inline() -> RunRecord:
        return process_udmi_validation_run(
            run.run_id,
            dict(run.parameters),
            run_store=service,
            execution_mode="inline_local_fallback",
            fallback_reason="JOB_EXECUTION_MODE is set to inline for local development.",
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_udmi_validation,
        run_inline=run_inline,
        label="UDMI validation",
    )


@router.post(
    "/mqtt-config/runs",
    response_model=JobAcceptedResponse,
    dependencies=[Depends(require_engineer)],
)
def create_mqtt_config_publish_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "mqtt_config_publish")

    # A live publish actively writes to a broker, so gate it on the same scan
    # authorization contract used by the discovery engines. The local
    # validate-only path (use_live_broker not set) is side-effect free and does
    # not require authorization.
    parameters = dict(run.parameters)
    _require_publish_authorization(parameters)

    processed_run = process_mqtt_config_publish_run(
        run.run_id,
        parameters,
        run_store=service,
        execution_mode="inline_local_fallback",
    )
    return JobAcceptedResponse(
        run_id=processed_run.run_id,
        job_type=processed_run.job_type,
        status=processed_run.status,
        message="MQTT config publish processed with labelled local inline fallback.",
    )


@router.post(
    "/mqtt-config/runs/{run_id}/rollback",
    response_model=JobAcceptedResponse,
    dependencies=[Depends(require_engineer)],
)
def rollback_mqtt_config_publish(run_id: str) -> JobAcceptedResponse:
    """Republish the previously-captured config value to roll back a publish.

    Re-publishes ``result_summary['previous_config']['payload']`` to the same
    config topic. Guarded by the SAME authorization + publish-confirmation gate
    as the forward publish (a rollback is itself a live write). If no prior
    value was captured the route returns 400 — there is nothing to roll back to.

    HONESTY: capturing the prior retained value requires a reachable broker, so
    in this environment the captured value is whatever the original publish
    recorded (often the request-supplied ``previous_config`` or none). The
    live-broker capture/replay path is on-site-validation surface.
    """
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type != "mqtt_config_publish":
        raise HTTPException(status_code=404, detail=f"MQTT config publish run '{run_id}' was not found.")

    previous = run.result_summary.get("previous_config")
    if not isinstance(previous, dict) or previous.get("payload") in (None, ""):
        raise HTTPException(
            status_code=400,
            detail=(
                "No previous config value was captured for this run, so there is "
                "nothing to roll back to. Rollback requires a recorded "
                "result_summary.previous_config.payload."
            ),
        )

    _require_publish_authorization(dict(run.parameters))

    processed_run = process_mqtt_config_rollback_run(
        run.run_id,
        dict(run.parameters),
        previous_config=previous,
        run_store=service,
        execution_mode="inline_local_fallback",
    )
    return JobAcceptedResponse(
        run_id=processed_run.run_id,
        job_type=processed_run.job_type,
        status=processed_run.status,
        message="MQTT config rollback processed with labelled local inline fallback.",
    )


@router.post("/bacnet/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_bacnet_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "bacnet_validation")

    def run_inline() -> RunRecord:
        return process_bacnet_validation_run(
            run.run_id,
            dict(run.parameters),
            run_store=service,
            execution_mode="inline_local_fallback",
            import_loader=_import_loader(),
            discovery_loader=_discovery_loader(),
            is_cancelled=make_cancel_checker(service, run.run_id),
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_bacnet_validation,
        run_inline=run_inline,
        label="BACnet validation",
    )


@router.post("/mapping/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_mapping_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    run = _create_run(request, "mapping_validation")

    def run_inline() -> RunRecord:
        return process_mapping_validation_run(
            run.run_id,
            dict(run.parameters),
            run_store=service,
            execution_mode="inline_local_fallback",
            import_loader=_import_loader(),
            discovery_loader=_discovery_loader(),
            is_cancelled=make_cancel_checker(service, run.run_id),
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_mapping_validation,
        run_inline=run_inline,
        label="BACnet to MQTT mapping validation",
    )


@router.get("/runs", response_model=RunListResponse, dependencies=[Depends(require_viewer)])
def list_validation_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=VALIDATION_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord, dependencies=[Depends(require_viewer)])
def get_validation_run(run_id: str) -> RunRecord:
    return _load_validation_run(run_id)


@router.get(
    "/runs/{run_id}/issues",
    response_model=ValidationIssuesResponse,
    dependencies=[Depends(require_viewer)],
)
def get_validation_issues(run_id: str) -> ValidationIssuesResponse:
    run = _load_validation_run(run_id)

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


def _load_validation_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in VALIDATION_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Validation run '{run_id}' was not found.")
    return run


# Actionable authorization message for live MQTT writes (publish + rollback).
_PUBLISH_AUTH_DETAIL = (
    "Live MQTT config publish requires authorization. Provide parameters.authorized = true, "
    "or parameters.scan_authorization = {\"authorized\": true, \"authorized_by\": \"<who>\"}. "
    "A validate-only run (use_live_broker not set) needs no authorization."
)


def _require_publish_authorization(parameters: dict) -> None:
    """Reject a LIVE publish that lacks the authorization contract.

    Only enforced when the run actually targets a live broker
    (``use_live_broker`` true or a ``broker_host`` present); the validate-only
    path is side-effect free.
    """
    from smart_commissioning_core.engines.safety import is_authorized
    from smart_commissioning_core.mqtt_settings import parse_bool

    targets_live_broker = parse_bool(parameters.get("use_live_broker")) or bool(
        str(parameters.get("broker_host") or "").strip()
    )
    if not targets_live_broker:
        return
    if not is_authorized(parameters):
        raise HTTPException(status_code=403, detail=_PUBLISH_AUTH_DETAIL)
