from fastapi import APIRouter, Depends
from smart_commissioning_core.rbac import Role

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
    users,
    validation,
)
from app.core.auth import require_auth, require_role

api_router = APIRouter()

# Health endpoints stay unauthenticated so liveness/readiness probes work
# without credentials (they expose no project data).
api_router.include_router(health.router, tags=["health"])

# Import format helpers (profile list + blank templates) are unauthenticated:
# they expose only column headers and a synthetic example row, i.e. the format
# an engineer needs to prepare a register before they have an API key. Mounted
# on api_router (not protected_router) so no key is required. Registered before
# the protected /imports routes so GET /imports/profiles resolves here rather
# than matching the protected GET /imports/{import_id}.
api_router.include_router(imports.public_router, prefix="/imports", tags=["imports"])

# Every other /api/v1 route requires authentication (app.core.auth). RBAC is
# then layered per-route inside each router (require_role on the data/mutation
# routes); two single-tier routers (blueprint, events) are gated here at the
# include level, and the hub ingest router is admin-only.
protected_router = APIRouter(dependencies=[Depends(require_auth)])
# Blueprint is a static read-only capability map: any authenticated viewer+.
protected_router.include_router(
    blueprint.router, tags=["blueprint"], dependencies=[Depends(require_role(Role.VIEWER))]
)
protected_router.include_router(configuration.router, prefix="/configuration", tags=["configuration"])
protected_router.include_router(imports.router, prefix="/imports", tags=["imports"])
protected_router.include_router(runs.router, prefix="/runs", tags=["runs"])
# SSE run-progress streaming (GET /runs/{run_id}/events). Mounted on the same
# /runs prefix and behind the same auth as the polling endpoints; the frontend
# consumes it via fetch()+ReadableStream so X-API-Key still rides the request.
# Reading run progress is a viewer+ capability, gated at the include level.
protected_router.include_router(
    events.router, prefix="/runs", tags=["runs", "events"], dependencies=[Depends(require_role(Role.VIEWER))]
)
protected_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
protected_router.include_router(validation.router, prefix="/validation", tags=["validation"])
protected_router.include_router(reports.router, prefix="/reports", tags=["reports"])
# Evidence integrity (verify), backup, and retention. Verify stays behind auth
# too: it can regenerate report artifacts, so it is not treated as public.
protected_router.include_router(evidence.router, prefix="/evidence", tags=["evidence"])
# Edge->hub sync: hub ingest endpoint (POST /hub/runs/ingest). The router is
# always mounted but every route returns 404 unless deployment_role == 'hub'
# (the role-guard lives in the route so toggling the setting needs no remount).
# Behind the same auth as every other /api/v1 route. Ingest immutably writes
# cross-edge run records, so it is restricted to admin (the hub operator role).
# The 404 role-guard runs first for a non-hub instance; on a hub, a non-admin
# caller gets 403.
protected_router.include_router(
    hub.router, prefix="/hub", tags=["hub"], dependencies=[Depends(require_role(Role.ADMIN))]
)
# Identity + RBAC: GET /me for any authenticated caller, /users management for
# admins (require_role(Role.ADMIN) inside the route). Behind the same auth as
# every other /api/v1 route.
protected_router.include_router(users.router, tags=["users"])
api_router.include_router(protected_router)
