import io
import zipfile
from pathlib import Path
from typing import get_args

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from smart_commissioning_core.rbac import Role

from app.api.uploads import check_content_length, read_upload_capped
from app.core.auth import require_role
from app.core.config import get_settings
from app.schemas.imports import (
    ImportBatchSummary,
    ImportErrorReport,
    ImportProfileSummary,
    ImportType,
)
from app.services.import_service import ImportService

# Two routers at the same /imports prefix:
#   * public_router  — the unauthenticated format helpers (profile list + blank
#     templates). router.py mounts it OUTSIDE the protected router so these GETs
#     need no API key, mirroring the health endpoints.
#   * router         — the authenticated import operations (upload + result reads),
#     mounted under the protected router (require_auth) with per-route RBAC.
public_router = APIRouter()
router = APIRouter()
service = ImportService()

# Valid import-type names, derived from the ImportType literal so the set stays
# in sync with the schema. Used to reject an unknown type with 400 (consistent
# with an unknown file extension) instead of FastAPI's enum-path-param 422.
_VALID_IMPORT_TYPES = set(get_args(ImportType))

# RBAC posture:
#   * GET /profiles and GET /templates/... are PUBLIC (no auth). They expose only
#     the import *format* — import-type names, required column headers, and one
#     synthetic example row — i.e. the documentation an engineer needs to prepare
#     a register before they have an API key. No project/site data is revealed.
#     (Hosted deployments still sit behind TLS + network isolation per
#     docs/team-pilot-deployment.md; this only removes the app-level key gate on
#     two static format helpers.)
#   * Reading an import's RESULTS (GET /{import_id}, /{import_id}/errors) stays
#     viewer+ — those reflect real uploaded register data.
#   * Creating an import (ingesting an uploaded register) stays engineer+.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)

def _guard_xlsx_decompressed_size(file_bytes: bytes, max_decompressed_bytes: int) -> None:
    """Basic zip-bomb guard: reject XLSX archives whose declared decompressed
    size exceeds the configured limit before handing them to openpyxl."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            declared_size = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid XLSX archive.") from error
    if declared_size > max_decompressed_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"XLSX decompressed size exceeds the maximum allowed {max_decompressed_bytes} bytes.",
        )


@public_router.get("/profiles", response_model=list[ImportProfileSummary])
def list_import_profiles() -> list[ImportProfileSummary]:
    # Public: import-format metadata only (see RBAC posture note above).
    return service.list_profiles()


@public_router.get("/templates/{import_type}.{file_type}")
def download_import_template(import_type: str, file_type: str) -> Response:
    # Public: a blank format helper (column headers + one example row), no data.
    # import_type is a plain str (not an ImportType path param) so an unknown
    # type returns 400 — consistent with an unknown extension — rather than the
    # 422 FastAPI would raise for an invalid enum path param. Validate here
    # before build_template, which would otherwise KeyError (500) on a bad type.
    if import_type not in _VALID_IMPORT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown import type '{import_type}'. Valid types: {', '.join(sorted(_VALID_IMPORT_TYPES))}.",
        )
    try:
        content = service.build_template(import_type, file_type)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    media_types = {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    file_names = {
        "csv": f"{import_type}_default_template.csv",
        "xlsx": f"{import_type}_default_template.xlsx",
    }
    return Response(
        content=content,
        media_type=media_types[file_type],
        headers={"Content-Disposition": f'attachment; filename="{file_names[file_type]}"'},
    )


@router.post("", response_model=ImportBatchSummary, dependencies=[Depends(require_engineer)])
async def create_import(
    request: Request,
    import_type: ImportType = Form(...),
    project_id: str | None = Form(default=None),
    site_id: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> ImportBatchSummary:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")

    settings = get_settings()

    # Fast pre-check on the declared body size, then the authoritative capped
    # read (see app.api.uploads).
    check_content_length(request, settings.max_upload_bytes)
    file_bytes = await read_upload_capped(file, settings.max_upload_bytes)

    if Path(file.filename).suffix.lower() == ".xlsx":
        _guard_xlsx_decompressed_size(file_bytes, settings.max_xlsx_decompressed_bytes)

    try:
        summary, _ = service.create_import(
            import_type=import_type,
            file_name=Path(file.filename).name,
            file_bytes=file_bytes,
            project_id=project_id,
            site_id=site_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return summary


# Declared BEFORE GET /{import_id} on purpose: a literal path must be registered
# ahead of the parameterised one, or "latest" is swallowed as an import_id and
# always 404s. Query params mirror createImport's project/site defaults so the
# lookup targets the same rows the upload wrote.
@router.get("/latest", response_model=ImportBatchSummary, dependencies=[Depends(require_viewer)])
def get_latest_import(
    import_type: ImportType,
    project_id: str | None = None,
    site_id: str | None = None,
) -> ImportBatchSummary:
    summary = service.get_latest_import(
        import_type=import_type, project_id=project_id, site_id=site_id
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="No import is on file for the given filters.")
    return summary


@router.get("/{import_id}", response_model=ImportBatchSummary, dependencies=[Depends(require_viewer)])
def get_import(import_id: str) -> ImportBatchSummary:
    try:
        return service.get_import(import_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Import '{import_id}' was not found.") from error


@router.get("/{import_id}/errors", response_model=ImportErrorReport, dependencies=[Depends(require_viewer)])
def get_import_errors(import_id: str) -> ImportErrorReport:
    try:
        return service.get_import_errors(import_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Import errors for '{import_id}' were not found.") from error
