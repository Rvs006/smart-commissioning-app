import json
import re
from datetime import UTC, datetime
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, Depends, HTTPException, Response
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from smart_commissioning_core.db.repositories import DiscoveryRepository
from smart_commissioning_core.rbac import Role

from app.core.auth import require_role
from app.schemas.jobs import ReportListResponse, ReportRequest, ReportSummary
from app.services.report_pdf import PdfDocument
from app.services.reports_integrity import INTEGRITY_KEY, build_integrity_metadata
from app.services.run_service import (
    DISCOVERY_JOB_TYPES,
    REPORT_JOB_TYPES,
    VALIDATION_JOB_TYPES,
    RunService,
)

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


# Report branding furniture (field ask 2026-07-15, ITP witnessing packs). One
# wordmark, one look across pdf/docx/xlsx page furniture: header = wordmark +
# document title with a thin rule; footer = wordmark + page number + the run id
# (traceability back to a run record on a printed page). The run id is stable
# per run, so the bytes stay reproducible. Phase 1 is text only — logo image
# embedding is deferred to a later release. The zip and topics.xlsx export are
# deliberately NOT branded (zip has no page concept; the export is not a report).
_BRAND_NAME = "ELECTRACOM"
_BRAND_DOC_TITLE = "Smart Commissioning Report"

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

# Discovery inventory sections (field ask 2026-07-15, per-head handover packs).
# The frontend / API may reference these titles — keep them stable across
# formats, same convention as the validation section titles above.
_INVENTORY_SUMMARY_TITLE = "Discovery summary"
_INVENTORY_IP_TITLE = "Discovered IP hosts"
_INVENTORY_BACNET_DEVICES_TITLE = "Discovered BACnet devices"
_INVENTORY_BACNET_POINTS_TITLE = "Discovered BACnet points"
_INVENTORY_BACNET_SILENT_TITLE = "Expected BACnet devices not responding"
_INVENTORY_MQTT_TITLE = "Discovered MQTT topics"

_INVENTORY_SUMMARY_COLUMNS = ("Source Run", "Type", "Status", "Counts")
_IP_INVENTORY_COLUMNS = (
    "Source Run",
    "Address",
    "Hostname",
    "MAC",
    "Open Ports",
    "Forbidden Open",
    "Unexpected Open",
    "Missing Expected",
)
_BACNET_DEVICE_COLUMNS = (
    "Source Run",
    "Instance",
    "Address",
    "Name",
    "Vendor",
    "Model",
    "Register Asset",
    "Points",
)
_BACNET_POINT_COLUMNS = ("Source Run", "Device", "Point ID", "Point Name", "Value", "Units")
_BACNET_SILENT_COLUMNS = ("Source Run", "Register Asset", "Instance", "Address", "Directed Who-Is")
_MQTT_TOPIC_COLUMNS = ("Source Run", "Topic", "Messages", "Device Ref")

# Honesty-rule wording for the expected-but-silent section: it mirrors the
# engine's own framing (bacnet_discovery: "amber, never a failure, never device
# absent") and the BACnet-135 rationale. It must never read "fail" or "absent".
_BACNET_SILENT_NOTE = (
    "Expected devices that did not answer during the scan window. Directed-Who-Is silence is "
    "inconclusive under BACnet-135 (an off-subnet device may reply with a local broadcast we "
    "cannot hear); these rows are neither confirmed present nor absent."
)
# Shown when a discovery head was scoped but recorded no rows: an empty scan is
# a recorded result, not a gap in the report.
_INVENTORY_EMPTY_NOTE = (
    "No rows recorded by the scoped discovery runs (an empty scan is a recorded result, "
    "not an omission)."
)

# Excel caps sheet names at 31 chars; only the silent title exceeds it, so map
# it to a short unique name (the full title is surfaced in the sheet's row 1).
_INVENTORY_SHEET_NAMES = {_INVENTORY_BACNET_SILENT_TITLE: "Expected not responding"}

# xlsx column widths by column name (reused across every inventory sheet — the
# names are unique enough that one map covers all sections). Wide free-text
# columns (Topic/Point Name/Counts) get room; id-like columns a fixed width.
_INVENTORY_COLUMN_WIDTHS = {
    "Source Run": 26,
    "Address": 22,
    "Hostname": 24,
    "MAC": 20,
    "Open Ports": 24,
    "Forbidden Open": 16,
    "Unexpected Open": 16,
    "Missing Expected": 16,
    "Instance": 12,
    "Name": 28,
    "Vendor": 18,
    "Model": 18,
    "Register Asset": 26,
    "Points": 10,
    "Device": 20,
    "Point ID": 18,
    "Point Name": 40,
    "Value": 20,
    "Units": 12,
    "Directed Who-Is": 16,
    "Topic": 40,
    "Messages": 12,
    "Device Ref": 24,
    "Type": 18,
    "Status": 14,
    "Counts": 60,
}


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


def _inv_cell(value: object) -> str:
    """Render a single inventory cell: str() a real value, _NO_VALUE for blank/None.

    Never fabricates: an absent value or an empty list is shown as a blank, never
    a synthesised verdict (honesty rule).
    """
    if value is None:
        return _NO_VALUE
    text = str(value)
    return text if text else _NO_VALUE


def _inv_ports(value: object) -> str:
    """Join a persisted port list; an empty/absent list renders as _NO_VALUE.

    The engine stamps these lists (open_ports, forbidden_open_ports, …); the
    report renders the recorded fact, never a recomputed "fail" for an empty
    list (a host with no open ports is a recorded result).
    """
    if isinstance(value, list) and value:
        return ", ".join(str(port) for port in value)
    return _NO_VALUE


def _inv_count(value: object) -> str:
    """A count fragment for the Discovery-summary Counts cell.

    A missing key renders as _NO_VALUE — never an invented number.
    """
    count = _int_or_none(value)
    return str(count) if count is not None else _NO_VALUE


def _discovery_inventory(run: object) -> list[dict[str, object]] | None:
    """Ordered inventory sections for a report scoped to discovery runs.

    Returns None when no source run is a discovery run (non-discovery reports
    omit the inventory entirely), otherwise a list of section dicts
    ``{title, columns, rows, note}`` so every format builder is one generic loop.

    Determinism: device/point/topic rows inherit DiscoveryRepository's
    ``ORDER BY (position, id)`` over the immutable terminal-run rows; the only
    rows not already DB-ordered are the expected-not-responding rows, sorted here
    by (asset_id, device_instance, source run). No repository ``id`` or
    ``created_at`` field is ever placed in a rendered row — including one would
    break byte-reproducibility of the signed artifact.
    """
    sources = [source for source in _source_runs(run) if source.job_type in DISCOVERY_JOB_TYPES]
    if not sources:
        return None

    repo = DiscoveryRepository(service.engine)

    summary_rows: list[dict[str, str]] = []
    ip_rows: list[dict[str, str]] = []
    device_rows: list[dict[str, str]] = []
    point_rows: list[dict[str, str]] = []
    topic_rows: list[dict[str, str]] = []
    # (sort key, row) so the silent rows can be ordered independently of scope.
    silent_entries: list[tuple[tuple[str, str, str], dict[str, str]]] = []

    has_ip = has_bacnet = has_mqtt = has_silent = False

    for source in sources:
        run_id = str(source.run_id)
        summary = source.result_summary if isinstance(source.result_summary, dict) else {}

        if source.job_type == "ip_discovery":
            has_ip = True
            devices = [
                device
                for device in repo.list_devices(source.run_id)
                if device.get("device_type") == "ip_host"
            ]
            for device in devices:
                attributes = device.get("attributes") or {}
                ip_rows.append(
                    {
                        "Source Run": run_id,
                        "Address": _inv_cell(device.get("address")),
                        "Hostname": _inv_cell(device.get("name")),
                        "MAC": _inv_cell(attributes.get("mac_address")),
                        "Open Ports": _inv_ports(attributes.get("open_ports")),
                        "Forbidden Open": _inv_ports(attributes.get("forbidden_open_ports")),
                        "Unexpected Open": _inv_ports(attributes.get("unexpected_open_ports")),
                        "Missing Expected": _inv_ports(attributes.get("missing_expected_ports")),
                    }
                )
            counts = f"{_inv_count(summary.get('hosts_scanned'))} hosts scanned, "
            counts += f"{_inv_count(summary.get('hosts_responsive'))} responsive"

        elif source.job_type == "bacnet_discovery":
            has_bacnet = True
            devices = [
                device
                for device in repo.list_devices(source.run_id)
                if device.get("device_type") == "bacnet_device"
            ]
            points = repo.list_points(source.run_id)
            # Points per device: the point's device_ref is the device's asset_id.
            points_by_asset: dict[object, int] = {}
            for point in points:
                points_by_asset[point.get("device_ref")] = points_by_asset.get(point.get("device_ref"), 0) + 1
            for device in devices:
                attributes = device.get("attributes") or {}
                register = attributes.get("register_asset_name") or attributes.get("register_asset_id")
                device_rows.append(
                    {
                        "Source Run": run_id,
                        "Instance": _inv_cell(attributes.get("device_instance")),
                        "Address": _inv_cell(device.get("address")),
                        "Name": _inv_cell(device.get("name")),
                        "Vendor": _inv_cell(device.get("vendor")),
                        "Model": _inv_cell(device.get("model")),
                        "Register Asset": _inv_cell(register),
                        "Points": str(points_by_asset.get(attributes.get("asset_id"), 0)),
                    }
                )
            for point in points:
                attributes = point.get("attributes") or {}
                read_error = attributes.get("read_error")
                if read_error:
                    # A read failure is rendered as the error, never a value.
                    value = str(read_error)
                else:
                    observed = point.get("observed_value") or {}
                    value = _inv_cell(observed.get("value"))
                point_rows.append(
                    {
                        "Source Run": run_id,
                        "Device": _inv_cell(point.get("device_ref")),
                        "Point ID": _inv_cell(point.get("point_id")),
                        "Point Name": _inv_cell(point.get("point_name")),
                        "Value": value,
                        "Units": _inv_cell(point.get("units")),
                    }
                )
            counts = f"{len(devices)} devices, {len(points)} points read"
            if "expected_responding_count" in summary and "expected_device_count" in summary:
                responding = _inv_count(summary.get("expected_responding_count"))
                expected = _inv_count(summary.get("expected_device_count"))
                counts += f", {responding}/{expected} expected devices responding"
            # Pre-v0.1.12 bacnet runs never persisted expected_not_responding;
            # gate the silent section on the key's presence, not truthiness.
            if "expected_not_responding" in summary:
                has_silent = True
                silent_list = summary.get("expected_not_responding")
                if isinstance(silent_list, list):
                    for entry in silent_list:
                        if not isinstance(entry, dict):
                            continue
                        asset_id = entry.get("asset_id")
                        asset_name = entry.get("asset_name")
                        instance = entry.get("device_instance")
                        if asset_name and asset_id is not None:
                            register = f"{asset_name} ({asset_id})"
                        else:
                            register = asset_name or asset_id
                        directed = "sent" if entry.get("directed_probe_sent") else "not sent"
                        silent_entries.append(
                            (
                                (str(asset_id), str(instance), run_id),
                                {
                                    "Source Run": run_id,
                                    "Register Asset": _inv_cell(register),
                                    "Instance": _inv_cell(instance),
                                    "Address": _inv_cell(entry.get("address")),
                                    "Directed Who-Is": directed,
                                },
                            )
                        )

        else:  # mqtt_discovery
            has_mqtt = True
            for topic in repo.list_topics(source.run_id):
                attributes = topic.get("attributes") or {}
                # last_payload is deliberately excluded — non-tabular, and a
                # signed shareable artifact should not embed captured payloads.
                topic_rows.append(
                    {
                        "Source Run": run_id,
                        "Topic": _inv_cell(topic.get("topic")),
                        "Messages": _inv_cell(topic.get("message_count")),
                        "Device Ref": _inv_cell(attributes.get("device_ref")),
                    }
                )
            counts = f"{_inv_count(summary.get('topics_discovered'))} topics, "
            counts += f"{_inv_count(summary.get('messages_captured'))} messages"

        summary_rows.append(
            {
                "Source Run": run_id,
                "Type": str(source.job_type),
                "Status": str(source.status),
                "Counts": counts,
            }
        )

    silent_entries.sort(key=lambda item: item[0])
    silent_rows = [row for _, row in silent_entries]

    def _section(
        title: str,
        columns: tuple[str, ...],
        rows: list[dict[str, str]],
        *,
        note: str | None = None,
    ) -> dict[str, object]:
        # Non-silent sections with no rows carry the empty note so every builder
        # renders "empty scan is a recorded result" instead of a bare heading.
        effective_note = note if note is not None else (None if rows else _INVENTORY_EMPTY_NOTE)
        return {"title": title, "columns": columns, "rows": rows, "note": effective_note}

    sections: list[dict[str, object]] = [
        _section(_INVENTORY_SUMMARY_TITLE, _INVENTORY_SUMMARY_COLUMNS, summary_rows)
    ]
    if has_ip:
        sections.append(_section(_INVENTORY_IP_TITLE, _IP_INVENTORY_COLUMNS, ip_rows))
    if has_bacnet:
        sections.append(_section(_INVENTORY_BACNET_DEVICES_TITLE, _BACNET_DEVICE_COLUMNS, device_rows))
        sections.append(_section(_INVENTORY_BACNET_POINTS_TITLE, _BACNET_POINT_COLUMNS, point_rows))
    if has_silent:
        sections.append(
            _section(
                _INVENTORY_BACNET_SILENT_TITLE,
                _BACNET_SILENT_COLUMNS,
                silent_rows,
                note=_BACNET_SILENT_NOTE,
            )
        )
    if has_mqtt:
        sections.append(_section(_INVENTORY_MQTT_TITLE, _MQTT_TOPIC_COLUMNS, topic_rows))
    return sections


def _apply_xlsx_branding(workbook: Workbook, run_id: str) -> None:
    """Apply the text-only branding band to every sheet's page header/footer.

    Header: wordmark (left) + document title (right). Footer: wordmark (left) +
    "Page N of M" (center) + run id (right). These surface in Excel's Page
    Layout view and on print — exactly the ITP-pack use. openpyxl serializes
    header/footer as a static <headerFooter> element per sheet, so the bytes are
    deterministic and survive _normalize_zip_bytes unchanged. Called once, after
    all sheets exist, so every sheet is covered regardless of which conditional
    validation sections were created.
    """
    for sheet in workbook.worksheets:
        sheet.oddHeader.left.text = _BRAND_NAME
        sheet.oddHeader.right.text = _BRAND_DOC_TITLE
        sheet.oddFooter.left.text = _BRAND_NAME
        # openpyxl's friendly page tokens: &[Page] -> &P, &[Pages] would -> &N;
        # &N is written directly. Serialized into each sheet XML as &amp;P/&amp;N.
        sheet.oddFooter.center.text = "Page &[Page] of &N"
        sheet.oddFooter.right.text = run_id


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
    # Discovery inventory sheets (one per section) when the scoped source runs
    # include discovery runs; other report types keep their prior shape.
    inventory = _discovery_inventory(run)
    if inventory is not None:
        for section in inventory:
            columns = section["columns"]
            sheet_title = _INVENTORY_SHEET_NAMES.get(section["title"], section["title"])
            inventory_sheet = workbook.create_sheet(sheet_title)
            # A remapped (truncated) sheet name loses the full title — surface it
            # as row 1 so the abbreviation is never the only record of the head.
            if sheet_title != section["title"]:
                inventory_sheet.append([section["title"]])
            if section["note"]:
                inventory_sheet.append([section["note"]])
            inventory_sheet.append(list(columns))
            for row in section["rows"]:
                inventory_sheet.append([row[column] for column in columns])
            for index, column in enumerate(columns, start=1):
                width = _INVENTORY_COLUMN_WIDTHS.get(column)
                if width:
                    inventory_sheet.column_dimensions[get_column_letter(index)].width = width
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
    # Branding must run after every create_sheet so all sheets carry the band.
    _apply_xlsx_branding(workbook, str(run.run_id))
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


# --- DOCX branding parts (real OOXML header/footer) --------------------------
# Word requires the header/footer as separate parts referenced from the section
# properties via relationship ids. The header is a constant; the footer carries
# the run id so it is built per run. fldSimple PAGE/NUMPAGES fields hold a "1"
# placeholder that Word recomputes on open/print (kept static so the bytes stay
# reproducible — do NOT precompute it into a real page count).
_DOCX_HEADER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:p><w:pPr>"
    '<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" w:color="auto"/></w:pBdr>'
    '<w:tabs><w:tab w:val="right" w:pos="9026"/></w:tabs>'
    "</w:pPr>"
    f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{escape(_BRAND_NAME)}</w:t></w:r>'
    "<w:r><w:tab/></w:r>"
    f'<w:r><w:t xml:space="preserve">{escape(_BRAND_DOC_TITLE)}</w:t></w:r>'
    "</w:p></w:hdr>"
)

# Two relationships from word/document.xml to the header and footer parts. This
# file does not exist in the base 3-member docx — it must be created.
_DOCX_DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdHdr1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rIdFtr1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"""

# Section properties wiring the header/footer references in and declaring A4 (to
# match the PDF; a sectPr-less docx defaults to Letter in Word). Child order —
# headerReference, footerReference, pgSz, pgMar — follows the ECMA-376 sequence.
_DOCX_SECTPR = (
    "<w:sectPr>"
    '<w:headerReference w:type="default" r:id="rIdHdr1"/>'
    '<w:footerReference w:type="default" r:id="rIdFtr1"/>'
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
    "</w:sectPr>"
)


def _build_docx_footer_xml(run_id: str) -> str:
    """word/footer1.xml: wordmark + Page N of M (PAGE/NUMPAGES fields) + run id."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p><w:pPr>"
        '<w:tabs><w:tab w:val="center" w:pos="4513"/><w:tab w:val="right" w:pos="9026"/></w:tabs>'
        "</w:pPr>"
        f'<w:r><w:t xml:space="preserve">{escape(_BRAND_NAME)}</w:t></w:r>'
        "<w:r><w:tab/></w:r>"
        '<w:r><w:t xml:space="preserve">Page </w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        '<w:r><w:t xml:space="preserve"> of </w:t></w:r>'
        '<w:fldSimple w:instr=" NUMPAGES "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        "<w:r><w:tab/></w:r>"
        f'<w:r><w:t xml:space="preserve">{escape(run_id)}</w:t></w:r>'
        "</w:p></w:ftr>"
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

    inventory = _discovery_inventory(run)
    if inventory is not None:
        for section in inventory:
            blocks.append(_docx_paragraph(section["title"], bold=True))
            if section["note"]:
                blocks.append(_docx_paragraph(section["note"]))
            if section["rows"]:
                blocks.append(_docx_table(section["columns"], section["rows"]))
                # Trailing paragraph so a body never ends on <w:tbl> (Word rule).
                blocks.append(_docx_paragraph(""))

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
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    {body_xml}
    {_DOCX_SECTPR}
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
  <Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
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
        archive.writestr("word/_rels/document.xml.rels", _DOCX_DOCUMENT_RELS)
        archive.writestr("word/header1.xml", _DOCX_HEADER_XML)
        archive.writestr("word/footer1.xml", _build_docx_footer_xml(str(run.run_id)))
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
        # Discovery inventory (parity with the document formats): its own member
        # when the source runs include discovery runs, absent otherwise. Key
        # order is fixed by construction so the member bytes stay reproducible.
        inventory = _discovery_inventory(run)
        if inventory is not None:
            archive.writestr(
                "discovery_inventory.json",
                json.dumps(
                    {
                        "sections": [
                            {
                                "title": section["title"],
                                "columns": list(section["columns"]),
                                "rows": section["rows"],
                                **({"note": section["note"]} if section["note"] else {}),
                            }
                            for section in inventory
                        ]
                    },
                    indent=2,
                ),
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

# Relative column weights for the fixed-width inventory PDF tables. Long text
# columns (Counts, Topic, Point Name) get more room; every cell still truncates
# with an ellipsis. Truncation is acceptable ONLY because the xlsx and zip
# artifacts carry the full untruncated values — same honesty trade-off as
# _PDF_SUMMARY_WEIGHTS; do not "fix" truncation by dropping columns.
_PDF_INVENTORY_SUMMARY_WEIGHTS = (80, 60, 45, 220)
_PDF_IP_WEIGHTS = (92, 74, 70, 58, 62, 46, 46, 46)
_PDF_BACNET_DEVICE_WEIGHTS = (92, 42, 62, 74, 52, 52, 74, 30)
_PDF_BACNET_POINT_WEIGHTS = (86, 60, 56, 104, 78, 40)
_PDF_BACNET_SILENT_WEIGHTS = (86, 96, 44, 72, 58)
_PDF_MQTT_WEIGHTS = (72, 214, 46, 80)
_PDF_INVENTORY_WEIGHTS = {
    _INVENTORY_SUMMARY_TITLE: _PDF_INVENTORY_SUMMARY_WEIGHTS,
    _INVENTORY_IP_TITLE: _PDF_IP_WEIGHTS,
    _INVENTORY_BACNET_DEVICES_TITLE: _PDF_BACNET_DEVICE_WEIGHTS,
    _INVENTORY_BACNET_POINTS_TITLE: _PDF_BACNET_POINT_WEIGHTS,
    _INVENTORY_BACNET_SILENT_TITLE: _PDF_BACNET_SILENT_WEIGHTS,
    _INVENTORY_MQTT_TITLE: _PDF_MQTT_WEIGHTS,
}


def _build_pdf_report(run: object) -> bytes:
    document = PdfDocument(
        header_left=_BRAND_NAME,
        header_right=_BRAND_DOC_TITLE,
        footer_left=_BRAND_NAME,
        footer_right=str(run.run_id),
    )
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

    inventory = _discovery_inventory(run)
    if inventory is not None:
        for section in inventory:
            document.add_heading(section["title"])
            if section["note"]:
                document.add_paragraph(section["note"])
            rows = section["rows"]
            if rows:
                columns = section["columns"]
                document.add_table(
                    columns,
                    [[row[column] for column in columns] for row in rows],
                    widths=_PDF_INVENTORY_WEIGHTS.get(section["title"]),
                    size=_PDF_DENSE_TABLE_SIZE,
                )

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
