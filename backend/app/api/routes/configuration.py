from fastapi import APIRouter, HTTPException

from app.schemas.configuration import (
    ConfigurationSnapshot,
    ConfigurationValidationResult,
    SecretMaterialRequest,
    SecretMaterialResponse,
)
from app.services.configuration_service import ConfigurationService

router = APIRouter()
service = ConfigurationService()


@router.get("", response_model=ConfigurationSnapshot)
def get_configuration() -> ConfigurationSnapshot:
    return service.load()


@router.put("", response_model=ConfigurationSnapshot)
def update_configuration(configuration: ConfigurationSnapshot) -> ConfigurationSnapshot:
    result = service.validate(configuration)
    if not result.valid:
        raise HTTPException(status_code=400, detail=result.errors)
    return service.save(configuration)


@router.post("/validate", response_model=ConfigurationValidationResult)
def validate_configuration(configuration: ConfigurationSnapshot) -> ConfigurationValidationResult:
    return service.validate(configuration)


@router.post("/secrets", response_model=SecretMaterialResponse)
def store_secret_material(request: SecretMaterialRequest) -> SecretMaterialResponse:
    try:
        return service.store_secret(request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
