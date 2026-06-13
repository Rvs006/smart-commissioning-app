"""Evidence integrity, backup, and retention endpoints (mounted under /api/v1).

Routes:
  * GET  /evidence/reports/{report_id}/verify  — recompute the artifact hash and
    verify the stored Ed25519 signature; returns
    {hash_matches, signature_valid, signed_at, public_key_fingerprint, ...}.
  * POST /evidence/backup                       — build a backup bundle (SQLite +
    secrets + import files) and stream it back as a download.
  * POST /evidence/retention/preview            — DRY-RUN: list what WOULD be
    purged. Deletes nothing.
  * POST /evidence/retention/apply              — destructive purge; requires an
    explicit confirmation in the body AND auth (wired in app.api.router).

Authentication for the whole router is applied by the parent protected router
(app.api.router); verify is intentionally kept behind auth as well (see the
router comment / decisions).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from smart_commissioning_core.rbac import Role

from app.api.routes.reports import _build_report_artifact, _to_report_summary
from app.core.auth import require_role
from app.core.runtime import IMPORT_FILES_ROOT, SECRETS_ROOT
from app.services.backup_service import BackupError, BackupSources, create_backup_bundle
from app.services.reports_integrity import (
    INTEGRITY_KEY,
    fingerprint_for_pem,
    load_signing_key,
    verify_artifact,
)
from app.services.retention_service import RetentionService, cutoff_from_keep_days
from app.services.run_service import RunService

router = APIRouter()
service = RunService()

# RBAC: verifying a report's integrity is read-only (viewer+); building a backup
# bundle and previewing retention are engineer+; APPLYING retention is a
# destructive purge reserved for admin.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)
require_admin = require_role(Role.ADMIN)


class ReportVerifyResponse(BaseModel):
    report_id: str
    hash_matches: bool
    signature_valid: bool | None
    # True iff the stored record's key matches the CURRENT signing key; False
    # surfaces a swapped-key record (tamper-of-stored-record). None when it
    # cannot be determined (unsigned record or no crypto).
    key_matches_current: bool | None = None
    signed_at: str | None
    public_key_fingerprint: str | None
    stored_hash: str | None
    computed_hash: str


@router.get(
    "/reports/{report_id}/verify",
    response_model=ReportVerifyResponse,
    dependencies=[Depends(require_viewer)],
)
def verify_report(report_id: str) -> ReportVerifyResponse:
    """Recompute the report artifact hash and verify its stored signature.

    The artifact is regenerated deterministically from the persisted run record
    (the audit requirement: reports derive from stored runs, not live state),
    then re-hashed and checked against the integrity metadata recorded at
    generation time. If the report was never downloaded/generated there is no
    integrity record yet -> 404.
    """
    try:
        run = service.get_run(report_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error
    if run.job_type != "report_generation":
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.")

    metadata = run.result_summary.get(INTEGRITY_KEY) if isinstance(run.result_summary, dict) else None
    if not isinstance(metadata, dict):
        raise HTTPException(
            status_code=404,
            detail=f"Report '{report_id}' has no integrity record; generate/download it first.",
        )

    report = _to_report_summary(report_id)
    artifact, _ = _build_report_artifact(run, report.output_format)
    outcome = verify_artifact(artifact, metadata)

    fingerprint = outcome.get("public_key_fingerprint")
    if fingerprint is None:
        stored_pem = metadata.get("public_key_pem")
        if isinstance(stored_pem, str):
            fingerprint = fingerprint_for_pem(stored_pem)

    return ReportVerifyResponse(
        report_id=report_id,
        hash_matches=bool(outcome["hash_matches"]),
        signature_valid=outcome["signature_valid"],
        key_matches_current=outcome.get("key_matches_current"),
        signed_at=outcome["signed_at"],
        public_key_fingerprint=fingerprint,
        stored_hash=outcome["stored_hash"],
        computed_hash=str(outcome["computed_hash"]),
    )


@router.post("/backup", dependencies=[Depends(require_engineer)])
def create_backup() -> Response:
    """Build a signed backup bundle and return it as a download.

    Destructive-adjacent but read-only on the source: it snapshots the SQLite DB
    (online backup API), the secrets dir, and import files. Auth is enforced by
    the parent protected router.
    """
    sources = BackupSources(
        database_url=service.engine.url.render_as_string(hide_password=False),
        secrets_root=SECRETS_ROOT,
        imports_files_root=IMPORT_FILES_ROOT,
    )
    created_at = datetime.now(UTC)
    try:
        bundle = create_backup_bundle(
            sources,
            created_at=created_at,
            signing_key=load_signing_key(),
        )
    except BackupError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    file_name = f"smart_commissioning_backup_{created_at.strftime('%Y%m%dT%H%M%SZ')}.zip"
    return Response(
        content=bundle,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


class RetentionPreviewRequest(BaseModel):
    keep_days: int = Field(ge=0, description="Retain runs created within this many days.")


class RetentionApplyRequest(RetentionPreviewRequest):
    # An explicit, unambiguous confirmation token the caller MUST send to delete.
    confirm: bool = Field(description="Must be true to actually delete.")
    acknowledge: str = Field(
        description='Must equal "DELETE" to confirm a destructive purge.',
    )


@router.post("/retention/preview", dependencies=[Depends(require_engineer)])
def retention_preview(request: RetentionPreviewRequest) -> dict[str, object]:
    """DRY-RUN: report runs that WOULD be purged under the policy. Deletes nothing."""
    cutoff = cutoff_from_keep_days(request.keep_days)
    result = RetentionService(service.engine).preview(before=cutoff)
    return result.as_dict()


@router.post("/retention/apply", dependencies=[Depends(require_admin)])
def retention_apply(request: RetentionApplyRequest) -> dict[str, object]:
    """Destructive purge of eligible (non-evidence) runs older than the cutoff.

    Requires both ``confirm=true`` and ``acknowledge="DELETE"``; the service
    layer also re-checks confirm as defense in depth. Auth is enforced by the
    parent protected router.
    """
    if not request.confirm or request.acknowledge != "DELETE":
        raise HTTPException(
            status_code=400,
            detail='Destructive purge requires confirm=true and acknowledge="DELETE".',
        )
    cutoff = cutoff_from_keep_days(request.keep_days)
    result = RetentionService(service.engine).apply(before=cutoff, confirm=True)
    return result.as_dict()
