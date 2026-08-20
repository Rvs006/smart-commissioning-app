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

from fastapi import APIRouter, Depends, HTTPException, Request
from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.engines.bacnet_scanner_sidecar import process_bacnet_scanner_run
from smart_commissioning_core.engines.ip_scanner_sidecar import process_ip_scanner_run
from smart_commissioning_core.engines.mqtt_scanner_sidecar import process_mqtt_scanner_run

from app.api.routes.discovery import (
    _create_run,
    _dispatch,
    _require_legacy_scan_authorization,
    _settings_throttle,
    _stamp_legacy_authorizer,
    require_engineer,
    service,
)
from app.core.auth import AuthPrincipal, get_principal
from app.core.config import get_settings
from app.core.scopes import require_project_site_access
from app.schemas.jobs import JobAcceptedResponse, JobCreateRequest
from app.services.engine_dispatch import is_dry_run
from app.services.sidecar_supervisor import (
    BACNET_SCANNER,
    IP_SCANNER,
    MQTT_SCANNER,
    SidecarUnavailable,
)

router = APIRouter()


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
