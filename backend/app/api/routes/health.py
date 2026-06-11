from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.services.run_service import RunService

router = APIRouter()
service = RunService()


@router.get("/health")
def get_health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "smart-commissioning-api",
        "environment": get_settings().environment,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/ready")
def get_readiness(response: Response) -> dict[str, object]:
    run_store_ready, run_store_message = service.runtime_ready()
    status = "ready" if run_store_ready else "not_ready"
    if not run_store_ready:
        response.status_code = 503
    return {
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": {
            "run_store": {
                "status": "ok" if run_store_ready else "error",
                "message": run_store_message,
            },
        },
    }
