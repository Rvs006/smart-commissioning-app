"""Dedicated, scoped Sync v2 capabilities and ingest endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from smart_commissioning_core.sync_v2 import (
    BUNDLE_MEDIA_TYPE,
    RECEIPT_CLASSES,
    SyncV2Error,
)

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.sync_auth import SyncPrincipal, require_sync_credential
from app.services.sync_v2_service import ingest_sync_v2_bundle

router = APIRouter()


@router.get("/capabilities")
def capabilities(
    _principal: SyncPrincipal = Depends(require_sync_credential),
) -> dict[str, object]:
    settings = get_settings()
    return {
        "protocol": "smart-commissioning-sync",
        "protocol_versions": ["2.0", "1.0"],
        "preferred_protocol_version": "2.0",
        "media_type": BUNDLE_MEDIA_TYPE,
        "ingest_path": "/api/v1/hub/sync/v2/ingest",
        "receipt_classes": sorted(RECEIPT_CLASSES),
        "max_bundle_bytes": _max_bundle_bytes(settings),
        "max_items": int(getattr(settings, "max_sync_items", 500)),
    }


@router.post("/v2/ingest")
async def ingest(
    request: Request,
    principal: SyncPrincipal = Depends(require_sync_credential),
) -> dict[str, object]:
    settings = get_settings()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {BUNDLE_MEDIA_TYPE, "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Unsupported synchronization media type.")
    limit = _max_bundle_bytes(settings)
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > limit:
                raise HTTPException(status_code=413, detail="Synchronization bundle is too large.")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from error
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(status_code=413, detail="Synchronization bundle is too large.")
        chunks.append(chunk)
    bundle = b"".join(chunks)
    if not bundle:
        raise HTTPException(status_code=400, detail="Synchronization bundle is empty.")
    try:
        return ingest_sync_v2_bundle(
            get_engine(),
            bundle,
            principal=principal,
            now=datetime.now(UTC),
            max_items=int(getattr(settings, "max_sync_items", 500)),
            max_uncompressed_bytes=int(
                getattr(settings, "max_sync_uncompressed_bytes", 200 * 1024 * 1024)
            ),
        )
    except SyncV2Error as error:
        raise HTTPException(status_code=400, detail="Invalid Sync v2 bundle.") from error


def _max_bundle_bytes(settings: object) -> int:
    return int(
        getattr(
            settings,
            "max_sync_bundle_bytes",
            getattr(settings, "max_upload_bytes", 20 * 1024 * 1024),
        )
    )
