import json
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Response
from openpyxl import Workbook

from app.schemas.jobs import ReportListResponse, ReportRequest, ReportSummary
from app.services.run_service import REPORT_JOB_TYPES, RunService

router = APIRouter()
service = RunService()


def _to_report_summary(report_id: str) -> ReportSummary:
    run = service.get_run(report_id)
    if run.job_type != "report_generation":
        raise FileNotFoundError(report_id)

    report_type = run.parameters.get("report_type", "evidence_pack")
    if not isinstance(report_type, str):
        report_type = "evidence_pack"
    output_format = run.parameters.get("output_format", "zip")
    if output_format not in {"docx", "xlsx", "zip"}:
        output_format = "zip"
    return ReportSummary(
        report_id=run.run_id,
        report_type=report_type,
        output_format=output_format,
        status=run.status,
        file_name=f"{report_type}_{run.run_id}.{output_format}",
    )


@router.post("", response_model=ReportSummary)
def create_report(request: ReportRequest) -> ReportSummary:
    _, report = service.create_report_run(request)
    return report


@router.get("", response_model=ReportListResponse)
def list_reports() -> ReportListResponse:
    reports: list[ReportSummary] = []
    for run in service.list_runs(job_types=REPORT_JOB_TYPES):
        reports.append(_to_report_summary(run.run_id))
    return ReportListResponse(reports=reports)


@router.get("/{report_id}", response_model=ReportSummary)
def get_report(report_id: str) -> ReportSummary:
    try:
        return _to_report_summary(report_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error


@router.get("/{report_id}/download")
def download_report(report_id: str) -> Response:
    try:
        run = service.get_run(report_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error
    if run.job_type != "report_generation":
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.")

    report = _to_report_summary(report_id)
    content, media_type = _build_report_artifact(run, report.output_format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{report.file_name}"'},
    )


def _build_report_artifact(run: object, output_format: str) -> tuple[bytes, str]:
    if output_format == "xlsx":
        return _build_xlsx_report(run), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if output_format == "docx":
        return _build_docx_report(run), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return _build_zip_report(run), "application/zip"


def _report_rows(run: object) -> list[tuple[str, str]]:
    parameters = getattr(run, "parameters")
    return [
        ("Report type", str(parameters.get("report_type", "evidence_pack"))),
        ("Output format", str(parameters.get("output_format", "zip")).upper()),
        ("Project", str(getattr(run, "project_id"))),
        ("Site", str(getattr(run, "site_id"))),
        ("Status", str(getattr(run, "status"))),
        ("Source runs", ", ".join(str(item) for item in parameters.get("source_run_ids", [])) or "All completed runs"),
        ("Generated", datetime.now(timezone.utc).isoformat()),
    ]


def _build_xlsx_report(run: object) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Report Summary"
    sheet.append(["Field", "Value"])
    for row in _report_rows(run):
        sheet.append(list(row))
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 56
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_docx_report(run: object) -> bytes:
    paragraphs = "\n".join(
        f"<w:p><w:r><w:t>{escape(label)}: {escape(value)}</w:t></w:r></w:p>"
        for label, value in _report_rows(run)
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Smart Commissioning Report</w:t></w:r></w:p>
    {paragraphs}
    <w:sectPr/>
  </w:body>
</w:document>"""
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _build_zip_report(run: object) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("summary.json", json.dumps(dict(_report_rows(run)), indent=2))
    return buffer.getvalue()
