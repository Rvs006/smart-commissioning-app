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
from app.services.configuration_service import ConfigurationService
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
config_service = ConfigurationService()
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


def _expected_schedule_from_register_row(row: dict) -> dict:
    """Map an mqtt_register row to the UDMI matcher's expected_schedule.

    Make/Model/GUID/Serial/Firmware/Site/Room feed the metadata/state identity
    checks; comma-separated Expected points + Expected units become the per-point
    units map; Expected schema version drives the payload version match and the
    per-version structural checks. Blank fields are dropped so the matcher only
    checks what's set.
    """
    points = [p.strip() for p in str(row.get("Expected points", "")).split(",") if p.strip()]
    units = [u.strip() for u in str(row.get("Expected units", "")).split(",") if u.strip()]
    fields = {
        "asset_id": row.get("Asset ID") or row.get("Asset name"),
        "manufacturer": row.get("Make"),
        "model": row.get("Model"),
        "serial": row.get("Serial number"),
        "firmware": row.get("Firmware"),
        "guid": row.get("GUID"),
        "site": row.get("Site"),
        "room": row.get("Room"),
        "udmi_version": row.get("Expected schema version"),
        "reporting_interval_seconds": row.get("Expected reporting interval"),
    }
    schedule = {key: value for key, value in fields.items() if value}
    units_map = {point: (units[index] if index < len(units) else "") for index, point in enumerate(points)}
    if units_map:
        schedule["units"] = units_map
    return schedule


def _capture_topics_from_expected(expected_topic: object, payload_type: object = None) -> dict:
    """Derive state/metadata/pointset capture topics from a register Expected topic.

    Accepts a ``prefix/#`` wildcard (covers all three), an explicit per-type topic,
    or a comma-separated list of those — matching the register's topic conventions.
    A wildcard also subscribes the legacy singular ``<prefix>/event/pointset`` so
    sites on that convention still deliver their pointset payload.
    """
    topics: dict[str, object] = {}
    extra_topics: list[str] = []
    roots: set[str] = set()
    requested_type = str(payload_type or "").strip().casefold()
    for part in str(expected_topic or "").split(","):
        topic = part.strip()
        if not topic:
            continue
        # Keep a register wildcard in the live subscription set as well as
        # its derived siblings; some site ACLs/brokers behave differently for
        # wildcard versus concrete subscriptions. Explicit topics remain
        # unchanged to avoid broadening their contract.
        if topic.endswith("/#"):
            topics.setdefault("register_topic_filter", topic)
        if topic.endswith("/#"):
            prefix = topic[:-2].rstrip("/")
            roots.add(prefix)
            if requested_type in {"", "state"}:
                topics.setdefault("state_topic", prefix + "/state")
            if requested_type in {"", "metadata"}:
                topics.setdefault("metadata_topic", prefix + "/metadata")
            if requested_type in {"", "pointset"}:
                topics.setdefault("pointset_topic", prefix + "/events/pointset")
                extra_topics.append(prefix + "/event/pointset")
        elif topic.endswith("/state") and requested_type in {"", "state"}:
            roots.add(topic.removesuffix("/state"))
            topics["state_topic"] = topic
        elif topic.endswith("/metadata") and requested_type in {"", "metadata"}:
            roots.add(topic.removesuffix("/metadata"))
            topics["metadata_topic"] = topic
        elif topic.endswith("/events/pointset") and requested_type in {"", "pointset"}:
            roots.add(topic.removesuffix("/events/pointset"))
            topics["pointset_topic"] = topic
        elif topic.endswith("/event/pointset") and requested_type in {"", "pointset"}:
            roots.add(topic.removesuffix("/event/pointset"))
            topics["pointset_topic"] = topic

    # Pete's register contract: blank Payload type represents one WHOLE asset,
    # so even one explicit sibling topic must require all three payload slots.
    required_slots = {"state_topic", "metadata_topic", "pointset_topic"}
    if not requested_type and roots and not required_slots.issubset(topics):
        if len(roots) == 1:
            prefix = next(iter(roots))
            topics.setdefault("state_topic", prefix + "/state")
            topics.setdefault("metadata_topic", prefix + "/metadata")
            topics.setdefault("pointset_topic", prefix + "/events/pointset")
            extra_topics.append(prefix + "/event/pointset")
    if extra_topics:
        topics["extra_capture_topics"] = list(dict.fromkeys(extra_topics))
    return topics


def _asset_entry_from_row(row: dict) -> dict:
    """One UDMI `assets` fan-out entry: expected_schedule + per-asset capture topics."""
    return {
        "expected_schedule": _expected_schedule_from_register_row(row),
        **_capture_topics_from_expected(row.get("Expected topic"), row.get("Payload type")),
    }


def _expected_assets_from_register(project_id: str, site_id: str) -> list[dict]:
    """One fan-out entry per row of the newest mqtt_register import (empty if none)."""
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="mqtt_register"
    )
    for record in imports:  # newest-first
        rows = record.get("accepted_rows", [])
        if rows:
            return [_asset_entry_from_row(row) for row in rows]
    return []


@router.post("/udmi/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_udmi_validation_run(request: JobCreateRequest) -> JobAcceptedResponse:
    # When the operator hasn't pasted expected values, fill them from the imported
    # MQTT register so the workbench validates against Make/Model/GUID/points/units
    # without re-typing. Register rows always become an `assets` list (even a
    # single row) so each asset keeps its register-derived capture topics and the
    # matcher/live capture fan out per asset. An explicit asset_id narrows the
    # list to that row.
    parameters = dict(request.parameters)
    parameters.setdefault("qos", config_service.mqtt_subscribe_defaults(request.project_id, request.site_id)["qos"])
    if not parameters.get("expected_schedule") and not parameters.get("assets"):
        assets = _expected_assets_from_register(request.project_id, request.site_id)
        asset_id = str(parameters.get("asset_id") or "").strip()
        if asset_id and assets:
            chosen = next((a for a in assets if asset_id == a["expected_schedule"].get("asset_id")), assets[0])
            assets = [chosen]
        if assets:
            if len(assets) == 1:
                # A caller may pair the single register row with directly
                # supplied payloads; keep them reviewable against that row.
                for key in ("state_payload", "metadata_payload", "pointset_payload", "messages"):
                    if parameters.get(key) is not None:
                        assets[0].setdefault(key, parameters[key])
            parameters["assets"] = assets
        elif parameters.get("use_register"):
            # The operator explicitly asked to validate against the imported
            # register and there is none: refuse rather than silently falling
            # back to the packaged sample fixture and presenting it as a result.
            raise HTTPException(
                status_code=400,
                detail=(
                    "No accepted MQTT register import was found for this project/site. "
                    "Upload an mqtt_register file, or untick the register option to "
                    "validate pasted payloads instead."
                ),
            )
    run = _create_run(request.model_copy(update={"parameters": parameters}), "udmi_validation")

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
        enqueue=queue_service.enqueue_for("validate_udmi_payloads", "validation"),
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
        enqueue=queue_service.enqueue_for("validate_bacnet_points", "validation"),
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
        enqueue=queue_service.enqueue_for("compare_bacnet_mqtt", "validation"),
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
