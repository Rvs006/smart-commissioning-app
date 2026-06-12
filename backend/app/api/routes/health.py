from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.observability import DependencyStatus, check_database, check_redis

router = APIRouter()


@router.get("/health")
def get_health() -> dict[str, object]:
    """Cheap liveness probe: no dependency I/O, always answers if the process is up."""
    return {
        "status": "ok",
        "service": "smart-commissioning-api",
        "environment": get_settings().environment,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/ready")
def get_readiness(response: Response) -> dict[str, object]:
    """Readiness probe: verifies the dependencies this deployment actually needs.

    Always probes the database (SELECT 1). Probes Redis only when the queue is
    required (``job_execution_mode != 'inline'``); in inline/portable mode Redis
    is not needed, so it is reported informationally (``required: false``) and an
    unreachable broker does NOT make the service not-ready. The body carries
    per-dependency status and never includes credentials (the redis check
    reports host[:port] only).
    """
    settings = get_settings()
    redis_required = settings.job_execution_mode != "inline"

    checks: list[DependencyStatus] = [check_database(get_engine())]
    # Only probe Redis when the deployment needs the queue; an inline/portable
    # deployment must not fail readiness on a Redis it never uses.
    if redis_required:
        checks.append(check_redis(settings.redis_url, required=True))

    # Not-ready only if a REQUIRED dependency is down.
    ready = all(check.ok for check in checks if check.required)
    if not ready:
        response.status_code = 503

    return {
        "status": "ready" if ready else "not_ready",
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": {check.name: check.as_dict() for check in checks},
    }
