from fastapi import APIRouter

from app.api.routes import blueprint, configuration, discovery, health, imports, reports, validation

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(blueprint.router, tags=["blueprint"])
api_router.include_router(configuration.router, prefix="/configuration", tags=["configuration"])
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
api_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
api_router.include_router(validation.router, prefix="/validation", tags=["validation"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])

