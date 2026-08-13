"""Scoped download of verified protected raw evidence."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Response
from smart_commissioning_core.rbac import Role

from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.db import get_engine
from app.core.scopes import load_scoped_run
from app.services.raw_evidence_artifacts import (
    RawEvidenceArtifactStore,
    RawEvidenceIntegrityError,
    RawEvidenceNotFoundError,
    RawEvidenceSecurityError,
)

router = APIRouter()
require_viewer = require_role(Role.VIEWER)

_SAFE_FILE_PART = re.compile(r"[^a-z0-9_-]+")


def get_raw_evidence_store() -> RawEvidenceArtifactStore:
    """Return the runtime-owned store; injectable for route integration tests."""

    return RawEvidenceArtifactStore(get_engine())


@router.get(
    "/runs/{run_id}/raw-evidence/{artifact_id}",
    dependencies=[Depends(require_viewer)],
    responses={
        200: {"description": "Verified protected evidence bytes."},
        404: {"description": "Run or artifact not found in the caller's scope."},
        409: {"description": "Stored evidence failed integrity verification."},
    },
)
def download_raw_evidence(
    run_id: str,
    artifact_id: str,
    principal: AuthPrincipal = Depends(get_principal),
    store: RawEvidenceArtifactStore = Depends(get_raw_evidence_store),
) -> Response:
    """Return exact bytes after run scope, record, and file verification."""

    load_scoped_run(run_id, principal, engine=store.engine, query_only=True)
    try:
        descriptor, payload = store.read_for_download(
            run_id=run_id,
            artifact_id=artifact_id,
            audit_actor=principal.user_id or f"synthetic:{principal.source}",
        )
    except RawEvidenceNotFoundError as error:
        raise HTTPException(status_code=404, detail="Raw evidence artifact not found.") from error
    except (RawEvidenceIntegrityError, RawEvidenceSecurityError) as error:
        raise HTTPException(
            status_code=409,
            detail="Raw evidence failed integrity verification.",
        ) from error

    artifact_type = _SAFE_FILE_PART.sub("-", descriptor.artifact_type.lower()).strip("-")
    filename = f"{artifact_type or 'raw-evidence'}-{descriptor.artifact_id}.bin"
    return Response(
        content=payload,
        media_type=descriptor.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "ETag": f'"sha256:{descriptor.sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
