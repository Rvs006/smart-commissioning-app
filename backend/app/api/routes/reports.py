import json
import re
from datetime import UTC, datetime
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from openpyxl import Workbook
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.jobs import ReportListResponse, ReportRequest, ReportSummary
from app.services.reports_integrity import INTEGRITY_KEY, build_integrity_metadata
from app.services.run_service import REPORT_JOB_TYPES, RunService

router = APIRouter()
service = RunService()

# RBAC: listing/reading/downloading a report is viewer+; generating a report
# (creating a report run) is engineer+.
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)


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


@router.post("", response_model=ReportSummary, dependencies=[Depends(require_engineer)])
def create_report(request: ReportRequest) -> ReportSummary:
    _, report = service.create_report_run(request)
    return report


@router.get("", response_model=ReportListResponse, dependencies=[Depends(require_viewer)])
def list_reports() -> ReportListResponse:
    reports: list[ReportSummary] = []
    for run in service.list_runs(job_types=REPORT_JOB_TYPES):
        reports.append(_to_report_summary(run.run_id))
    return ReportListResponse(reports=reports)


@router.get("/{report_id}", response_model=ReportSummary, dependencies=[Depends(require_viewer)])
def get_report(report_id: str) -> ReportSummary:
    try:
        return _to_report_summary(report_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error


@router.get("/{report_id}/download", dependencies=[Depends(require_viewer)])
def download_report(report_id: str) -> Response:
    try:
        run = service.get_run(report_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.") from error
    if run.job_type != "report_generation":
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' was not found.")

    report = _to_report_summary(report_id)
    content, media_type = _build_report_artifact(run, report.output_format)
    _persist_integrity(run, content)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{report.file_name}"'},
    )


def _generated_at(run: object) -> str:
    """Stable artifact-generation timestamp for the run.

    Reports MUST be derivable from the stored run record (the audit
    requirement), so the "Generated" field cannot be ``datetime.now`` at
    download time — that would make the bytes non-reproducible and break
    hash-based verification. The first download persists a fixed timestamp in
    result_summary; every later regeneration reuses it, yielding byte-identical
    artifacts that the verify endpoint can re-hash.
    """
    summary = run.result_summary if isinstance(run.result_summary, dict) else {}
    existing = summary.get("report_generated_at")
    if isinstance(existing, str) and existing:
        return existing
    generated_at = datetime.now(UTC).isoformat()
    service.update_result_summary(run.run_id, {"report_generated_at": generated_at})
    # Reflect the persisted value on the in-memory run so this request's artifact
    # matches what future regenerations will produce.
    if isinstance(run.result_summary, dict):
        run.result_summary["report_generated_at"] = generated_at
    return generated_at


def _persist_integrity(run: object, artifact: bytes) -> dict[str, object]:
    """Compute + persist SHA-256 + Ed25519 signature for the artifact bytes.

    Stored under result_summary["integrity"]. Recomputed every download so a
    regenerated (byte-identical) artifact re-confirms the recorded hash.
    """
    metadata = build_integrity_metadata(artifact)
    service.update_result_summary(run.run_id, {INTEGRITY_KEY: metadata})
    if isinstance(run.result_summary, dict):
        run.result_summary[INTEGRITY_KEY] = metadata
    return metadata


# Fixed member timestamp (1980-01-01, the zip epoch) so regenerated artifacts
# are byte-identical. Reports are derived from the stored run record, so the
# bytes must be reproducible for hash-based verification — embedded "now"
# timestamps (zipfile uses localtime; openpyxl stamps created/modified) would
# otherwise make every regeneration hash differently.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
# Fixed instant pinned into openpyxl core properties (docProps/core.xml).
_ARTIFACT_PROPERTIES_EPOCH = datetime(1980, 1, 1, tzinfo=UTC)


def _build_report_artifact(run: object, output_format: str) -> tuple[bytes, str]:
    if output_format == "xlsx":
        artifact = _normalize_zip_bytes(_build_xlsx_report(run))
        return artifact, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if output_format == "docx":
        artifact = _normalize_zip_bytes(_build_docx_report(run))
        return artifact, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return _normalize_zip_bytes(_build_zip_report(run)), "application/zip"


def _normalize_zip_bytes(data: bytes) -> bytes:
    """Rewrite a zip container with deterministic member order + timestamps.

    All three report formats are ZIP containers (xlsx/docx are OOXML zips). The
    rewrite sorts entries by name and pins every date_time to the zip epoch so
    the same run record always produces byte-identical artifacts, which is what
    lets the verify endpoint re-hash a regenerated artifact.
    """
    source = BytesIO(data)
    out = BytesIO()
    with ZipFile(source, "r") as reader, ZipFile(out, "w", ZIP_DEFLATED) as writer:
        for name in sorted(reader.namelist()):
            info = ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            writer.writestr(info, _pin_member_timestamps(name, reader.read(name)))
    return out.getvalue()


# openpyxl overwrites dcterms:modified with "now" on save regardless of the
# workbook properties, so pin it (and created) to the epoch in core.xml content.
_MODIFIED_RE = re.compile(rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)")


def _pin_member_timestamps(name: str, payload: bytes) -> bytes:
    """Pin OOXML core-property timestamps so the artifact bytes are reproducible."""
    if name == "docProps/core.xml":
        return _MODIFIED_RE.sub(rb"\g<1>1980-01-01T00:00:00Z\g<2>", payload)
    return payload


def _report_rows(run: object) -> list[tuple[str, str]]:
    parameters = run.parameters
    return [
        ("Report type", str(parameters.get("report_type", "evidence_pack"))),
        ("Output format", str(parameters.get("output_format", "zip")).upper()),
        ("Project", str(run.project_id)),
        ("Site", str(run.site_id)),
        ("Status", str(run.status)),
        ("Source runs", ", ".join(str(item) for item in parameters.get("source_run_ids", [])) or "All completed runs"),
        ("Generated", _generated_at(run)),
    ]


def _build_xlsx_report(run: object) -> bytes:
    workbook = Workbook()
    # openpyxl stamps docProps/core.xml with the current time on save; pin the
    # core properties to a fixed instant so the artifact bytes are reproducible
    # from the run record (required for hash-based verification).
    workbook.properties.created = _ARTIFACT_PROPERTIES_EPOCH
    workbook.properties.modified = _ARTIFACT_PROPERTIES_EPOCH
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
