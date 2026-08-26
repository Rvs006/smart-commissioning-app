"""Scanner routes: the standalone loopback-sidecar scanners (job_type ``ip_scanner``).

These runs drive a vendored local scanner process (``network-ip-scanner``) that
``SidecarSupervisor`` owns on a loopback port. Unlike the sealed-preview
discovery engines, the sidecar has no worker actor and cannot run queued: this
route is INLINE-ONLY and rejects a non-inline deployment with 503, exactly like
the operator-managed Nmap provider.

Scan authorization uses the same legacy consent gate the MQTT discovery route
uses (``parameters.authorized`` / ``scan_authorization``): a real (non-dry-run)
scan needs an explicit authorization, dry-run previews are side-effect free and
pass without one. (The full sealed-preview ceremony would require a
``scan_contract_v1`` protocol key that ``engines/base`` does not yet recognize
for ``ip_scanner``; adding it is out of scope for this first cut.)

HONESTY: when the sidecar is unavailable this route returns 503 for a live scan;
if it dies mid-scan the adapter engine records a real failed run rather than
fabricating results.
"""

import logging
from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException, Request
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.bacnet_scanner_sidecar import (
    SidecarTransportError,
    browse_device_objects,
    process_bacnet_scanner_run,
)
from smart_commissioning_core.engines.ip_scanner_sidecar import (
    _register_csv as _ip_register_csv,  # shared serializer: rows -> the sidecar's 9-col CSV
)
from smart_commissioning_core.engines.ip_scanner_sidecar import (
    process_ip_scanner_run,
    register_rows_from_devices,
)
from smart_commissioning_core.engines.mqtt_scanner_sidecar import process_mqtt_scanner_run

from app.api.routes.discovery import (
    _create_run,
    _dispatch,
    _load_discovery_run,
    _require_legacy_scan_authorization,
    _resolve_source_interface,
    _settings_throttle,
    _stamp_legacy_authorizer,
    require_engineer,
    service,
)
from app.core.auth import AuthPrincipal, get_principal
from app.core.config import get_settings
from app.core.scopes import require_project_site_access
from app.schemas.imports import ImportBatchSummary
from app.schemas.jobs import (
    BacnetObjectBrowseRequest,
    BacnetObjectBrowseResponse,
    JobAcceptedResponse,
    JobCreateRequest,
)
from app.services.engine_dispatch import is_dry_run
from app.services.import_service import ImportService
from app.services.mqtt_live_session import service as live_session_service
from app.services.sidecar_supervisor import (
    BACNET_SCANNER,
    IP_SCANNER,
    MQTT_SCANNER,
    SidecarUnavailable,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _bind_scanner_register(
    project_id: str,
    site_id: str,
    parameters: dict,
    *,
    import_type: str,
    records: list[dict] | None = None,
) -> None:
    """Freeze the newest usable ``import_type`` register into the run parameters.

    Route-owned, mirroring ``discovery._bind_mqtt_register``: a caller cannot
    smuggle an import from another workspace by supplying its id in the request
    body. Without this, an uploaded (or saved-as) sidecar register was never
    bound, so the sidecar scan had nothing to RAG-compare against. ``records``
    is injectable for tests. The key ``register_import_id`` is exactly what each
    sidecar adapter's ``_load_register_rows`` reads (all three sidecars share it,
    each with its own register import type).
    """
    parameters.pop("register_import_id", None)
    if records is None:
        records = ImportRepository(service.engine).list(
            project_id=project_id, site_id=site_id, import_type=import_type
        )
    for record in records:
        if record.get("accepted_rows"):
            parameters["register_import_id"] = str(record["import_id"])
            return


def _bind_ip_scanner_register(
    project_id: str,
    site_id: str,
    parameters: dict,
    *,
    records: list[dict] | None = None,
) -> None:
    """Bind the newest accepted ``ip_scanner_register`` (see :func:`_bind_scanner_register`)."""
    _bind_scanner_register(
        project_id, site_id, parameters, import_type="ip_scanner_register", records=records
    )


@router.post("/ip_sidecar/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_ip_scanner_run(
    request: JobCreateRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)

    # Inline-only: there is no worker actor for the local sidecar, so a queued /
    # external deployment could never execute this run (operator_managed_nmap
    # precedent).
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "IP scanner runs require the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )

    parameters = dict(request.parameters)
    _require_legacy_scan_authorization(parameters)
    _stamp_legacy_authorizer(parameters, principal)
    # The adapter reads project/site off parameters for its persisted records.
    parameters.setdefault("project_id", request.project_id)
    parameters.setdefault("site_id", request.site_id)
    # Bind the newest accepted ip_scanner_register (uploaded or saved-as-register)
    # so the sidecar RAG-compares against it. Unconditional (dry-run included),
    # mirroring the mqtt register binder.
    _bind_ip_scanner_register(request.project_id, request.site_id, parameters)

    # Resolve the sidecar's loopback URL up front for a live scan so an
    # unavailable sidecar fails the request with 503 rather than starting a run
    # that can only fail. Dry-run previews perform no I/O and need no sidecar.
    base_url: str | None = None
    if not is_dry_run(parameters):
        supervisor = getattr(http_request.app.state, "sidecar_supervisor", None)
        if supervisor is None:
            raise HTTPException(status_code=503, detail="IP scanner sidecar is not available.")
        try:
            base_url = supervisor.base_url_for(IP_SCANNER)
        except SidecarUnavailable as error:
            raise HTTPException(status_code=503, detail="IP scanner sidecar is not available.") from error

    run = _create_run(request.model_copy(update={"parameters": parameters}), "ip_scanner", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_ip_scanner_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_devices,
            sidecar_base_url=base_url,
            import_loader=ImportRepository(service.engine).get_accepted_rows,
        )

    # enqueue=None keeps this strictly inline (dispatch_run runs inline for None).
    return _dispatch(run, enqueue=None, run_inline=run_inline, label="IP scanner")


def dispatch_captured_ip_scanner_run(
    *,
    project_id: str,
    site_id: str,
    principal: AuthPrincipal,
    start_ip: str,
    captured: dict,
) -> object:
    """Results-out (pipe 3): persist an already-completed embedded-panel IP scan as
    a real ``ip_scanner`` discovery run by replaying the captured ``{rows, summary}``
    through the sidecar engine with a one-shot client (no network I/O). Same
    create/dispatch path as ``create_ip_scanner_run`` above, so the Results tab, run
    history, and reports light up identically. The scan already ran in the panel, so
    ``authorized`` is stamped to the panel owner - this replays captured results, it
    does not start a new scan."""
    parameters: dict = {
        "project_id": project_id,
        "site_id": site_id,
        "authorized": True,
        "start_ip": start_ip,
    }
    _require_legacy_scan_authorization(parameters)
    _stamp_legacy_authorizer(parameters, principal)
    request = JobCreateRequest(
        project_id=project_id, site_id=site_id, job_type="ip_scanner", parameters=parameters
    )
    run = _create_run(request, "ip_scanner", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_ip_scanner_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=False,
            persist_records=run_store.replace_devices,
            sidecar_client=lambda **_: captured,
            import_loader=ImportRepository(service.engine).get_accepted_rows,
        )

    return _dispatch(run, enqueue=None, run_inline=run_inline, label="IP scanner (panel)")


@router.post("/bacnet_sidecar/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_bacnet_scanner_run(
    request: JobCreateRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)

    # Inline-only: no worker actor for the local sidecar (see create_ip_scanner_run).
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "BACnet scanner runs require the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )

    parameters = dict(request.parameters)
    _require_legacy_scan_authorization(parameters)
    _stamp_legacy_authorizer(parameters, principal)
    parameters.setdefault("project_id", request.project_id)
    parameters.setdefault("site_id", request.site_id)
    # Bind the newest accepted bacnet_scanner_register so the sidecar RAG-compares
    # against it (unconditional, dry-run included; mirrors the IP binder). Without
    # this the adapter read register_import_id but nothing ever set it.
    _bind_scanner_register(
        request.project_id, request.site_id, parameters, import_type="bacnet_scanner_register"
    )
    # Freeze the configured Source Interface into source_ip so the sidecar can pick
    # its adapter index. Without this the BACnet adapter fails "No Source Interface
    # is set" even when the operator selected an interface under Configuration -
    # the sidecar route (unlike the sealed discovery.py lane) never resolved it.
    # Mirrors discovery.py's per-run freeze; a dry run freezes identity only.
    _resolve_source_interface(request.project_id, request.site_id, parameters)

    # Resolve the sidecar URL up front for a live scan; an unavailable sidecar
    # fails with 503 rather than starting a run that can only fail.
    base_url: str | None = None
    if not is_dry_run(parameters):
        supervisor = getattr(http_request.app.state, "sidecar_supervisor", None)
        if supervisor is None:
            raise HTTPException(status_code=503, detail="BACnet scanner sidecar is not available.")
        try:
            base_url = supervisor.base_url_for(BACNET_SCANNER)
        except SidecarUnavailable as error:
            raise HTTPException(status_code=503, detail="BACnet scanner sidecar is not available.") from error

    run = _create_run(request.model_copy(update={"parameters": parameters}), "bacnet_scanner", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_bacnet_scanner_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_devices,
            sidecar_base_url=base_url,
            import_loader=ImportRepository(service.engine).get_accepted_rows,
        )

    return _dispatch(run, enqueue=None, run_inline=run_inline, label="BACnet scanner")


@router.post("/mqtt_sidecar/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_mqtt_scanner_run(
    request: JobCreateRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)

    # Inline-only: no worker actor for the local sidecar (see create_ip_scanner_run).
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "MQTT scanner runs require the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )

    parameters = dict(request.parameters)
    # MQTT is a capture-only, read-only broker observation, but the same legacy
    # consent gate the MQTT discovery route uses still governs a live capture.
    _require_legacy_scan_authorization(parameters)
    _stamp_legacy_authorizer(parameters, principal)
    parameters.setdefault("project_id", request.project_id)
    parameters.setdefault("site_id", request.site_id)
    # Bind the newest accepted mqtt_scanner_register so the sidecar RAG-compares
    # against it (unconditional, dry-run included; mirrors the IP binder).
    _bind_scanner_register(
        request.project_id, request.site_id, parameters, import_type="mqtt_scanner_register"
    )

    # Resolve the sidecar URL up front for a live capture; an unavailable sidecar
    # fails with 503 rather than starting a run that can only fail.
    base_url: str | None = None
    if not is_dry_run(parameters):
        supervisor = getattr(http_request.app.state, "sidecar_supervisor", None)
        if supervisor is None:
            raise HTTPException(status_code=503, detail="MQTT scanner sidecar is not available.")
        try:
            base_url = supervisor.base_url_for(MQTT_SCANNER)
        except SidecarUnavailable as error:
            raise HTTPException(status_code=503, detail="MQTT scanner sidecar is not available.") from error

    # A live MQTT session and a capture run both drive the sidecar's one
    # connection, so they are mutually exclusive. Hold the live-session lock while
    # checking + creating the run so this cannot race with live/connect (which
    # holds the same lock while checking for a running run). Dry runs touch no
    # sidecar and are exempt. The lock is non-reentrant -> use current_locked().
    guard = nullcontext() if is_dry_run(parameters) else live_session_service.lock
    with guard:
        if not is_dry_run(parameters):
            held = live_session_service.current_locked()
            if held is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"An MQTT live session is open (started by {held.owner} at "
                        f"{held.since.isoformat()}). Stop the live view before starting a capture run."
                    ),
                )
        run = _create_run(request.model_copy(update={"parameters": parameters}), "mqtt_scanner", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_mqtt_scanner_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            persist_records=run_store.replace_devices,
            sidecar_base_url=base_url,
            import_loader=ImportRepository(service.engine).get_accepted_rows,
        )

    return _dispatch(run, enqueue=None, run_inline=run_inline, label="MQTT scanner")


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _split_bacnet_address(address: object) -> tuple[str, int | None]:
    """Split a persisted device address into (ip, port).

    The sidecar's ``compare()`` embeds a non-default port as ``ip:port``; a bare
    ip carries no port (the sidecar then uses its default). Never guesses a port.
    """
    text = str(address or "").strip()
    if ":" in text:
        head, _, tail = text.rpartition(":")
        if head and tail.isdigit():
            return head, int(tail)
    return text, None


def _find_bacnet_device(run_id: str, device_instance: int) -> dict | None:
    """Resolve the browse target from the run's OWN evidence, never a client value."""
    for device in DiscoveryRepository(service.engine).list_devices(run_id):
        if device.get("device_type") != "bacnet_device":
            continue
        attributes = device.get("attributes") or {}
        if _optional_int(attributes.get("device_instance")) == device_instance:
            return device
    return None


@router.post(
    "/bacnet_sidecar/runs/{run_id}/object-browse",
    response_model=BacnetObjectBrowseResponse,
    dependencies=[Depends(require_engineer)],
)
def browse_bacnet_scanner_objects(
    run_id: str,
    request: BacnetObjectBrowseRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> BacnetObjectBrowseResponse:
    """Read one device's live object list on demand for a succeeded bacnet_scanner run.

    An ephemeral read: it drives the sidecar's ``/api/objects`` to completion and
    returns JSON. It persists NOTHING and never touches the scan's device table,
    so the sealed results are unchanged. The target device is resolved from the
    run's own evidence (never a client-supplied address). A live read is real
    network I/O, so the same legacy consent gate the scan run uses applies.

    Single-tenant sidecar: this shares the sidecar's one BACnet UDP client with a
    concurrent scan (invoke-id multiplexed, so protocol-safe) but never touches
    its in-memory register, so there is no shared mutable state to guard. No mutex
    in M1.
    """
    run = _load_discovery_run(run_id, principal)  # scoped access + 404
    if run.job_type != "bacnet_scanner":
        # Browse is the sidecar lane only; conceal other lanes as not-found.
        raise HTTPException(status_code=404, detail="BACnet scanner run was not found.")
    if run.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail="Object browse requires a succeeded BACnet scanner run.",
        )
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "BACnet object browse requires the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )
    # A live browse is real network I/O -> same consent gate as the scan run.
    _require_legacy_scan_authorization({"authorized": request.authorized})

    supervisor = getattr(http_request.app.state, "sidecar_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="BACnet scanner sidecar is not available.")
    try:
        base_url = supervisor.base_url_for(BACNET_SCANNER)
    except SidecarUnavailable as error:
        raise HTTPException(status_code=503, detail="BACnet scanner sidecar is not available.") from error

    device = _find_bacnet_device(run_id, request.device_instance)
    if device is None:
        raise HTTPException(status_code=404, detail="BACnet device is not present in this run.")
    attributes = device.get("attributes") or {}
    ip, port = _split_bacnet_address(device.get("address"))
    if not ip:
        raise HTTPException(status_code=409, detail="The recorded device has no address to read.")
    mac = attributes.get("mac")

    logger.info(
        "bacnet object browse run_id=%s device_instance=%s by=%s",
        run_id,
        request.device_instance,
        principal.username,
    )
    try:
        payload = browse_device_objects(
            base_url=base_url,
            instance=request.device_instance,
            ip=ip,
            port=port,
            network=_optional_int(attributes.get("network")),
            mac=str(mac) if mac else None,
            cap=request.cap,
            read_timeout_ms=request.read_timeout_ms,
        )
    except SidecarTransportError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    return BacnetObjectBrowseResponse(
        run_id=run_id,
        device_instance=request.device_instance,
        address=str(device.get("address") or ""),
        **payload,
    )


@router.post(
    "/ip_sidecar/runs/{run_id}/save-as-register",
    response_model=ImportBatchSummary,
    dependencies=[Depends(require_engineer)],
)
def save_ip_scan_as_register(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> ImportBatchSummary:
    """Turn a succeeded IP scanner run's responding devices into an ip_scanner_register.

    A DB read then a DB write: no network I/O and no sidecar, so it deliberately
    skips the scan-authorization consent gate and the inline-only 503 the live
    scanner routes carry (those would be untrue here). The created import goes
    through the same ``ImportService`` pipeline as an upload, so it is
    indistinguishable from an uploaded ``ip_scanner_register``; a later IP scan
    for this project/site binds and RAG-compares against it (see
    ``_bind_ip_scanner_register``).
    """
    run = _load_discovery_run(run_id, principal)  # scoped access + 404
    if run.job_type != "ip_scanner":
        raise HTTPException(status_code=404, detail="IP scanner run was not found.")
    if run.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail="Save as register requires a succeeded IP scanner run.",
        )

    devices = DiscoveryRepository(service.engine).list_devices(run_id)
    rows = register_rows_from_devices(devices)
    if not rows:
        raise HTTPException(
            status_code=409,
            detail="This run recorded no responding devices to save as a register.",
        )

    summary, _errors = ImportService(service.engine).create_import(
        import_type="ip_scanner_register",
        file_name=f"scan-register-{run_id}.csv",
        file_bytes=_ip_register_csv(rows).encode("utf-8"),
        project_id=run.project_id,
        site_id=run.site_id,
    )
    logger.info(
        "ip scan saved as register run_id=%s import_id=%s rows=%d by=%s",
        run_id,
        summary.import_id,
        summary.accepted_rows,
        principal.username,
    )
    return summary
