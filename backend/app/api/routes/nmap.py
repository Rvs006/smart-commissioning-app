"""Nmap capability reads and global-administrator authority mutations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from smart_commissioning_core.rbac import Role

from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.scopes import require_global_admin, require_project_site_access
from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapDeploymentPolicyCreateRequest,
    NmapDeploymentPolicyResponse,
    NmapDetectedInstallationResponse,
    NmapInstallationConfirmationRequest,
    NmapInstallationConfirmationResponse,
)
from app.services.nmap_capability_service import (
    NmapAuthorityError,
    NmapCapabilityService,
    NmapInstallationNotFoundError,
    NmapInstallationProbe,
    NmapPolicyStateError,
    WindowsNmapInstallationProbe,
)

router = APIRouter()
require_viewer = require_role(Role.VIEWER)


def get_nmap_installation_probe() -> NmapInstallationProbe:
    """Runtime injection seam for the local Windows detector and trust adapter."""
    return WindowsNmapInstallationProbe()


def _network_executor_id() -> str:
    settings = get_settings()
    if settings.network_executor_id:
        return settings.network_executor_id
    if settings.job_execution_mode == "inline":
        return f"inline:{settings.deployment_id}"
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Nmap requires SMART_COMMISSIONING_NETWORK_EXECUTOR_ID when execution "
            "is not pinned to the local inline runtime."
        ),
    )


def get_nmap_capability_service(
    probe: NmapInstallationProbe = Depends(get_nmap_installation_probe),
) -> NmapCapabilityService:
    settings = get_settings()
    return NmapCapabilityService(
        get_engine(),
        deployment_id=settings.deployment_id,
        network_executor_id=_network_executor_id(),
        probe=probe,
    )


def _authority_error(error: NmapAuthorityError) -> HTTPException:
    if isinstance(error, NmapInstallationNotFoundError):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, NmapPolicyStateError):
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=status_code, detail=str(error))


def _require_internal_feature() -> None:
    if not get_settings().nmap_internal_provider_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operator-managed Nmap is disabled in this deployment.",
        )


@router.get(
    "/capability",
    response_model=NmapCapabilityResponse,
    dependencies=[Depends(require_viewer)],
)
def read_capability(
    project_id: str = Query(min_length=1, max_length=255),
    site_id: str = Query(min_length=1, max_length=255),
    principal: AuthPrincipal = Depends(get_principal),
    service: NmapCapabilityService = Depends(get_nmap_capability_service),
) -> NmapCapabilityResponse:
    require_project_site_access(
        principal,
        project_id,
        site_id,
        engine=service.engine,
        query_only=True,
    )
    if not get_settings().nmap_internal_provider_enabled:
        return NmapCapabilityResponse(
            state="disabled",
            reason="deployment_feature_disabled",
        )
    return service.capability(project_id=project_id, site_id=site_id)


@router.post(
    "/policies",
    response_model=NmapDeploymentPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_policy(
    request: NmapDeploymentPolicyCreateRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
    service: NmapCapabilityService = Depends(get_nmap_capability_service),
) -> NmapDeploymentPolicyResponse:
    _require_internal_feature()
    try:
        return service.create_policy(request, principal=principal)
    except NmapAuthorityError as error:
        raise _authority_error(error) from error


@router.get(
    "/policies",
    response_model=tuple[NmapDeploymentPolicyResponse, ...],
)
def list_policy_history(
    _principal: AuthPrincipal = Depends(require_global_admin),
    service: NmapCapabilityService = Depends(get_nmap_capability_service),
) -> tuple[NmapDeploymentPolicyResponse, ...]:
    try:
        return service.list_policy_history()
    except NmapAuthorityError as error:
        raise _authority_error(error) from error


@router.get(
    "/installations/detected",
    response_model=tuple[NmapDetectedInstallationResponse, ...],
)
def detect_installations(
    _principal: AuthPrincipal = Depends(require_global_admin),
    service: NmapCapabilityService = Depends(get_nmap_capability_service),
) -> tuple[NmapDetectedInstallationResponse, ...]:
    _require_internal_feature()
    try:
        return service.inspect_current()
    except NmapAuthorityError as error:
        raise _authority_error(error) from error


@router.post(
    "/installations/confirm",
    response_model=NmapInstallationConfirmationResponse,
    status_code=status.HTTP_201_CREATED,
)
def confirm_installation(
    request: NmapInstallationConfirmationRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
    service: NmapCapabilityService = Depends(get_nmap_capability_service),
) -> NmapInstallationConfirmationResponse:
    _require_internal_feature()
    try:
        return service.confirm_installation(request, principal=principal)
    except NmapAuthorityError as error:
        raise _authority_error(error) from error
