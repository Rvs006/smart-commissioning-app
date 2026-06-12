from fastapi import APIRouter, HTTPException, Query

from app.schemas.jobs import JobType, RunListResponse, RunRecord
from app.services.run_service import RunService

router = APIRouter()
service = RunService()


@router.get("", response_model=RunListResponse)
def list_runs(
    project_id: str = Query(default="demo-project"),
    site_id: str = Query(default="demo-site"),
    job_type: JobType | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RunListResponse:
    """List run summaries for a project/site, newest first."""
    return RunListResponse(
        runs=service.list_runs(
            project_id=project_id,
            site_id=site_id,
            job_types={job_type} if job_type is not None else None,
            limit=limit,
            offset=offset,
        )
    )


@router.post("/{run_id}/cancel", response_model=RunRecord)
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
