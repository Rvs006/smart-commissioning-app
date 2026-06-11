import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from app.core.config import get_settings
from app.schemas.imports import (
    ImportBatchSummary,
    ImportErrorReport,
    ImportProfileSummary,
    ImportType,
)
from app.services.import_service import ImportService

router = APIRouter()
service = ImportService()

_READ_CHUNK_BYTES = 1024 * 1024


def _upload_too_large(max_upload_bytes: int) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"Uploaded file exceeds the maximum allowed size of {max_upload_bytes} bytes.",
    )


async def _read_upload_capped(file: UploadFile, max_upload_bytes: int) -> bytes:
    """Read the upload in chunks, rejecting once the cap is exceeded.

    The Content-Length header is checked before this as a fast pre-check, but
    the header cannot be trusted: this capped read is the authoritative limit.
    """
    buffer = bytearray()
    while chunk := await file.read(_READ_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > max_upload_bytes:
            raise _upload_too_large(max_upload_bytes)
    return bytes(buffer)


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


@router.get("/profiles", response_model=list[ImportProfileSummary])
def list_import_profiles() -> list[ImportProfileSummary]:
    return service.list_profiles()


@router.get("/templates/{import_type}.{file_type}")
def download_import_template(import_type: ImportType, file_type: str) -> Response:
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


@router.post("", response_model=ImportBatchSummary)
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

    # Fast pre-check on the declared body size (the multipart body is always
    # at least as large as the file). The header cannot be trusted, so the
    # capped read below enforces the limit on the bytes actually received.
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > settings.max_upload_bytes:
        raise _upload_too_large(settings.max_upload_bytes)

    file_bytes = await _read_upload_capped(file, settings.max_upload_bytes)

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


@router.get("/{import_id}", response_model=ImportBatchSummary)
def get_import(import_id: str) -> ImportBatchSummary:
    try:
        return service.get_import(import_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Import '{import_id}' was not found.") from error


@router.get("/{import_id}/errors", response_model=ImportErrorReport)
def get_import_errors(import_id: str) -> ImportErrorReport:
    try:
        return service.get_import_errors(import_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Import errors for '{import_id}' were not found.") from error
