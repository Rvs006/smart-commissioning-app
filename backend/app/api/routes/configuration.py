import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.configuration import (
    ConfigurationSnapshot,
    ConfigurationValidationResult,
    SecretMaterialRequest,
    SecretMaterialResponse,
)
from app.services.configuration_service import DEFAULT_PROJECT_ID, DEFAULT_SITE_ID, ConfigurationService
from app.services.log_service import apply_logging_settings

logger = logging.getLogger(__name__)

router = APIRouter()
service = ConfigurationService()

# RBAC: reading or validating configuration is viewer+ (both are side-effect
# free); persisting a configuration (PUT) or storing secret material is engineer+
# (publishing/managing configuration is engineer authority).
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


@router.get("", response_model=ConfigurationSnapshot, dependencies=[Depends(require_viewer)])
def get_configuration(
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationSnapshot:
    return service.load(project_id, site_id)


@router.put("", response_model=ConfigurationSnapshot, dependencies=[Depends(require_engineer)])
def update_configuration(
    configuration: ConfigurationSnapshot,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationSnapshot:
    result = service.validate(configuration)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.errors)
    saved = service.save(configuration, project_id=project_id, site_id=site_id)
    # Make a Log Level / Diagnostics Mode change take effect in the live process,
    # not only at next boot. The masked snapshot is fine here: only the plain
    # Log Level / Diagnostics Mode / Log Retention words are read. Guarded so a
    # logging hiccup can never fail a config save.
    try:
        apply_logging_settings(saved.logging.values)
    except Exception:  # noqa: BLE001 (applying logging settings is best-effort)
        logger.debug("Could not apply logging settings after save.", exc_info=True)
    return saved


@router.post(
    "/validate",
    response_model=ConfigurationValidationResult,
    dependencies=[Depends(require_viewer)],
)
def validate_configuration(configuration: ConfigurationSnapshot) -> ConfigurationValidationResult:
    return service.validate(configuration)


@router.post("/secrets", response_model=SecretMaterialResponse, dependencies=[Depends(require_engineer)])
def store_secret_material(
    request: SecretMaterialRequest,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> SecretMaterialResponse:
    try:
        return service.store_secret(request, project_id=project_id, site_id=site_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
