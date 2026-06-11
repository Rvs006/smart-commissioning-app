from fastapi import APIRouter, Depends

from app.api.routes import blueprint, configuration, discovery, health, imports, reports, runs, validation
from app.core.auth import require_auth

api_router = APIRouter()

# Health endpoints stay unauthenticated so liveness/readiness probes work
# without credentials (they expose no project data).
api_router.include_router(health.router, tags=["health"])

# Every other /api/v1 route requires authentication (app.core.auth).
protected_router = APIRouter(dependencies=[Depends(require_auth)])
protected_router.include_router(blueprint.router, tags=["blueprint"])
protected_router.include_router(configuration.router, prefix="/configuration", tags=["configuration"])
protected_router.include_router(imports.router, prefix="/imports", tags=["imports"])
protected_router.include_router(runs.router, prefix="/runs", tags=["runs"])
protected_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
protected_router.include_router(validation.router, prefix="/validation", tags=["validation"])
protected_router.include_router(reports.router, prefix="/reports", tags=["reports"])
api_router.include_router(protected_router)
