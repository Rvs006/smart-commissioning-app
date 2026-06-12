from fastapi import APIRouter, Depends

from app.api.routes import (
    blueprint,
    configuration,
    discovery,
    events,
    evidence,
    health,
    hub,
    imports,
    reports,
    runs,
    validation,
)
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
# SSE run-progress streaming (GET /runs/{run_id}/events). Mounted on the same
# /runs prefix and behind the same auth as the polling endpoints; the frontend
# consumes it via fetch()+ReadableStream so X-API-Key still rides the request.
protected_router.include_router(events.router, prefix="/runs", tags=["runs", "events"])
protected_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
protected_router.include_router(validation.router, prefix="/validation", tags=["validation"])
protected_router.include_router(reports.router, prefix="/reports", tags=["reports"])
# Evidence integrity (verify), backup, and retention. Verify stays behind auth
# too: it can regenerate report artifacts, so it is not treated as public.
protected_router.include_router(evidence.router, prefix="/evidence", tags=["evidence"])
# Edge->hub sync: hub ingest endpoint (POST /hub/runs/ingest). The router is
# always mounted but every route returns 404 unless deployment_role == 'hub'
# (the role-guard lives in the route so toggling the setting needs no remount).
# Behind the same auth as every other /api/v1 route.
protected_router.include_router(hub.router, prefix="/hub", tags=["hub"])
api_router.include_router(protected_router)
