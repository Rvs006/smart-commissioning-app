"""Engineer-gated log bundle retrieval and upload (mounted under /api/v1/logs).

Routes:
  * GET  /logs/bundle  — download every local ``*.log*`` file as a masked zip.
    Covers "retrieve logs without AnyDesk": an engineer at the laptop saves the
    bundle to a USB stick.
  * POST /logs/upload  — POST the same masked bundle to the configured
    ``Log Upload URL``. Returns the TRUTHFUL outcome; a non-responding endpoint
    is a 200 body with ``outcome: "no_response"``, never a fabricated success or
    a hard 5xx.

The bundle only ever contains files from the local logs directory (never the
secrets store, database, or import files) and credential-shaped values are
masked. The upload token is sent ONLY as an ``Authorization: Bearer`` header and
is never echoed in the response, the URL, or any log line.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.services.configuration_service import ConfigurationService
from app.services.log_service import (
    UPLOAD_URL_FIELD,
    build_log_bundle,
    upload_log_bundle,
)

router = APIRouter()
service = ConfigurationService()

# RBAC: both retrieving and uploading logs are engineer authority (they expose
# operational logs off the machine), matching the backend gate the UI mirrors.
require_engineer = require_role(Role.ENGINEER)


class LogUploadResponse(BaseModel):
    outcome: str
    status_code: int | None = None
    detail: str
    bundle_bytes: int
    files: list[str]


@router.get("/bundle", dependencies=[Depends(require_engineer)])
def download_log_bundle() -> Response:
    """Stream the local logs as a single masked zip download."""
    bundle, files = build_log_bundle()
    if not files:
        raise HTTPException(status_code=404, detail="No log files exist yet.")
    file_name = f"smart_commissioning_logs_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    return Response(
        content=bundle,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.post("/upload", response_model=LogUploadResponse, dependencies=[Depends(require_engineer)])
def upload_logs() -> LogUploadResponse:
    """Upload the masked log bundle to the configured Log Upload URL.

    Reads the URL/token from the stored configuration (unmasked, so the real
    token reaches the outbound request but never the response). All three
    outcomes return HTTP 200 — the ``outcome`` field is the result; a
    ``no_response`` is a truthful terminal answer, not a server error.
    """
    values = ConfigurationService().load(mask_secrets=False).logging.values
    url = str(values.get(UPLOAD_URL_FIELD, "") or "").strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail="No upload URL is configured. Set Configuration -> Logging & Diagnostics -> "
            "Log Upload URL and save, then try again.",
        )
    token = str(values.get("Log Upload Token", "") or "")
    try:
        outcome = upload_log_bundle(url, token)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Configuration -> Logging & Diagnostics -> Log Upload URL: {error}",
        ) from error
    return LogUploadResponse(
        outcome=outcome.outcome,
        status_code=outcome.status_code,
        detail=outcome.detail,
        bundle_bytes=outcome.bundle_bytes,
        files=outcome.files,
    )
