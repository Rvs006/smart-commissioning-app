from fastapi import APIRouter, Query

from app.schemas.jobs import JobType, RunListResponse
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
