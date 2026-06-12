from fastapi import APIRouter, HTTPException, Query

from app.schemas.configuration import (
    ConfigurationSnapshot,
    ConfigurationValidationResult,
    SecretMaterialRequest,
    SecretMaterialResponse,
)
from app.services.configuration_service import DEFAULT_PROJECT_ID, DEFAULT_SITE_ID, ConfigurationService

router = APIRouter()
service = ConfigurationService()


@router.get("", response_model=ConfigurationSnapshot)
def get_configuration(
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationSnapshot:
    return service.load(project_id, site_id)


@router.put("", response_model=ConfigurationSnapshot)
def update_configuration(
    configuration: ConfigurationSnapshot,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> ConfigurationSnapshot:
    result = service.validate(configuration)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.errors)
    return service.save(configuration, project_id=project_id, site_id=site_id)


@router.post("/validate", response_model=ConfigurationValidationResult)
def validate_configuration(configuration: ConfigurationSnapshot) -> ConfigurationValidationResult:
    return service.validate(configuration)


@router.post("/secrets", response_model=SecretMaterialResponse)
def store_secret_material(
    request: SecretMaterialRequest,
    project_id: str = Query(default=DEFAULT_PROJECT_ID),
    site_id: str = Query(default=DEFAULT_SITE_ID),
) -> SecretMaterialResponse:
    try:
        return service.store_secret(request, project_id=project_id, site_id=site_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
