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
from smart_commissioning_core.engines.bacnet_params import (
    PARAM_BACNET_TARGETS,
    TARGET_ADDRESS,
    TARGET_ASSET_ID,
    TARGET_ASSET_NAME,
    TARGET_DEVICE_INSTANCE,
    TARGET_NETWORK,
    parse_targets,
)
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.safety import is_authorized
from smart_commissioning_core.rbac import Role

from app.core.auth import AuthPrincipal, get_principal, require_role
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
from app.services import interface_service
from app.services.configuration_service import ConfigurationService
from app.services.engine_dispatch import (
    build_throttle,
    is_dry_run,
    make_cancel_checker,
    make_device_persister,
    make_device_point_persister,
    make_topic_persister,
    resolve_bacnet_backend,
    resolve_ip_enrichment,
    resolve_source_interface,
)
from app.services.job_queue import JobQueueService, JobQueueUnavailable
from app.services.run_dispatch import dispatch_run
from app.services.run_service import DISCOVERY_JOB_TYPES, RunService

router = APIRouter()
service = RunService()
queue_service = JobQueueService()
config_service = ConfigurationService()

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


def _stamp_authorizer(parameters: dict, principal: AuthPrincipal) -> None:
    """Record the REAL authenticated principal as ``scan_authorization.authorized_by``
    on an authorized real scan, so the audit trail names who actually authorized
    the run instead of a hard-coded client label. Any operator-supplied note /
    authorized_at (and other keys) are preserved; only authorized/authorized_by
    are asserted from the server side. Dry runs and unauthorized requests are left
    untouched — a dry run needs no authorization, so stamping one would imply a
    consent that was never required.
    """
    if is_dry_run(parameters) or not is_authorized(parameters):
        return
    existing = parameters.get("scan_authorization")
    existing = existing if isinstance(existing, dict) else {}
    parameters["scan_authorization"] = {
        **existing,
        "authorized": True,
        "authorized_by": principal.username,
    }


def _create_run(request: JobCreateRequest, expected_job_type: JobType) -> RunRecord:
    try:
        return service.create_job_run(request, expected_job_type=expected_job_type)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _discovery_repository() -> DiscoveryRepository:
    return DiscoveryRepository(service.engine)


def _configured_source_interface(project_id: str, site_id: str) -> str | None:
    """The saved device."Source Interface" value (source-NIC selection), or None.

    Reads the same saved config snapshot the MQTT defaults use; an empty / absent
    value means "Auto (OS default route)" and is normalised to None so the
    resolver treats it as a no-op (OS picks the egress interface).
    """
    values = config_service.load(project_id, site_id).device.values
    return str(values.get("Source Interface") or "").strip() or None


def _resolve_source_interface(project_id: str, site_id: str, parameters: dict) -> None:
    """Inject the configured source NIC (source_ip / local_address) into the run
    parameters BEFORE the run is persisted, so the inline and worker paths both
    bind their active-scan sockets to it, then guard that the EFFECTIVE source_ip
    (configured value or an operator run-parameter override) is still present and
    up on this host. A malformed or unavailable value surfaces as a 400 at run
    creation, matching the other validation failures on these routes — no
    orphaned run is persisted and the run NEVER silently falls back to another NIC.

    Dry runs skip the availability guard deliberately (side-effect-free preview
    convention). A NIC dropping between run creation and worker pickup is still
    caught by the engine-level honesty checks (ip_scan bind pre-check, MQTT
    OSError -> the broker_unreachable family, BACnet _ensure_app RuntimeError).
    """
    try:
        resolve_source_interface(parameters, _configured_source_interface(project_id, site_id))
        if not is_dry_run(parameters) and parameters.get("source_ip"):
            interface_service.ensure_source_ip_available(str(parameters["source_ip"]))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


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


def _resolve_bacnet_transport(project_id: str, site_id: str, parameters: dict) -> None:
    """Inject the saved BACnet transport config (foreign-device registration) into
    the run parameters BEFORE the run is persisted, mirroring the MQTT route's
    ``mqtt_subscribe_defaults`` + ``setdefault`` shape.

    ``setdefault`` per key, so an operator run-parameter override wins — consistent
    with source_ip / local_address / qos on these routes. Nothing is injected
    unless the saved config has Foreign Device = Enabled, so a default install's
    run parameters are unchanged and discovery stays local-broadcast exactly as
    it behaves today.

    An unusable BBMD Address surfaces as a 400 here, at run creation, with a
    fix-your-config message — no orphaned run is persisted, and the run never
    silently falls back to broadcast (that silent fallback IS the bug this
    release fixes: a run the operator asked to send through a BBMD would report a
    clean, empty, local scan).

    Dry runs are deliberately NOT skipped: the plan must echo the same resolved
    transport the real scan would use, so the transport can be verified before a
    single packet is sent.
    """
    try:
        defaults = config_service.bacnet_transport_defaults(project_id, site_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    for key, value in defaults.items():
        parameters.setdefault(key, value)


def _bacnet_register_targets(record: dict) -> list[dict]:
    """Contract-shaped target rows from one bacnet_register import's accepted rows.

    Maps the register's column names onto the shared row keys, then delegates ALL
    normalisation (types, bounds, dedupe on (address, device_instance), first-seen
    order) to the contract's ``parse_targets`` — the same function the engine reads
    back with, which is what stops the write and the read from drifting apart.

    Malformed rows are SKIPPED, never fatal: a legacy import predating the
    numeric/IP validators must not turn a scan into a 500. ``parse_targets`` never
    raises on JSON-shaped input, and the ``isinstance`` guard here keeps a
    non-dict row from breaking the ``.get`` before it gets there.
    """
    return [
        target.as_dict()
        for target in parse_targets(
            {
                TARGET_ADDRESS: row.get("IP address"),
                TARGET_DEVICE_INSTANCE: row.get("BACnet device instance"),
                TARGET_ASSET_ID: row.get("Asset ID"),
                TARGET_ASSET_NAME: row.get("Asset name"),
                TARGET_NETWORK: row.get("BACnet network"),
            }
            for row in record.get("accepted_rows", [])
            if isinstance(row, dict)
        )
    ]


def _ensure_bacnet_targets(project_id: str, site_id: str, parameters: dict) -> None:
    """Resolve the expected-device list for a BACnet discovery run from the newest
    accepted ``bacnet_register`` import, so the engine can probe registered devices
    directly and report which expected devices stayed silent.

    An operator-supplied ``bacnet_targets`` wins untouched, like every other
    resolver on these routes.

    NO REGISTER IS NOT AN ERROR — unlike :func:`_ensure_ip_targets`, which 400s
    because an IP sweep with no targets has nothing to do at all. BACnet
    broadcast-only discovery is a legitimate scan: with no register the targets
    are simply absent and the run proceeds on the broadcast (and, if configured,
    foreign-device) lanes. Do not copy the IP route's 400 here.
    """
    if parameters.get(PARAM_BACNET_TARGETS):
        return
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="bacnet_register"
    )
    for record in imports:  # newest-first
        if targets := _bacnet_register_targets(record):
            parameters[PARAM_BACNET_TARGETS] = targets
            return


def _ip_register_by_address(project_id: str, site_id: str, column: str) -> dict[str, str]:
    """``{Expected IP address: <column>}`` from the newest ip_register import that
    has any non-empty value in ``column`` (every accepted row has an IP address)."""
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="ip_register"
    )
    for record in imports:  # newest-first
        by_address = {
            address: value
            for row in record.get("accepted_rows", [])
            if (value := str(row.get(column, "") or "").strip())
            and (address := str(row.get("Expected IP address", "") or "").strip())
        }
        if by_address:
            return by_address
    return {}


def _resolve_forbidden_ports(project_id: str, site_id: str, parameters: dict) -> None:
    """Forbidden ports from the register's "Ports that should not be enabled": a
    per-asset ``forbidden_ports_by_address`` map (engine flags each host against its
    own set) plus a global ``forbidden_ports`` union for hosts not in the map.
    Operator-supplied values win.
    """
    by_address = _ip_register_by_address(project_id, site_id, "Ports that should not be enabled")
    if not by_address:
        return
    if not parameters.get("forbidden_ports"):
        parameters["forbidden_ports"] = ",".join(by_address.values())
    parameters.setdefault("forbidden_ports_by_address", by_address)


def _resolve_expected_ports(project_id: str, site_id: str, parameters: dict) -> None:
    """Fill ``expected_ports_by_address`` from the register's "Expected services/ports"
    so the engine flags any OPEN port NOT expected for that host (needs a port range
    to be meaningful). Operator-supplied value wins.
    """
    if not parameters.get("expected_ports_by_address"):
        if by_address := _ip_register_by_address(project_id, site_id, "Expected services/ports"):
            parameters["expected_ports_by_address"] = by_address


def _resolve_expected_hostnames(project_id: str, site_id: str, parameters: dict) -> None:
    """Fill ``expected_hostname_by_address`` from the register's "Expected hostname"
    so the engine can flag a reverse-DNS result that contradicts the register
    (rows with a blank hostname are skipped by the map builder, so they can never
    mismatch). Operator-supplied value wins.
    """
    if not parameters.get("expected_hostname_by_address"):
        if by_address := _ip_register_by_address(project_id, site_id, "Expected hostname"):
            parameters["expected_hostname_by_address"] = by_address


def _resolve_asset_ids(project_id: str, site_id: str, parameters: dict) -> None:
    """Fill ``asset_id_by_address`` from the register so the live "Asset" column
    resolves each scanned host to its registered identity — the Asset ID, else
    the Asset name (asset identity is one-of). First-seen address wins
    (``setdefault``), from the newest register import that carries any identity.
    Operator-supplied value wins; a host absent from the register stays None.
    """
    if parameters.get("asset_id_by_address"):
        return
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="ip_register"
    )
    for record in imports:  # newest-first
        by_address: dict[str, str] = {}
        for row in record.get("accepted_rows", []):
            address = str(row.get("Expected IP address", "") or "").strip()
            identity = (
                str(row.get("Asset ID", "") or "").strip()
                or str(row.get("Asset name", "") or "").strip()
            )
            if address and identity:
                by_address.setdefault(address, identity)
        if by_address:
            parameters["asset_id_by_address"] = by_address
            return


@router.post("/ip/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_ip_discovery_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    # Validate authorization + resolve scan targets BEFORE creating the run, so a
    # rejected request never leaves an orphaned queued run, and the resolved
    # register addresses are persisted into the run record (the worker path reads
    # run.parameters, not just the inline dict).
    parameters = dict(request.parameters)
    _require_scan_authorization(parameters)
    _stamp_authorizer(parameters, principal)
    resolve_ip_enrichment(parameters)
    _ensure_ip_targets(request.project_id, request.site_id, parameters)
    _resolve_forbidden_ports(request.project_id, request.site_id, parameters)
    _resolve_expected_ports(request.project_id, request.site_id, parameters)
    _resolve_expected_hostnames(request.project_id, request.site_id, parameters)
    _resolve_asset_ids(request.project_id, request.site_id, parameters)
    _resolve_source_interface(request.project_id, request.site_id, parameters)
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
        enqueue=queue_service.enqueue_for("discover_ip_range", "discovery"),
        run_inline=run_inline,
        label="IP discovery",
    )


@router.post("/bacnet/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_bacnet_discovery_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    # Resolve parameters (auth check + source-NIC injection) BEFORE creating the
    # run, so the injected source_ip / local_address are persisted into
    # run.parameters for the worker path — matching the IP / MQTT routes. (BACnet
    # binds via parameters["local_address"], already consumed by the engine.)
    parameters = dict(request.parameters)
    _require_scan_authorization(parameters)
    _stamp_authorizer(parameters, principal)
    _resolve_source_interface(request.project_id, request.site_id, parameters)
    # Transport (foreign-device registration via a BBMD) and targeting (the
    # expected devices from the bacnet_register) are resolved HERE, before
    # _create_run, for the same reason source_ip is: the worker path reads the
    # PERSISTED run.parameters, not this dict. create_job_run only shallow-copies
    # what it is given, so injecting before it is what makes the inline (portable
    # exe) path and the hosted worker path run against identical parameters —
    # inject after it and the portable build would foreign-device register while
    # the worker silently broadcast.
    _resolve_bacnet_transport(request.project_id, request.site_id, parameters)
    _ensure_bacnet_targets(request.project_id, request.site_id, parameters)
    # HONESTY: an authorized real BACnet scan defaults to the real bacpypes3
    # backend so it ATTEMPTS real discovery (never silently returns simulated
    # data). Persisted into run.parameters BEFORE _create_run so both the inline
    # and worker paths select the same backend. Dry runs may use simulation;
    # unsafe or unknown live selectors are rejected before a run is created.
    try:
        resolve_bacnet_backend(parameters)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    run = _create_run(request.model_copy(update={"parameters": parameters}), "bacnet_discovery")

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
        enqueue=queue_service.enqueue_for("discover_bacnet", "discovery"),
        run_inline=run_inline,
        label="BACnet discovery",
    )


@router.post("/mqtt/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_mqtt_discovery_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    parameters = dict(request.parameters)
    _require_scan_authorization(parameters)
    _stamp_authorizer(parameters, principal)
    # Inherit Root Topic (-> default subscribe filter) and QoS from saved config
    # when the operator didn't override them on the run.
    defaults = config_service.mqtt_subscribe_defaults(request.project_id, request.site_id)
    parameters.setdefault("qos", defaults["qos"])
    if defaults.get("topic_filter") and not any(parameters.get(k) for k in ("topic_filter", "topic_prefix", "topics")):
        parameters["topic_filter"] = defaults["topic_filter"]
    _resolve_source_interface(request.project_id, request.site_id, parameters)
    run = _create_run(request.model_copy(update={"parameters": parameters}), "mqtt_discovery")

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
        enqueue=queue_service.enqueue_for("discover_mqtt", "discovery"),
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
