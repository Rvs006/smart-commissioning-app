import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.configuration import (
    ConfigurationExportEnvelope,
    ConfigurationImportRequest,
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


@router.get(
    "/export-with-secrets",
    response_model=ConfigurationExportEnvelope,
    dependencies=[Depends(require_engineer)],
)
def export_configuration_with_secrets(
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationExportEnvelope:
    """Export the configuration INCLUDING plain-text secrets (engineer only).

    Distinct from GET /configuration (which always masks). The returned envelope
    carries the MQTT password/tokens and the certificate/key PEM material so it
    can be imported on another machine — engineer-gated and warned about in the UI.
    """
    return service.export_with_secrets(project_id, site_id)


@router.post(
    "/import",
    response_model=ConfigurationSnapshot,
    dependencies=[Depends(require_engineer)],
)
def import_configuration(
    request: ConfigurationImportRequest,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationSnapshot:
    """Import a configuration, restoring any exported secret material.

    Validation mirrors PUT (invalid snapshot -> 400 with the error list); a bad
    secret field/reference -> 400. Returns the masked snapshot, like PUT, and
    applies logging settings best-effort so a Log Level change takes effect now.
    """
    result = service.validate(request.configuration)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.errors)
    try:
        saved = service.import_with_secrets(request, project_id=project_id, site_id=site_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        apply_logging_settings(saved.logging.values)
    except Exception:  # noqa: BLE001 (applying logging settings is best-effort)
        logger.debug("Could not apply logging settings after import.", exc_info=True)
    return saved


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
