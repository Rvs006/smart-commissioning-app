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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from smart_commissioning_core.integrity import sha256_bytes
from smart_commissioning_core.rbac import Role

from app.api.routes.reports import _report_artifact_for_serving, _to_report_summary
from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.config import get_settings
from app.core.runtime import ARTIFACTS_ROOT, IMPORT_FILES_ROOT, REPORT_SIGNING_ROOT, SECRETS_ROOT
from app.core.scopes import load_scoped_report, require_global_admin
from app.services.backup_service import (
    BackupError,
    BackupSources,
    create_backup_bundle,
    encrypt_backup_bundle,
)
from app.services.report_artifacts import (
    ReportArtifactIntegrityError,
    verify_signed_manifest,
)
from app.services.reports_integrity import (
    INTEGRITY_KEY,
    fingerprint_for_pem,
    load_signing_key,
    verify_artifact,
)
from app.services.retention_service import (
    ObservationRetentionConflictError,
    ObservationRetentionNotFoundError,
    ObservationRetentionService,
    ObservationRetentionValidationError,
    RetentionService,
    cutoff_from_keep_days,
)
from app.services.run_service import RunService

router = APIRouter()
service = RunService()

# RBAC: verifying a report's integrity is read-only (viewer+); building a backup
# bundle and previewing retention are engineer+; APPLYING retention is a
# destructive purge reserved for admin.
require_viewer = require_role(Role.VIEWER)


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


class BackupRequest(BaseModel):
    recipient_public_key_pem: str | None = Field(default=None, max_length=16_384)


@router.get(
    "/reports/{report_id}/verify",
    response_model=ReportVerifyResponse,
    dependencies=[Depends(require_viewer)],
)
def verify_report(
    report_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> ReportVerifyResponse:
    """Recompute the report artifact hash and verify its stored signature.

    The artifact is regenerated deterministically from the persisted run record
    (the audit requirement: reports derive from stored runs, not live state),
    then re-hashed and checked against the integrity metadata recorded at
    generation time. If the report was never downloaded/generated there is no
    integrity record yet -> 404.
    """
    load_scoped_report(report_id, principal, engine=service.engine)
    try:
        run = service.get_report_for_serving(report_id)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error
    if run.job_type != "report_generation":
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.")

    report = _to_report_summary(run)
    try:
        artifact, _media_type, manifest = _report_artifact_for_serving(
            run,
            report.output_format,
            require_valid_signature=False,
        )
    except ReportArtifactIntegrityError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    if manifest is not None:
        computed_hash = sha256_bytes(artifact)
        stored_hash = manifest.get("artifact_sha256")
        stored_key_id = manifest.get("signing_key_id")
        try:
            current_key_id = load_signing_key().public_key_fingerprint()
        except Exception:
            current_key_id = None
        return ReportVerifyResponse(
            report_id=report_id,
            hash_matches=isinstance(stored_hash, str) and stored_hash == computed_hash,
            signature_valid=verify_signed_manifest(manifest),
            key_matches_current=(
                stored_key_id == current_key_id
                if isinstance(stored_key_id, str) and isinstance(current_key_id, str)
                else None
            ),
            signed_at=manifest.get("signed_at") if isinstance(manifest.get("signed_at"), str) else None,
            public_key_fingerprint=stored_key_id if isinstance(stored_key_id, str) else None,
            stored_hash=stored_hash if isinstance(stored_hash, str) else None,
            computed_hash=computed_hash,
        )

    metadata = run.result_summary.get(INTEGRITY_KEY) if isinstance(run.result_summary, dict) else None
    if not isinstance(metadata, dict):
        raise HTTPException(
            status_code=404,
            detail=f"Report '{report_id}' has no integrity record; generate/download it first.",
        )

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


@router.post("/backup")
def create_backup(
    request: BackupRequest | None = None,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> Response:
    """Build a signed backup bundle and return it as a download.

    Destructive-adjacent but read-only on the source: it snapshots the SQLite DB
    (online backup API), the secrets dir, and import files. Auth is enforced by
    the parent protected router.
    """
    recipient_public_key_pem = str(request.recipient_public_key_pem or "").strip() if request is not None else ""
    encryption_required = principal.source == "user_key" or get_settings().deployment_role != "standalone"
    if encryption_required and not recipient_public_key_pem:
        raise HTTPException(
            status_code=400,
            detail=(
                "Whole-runtime backup download requires a recipient RSA public key "
                "for named-user, edge, and hub deployments."
            ),
        )

    sources = BackupSources(
        database_url=service.engine.url.render_as_string(hide_password=False),
        secrets_root=SECRETS_ROOT,
        imports_files_root=IMPORT_FILES_ROOT,
        report_artifacts_root=ARTIFACTS_ROOT,
        report_signing_root=REPORT_SIGNING_ROOT,
    )
    created_at = datetime.now(UTC)
    try:
        bundle = create_backup_bundle(
            sources,
            created_at=created_at,
            signing_key=load_signing_key(),
        )
        if recipient_public_key_pem:
            bundle = encrypt_backup_bundle(bundle, recipient_public_key_pem)
    except BackupError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    encrypted = bool(recipient_public_key_pem)
    suffix = "scbackup" if encrypted else "zip"
    file_name = f"smart_commissioning_backup_{created_at.strftime('%Y%m%dT%H%M%SZ')}.{suffix}"
    return Response(
        content=bundle,
        media_type=("application/vnd.smart-commissioning.backup+json" if encrypted else "application/zip"),
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


@router.post("/retention/preview")
def retention_preview(
    request: RetentionPreviewRequest,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """DRY-RUN: report runs that WOULD be purged under the policy. Deletes nothing."""
    cutoff = cutoff_from_keep_days(request.keep_days)
    result = RetentionService(service.engine).preview(before=cutoff)
    return result.as_dict()


@router.post("/retention/apply")
def retention_apply(
    request: RetentionApplyRequest,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
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


class ObservationRetentionPreviewRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    keep_days: int = Field(
        default=30,
        description="Retain provisional observations for at least this many days after seal.",
    )
    batch_limit: int = Field(
        default=500,
        description="Maximum scoped observation rows inspected in each apply call.",
    )


class ObservationRetentionApplyRequest(BaseModel):
    acknowledge: str = Field(
        description='Must equal "DELETE PROVISIONAL OBSERVATIONS".',
    )


class RetentionHoldPlaceRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    hold_type: Literal["legal", "evidence"]
    reason: str = Field(min_length=1, max_length=4_096)
    evidence_set_id: str | None = Field(default=None, min_length=1, max_length=128)


class RetentionHoldReleaseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=4_096)


def _retention_actor(principal: AuthPrincipal) -> str:
    return principal.user_id or principal.source


def _raise_observation_retention_http(error: Exception) -> None:
    if isinstance(error, ObservationRetentionValidationError):
        raise HTTPException(status_code=400, detail=str(error)) from error
    if isinstance(error, ObservationRetentionNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ObservationRetentionConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise error


@router.post("/retention/observations/preview")
def observation_retention_preview(
    request: ObservationRetentionPreviewRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Persist an exact-scope preview and its immutable observation high-water."""
    try:
        return ObservationRetentionService(service.engine).preview(
            project_id=request.project_id,
            site_id=request.site_id,
            keep_days=request.keep_days,
            batch_limit=request.batch_limit,
            actor=_retention_actor(principal),
        )
    except (
        ObservationRetentionValidationError,
        ObservationRetentionNotFoundError,
        ObservationRetentionConflictError,
    ) as error:
        _raise_observation_retention_http(error)


@router.post("/retention/observations/jobs/{job_id}/apply")
def observation_retention_apply(
    job_id: str,
    request: ObservationRetentionApplyRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Delete one bounded batch, rechecking active slots and holds at apply time."""
    try:
        return ObservationRetentionService(service.engine).apply(
            job_id=job_id,
            acknowledge=request.acknowledge,
            actor=_retention_actor(principal),
        )
    except (
        ObservationRetentionValidationError,
        ObservationRetentionNotFoundError,
        ObservationRetentionConflictError,
    ) as error:
        _raise_observation_retention_http(error)


@router.get("/retention/observations/jobs/active")
def observation_retention_active_job(
    project_id: str,
    site_id: str,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Recover the active preview for one exact project/site scope."""
    try:
        return ObservationRetentionService(service.engine).get_active_job(
            project_id=project_id,
            site_id=site_id,
        )
    except ObservationRetentionNotFoundError as error:
        _raise_observation_retention_http(error)


@router.get("/retention/observations/jobs/{job_id}")
def observation_retention_job_status(
    job_id: str,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Return the durable cursor, status, actors, timestamps, and counts for a job."""
    try:
        return ObservationRetentionService(service.engine).get_job(job_id)
    except ObservationRetentionNotFoundError as error:
        _raise_observation_retention_http(error)


@router.post("/retention/holds")
def place_retention_hold(
    request: RetentionHoldPlaceRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Place an audited legal or evidence hold on one run."""
    try:
        return ObservationRetentionService(service.engine).place_hold(
            run_id=request.run_id,
            hold_type=request.hold_type,
            reason=request.reason,
            evidence_set_id=request.evidence_set_id,
            actor=_retention_actor(principal),
        )
    except (
        ObservationRetentionValidationError,
        ObservationRetentionNotFoundError,
        ObservationRetentionConflictError,
    ) as error:
        _raise_observation_retention_http(error)


@router.get("/retention/holds")
def list_retention_holds(
    project_id: str,
    site_id: str,
    include_released: bool = False,
    _principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """List active or historical holds for one exact project/site."""
    try:
        holds = ObservationRetentionService(service.engine).list_holds(
            project_id=project_id,
            site_id=site_id,
            include_released=include_released,
        )
    except ObservationRetentionNotFoundError as error:
        _raise_observation_retention_http(error)
    return {"holds": holds, "count": len(holds)}


@router.post("/retention/holds/{hold_id}/release")
def release_retention_hold(
    hold_id: str,
    request: RetentionHoldReleaseRequest,
    principal: AuthPrincipal = Depends(require_global_admin),
) -> dict[str, object]:
    """Release one active hold while retaining its audit row."""
    try:
        return ObservationRetentionService(service.engine).release_hold(
            hold_id=hold_id,
            reason=request.reason,
            actor=_retention_actor(principal),
        )
    except (
        ObservationRetentionValidationError,
        ObservationRetentionNotFoundError,
        ObservationRetentionConflictError,
    ) as error:
        _raise_observation_retention_http(error)
