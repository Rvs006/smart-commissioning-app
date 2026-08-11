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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from openpyxl import Workbook
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.db.run_lifecycle import (
    DiscoveryObservationIntegrityError,
    ProtocolConflictError,
    RunLifecycleRepository,
    ScanAuthorizationError,
)
from smart_commissioning_core.engines.bacnet_discovery import process_bacnet_discovery_run
from smart_commissioning_core.engines.base import ThrottleConfig
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import (
    DEFAULT_CAPTURE_SECONDS,
    process_mqtt_discovery_run,
)
from smart_commissioning_core.engines.safety import is_authorized
from smart_commissioning_core.mqtt_settings import INDEFINITE_BACKSTOP_SECONDS, parse_capture_seconds
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.run_lifecycle import ScanAuthorizationV1, TerminalResultV1

from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.config import get_settings
from app.core.scopes import (
    allowed_scope_pairs,
    load_scoped_run,
    require_global_admin,
    require_project_site_access,
)
from app.schemas.jobs import (
    DiscoveryObservationPageResponse,
    DiscoveryObservationRecord,
    DiscoveryObservationTerminal,
    DiscoveryPointsResponse,
    DiscoveryResultsResponse,
    DiscoveryTopicsResponse,
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunListResponse,
    RunRecord,
)
from app.schemas.scan_authorizations import (
    CreateScanAuthorizationRequest,
    RevokeScanAuthorizationRequest,
)
from app.services import interface_service
from app.services.configuration_service import ConfigurationService
from app.services.discovery_contract_service import (
    resolve_bacnet_discovery_parameters,
    resolve_ip_discovery_parameters,
)
from app.services.engine_dispatch import (
    build_throttle,
    is_dry_run,
    make_cancel_checker,
    resolve_bacnet_backend,
    resolve_ip_enrichment,
    resolve_source_interface,
)
from app.services.job_queue import JobQueueService
from app.services.register_topics import expected_topic_filters
from app.services.retention_service import (
    ObservationRetentionConflictError,
    ObservationRetentionService,
)
from app.services.run_dispatch import dispatch_run
from app.services.run_service import DISCOVERY_JOB_TYPES, RunService
from app.services.scan_authorization_service import ScanAuthorizationService

router = APIRouter()
service = RunService()
queue_service = JobQueueService()
config_service = ConfigurationService()
lifecycle_repository = RunLifecycleRepository(service.engine)
observation_retention_service = ObservationRetentionService(service.engine)

# RBAC: reading discovery data is viewer+; creating/running a discovery job
# (which can drive a real network scan) is engineer+. The separate scan
# authorization consent (parameters.authorized / scan_authorization) still
# applies on top of this — RBAC is WHO may act, scan-auth is the safety consent.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)

# Hard ceiling on an explicit MQTT capture window: 48 hours. Sourced from the
# single shared core constant (also the backstop for a blank/indefinite capture)
# so this route cap, validation.py MAX_UDMI_CAPTURE_SECONDS, and the engines can
# never drift. MUST stay strictly BELOW the discover_mqtt actor's time_limit
# (worker/app/tasks.py runs at cap + 1h) — else a maximum-length accepted capture
# would be killed at the wire by its own executor. The contract is asserted in
# backend/tests/test_mqtt_capture_window.py.
MQTT_MAX_CAPTURE_SECONDS = int(INDEFINITE_BACKSTOP_SECONDS)


# Actionable message returned when a real scan lacks authorization. Mirrors the
# safety module's contract so the operator knows exactly how to authorize.
_SCAN_AUTH_DETAIL = (
    "Active network scan requires a sealed preview and one-use scan authorization. "
    "Create a dry_run preview first, approve that exact packet plan, then provide "
    "preview_run_id and scan_authorization_id with an empty parameters object."
)


def _settings_throttle(parameters: dict) -> ThrottleConfig:
    settings = get_settings()
    return build_throttle(
        parameters,
        max_concurrency=settings.scan_max_concurrency,
        rate_limit_per_sec=settings.scan_rate_limit_per_sec,
        connect_timeout_s=settings.scan_connect_timeout_s,
    )


def _prepare_scan_parameters(
    request: JobCreateRequest,
    *,
    expected_job_type: JobType,
    principal: AuthPrincipal,
) -> tuple[dict[str, object], bool]:
    """Separate a side-effect-free preview from a preview-bound live start."""

    parameters = dict(request.parameters)
    dry_run = is_dry_run(parameters)
    has_preview = bool(request.preview_run_id)
    has_authorization = bool(request.scan_authorization_id)
    if dry_run:
        if has_preview or has_authorization:
            raise HTTPException(
                status_code=400,
                detail="A dry preview cannot consume a scan authorization.",
            )
        return parameters, True
    if not has_preview or not has_authorization:
        raise HTTPException(status_code=400, detail=_SCAN_AUTH_DETAIL)
    if parameters:
        raise HTTPException(
            status_code=400,
            detail=(
                "A live scan must use the exact sealed preview. Send an empty "
                "parameters object with preview_run_id and scan_authorization_id."
            ),
        )
    try:
        resolved = ScanAuthorizationService(service).prepare_live_parameters(
            preview_run_id=str(request.preview_run_id),
            authorization_id=str(request.scan_authorization_id),
            project_id=request.project_id,
            site_id=request.site_id,
            job_type=expected_job_type,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Sealed preview or authorization was not found.") from error
    except ScanAuthorizationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _verify_frozen_control_identity(resolved, principal)
    return resolved, False


def _bind_control_identity(parameters: dict[str, object], principal: AuthPrincipal) -> None:
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, dict):
        raise HTTPException(status_code=400, detail="scan_contract_v1 is missing")
    contract["initiating_user_id"] = principal.user_id
    contract["principal_source"] = principal.source
    contract["deployment_role"] = get_settings().deployment_role
    packet_plan = {key: value for key, value in contract.items() if key != "packet_plan_sha256"}
    contract["packet_plan_sha256"] = canonical_sha256(packet_plan)


def _bind_retry_relation(
    parameters: dict[str, object],
    *,
    request: JobCreateRequest,
    principal: AuthPrincipal,
) -> None:
    """Freeze an optional retry parent into the packet plan and relational context."""
    parent_run_id = str(parameters.pop("retry_parent_run_id", "") or "").strip()
    if not parent_run_id:
        return
    scoped = load_scoped_run(parent_run_id, principal, engine=service.engine)
    if (scoped.project_id, scoped.site_id) != (request.project_id, request.site_id):
        raise HTTPException(status_code=404, detail="Run not found.")
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, dict):
        raise HTTPException(status_code=400, detail="scan_contract_v1 is missing")
    contract["relation_snapshot"] = {
        "relation": "retry",
        "parent_run_id": parent_run_id,
    }


def _verify_frozen_control_identity(parameters: dict[str, object], principal: AuthPrincipal) -> None:
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, dict):
        raise HTTPException(status_code=409, detail="sealed preview is missing control identity")
    expected = (
        contract.get("initiating_user_id"),
        contract.get("principal_source"),
        contract.get("deployment_role"),
    )
    current = (principal.user_id, principal.source, get_settings().deployment_role)
    if expected != current:
        raise HTTPException(
            status_code=409,
            detail=(
                "Live scan must be started by the same scoped principal and "
                "deployment role that created the sealed preview."
            ),
        )


def _require_legacy_scan_authorization(parameters: dict[str, object]) -> None:
    """Temporary compatibility gate for MQTT while its contract is migrated."""

    if is_dry_run(parameters):
        return
    if not is_authorized(parameters):
        raise HTTPException(status_code=403, detail=_SCAN_AUTH_DETAIL)


def _stamp_legacy_authorizer(parameters: dict[str, object], principal: AuthPrincipal) -> None:
    if is_dry_run(parameters) or not is_authorized(parameters):
        return
    existing = parameters.get("scan_authorization")
    existing = existing if isinstance(existing, dict) else {}
    parameters["scan_authorization"] = {
        **existing,
        "authorized": True,
        "authorized_by": principal.username,
    }


def _create_run(
    request: JobCreateRequest,
    expected_job_type: JobType,
    principal: AuthPrincipal,
) -> RunRecord:
    try:
        return service.create_job_run(
            request,
            expected_job_type=expected_job_type,
            requesting_principal=principal.username,
        )
    except ProtocolConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A run already owns this protocol connection.",
                "active_run_id": error.active_run_id,
            },
        ) from error
    except ScanAuthorizationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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
        defaults = {
            **config_service.bacnet_transport_defaults(project_id, site_id),
            **config_service.bacnet_internetwork_defaults(project_id, site_id),
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    for key, value in defaults.items():
        parameters.setdefault(key, value)


def _guard_frozen_source_interface(parameters: dict[str, object]) -> None:
    """Validate the preview's selected NIC without consulting changed config."""

    source_ip = str(parameters.get("source_ip") or "").strip()
    if not source_ip:
        return
    try:
        interface_service.ensure_source_ip_available(source_ip)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post(
    "/scan-authorizations",
    response_model=ScanAuthorizationV1,
    status_code=201,
)
def create_scan_authorization(
    request: CreateScanAuthorizationRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> ScanAuthorizationV1:
    load_scoped_run(request.preview_run_id, principal, engine=service.engine)
    try:
        return ScanAuthorizationService(service).create(
            preview_run_id=request.preview_run_id,
            approved_by=principal.username,
            ticket=request.ticket,
            purpose=request.purpose,
            not_before=request.not_before,
            not_after=request.not_after,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Sealed preview was not found.") from error
    except ScanAuthorizationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get(
    "/scan-authorizations",
    response_model=list[ScanAuthorizationV1],
)
def list_scan_authorizations(
    project_id: str | None = None,
    site_id: str | None = None,
    preview_run_id: str | None = None,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> list[ScanAuthorizationV1]:
    return ScanAuthorizationService(service).list(
        project_id=project_id,
        site_id=site_id,
        preview_run_id=preview_run_id,
    )


def _effective_throttle(parameters: dict[str, object]) -> dict[str, object]:
    throttle = _settings_throttle(parameters)
    return {
        "max_concurrency": throttle.max_concurrency,
        "rate_limit_per_sec": throttle.rate_limit_per_sec,
        "connect_timeout_s": throttle.connect_timeout_s,
    }


@router.get(
    "/scan-authorizations/{authorization_id}",
    response_model=ScanAuthorizationV1,
)
def get_scan_authorization(
    authorization_id: str,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> ScanAuthorizationV1:
    try:
        return ScanAuthorizationService(service).get(authorization_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Scan authorization was not found.") from error


@router.post(
    "/scan-authorizations/{authorization_id}/revoke",
    response_model=ScanAuthorizationV1,
)
def revoke_scan_authorization(
    authorization_id: str,
    request: RevokeScanAuthorizationRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> ScanAuthorizationV1:
    try:
        return ScanAuthorizationService(service).revoke(
            authorization_id,
            revoked_by=principal.username,
            reason=request.reason,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Scan authorization was not found.") from error
    except ScanAuthorizationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/ip/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_ip_discovery_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)
    if request.preview_run_id:
        load_scoped_run(request.preview_run_id, principal, engine=service.engine)
    parameters, preview = _prepare_scan_parameters(request, expected_job_type="ip_discovery", principal=principal)
    if preview:
        resolve_ip_enrichment(parameters)
        _resolve_source_interface(request.project_id, request.site_id, parameters)
        try:
            parameters = resolve_ip_discovery_parameters(
                parameters,
                project_id=request.project_id,
                site_id=request.site_id,
                import_repository=ImportRepository(service.engine),
                effective_throttle=_effective_throttle(parameters),
            )
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail="Selected IP register was not found.",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        _bind_retry_relation(
            parameters,
            request=request,
            principal=principal,
        )
        _bind_control_identity(parameters, principal)
    else:
        _guard_frozen_source_interface(parameters)
    run = _create_run(request.model_copy(update={"parameters": parameters}), "ip_discovery", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_ip_discovery_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_devices,
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
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)
    if request.preview_run_id:
        load_scoped_run(request.preview_run_id, principal, engine=service.engine)
    parameters, preview = _prepare_scan_parameters(request, expected_job_type="bacnet_discovery", principal=principal)
    if preview:
        _resolve_source_interface(request.project_id, request.site_id, parameters)
        _resolve_bacnet_transport(request.project_id, request.site_id, parameters)
        try:
            parameters = resolve_bacnet_discovery_parameters(
                parameters,
                project_id=request.project_id,
                site_id=request.site_id,
                import_repository=ImportRepository(service.engine),
                effective_throttle=_effective_throttle(parameters),
            )
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail="Selected BACnet authority was not found.",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        _bind_retry_relation(
            parameters,
            request=request,
            principal=principal,
        )
        _bind_control_identity(parameters, principal)
    else:
        _guard_frozen_source_interface(parameters)
    # HONESTY: an authorized real BACnet scan defaults to the real bacpypes3
    # backend so it ATTEMPTS real discovery (never silently returns simulated
    # data). Persisted into run.parameters BEFORE _create_run so both the inline
    # and worker paths select the same backend. Dry runs may use simulation;
    # unsafe or unknown live selectors are rejected before a run is created.
    try:
        resolve_bacnet_backend(parameters)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    run = _create_run(request.model_copy(update={"parameters": parameters}), "bacnet_discovery", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_bacnet_discovery_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_devices_and_points,
            is_cancelled=make_cancel_checker(run_store, run.run_id),
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
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)
    parameters = dict(request.parameters)
    _require_legacy_scan_authorization(parameters)
    _stamp_legacy_authorizer(parameters, principal)
    # Reject an over-cap capture window BEFORE creating the run (UDMI precedent
    # routes/validation.py) so a rejected request never leaves an orphaned run.
    # None (0/blank = indefinite) passes: it runs until Stop run, the distinct-
    # topic cap, or the 48-hour backstop the engine applies to the transport.
    capture_seconds = parse_capture_seconds(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS)
    if capture_seconds is not None and capture_seconds > MQTT_MAX_CAPTURE_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"capture_seconds may be at most {MQTT_MAX_CAPTURE_SECONDS} seconds "
                "(48 hours); the worker would kill a longer capture at its time limit."
            ),
        )
    # Inherit only QoS from saved config; the run's topic scope is chosen per run
    # on the MQTT Discovery page (a blank filter means the engine's own "#"
    # capture-all default). The saved Root Topic field was removed (ITEM-2), so
    # nothing is injected here for topics.
    parameters.setdefault("qos", config_service.mqtt_subscribe_defaults(request.project_id, request.site_id)["qos"])
    _bind_mqtt_register(request.project_id, request.site_id, parameters)
    _resolve_source_interface(request.project_id, request.site_id, parameters)
    run = _create_run(request.model_copy(update={"parameters": parameters}), "mqtt_discovery", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        # live_capture defaults to the real raw-socket subscribe_and_capture; in
        # the API process broker egress may be absent, but the engine honestly
        # records 'broker_unreachable'/'live_capture_unavailable' rather than
        # faking success, so we keep the real default here.
        return process_mqtt_discovery_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_topics,
            # Synchronous inline (INLINE_RUN_ASYNC=0) blocks this request until the
            # run finishes, so the client never gets a run_id to reach Stop run — a
            # blank window must bound itself. Async inline is backgrounded (ITEM-4).
            run_is_backgrounded=get_settings().inline_run_async,
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_for("discover_mqtt", "discovery"),
        run_inline=run_inline,
        label="MQTT discovery",
    )


def _dispatch(run: RunRecord, *, enqueue, run_inline, label: str) -> JobAcceptedResponse:
    return dispatch_run(
        run,
        service=service,
        enqueue=enqueue,
        run_inline=run_inline,
        inline_message=f"{label} run started (local inline execution). Follow progress in the run monitor.",
        queued_message=f"{label} job queued for worker execution.",
    )


@router.get("/runs", response_model=RunListResponse, dependencies=[Depends(require_viewer)])
def list_discovery_runs(
    principal: AuthPrincipal = Depends(get_principal),
) -> RunListResponse:
    return RunListResponse(
        runs=service.list_runs(
            job_types=DISCOVERY_JOB_TYPES,
            scope_pairs=allowed_scope_pairs(principal, engine=service.engine),
        )
    )


@router.get("/runs/{run_id}", response_model=RunRecord, dependencies=[Depends(require_viewer)])
def get_discovery_run(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> RunRecord:
    run = _load_discovery_run(run_id, principal)
    return run


@router.get(
    "/runs/{run_id}/observations",
    response_model=DiscoveryObservationPageResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_observations(
    run_id: str,
    response: Response,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    principal: AuthPrincipal = Depends(get_principal),
) -> DiscoveryObservationPageResponse:
    """Return one scoped, viewer-safe progressive observation cursor page."""

    scoped = load_scoped_run(
        run_id,
        principal,
        engine=service.engine,
        query_only=True,
    )
    try:
        run = service.get_run_read_only(run_id)
        attempt = service.get_run_attempt_read_only(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Discovery run not found.") from error
    if run.job_type not in {"ip_discovery", "bacnet_discovery"}:
        raise HTTPException(status_code=404, detail="Discovery run not found.")

    try:
        terminal_marker = service.get_terminal_observation_marker_read_only(run_id)
    except RuntimeError as error:
        raise HTTPException(
            status_code=409,
            detail="Stored discovery observation evidence failed integrity verification.",
        ) from error
    if attempt == 0:
        if after > 0:
            raise HTTPException(
                status_code=409,
                detail="Observation cursor is no longer valid.",
            )
        response.headers["Cache-Control"] = "no-store"
        return DiscoveryObservationPageResponse(
            run_id=run_id,
            attempt=1,
            observations=[],
            next_cursor=0,
            latest_cursor=0,
            has_more=False,
            observations_pruned=False,
            terminal=(
                DiscoveryObservationTerminal(
                    status=terminal_marker["status"],
                    terminal_cursor=0,
                )
                if terminal_marker is not None
                else None
            ),
        )

    cutoff = lifecycle_repository.get_discovery_observation_cutoff(
        run_id,
        attempt,
        project_id=scoped.project_id,
        site_id=scoped.site_id,
    )
    observations_quarantined = bool(
        terminal_marker is not None and terminal_marker.get("observations_quarantined") is True
    )
    observations_pruned = False
    if terminal_marker is not None and not observations_quarantined:
        missing_observation_count = int(terminal_marker["observation_count"]) - cutoff.observation_count
        if missing_observation_count < 0:
            raise HTTPException(
                status_code=409,
                detail="Stored discovery observation evidence failed integrity verification.",
            )
        if missing_observation_count > 0:
            try:
                attested_deletion_count = (
                    observation_retention_service.get_attested_observation_deletion_count(
                        run_id=run_id,
                        project_id=scoped.project_id,
                        site_id=scoped.site_id,
                        attempt=int(terminal_marker["attempt"]),
                        terminal_cursor=int(terminal_marker["terminal_cursor"]),
                        observation_count=int(terminal_marker["observation_count"]),
                        observation_stream_sha256=str(
                            terminal_marker["observation_stream_sha256"]
                        ),
                    )
                )
            except ObservationRetentionConflictError as error:
                raise HTTPException(
                    status_code=409,
                    detail="Stored discovery observation evidence failed integrity verification.",
                ) from error
            if attested_deletion_count != missing_observation_count:
                raise HTTPException(
                    status_code=409,
                    detail="Stored discovery observation evidence failed integrity verification.",
                )
            observations_pruned = True
    latest_cursor = int(terminal_marker["terminal_cursor"]) if terminal_marker is not None else cutoff.terminal_cursor
    if after > latest_cursor:
        raise HTTPException(status_code=409, detail="Observation cursor is no longer valid.")

    try:
        requested_rows = (
            []
            if observations_quarantined
            else lifecycle_repository.list_discovery_observations(
                run_id,
                cutoff.attempt,
                after_cursor=after,
                limit=limit,
                project_id=scoped.project_id,
                site_id=scoped.site_id,
            )
        )
    except DiscoveryObservationIntegrityError as error:
        raise HTTPException(
            status_code=409,
            detail="Stored discovery observation evidence failed integrity verification.",
        ) from error
    observations = [DiscoveryObservationRecord.model_validate(item.model_dump(mode="json")) for item in requested_rows]
    if terminal_marker is not None:
        cutoff_after_page = lifecycle_repository.get_discovery_observation_cutoff(
            run_id,
            attempt,
            project_id=scoped.project_id,
            site_id=scoped.site_id,
        )
        if cutoff_after_page != cutoff:
            raise HTTPException(
                status_code=409,
                detail="Stored discovery observation evidence failed integrity verification.",
            )
    next_cursor = observations[-1].cursor if observations else after
    response.headers["Cache-Control"] = "no-store"
    terminal = (
        DiscoveryObservationTerminal(
            status=terminal_marker["status"],
            terminal_cursor=int(terminal_marker["terminal_cursor"]),
        )
        if terminal_marker is not None
        else None
    )
    return DiscoveryObservationPageResponse(
        run_id=run_id,
        attempt=cutoff.attempt,
        observations=observations,
        next_cursor=next_cursor,
        latest_cursor=latest_cursor,
        has_more=not observations_quarantined and not observations_pruned and next_cursor < latest_cursor,
        observations_pruned=observations_pruned,
        observations_quarantined=observations_quarantined,
        terminal=terminal,
    )


@router.get(
    "/runs/{run_id}/results",
    response_model=DiscoveryResultsResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_results(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> DiscoveryResultsResponse:
    run = _load_discovery_run(run_id, principal)
    terminal = _verified_discovery_terminal(run)

    result_summary = dict(terminal.summary) if terminal is not None else run.result_summary
    discovered_assets = result_summary.get("discovered_assets", [])
    if not isinstance(discovered_assets, list):
        discovered_assets = []

    repository = _discovery_repository()
    devices = list(terminal.devices) if terminal is not None else repository.list_devices(run_id)
    points = list(terminal.points) if terminal is not None else repository.list_points(run_id)
    topic_rows = list(terminal.topics) if terminal is not None else repository.list_topics(run_id)
    authoritative_run = run.model_copy(update={"result_summary": result_summary})
    topics, register_comparison = _annotate_register_matches(authoritative_run, topic_rows)
    return DiscoveryResultsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        result_summary=result_summary,
        discovered_assets=discovered_assets,
        devices=devices,
        points=points,
        topics=topics,
        register_comparison=register_comparison,
    )


@router.get(
    "/runs/{run_id}/points",
    response_model=DiscoveryPointsResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_points(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> DiscoveryPointsResponse:
    run = _load_discovery_run(run_id, principal)
    terminal = _verified_discovery_terminal(run)
    return DiscoveryPointsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        points=(list(terminal.points) if terminal is not None else _discovery_repository().list_points(run_id)),
    )


@router.get(
    "/runs/{run_id}/topics",
    response_model=DiscoveryTopicsResponse,
    dependencies=[Depends(require_viewer)],
)
def get_discovery_topics(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> DiscoveryTopicsResponse:
    run = _load_discovery_run(run_id, principal)
    terminal = (
        None
        if run.job_type == "mqtt_discovery" and run.status not in {"succeeded", "failed", "cancelled"}
        else _verified_discovery_terminal(run)
    )
    result_summary = dict(terminal.summary) if terminal is not None else run.result_summary
    authoritative_run = run.model_copy(update={"result_summary": result_summary})
    topics, register_comparison = _annotate_register_matches(
        authoritative_run,
        (list(terminal.topics) if terminal is not None else _discovery_repository().list_topics(run_id)),
    )
    return DiscoveryTopicsResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        topics=topics,
        register_comparison=register_comparison,
    )


@router.get("/runs/{run_id}/topics.xlsx", dependencies=[Depends(require_viewer)])
def export_discovery_topics_xlsx(
    run_id: str,
    topic_filter: str | None = None,
    principal: AuthPrincipal = Depends(get_principal),
) -> Response:
    """Export the captured latest-payload-per-topic rows as an XLSX (mq9nhbzu).

    Reuses the same persisted topic rows the capture panel/CSV use (no live
    broker), generated server-side with openpyxl like reports/import templates,
    so empty stays empty (no fabricated payloads). An optional ``topic_filter``
    applies the same ``+``/``#`` wildcard semantics as the on-screen filter so
    the export matches what the operator sees.
    """
    run = _load_discovery_run(run_id, principal)
    terminal = (
        None
        if run.job_type == "mqtt_discovery" and run.status not in {"succeeded", "failed", "cancelled"}
        else _verified_discovery_terminal(run)
    )
    rows = list(terminal.topics) if terminal is not None else _discovery_repository().list_topics(run_id)
    if topic_filter:
        rows = [row for row in rows if _matches_topic_filter(str(row.get("topic") or ""), topic_filter)]
    # Annotate the (already filter-narrowed) rows with the same register-match
    # verdict the on-screen table uses, so the export carries the "Register
    # Match" column (appended last, so existing column indexes stay valid).
    rows, _ = _annotate_register_matches(run, rows)
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
    # "Register Match" is appended LAST so the existing columns (indexes 0-4)
    # and their assertions are unchanged; it is blank when no register applies.
    sheet.append(["Topic", "Asset", "Last Seen", "Message Count", "Latest Payload", "Register Match"])
    for row in rows:
        attributes = row.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        last_payload = row.get("last_payload")
        payload_text = json.dumps(last_payload) if isinstance(last_payload, dict) and last_payload else ""
        sheet.append(
            [
                _xlsx_cell(row.get("topic")),
                _xlsx_cell(attributes.get("device_ref")),
                _xlsx_cell(row.get("created_at")),
                _xlsx_cell(row.get("message_count")),
                payload_text,
                _register_match_cell(attributes),
            ]
        )
    for column, width in {"A": 40, "B": 20, "C": 24, "D": 14, "E": 60, "F": 28}.items():
        sheet.column_dimensions[column].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_cell(value: object) -> str:
    if value is None or value == "":
        return ""
    return value if isinstance(value, str) else str(value)


def _register_match_cell(attributes: dict) -> str:
    """The 'Register Match' export cell, mirroring the on-screen verdict.

    ``matched (<filter>)`` / ``not in register`` / ``""`` when the row carries no
    register verdict (no register imported, or a dry/failed run that observed
    nothing — the honesty rule keeps those blank rather than red)."""
    match = attributes.get("register_match")
    if match == "matched":
        topic_filter = attributes.get("register_matched_filter")
        return f"matched ({topic_filter})" if topic_filter else "matched"
    if match == "unmatched":
        return "not in register"
    return ""


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


def _load_discovery_run(run_id: str, principal: AuthPrincipal) -> RunRecord:
    load_scoped_run(run_id, principal, engine=service.engine)
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in DISCOVERY_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' was not found.")
    return run


def _verified_discovery_terminal(run: RunRecord) -> TerminalResultV1 | None:
    """Use the sealed RunResult as terminal authority, with narrow legacy fallback."""

    if run.status not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Terminal discovery results are not ready.",
        )
    try:
        return service.get_verified_terminal_result(run.run_id)
    except (RuntimeError, ValueError) as error:
        raise HTTPException(
            status_code=409,
            detail="Stored discovery evidence failed integrity verification.",
        ) from error


def _bind_mqtt_register(project_id: str, site_id: str, parameters: dict[str, object]) -> None:
    """Freeze the newest usable MQTT register into the run parameters."""

    # The route owns this binding. A caller cannot smuggle an import from
    # another workspace by supplying its id in the request body.
    parameters.pop("mqtt_register_import_id", None)
    imports = ImportRepository(service.engine).list(project_id=project_id, site_id=site_id, import_type="mqtt_register")
    for record in imports:
        if record.get("accepted_rows"):
            parameters["mqtt_register_import_id"] = str(record["import_id"])
            return


def _mqtt_register_filters(
    run: RunRecord,
) -> tuple[list[tuple[str, str]], str | None]:
    """Return filters from the exact register bound when this run was created."""

    raw_import_id = run.parameters.get("mqtt_register_import_id")
    if not isinstance(raw_import_id, str) or not raw_import_id:
        return [], None
    repository = ImportRepository(service.engine)
    try:
        record = repository.get(raw_import_id)
    except FileNotFoundError:
        return [], None
    if (
        record.get("import_type") != "mqtt_register"
        or record.get("project_id") != run.project_id
        or record.get("site_id") != run.site_id
    ):
        return [], None
    rows = record.get("accepted_rows", [])
    if isinstance(rows, list) and rows:
        return expected_topic_filters(rows), str(record.get("original_filename") or "")
    return [], None


def _annotate_register_matches(run: RunRecord, topics: list[dict]) -> tuple[list[dict], dict | None]:
    """Stamp observed topics using the register frozen into the run at creation.

    Verdicts are derived on GET from the exact import bound at run creation.
    The binding is immutable; later imports cannot rewrite completed evidence.

    HONESTY RULE, applied deliberately:
      * Only a SUCCEEDED, non-dry mqtt_discovery run is compared. A dry run sent
        no packets and a failed run observed nothing, so stamping an "expected but
        unobserved" verdict there would fabricate an observation.
      * With NO register we return ``{"register_available": False}`` and DO NOT
        stamp any row — no template means no verdict; we never mark everything red.
      * "unobserved" filters are reported as a summary count, never as absence of a
        device: a silent topic is not proof the device is gone.
    """
    if not (
        run.job_type == "mqtt_discovery" and run.status == "succeeded" and run.result_summary.get("dry_run") is not True
    ):
        return topics, None
    filters, filename = _mqtt_register_filters(run)
    if not filters:
        return topics, {"register_available": False}

    annotated: list[dict] = []
    matched_count = 0
    unmatched_count = 0
    matched_filters: set[str] = set()
    for topic in topics:
        row = dict(topic)
        raw_attributes = row.get("attributes")
        attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}
        topic_str = str(row.get("topic") or "")
        match: tuple[str, str] | None = None
        for asset_id, topic_filter in filters:  # concrete-before-wildcard, first wins
            if _matches_topic_filter(topic_str, topic_filter):
                match = (asset_id, topic_filter)
                break
        if match is not None:
            asset_id, topic_filter = match
            attributes["register_match"] = "matched"
            attributes["register_matched_filter"] = topic_filter
            attributes["register_asset_id"] = asset_id
            matched_filters.add(topic_filter)
            matched_count += 1
        else:
            attributes["register_match"] = "unmatched"
            unmatched_count += 1
        row["attributes"] = attributes
        annotated.append(row)

    unobserved = [
        {"asset_id": asset_id, "filter": topic_filter}
        for asset_id, topic_filter in filters
        if topic_filter not in matched_filters
    ]
    summary = {
        "register_available": True,
        "import_filename": filename,
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "expected_filter_count": len(filters),
        "unobserved_filters": unobserved,
    }
    return annotated, summary
