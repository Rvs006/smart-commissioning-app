import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from smart_commissioning_core.rbac import Role

from app.core.auth import AuthPrincipal, require_role
from app.core.db import get_engine
from app.core.scopes import require_project_site_access
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


def _require_scope(
    principal: AuthPrincipal,
    project_id: str,
    site_id: str,
) -> None:
    require_project_site_access(
        principal,
        project_id,
        site_id,
        engine=get_engine(),
    )


@router.get("", response_model=ConfigurationSnapshot)
def get_configuration(
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
    principal: AuthPrincipal = Depends(require_viewer),
) -> ConfigurationSnapshot:
    _require_scope(principal, project_id, site_id)
    return service.load(project_id, site_id)


@router.put("", response_model=ConfigurationSnapshot)
def update_configuration(
    configuration: ConfigurationSnapshot,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
    principal: AuthPrincipal = Depends(require_engineer),
) -> ConfigurationSnapshot:
    _require_scope(principal, project_id, site_id)
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
)
def export_configuration_with_secrets(
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
    principal: AuthPrincipal = Depends(require_engineer),
) -> ConfigurationExportEnvelope:
    """Compatibility export with every secret value excluded.

    The former route name remains for one release so older clients receive a
    masked envelope instead of plaintext credentials or key material.
    """
    _require_scope(principal, project_id, site_id)
    return service.export_with_secrets(project_id, site_id)


@router.post(
    "/import",
    response_model=ConfigurationSnapshot,
)
def import_configuration(
    request: ConfigurationImportRequest,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
    principal: AuthPrincipal = Depends(require_engineer),
) -> ConfigurationSnapshot:
    """Import configuration, accepting secret material only from legacy v2 envelopes.

    Validation mirrors PUT (invalid snapshot -> 400 with the error list); a bad
    secret field/reference -> 400. Returns the masked snapshot, like PUT, and
    applies logging settings best-effort so a Log Level change takes effect now.
    """
    _require_scope(principal, project_id, site_id)
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


@router.post("/secrets", response_model=SecretMaterialResponse)
def store_secret_material(
    request: SecretMaterialRequest,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
    principal: AuthPrincipal = Depends(require_engineer),
) -> SecretMaterialResponse:
    _require_scope(principal, project_id, site_id)
    try:
        return service.store_secret(request, project_id=project_id, site_id=site_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
