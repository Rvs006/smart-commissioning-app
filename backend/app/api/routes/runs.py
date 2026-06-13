from fastapi import APIRouter, Depends, HTTPException, Query
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.jobs import JobStatus, JobType, RunListResponse, RunRecord
from app.services.run_service import RunService

router = APIRouter()
service = RunService()

# Read access for any authenticated caller (viewer+); mutations require engineer+.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


@router.get("", response_model=RunListResponse, dependencies=[Depends(require_viewer)])
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
        )
    )


@router.post("/{run_id}/cancel", response_model=RunRecord, dependencies=[Depends(require_engineer)])
def cancel_run(run_id: str) -> RunRecord:
    """Request cooperative cancellation of a run.

    Sets the run's ``cancel_requested`` flag (does NOT itself flip status).
    Engines poll this flag via the framework and stop early, flipping the
    terminal status to ``cancelled`` when they observe it. A run that has
    already finished is unaffected (the flag is recorded but no engine is
    running to act on it). Returns the updated run; 404 for a missing run.
    """
    try:
        return service.request_cancel(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
