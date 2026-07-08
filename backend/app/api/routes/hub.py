"""Hub-side ingest endpoint for the edge->hub sync core (mounted under /api/v1).

Route:
  * POST /hub/runs/ingest — accept a signed ``.scbundle`` (raw request body bytes
    OR a multipart ``file`` upload), verify it with core
    :func:`smart_commissioning_core.sync.ingest_sync_bundle`, and return the
    :class:`IngestSummary` as JSON.

Role guard: this router is only active when ``settings.deployment_role == 'hub'``.
A standalone or edge instance returns 404 for every hub route (the endpoint does
not exist for them), so an edge can never be tricked into accepting ingests.

Trust: the route passes the REAL trusted-edges map from settings
(``Settings.load_trusted_edges()``) to core. It NEVER trusts the bundle's
self-declared edge identity beyond what that map allows — core fails closed
(untrusted edge / forged key / bad signature / bad hash all reject the whole
bundle, writing nothing). The bundle's embedded fingerprint is not believed; core
derives the authoritative fingerprint from the embedded PEM.

Authentication is applied by the parent protected router (app.api.router): the
same require_auth dependency as every other /api/v1 route. The existing upload
size cap (``settings.max_upload_bytes``) is enforced here too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from smart_commissioning_core.sync import ingest_sync_bundle

from app.api.uploads import check_content_length, read_upload_capped, upload_too_large
from app.core.config import get_settings
from app.core.db import get_engine

router = APIRouter()


def _require_hub_role() -> None:
    """404 unless this instance is configured as a hub.

    A standalone/edge instance must not expose an ingest surface, so the route is
    reported as not-found rather than 403 (the resource genuinely does not exist
    in that role). The role-guard is re-checked per request so flipping the
    setting (and clearing the settings cache) takes effect without a remount.
    """
    if get_settings().deployment_role != "hub":
        raise HTTPException(status_code=404, detail="Not Found")


@router.post("/runs/ingest")
async def ingest_runs(
    request: Request,
    file: UploadFile | None = File(default=None),
) -> dict[str, object]:
    """Verify + immutably ingest a signed run bundle; return the IngestSummary.

    Accepts the bundle either as a multipart ``file`` upload or as the raw
    request body (``application/octet-stream``). The whole bundle is verified by
    core before anything is written (fail-closed); the JSON response mirrors
    ``IngestSummary.as_dict()`` so the caller (the edge sync CLI or an operator)
    can confirm exactly what was inserted / skipped / rejected.

    HTTP status is 200 even for a rejected bundle: rejection is a normal,
    well-defined outcome reported in the body (``accepted=false`` +
    ``rejected_reason``), not a transport error. Only malformed requests (no
    bundle bytes, oversize) raise 4xx.
    """
    _require_hub_role()
    settings = get_settings()

    # Fast pre-check on the declared body size; the capped read is authoritative.
    check_content_length(request, settings.max_upload_bytes, "bundle")

    if file is not None:
        bundle_bytes = await read_upload_capped(file, settings.max_upload_bytes, "bundle")
    else:
        bundle_bytes = await request.body()
        if len(bundle_bytes) > settings.max_upload_bytes:
            raise upload_too_large(settings.max_upload_bytes, "bundle")

    if not bundle_bytes:
        raise HTTPException(status_code=400, detail="Request did not contain a bundle.")

    try:
        trusted_edges = settings.load_trusted_edges()
    except ValueError as error:
        # A misconfigured hub (bad trusted-edges file) is a server fault, not a
        # client error: surface it as 500 with the reason rather than silently
        # trusting nothing and reporting every bundle as untrusted.
        raise HTTPException(status_code=500, detail=f"Hub trust configuration error: {error}") from error

    summary = ingest_sync_bundle(
        get_engine(),
        bundle_bytes,
        trusted_edges=trusted_edges,
        now=datetime.now(UTC),
    )
    return summary.as_dict()
