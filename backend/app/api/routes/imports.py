from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from app.schemas.imports import (
    ImportBatchSummary,
    ImportErrorReport,
    ImportProfileSummary,
    ImportType,
)
from app.services.import_service import ImportService

router = APIRouter()
service = ImportService()


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
    import_type: ImportType = Form(...),
    project_id: str | None = Form(default=None),
    site_id: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> ImportBatchSummary:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")

    try:
        summary, _ = service.create_import(
            import_type=import_type,
            file_name=Path(file.filename).name,
            file_bytes=await file.read(),
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
