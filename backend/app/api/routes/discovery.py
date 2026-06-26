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

import json
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Response
from openpyxl import Workbook
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.bacnet_discovery import process_bacnet_discovery_run
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.safety import is_authorized
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
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

# RBAC: reading discovery data is viewer+; creating/running a discovery job
# (which can drive a real network scan) is engineer+. The separate scan
# authorization consent (parameters.authorized / scan_authorization) still
# applies on top of this — RBAC is WHO may act, scan-auth is the safety consent.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


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


# Explicit target keys an operator may set to scope an IP sweep. When none are
# present, the scan falls back to the imported IP register's expected addresses.
_EXPLICIT_IP_TARGET_KEYS = ("cidr", "start", "start_ip", "end", "end_ip", "addresses")


def _ensure_ip_targets(project_id: str, site_id: str, parameters: dict) -> None:
    """Resolve scan targets for an IP discovery run.

    An explicit target (``cidr`` / ``start``-``end`` / ``addresses``) is used
    untouched. Otherwise ``addresses`` is filled from the newest accepted IP
    register import for this project/site (deduped, first-seen order), so the
    "import register -> run discovery" flow sweeps exactly the registered hosts
    — the engine only knew how to expand a cidr/range, which the frontend never
    supplied (the old opaque "engine failed"). Raises 400 when there is nothing
    to scan, with an actionable message instead of the sanitized engine failure.
    """
    if any(parameters.get(key) for key in _EXPLICIT_IP_TARGET_KEYS):
        return
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="ip_register"
    )
    for record in imports:  # newest-first
        addresses = list(dict.fromkeys(
            a for row in record.get("accepted_rows", [])
            if (a := str(row.get("Expected IP address", "") or "").strip())
        ))
        if addresses:
            parameters["addresses"] = addresses
            return
    raise HTTPException(
        status_code=400,
        detail=(
            "No scan targets found. Import an IP register (with an "
            "'Expected IP address' column) for this project/site, or provide a "
            "'cidr' or 'start'/'end' range, before running IP discovery."
        ),
    )


def _resolve_forbidden_ports(project_id: str, site_id: str, parameters: dict) -> None:
    """Fill ``forbidden_ports`` from the IP register's "Ports that should not be
    enabled" column (union across rows of the newest import that lists any), so a
    flagged-if-open check needs no extra operator input. Operator-supplied
    ``forbidden_ports`` always wins.

    Additionally builds ``forbidden_ports_by_address`` — a per-asset map
    ``{expected_ip_address: forbidden_spec}`` from the same rows — so the engine
    can flag each host against its OWN forbidden set, falling back to the global
    union for any host not in the map. Operator-supplied global ports still win
    for the union; the per-asset map is purely additive context for the engine.
    """
    has_global = bool(parameters.get("forbidden_ports"))
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="ip_register"
    )
    for record in imports:  # newest-first
        specs: list[str] = []
        by_address: dict[str, str] = {}
        for row in record.get("accepted_rows", []):
            spec = str(row.get("Ports that should not be enabled", "") or "").strip()
            if not spec:
                continue
            specs.append(spec)
            address = str(row.get("Expected IP address", "") or "").strip()
            if address:
                by_address[address] = spec
        if specs:
            if not has_global:
                parameters["forbidden_ports"] = ",".join(specs)
            if by_address:
                parameters["forbidden_ports_by_address"] = by_address
            return


@router.post("/ip/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_ip_discovery_run(request: JobCreateRequest) -> JobAcceptedResponse:
    # Validate authorization + resolve scan targets BEFORE creating the run, so a
    # rejected request never leaves an orphaned queued run, and the resolved
    # register addresses are persisted into the run record (the worker path reads
    # run.parameters, not just the inline dict).
    parameters = dict(request.parameters)
    _require_scan_authorization(parameters)
    _ensure_ip_targets(request.project_id, request.site_id, parameters)
    _resolve_forbidden_ports(request.project_id, request.site_id, parameters)
    run = _create_run(request.model_copy(update={"parameters": parameters}), "ip_discovery")

    def run_inline() -> RunRecord:
        persist = make_device_persister(_discovery_repository())
        return process_ip_discovery_run(
            run.run_id,
            dict(run.parameters),
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


@router.post("/bacnet/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
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


@router.post("/mqtt/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
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


@router.get("/runs", response_model=RunListResponse, dependencies=[Depends(require_viewer)])
def list_discovery_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=DISCOVERY_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord, dependencies=[Depends(require_viewer)])
def get_discovery_run(run_id: str) -> RunRecord:
    run = _load_discovery_run(run_id)
    return run


@router.get(
    "/runs/{run_id}/results",
    response_model=DiscoveryResultsResponse,
    dependencies=[Depends(require_viewer)],
)
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


@router.get(
    "/runs/{run_id}/points",
    response_model=DiscoveryPointsResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_points(run_id: str) -> DiscoveryPointsResponse:
    run = _load_discovery_run(run_id)
    return DiscoveryPointsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        points=_discovery_repository().list_points(run_id),
    )


@router.get(
    "/runs/{run_id}/topics",
    response_model=DiscoveryTopicsResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_topics(run_id: str) -> DiscoveryTopicsResponse:
    run = _load_discovery_run(run_id)
    return DiscoveryTopicsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        topics=_discovery_repository().list_topics(run_id),
    )


@router.get("/runs/{run_id}/topics.xlsx", dependencies=[Depends(require_viewer)])
def export_discovery_topics_xlsx(run_id: str, topic_filter: str | None = None) -> Response:
    """Export the captured latest-payload-per-topic rows as an XLSX (mq9nhbzu).

    Reuses the same persisted topic rows the capture panel/CSV use (no live
    broker), generated server-side with openpyxl like reports/import templates,
    so empty stays empty (no fabricated payloads). An optional ``topic_filter``
    applies the same ``+``/``#`` wildcard semantics as the on-screen filter so
    the export matches what the operator sees.
    """
    _load_discovery_run(run_id)
    rows = _discovery_repository().list_topics(run_id)
    if topic_filter:
        rows = [row for row in rows if _matches_topic_filter(str(row.get("topic") or ""), topic_filter)]
    return Response(
        content=_build_topics_xlsx(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="mqtt-capture-{run_id}.xlsx"'},
    )


def _build_topics_xlsx(rows: list[dict[str, object]]) -> bytes:
    """Build an XLSX of the capture rows, columns mirroring the panel CSV."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "MQTT Capture"
    sheet.append(["Topic", "Asset", "Last Seen", "Message Count", "Latest Payload"])
    for row in rows:
        attributes = row.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        last_payload = row.get("last_payload")
        payload_text = (
            json.dumps(last_payload) if isinstance(last_payload, dict) and last_payload else ""
        )
        sheet.append(
            [
                _xlsx_cell(row.get("topic")),
                _xlsx_cell(attributes.get("device_ref")),
                _xlsx_cell(row.get("created_at")),
                _xlsx_cell(row.get("message_count")),
                payload_text,
            ]
        )
    for column, width in {"A": 40, "B": 20, "C": 24, "D": 14, "E": 60}.items():
        sheet.column_dimensions[column].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_cell(value: object) -> str:
    if value is None or value == "":
        return ""
    return value if isinstance(value, str) else str(value)


def _matches_topic_filter(topic: str, pattern: str) -> bool:
    """MQTT wildcard match mirroring the frontend matchesTopicFilter (discoveryRows.ts)."""
    trimmed = pattern.strip()
    if trimmed in ("", "#"):
        return True
    filter_parts = trimmed.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part == "+":
            continue
        if part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


def _load_discovery_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in DISCOVERY_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' was not found.")
    return run
