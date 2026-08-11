from fastapi import APIRouter, Depends, HTTPException, Query
from smart_commissioning_core.rbac import Role

from app.core.auth import AuthPrincipal, require_role
from app.core.scopes import allowed_scope_pairs, load_scoped_run
from app.schemas.jobs import JobStatus, JobType, RunListResponse, RunRecord
from app.services.run_service import RunService

router = APIRouter()
service = RunService()

# Read access for any authenticated caller (viewer+); mutations require engineer+.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


@router.get("", response_model=RunListResponse)
def list_runs(
    project_id: str = Query(default="demo-project"),
    site_id: str = Query(default="demo-site"),
    job_type: JobType | None = Query(default=None),
    edge_id: str | None = Query(
        default=None,
        description="Filter to runs originating from this edge id (hub multi-project attribution).",
    ),
    status: JobStatus | None = Query(
        default=None,
        description="Filter to runs in this status (queued/running/succeeded/failed/cancelled).",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: AuthPrincipal = Depends(require_viewer),
) -> RunListResponse:
    """List run summaries for a project/site, newest first.

    Each summary now carries ``edge_id`` (the originating edge; null for a local
    run). Optional ``edge_id`` and ``status`` filters narrow the list in addition
    to the existing ``project_id`` / ``site_id`` / ``job_type`` filters.
    """
    return RunListResponse(
        runs=service.list_runs(
            project_id=project_id,
            site_id=site_id,
            job_types={job_type} if job_type is not None else None,
            edge_id=edge_id,
            status=status,
            limit=limit,
            offset=offset,
            scope_pairs=allowed_scope_pairs(principal, engine=service.engine),
        )
    )


@router.post("/{run_id}/cancel", response_model=RunRecord)
def cancel_run(
    run_id: str,
    principal: AuthPrincipal = Depends(require_engineer),
) -> RunRecord:
    """Request cooperative cancellation of a run.

    Sets the run's ``cancel_requested`` flag (does NOT itself flip status).
    Engines poll this flag via the framework and stop early, flipping the
    terminal status to ``cancelled`` when they observe it. Synchronous report
    generation is owned by POST /reports and returns 409 here instead of
    exposing its transient queued row to cancellation. Returns the updated run;
    404 for a missing run.
    """
    load_scoped_run(run_id, principal, engine=service.engine)
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Run not found.") from error
    if run.job_type == "report_generation":
        raise HTTPException(
            status_code=409,
            detail="Report generation runs are synchronous and cannot be cancelled.",
        )
    try:
        return service.request_cancel(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="Run not found.") from error
