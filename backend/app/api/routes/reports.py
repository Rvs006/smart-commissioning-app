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
from app.services.report_pdf import PdfDocument
from app.services.reports_integrity import INTEGRITY_KEY, build_integrity_metadata
from app.services.run_service import REPORT_JOB_TYPES, VALIDATION_JOB_TYPES, RunService

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
    if output_format not in {"docx", "pdf", "xlsx", "zip"}:
        output_format = "zip"
    # Scoped source runs, read back from the stored parameters. Same defensive
    # shape-checking as report_type/output_format above: a run record is
    # persisted JSON, so nothing guarantees the list survived as a list of str.
    # (Attribute access on run.parameters is unredacted — the field_serializer
    # only fires on serialization — and run ids are not sensitive.)
    raw_source_run_ids = run.parameters.get("source_run_ids")
    source_run_ids = (
        [item for item in raw_source_run_ids if isinstance(item, str)]
        if isinstance(raw_source_run_ids, list)
        else []
    )
    return ReportSummary(
        report_id=run.run_id,
        report_type=report_type,
        output_format=output_format,
        status=run.status,
        file_name=f"{report_type}_{run.run_id}.{output_format}",
        created_at=run.created_at,
        source_run_ids=source_run_ids,
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
    # PDF is NOT a zip container, so it must skip the zip normalisation pass the
    # OOXML/zip formats need; it is deterministic by construction instead (no
    # /CreationDate, no /ID — see app.services.report_pdf).
    if output_format == "pdf":
        return _build_pdf_report(run), "application/pdf"
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
        (
            "Source runs",
            ", ".join(str(item) for item in parameters.get("source_run_ids", []))
            or "None selected (no run findings included)",
        ),
        ("Generated", _generated_at(run)),
    ]


# Section titles for the end-to-end validation report (field ask 2026-07-14).
# The frontend references these strings — keep them stable across formats.
_SUMMARY_SECTION_TITLE = "Summary"
_FAILURE_SECTION_TITLE = "Failure detail"
_SILENT_SECTION_TITLE = "Silent systems"
_SILENT_NOTE = (
    "Silent systems are devices that published nothing within the allowed capture window; "
    "they are neither validated nor failed."
)
_NO_SILENT_NOTE = "No silent systems: every expected device published within the capture window."
_NO_FINDINGS_NOTE = "No findings in the scoped source runs."
# Fallback Device ID for pre-upgrade validation runs that recorded only the
# silent-device COUNT (not_publishing) and not the ids themselves.
_SILENT_IDS_NOT_RECORDED = "(ids not recorded by this run's app version)"
# Rendered where a run's record simply does not carry a value.
_NO_VALUE = "—"

_FINDING_COLUMNS = (
    "Source Run",
    "Issue ID",
    "Asset",
    "Severity",
    "Type",
    "Point",
    "Expected",
    "Observed",
    "Suggested Action",
    "Description",
)

_VALIDATION_SUMMARY_COLUMNS = (
    "Source Run",
    "Type",
    "Status",
    "Expected Devices",
    "Publishing",
    "Silent",
    "Blocking Issues",
    "Compliance %",
)

_SILENT_COLUMNS = ("Source Run", "Device ID")


def _source_runs(run: object) -> list[object]:
    """The report's source runs, in the order they were scoped (missing skipped).

    Order-preserving dedupe: a run id scoped twice must contribute one Summary
    row and one set of findings, not doubled device/blocking totals.
    """
    source_ids = run.parameters.get("source_run_ids", [])
    if not isinstance(source_ids, list):
        return []
    sources: list[object] = []
    seen: set[str] = set()
    for source_id in source_ids:
        key = str(source_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            sources.append(service.get_run(key))
        except FileNotFoundError:
            continue
    return sources


def _source_run_findings(run: object) -> list[dict[str, str]]:
    """Issues from the report's source runs, flattened + deterministically sorted.

    A report scoped to ``source_run_ids`` must carry the ACTUAL findings of those
    runs (not just an id label) to be a usable MSI handover deliverable. Terminal
    runs' issues are immutable, so the sorted output keeps the artifact
    byte-reproducible (required by the integrity verify endpoint).
    """
    findings: list[dict[str, str]] = []
    for source in _source_runs(run):
        for issue in source.issues:
            findings.append(
                {
                    "Source Run": str(source.run_id),
                    "Issue ID": str(getattr(issue, "issue_id", "") or ""),
                    "Asset": str(getattr(issue, "asset_id", "") or ""),
                    "Severity": str(getattr(issue, "severity", "") or ""),
                    "Type": str(getattr(issue, "issue_type", "") or ""),
                    "Point": str(getattr(issue, "point_name", "") or ""),
                    "Expected": str(getattr(issue, "expected_value", "") or ""),
                    "Observed": str(getattr(issue, "observed_value", "") or ""),
                    "Suggested Action": str(getattr(issue, "suggested_action", "") or ""),
                    "Description": str(getattr(issue, "description", "") or ""),
                }
            )
    findings.sort(key=lambda finding: (finding["Source Run"], finding["Issue ID"]))
    return findings


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


# Severities that block a clean handover. Mirrors core's
# udmi_validation._BLOCKING_SEVERITIES (critical + high/medium, which the
# workbench renders as "Fail") so a count derived from persisted issue records
# can never disagree with a run's own persisted blocking_issue_count.
_BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})


def _blocking_issue_count(source: object) -> int:
    """Blocking issues derived from the run's own persisted issue records.

    Pre-upgrade runs never recorded blocking_issue_count, and without a count
    the ≤99 compliance clamp cannot fire — the report could print "100%
    (liveness)" beside critical findings.
    """
    return sum(
        1
        for issue in source.issues
        if str(getattr(issue, "severity", "") or "").casefold() in _BLOCKING_SEVERITIES
    )


def _validation_summary(run: object) -> dict[str, object] | None:
    """Summary + Silent-systems data for a report scoped to validation runs.

    Returns None when no source run is a validation run, so non-validation
    reports omit the validation sections entirely. Discovery source runs are
    excluded from the rows (they are inventory, not validation outcomes).
    """
    sources = [source for source in _source_runs(run) if source.job_type in VALIDATION_JOB_TYPES]
    if not sources:
        return None

    rows: list[dict[str, str]] = []
    silent_rows: list[dict[str, str]] = []
    total_expected = total_silent = total_blocking = 0
    # Device-percent units: overall % = floor(sum(percent_i * expected_i) / sum(expected_i)).
    weighted_percent_sum = 0
    scored_expected_sum = 0
    liveness_used = False
    for source in sources:
        summary = source.result_summary if isinstance(source.result_summary, dict) else {}
        expected = _int_or_none(summary.get("expected_devices"))
        publishing = _int_or_none(summary.get("publishing_seen"))
        silent = _int_or_none(summary.get("not_publishing"))
        blocking = _int_or_none(summary.get("blocking_issue_count"))
        if blocking is None:
            # Pre-upgrade run that never persisted the count: derive it from
            # the run's issue records so the ≤99 clamps below still fire.
            blocking = _blocking_issue_count(source)

        compliance = _NO_VALUE
        if "payload_conformance_percent" in summary:
            percent = _int_or_none(summary.get("payload_conformance_percent"))
            if percent is not None:
                compliance = f"{percent}%"
                if expected:
                    weighted_percent_sum += percent * expected
                    scored_expected_sum += expected
        elif expected and publishing is not None:
            # Pre-upgrade source run without the conformance fields: fall back to
            # publishing liveness and SAY SO — liveness is not conformance.
            percent = round(100 * publishing / expected)
            if blocking > 0:
                # Mirror core's per-run clamp: 100% beside a blocking issue is a lie.
                percent = min(percent, 99)
            compliance = f"{percent}% (liveness)"
            weighted_percent_sum += 100 * publishing
            scored_expected_sum += expected
            liveness_used = True

        rows.append(
            {
                "Source Run": str(source.run_id),
                "Type": str(source.job_type),
                "Status": str(source.status),
                "Expected Devices": str(expected) if expected is not None else _NO_VALUE,
                "Publishing": str(publishing) if publishing is not None else _NO_VALUE,
                "Silent": str(silent) if silent is not None else _NO_VALUE,
                "Blocking Issues": str(blocking),
                "Compliance %": compliance,
            }
        )
        total_expected += expected or 0
        total_silent += silent or 0
        total_blocking += blocking

        device_ids = summary.get("not_publishing_devices")
        if isinstance(device_ids, list):
            for device_id in sorted(str(device) for device in device_ids):
                silent_rows.append({"Source Run": str(source.run_id), "Device ID": device_id})
        elif (silent or 0) > 0:
            silent_rows.append({"Source Run": str(source.run_id), "Device ID": _SILENT_IDS_NOT_RECORDED})

    # Overall compliance = floor(100 * conforming-devices-sum / expected-devices-sum),
    # where each run contributes conforming ~= percent_i * expected_i / 100 devices
    # (publishing_seen for pre-upgrade liveness runs). min() across runs is dishonest
    # (one dirty run would mask nothing / one clean run everything) and an unweighted
    # mean of percents ignores fleet size; the device-weighted ratio does neither.
    if scored_expected_sum:
        overall_percent = weighted_percent_sum // scored_expected_sum
        if total_blocking > 0:
            # Mirror the per-run clamp: 100% next to a blocking issue is a lie.
            overall_percent = min(overall_percent, 99)
        overall_compliance = f"{overall_percent}%" + (" (liveness)" if liveness_used else "")
    else:
        overall_compliance = _NO_VALUE

    overall = {
        "Total Devices": total_expected,
        "Total Silent": total_silent,
        "Total Blocking Issues": total_blocking,
        "Overall Compliance %": overall_compliance,
    }
    overall_row = {
        "Source Run": "Overall",
        "Type": "",
        "Status": "",
        "Expected Devices": str(total_expected),
        "Publishing": "",
        "Silent": str(total_silent),
        "Blocking Issues": str(total_blocking),
        "Compliance %": overall_compliance,
    }
    overall_text = (
        f"Overall: {total_expected} devices, {total_silent} silent, "
        f"{total_blocking} blocking issues, {overall_compliance} compliance."
    )
    return {
        "rows": rows,
        "silent_rows": silent_rows,
        "overall": overall,
        "overall_row": overall_row,
        "overall_text": overall_text,
    }


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
    # Validation sections (Summary / Silent systems) only when the scoped source
    # runs include validation runs; other report types keep their prior shape.
    validation = _validation_summary(run)
    if validation is not None:
        summary_sheet = workbook.create_sheet(_SUMMARY_SECTION_TITLE)
        summary_sheet.append(list(_VALIDATION_SUMMARY_COLUMNS))
        for row in validation["rows"]:
            summary_sheet.append([row[column] for column in _VALIDATION_SUMMARY_COLUMNS])
        summary_sheet.append([validation["overall_row"][column] for column in _VALIDATION_SUMMARY_COLUMNS])
        for column, width in {"A": 26, "B": 18, "C": 12, "D": 16, "E": 12, "F": 10, "G": 16, "H": 18}.items():
            summary_sheet.column_dimensions[column].width = width
    # Failure detail: findings from the scoped source runs (the actual report
    # content, not just the metadata above). Empty source runs -> header-only.
    findings = _source_run_findings(run)
    findings_sheet = workbook.create_sheet(_FAILURE_SECTION_TITLE)
    findings_sheet.append(list(_FINDING_COLUMNS))
    for finding in findings:
        findings_sheet.append([finding[column] for column in _FINDING_COLUMNS])
    findings_widths = {"A": 26, "B": 16, "C": 18, "D": 12, "E": 22, "F": 24, "G": 18, "H": 18, "I": 40, "J": 70}
    for column, width in findings_widths.items():
        findings_sheet.column_dimensions[column].width = width
    if validation is not None:
        silent_sheet = workbook.create_sheet(_SILENT_SECTION_TITLE)
        silent_sheet.append([_SILENT_NOTE])
        silent_sheet.append(list(_SILENT_COLUMNS))
        for row in validation["silent_rows"]:
            silent_sheet.append([row[column] for column in _SILENT_COLUMNS])
        silent_sheet.column_dimensions["A"].width = 30
        silent_sheet.column_dimensions["B"].width = 46
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _docx_paragraph(text: str, *, bold: bool = False) -> str:
    run_properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:p><w:r>{run_properties}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


# Single hairline borders so the hand-rolled tables read as tables in Word.
_DOCX_TABLE_BORDERS = "".join(
    f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
)


def _docx_table(columns: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    def cell(text: str, *, bold: bool = False) -> str:
        return f"<w:tc>{_docx_paragraph(text, bold=bold)}</w:tc>"

    grid = "".join("<w:gridCol/>" for _ in columns)
    header = "<w:tr>" + "".join(cell(column, bold=True) for column in columns) + "</w:tr>"
    body = "".join(
        "<w:tr>" + "".join(cell(row.get(column, "")) for column in columns) + "</w:tr>" for row in rows
    )
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>{_DOCX_TABLE_BORDERS}</w:tblBorders>'
        f"</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{header}{body}</w:tbl>"
    )


def _build_docx_report(run: object) -> bytes:
    blocks: list[str] = [_docx_paragraph("Smart Commissioning Report", bold=True)]
    blocks.extend(_docx_paragraph(f"{label}: {value}") for label, value in _report_rows(run))

    validation = _validation_summary(run)
    if validation is not None:
        blocks.append(_docx_paragraph(_SUMMARY_SECTION_TITLE, bold=True))
        blocks.append(_docx_table(_VALIDATION_SUMMARY_COLUMNS, validation["rows"]))
        # The paragraph after each table doubles as the Word-required trailing
        # paragraph (a body may not end <w:tbl><w:sectPr/>).
        blocks.append(_docx_paragraph(validation["overall_text"]))

    blocks.append(_docx_paragraph(_FAILURE_SECTION_TITLE, bold=True))
    findings = _source_run_findings(run)
    if findings:
        blocks.append(_docx_table(_FINDING_COLUMNS, findings))
        blocks.append(_docx_paragraph(""))
    else:
        blocks.append(_docx_paragraph(_NO_FINDINGS_NOTE))

    if validation is not None:
        blocks.append(_docx_paragraph(_SILENT_SECTION_TITLE, bold=True))
        blocks.append(_docx_paragraph(_SILENT_NOTE))
        if validation["silent_rows"]:
            blocks.append(_docx_table(_SILENT_COLUMNS, validation["silent_rows"]))
            blocks.append(_docx_paragraph(""))
        else:
            blocks.append(_docx_paragraph(_NO_SILENT_NOTE))

    body_xml = "\n    ".join(blocks)
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {body_xml}
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
        # The actual findings from the scoped source runs (deterministically
        # ordered so the artifact stays byte-reproducible).
        archive.writestr("findings.json", json.dumps(_source_run_findings(run), indent=2))
        # Parity with the document formats: the validation sections ship as
        # their own JSON members when the source runs include validation runs.
        validation = _validation_summary(run)
        if validation is not None:
            archive.writestr(
                "validation_summary.json",
                json.dumps(
                    {
                        "columns": list(_VALIDATION_SUMMARY_COLUMNS),
                        "rows": validation["rows"],
                        "overall": validation["overall"],
                    },
                    indent=2,
                ),
            )
            archive.writestr(
                "silent_systems.json",
                json.dumps({"note": _SILENT_NOTE, "rows": validation["silent_rows"]}, indent=2),
            )
    return buffer.getvalue()


# Relative column weights for the fixed-width PDF tables (long text columns get
# more room; every cell truncates with an ellipsis rather than overflowing).
# The dense tables render at 9pt and the summary weights are sized so the
# column headers and the compliance cell — including its honesty-critical
# "(liveness)" marker — always fit; run ids may truncate (they appear in full
# in the header rows and the Silent systems table).
_PDF_SUMMARY_WEIGHTS = (71, 68, 49, 82, 52, 30, 74, 69)
_PDF_SILENT_WEIGHTS = (1.0, 1.6)
_PDF_DENSE_TABLE_SIZE = 9.0

# PDF Failure detail renders in two parts: a slim identity table (short,
# id-like fields only — weighted so every header and a full run id fit at 9pt)
# followed per finding by word-wrapped paragraphs for the long free-text
# fields, which a 10-column table truncated into uselessness.
_PDF_FINDING_IDENTITY_COLUMNS = ("Source Run", "Issue ID", "Asset", "Severity", "Type", "Point")
_PDF_FINDING_IDENTITY_WEIGHTS = (136, 62, 72, 47, 86, 92)
_PDF_FINDING_DETAIL_FIELDS = ("Expected", "Observed", "Suggested Action", "Description")


def _build_pdf_report(run: object) -> bytes:
    document = PdfDocument()
    document.add_heading("Smart Commissioning Report", level=1)
    for label, value in _report_rows(run):
        document.add_paragraph(f"{label}: {value}")

    validation = _validation_summary(run)
    if validation is not None:
        document.add_heading(_SUMMARY_SECTION_TITLE)
        document.add_table(
            _VALIDATION_SUMMARY_COLUMNS,
            [[row[column] for column in _VALIDATION_SUMMARY_COLUMNS] for row in validation["rows"]],
            widths=_PDF_SUMMARY_WEIGHTS,
            size=_PDF_DENSE_TABLE_SIZE,
        )
        document.add_paragraph(validation["overall_text"])

    document.add_heading(_FAILURE_SECTION_TITLE)
    findings = _source_run_findings(run)
    if findings:
        document.add_table(
            _PDF_FINDING_IDENTITY_COLUMNS,
            [[finding[column] for column in _PDF_FINDING_IDENTITY_COLUMNS] for finding in findings],
            widths=_PDF_FINDING_IDENTITY_WEIGHTS,
            size=_PDF_DENSE_TABLE_SIZE,
        )
        # Long free-text fields render as full-width wrapped paragraphs (the
        # existing deterministic wrap), never truncated table cells.
        for finding in findings:
            identity = " — ".join(
                part
                for part in (finding["Issue ID"], finding["Asset"], finding["Point"])
                if part
            )
            document.add_paragraph(identity or finding["Source Run"], bold=True)
            for field in _PDF_FINDING_DETAIL_FIELDS:
                if finding[field]:
                    document.add_paragraph(f"{field}: {finding[field]}")
    else:
        document.add_paragraph(_NO_FINDINGS_NOTE)

    if validation is not None:
        document.add_heading(_SILENT_SECTION_TITLE)
        document.add_paragraph(_SILENT_NOTE)
        if validation["silent_rows"]:
            document.add_table(
                _SILENT_COLUMNS,
                [[row[column] for column in _SILENT_COLUMNS] for row in validation["silent_rows"]],
                widths=_PDF_SILENT_WEIGHTS,
            )
        else:
            document.add_paragraph(_NO_SILENT_NOTE)
    return document.render()
