import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smart_commissioning_core.engines.comparison_common import make_issue, normalise_unit
from smart_commissioning_core.mqtt_settings import (
    _broker_error_status,
    _string,
    build_mqtt_connection_settings,
    parse_bool,
    parse_capture_seconds,
    parse_int,
)
from smart_commissioning_core.mqtt_transport import (
    MqttCaptureInterrupted,
    MqttMessage,
    MqttTransportError,
    _topic_matches_filter,
    subscribe_and_capture,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_schema import (
    declared_version,
    is_nonpub_version,
    nonpub_version_key,
    structural_issues,
    versions_match,
)

# When the core package is installed editable from the repository checkout,
# parents[2] is the repository root (udmi_validation.py -> smart_commissioning_core -> core -> root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FULL_REPORT_PATH = (
    _REPO_ROOT
    / "device_udmi_payload_validation"
    / "device_udmi_payload_validation"
    / "full_report.json"
)
PACKAGED_FULL_REPORT_PATH = Path(__file__).resolve().parent / "fixtures" / "udmi_full_report.json"
ALLOWED_FIXTURE_DIRS = (
    PACKAGED_FULL_REPORT_PATH.parent,
    _REPO_ROOT / "device_udmi_payload_validation",
)

NUMERIC_UDMI_UNITS = {
    "amperes",
    "degrees_celsius",
    "hertz",
    "kilowatt_hours",
    "kilovolt_amperes",
    "kilovolt_amperes_reactive",
    "kilowatts",
    "parts_per_million",
    "percent",
    "volts",
}
KNOWN_UDMI_UNITS = NUMERIC_UDMI_UNITS | {"no_units", "boolean", "enum"}

# Register shorthand -> canonical UDMI unit (hyphenated, normalise_unit form),
# so a register that says "kwh" matches a metadata payload that says
# "kilowatt_hours" instead of tripping a false mismatch/unknown-unit issue.
_UNIT_ALIASES = {
    "kwh": "kilowatt-hours",
    "kw": "kilowatts",
    "kva": "kilovolt-amperes",
    "kvar": "kilovolt-amperes-reactive",
    "a": "amperes",
    "amp": "amperes",
    "amps": "amperes",
    "v": "volts",
    "hz": "hertz",
    "ppm": "parts-per-million",
    "%": "percent",
    "degc": "degrees-celsius",
    "deg-c": "degrees-celsius",
    "celsius": "degrees-celsius",
}
_KNOWN_CANONICAL_UNITS = {unit.replace("_", "-") for unit in KNOWN_UDMI_UNITS}
_NUMERIC_CANONICAL_UNITS = {unit.replace("_", "-") for unit in NUMERIC_UDMI_UNITS}

# Structural / version issues are attributed to the payload they were found in.
_PAYLOAD_ISSUE_TYPES = {
    "state": "state_validation",
    "metadata": "metadata_validation",
    "pointset": "pointset_validation",
}


def _canonical_unit(value: object) -> str | None:
    """Canonical hyphenated unit, or None when no unit was supplied at all.

    An explicitly declared unit-less unit ("no_units"/"none"/"unitless") is a
    real observed value — it canonicalises to "no-units" so a register that
    expects e.g. kilowatt-hours still gets a mismatch against it. Only a
    missing/blank value reads as None (no comparison possible).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalised = normalise_unit(text)
    if normalised is None:
        return "no-units"
    return _UNIT_ALIASES.get(normalised, normalised)


@dataclass(frozen=True)
class UdmiValidationResult:
    result_summary: dict[str, object]
    issues: list[ValidationIssueRecord]
    source_fixture: str


LiveCapture = Callable[..., list[MqttMessage]]
CancelCheck = Callable[[], bool]

# Capture defaults: the window matches mqtt_discovery's DEFAULT_CAPTURE_SECONDS;
# the message cap is a SAFETY ceiling only — completion is decided by
# _capture_stop_when (a payload seen for every expected topic), never by raw
# message count, so duplicate publishes on one chatty topic cannot end a
# capture before the quiet topics report.
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_MAX_MESSAGES = 500


def validate_udmi_full_report(
    parameters: dict[str, object] | None = None,
    *,
    live_capture: LiveCapture | None = subscribe_and_capture,
    cancel_check: CancelCheck | None = None,
) -> UdmiValidationResult:
    parameters = dict(parameters or {})
    capture_issues: list[ValidationIssueRecord] = []
    capture_summary = _capture_live_payloads(parameters, live_capture=live_capture, cancel_check=cancel_check)
    if capture_summary["issue"] is not None:
        capture_issues.append(capture_summary["issue"])

    if _uses_direct_payload_inputs(parameters):
        full_report = _inline_full_report(parameters)
        source = "schedule_payload_inputs"
        source_fixture = "inline_schedule_payloads"
    else:
        report_path = _resolve_report_path(parameters)
        full_report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(full_report, dict):
            raise ValueError("UDMI full report fixture must contain a JSON object.")
        source = "udmi_full_report_fixture"
        source_fixture = str(report_path)

    issues = _normalise_issues(full_report)
    issues.extend(capture_issues)
    register_rejection = _register_rejection_issue(parameters, issues)
    if register_rejection is not None:
        issues.append(register_rejection)
    issues.extend(_register_duplicate_id_issues(parameters, issues))
    issues.extend(_review_all_payload_issues(parameters or {}, issues))
    expected_devices = _list_value(full_report, "DeviceList")
    not_publishing = _list_value(full_report, "DevicesNotPublishing")
    latest_payload = _latest_payload_timestamp(parameters or {})
    # Per-asset, per-payload-type expected-vs-observed view for the results UI
    # (mq9m4bnv). Built only from real payloads the validator already has
    # (pasted inputs or live capture); the fixture path carries no payload JSON,
    # so it returns [] and is labelled 'none' rather than fabricating content.
    payload_views = _build_payload_views(parameters or {})
    conformance = _conformance_fields(expected_devices, not_publishing, issues)
    result_summary: dict[str, object] = {
        "expected_devices": len(expected_devices),
        "publishing_seen": max(0, len(expected_devices) - len(not_publishing)),
        "not_publishing": len(not_publishing),
        # Silent systems (field ask 2026-07-14): the report needs the device
        # IDS, not just the count — a device that never reported inside the
        # allowed window is reported as "silent", distinct from pass/fail.
        "not_publishing_devices": sorted(str(device) for device in not_publishing),
        "pointset_valid": len(_list_value(full_report, "DevicesPointsetValid")),
        "state_valid": len(_list_value(full_report, "DevicesStateValid")),
        "issue_count": len(issues),
        # Field ask 2026-07-14: the hero score must be fed by validation
        # outcomes, not publishing liveness — 100% next to a blocking issue is
        # a lie. See _conformance_fields for the scale.
        "blocking_issue_count": conformance["blocking_issue_count"],
        "payload_conformance_percent": conformance["payload_conformance_percent"],
        "message_count": _message_count(parameters or {}),
        "payload_last_seen": latest_payload,
        "source": source,
        "source_fixture": source_fixture,
        "broker_capture_attempted": capture_summary["attempted"],
        "broker_status_detail": capture_summary["status_detail"],
        # "bounded" / "indefinite" / "indefinite_bounded_no_cancel" for capture
        # runs; None when no capture was attempted. Surfaces honestly when an
        # indefinite request had to be bounded for lack of a cancel path.
        "capture_mode": capture_summary.get("capture_mode"),
        # The window the capture actually ran with (None = indefinite), so an
        # operator-entered duration that was defaulted or bounded is visible.
        "capture_window_seconds": capture_summary.get("capture_window_seconds"),
        "captured_topics": capture_summary["captured_topics"],
        "subscribed_topics": capture_summary.get("subscribed_topics", []),
        "payload_views": payload_views,
        "payload_view_source": _payload_view_source(
            captured_topics=capture_summary["captured_topics"],
            has_views=bool(payload_views),
        ),
    }
    return UdmiValidationResult(
        result_summary=result_summary,
        issues=issues,
        source_fixture=source_fixture,
    )


def _nonpub_schema_sets(parameters: dict[str, object]) -> dict[str, dict[str, dict]]:
    """Operator-uploaded nonpub schema sets from run parameters, keyed canonically.

    Shape: ``parameters["nonpub_schema_sets"] = {label: {filename: schema}}``
    (embedded at run creation from the DB-backed store, so the queued worker
    needs no filesystem access). Malformed entries are dropped, never raised —
    a bad upload must degrade to the missing-set finding, not kill the run.
    """
    raw = parameters.get("nonpub_schema_sets")
    if not isinstance(raw, dict):
        return {}
    sets: dict[str, dict[str, dict]] = {}
    for label, schema_set in raw.items():
        if not isinstance(label, str) or not isinstance(schema_set, dict):
            continue
        files = {
            str(name): schema
            for name, schema in schema_set.items()
            if isinstance(schema, dict)
        }
        if files:
            sets[nonpub_version_key(label)] = files
    return sets


# Severities that make a result row read "Fail" in the workbench (critical, and
# high/medium which the frontend maps to "major"). The hero score uses the same
# set so a red row can never coexist with a 100% score.
_BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})


def _conformance_fields(
    expected_devices: list[object],
    not_publishing: list[object],
    issues: list[ValidationIssueRecord],
) -> dict[str, object]:
    """Score fields fed by validation outcomes, not publishing liveness.

    Scale: a device conforms when it published AND carries no blocking-severity
    issue. ``payload_conformance_percent`` = floor(100 * conforming / expected),
    clamped to at most 99 whenever ANY blocking issue or silent device exists
    (including run-scoped issues that name no device — those mean devices were
    not fully verified, so 100% must be impossible). Silent devices are neither
    validated nor failed: their ``not_publishing`` liveness issues stay OUT of
    ``blocking_issue_count``, but the devices still depress the score through
    the conforming exclusion. ``None`` when no devices were expected, mirroring
    the frontend's existing null guard.
    """
    blocking = [
        issue
        for issue in issues
        if issue.issue_type != "not_publishing"
        and (issue.severity or "").casefold() in _BLOCKING_SEVERITIES
    ]
    fields: dict[str, object] = {"blocking_issue_count": len(blocking)}
    if not expected_devices:
        fields["payload_conformance_percent"] = None
        return fields
    not_publishing_ids = {str(device) for device in not_publishing}
    # Single-asset capture timeouts report silence only as an issue (never via
    # DevicesNotPublishing), so silent ids are collected from both sources.
    not_publishing_ids.update(
        str(issue.asset_id)
        for issue in issues
        if issue.issue_type == "not_publishing" and issue.asset_id
    )
    blocked_assets = {issue.asset_id for issue in blocking if issue.asset_id}
    conforming = [
        device
        for device in expected_devices
        if str(device) not in not_publishing_ids and str(device) not in blocked_assets
    ]
    percent = (100 * len(conforming)) // len(expected_devices)
    if blocking or not_publishing_ids:
        percent = min(percent, 99)
    fields["payload_conformance_percent"] = percent
    return fields


def _resolve_report_path(parameters: dict[str, object]) -> Path:
    raw_path = parameters.get("full_report_path") or parameters.get("fixture_path")
    if raw_path is None:
        return DEFAULT_FULL_REPORT_PATH if DEFAULT_FULL_REPORT_PATH.exists() else PACKAGED_FULL_REPORT_PATH
    if not isinstance(raw_path, str):
        raise ValueError("UDMI fixture path parameter must be a string.")

    report_path = Path(raw_path).expanduser()
    if not report_path.is_absolute():
        report_path = _REPO_ROOT / report_path
    report_path = report_path.resolve()
    if not any(report_path.is_relative_to(allowed_dir) for allowed_dir in ALLOWED_FIXTURE_DIRS):
        raise FileNotFoundError(
            f"UDMI fixture path outside allowed fixture directories: {raw_path}"
        )
    return report_path


def _normalise_issues(full_report: dict[str, Any]) -> list[ValidationIssueRecord]:
    issues: list[ValidationIssueRecord] = []

    capture_details = _dict_value(full_report, "DeviceCaptureDetails")
    for asset_id in _list_value(full_report, "DevicesNotPublishing"):
        description = f"Expected device {asset_id} did not publish during the validation window."
        detail = capture_details.get(asset_id)
        if isinstance(detail, str) and detail:
            description = f"{description} {detail}"
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="not_publishing",
                severity="high",
                description=description,
                suggested_action="Confirm the device publishes on the expected topics and widen the capture window if needed.",
            )
        )

    for asset_id in _dict_value(full_report, "DevicesNotExpected"):
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="unexpected_device",
                severity="medium",
                description=f"Device {asset_id} published but was not present in the expected asset list.",
            )
        )

    for asset_id, messages in _dict_value(full_report, "DevicePayloadErrors").items():
        for message in _messages(messages):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="payload_error",
                    severity="critical",
                    description=message,
                )
            )

    for asset_id, messages in _dict_value(full_report, "DevicePointsetErrors").items():
        for message in _messages(messages):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="pointset_validation",
                    severity="high",
                    description=message,
                )
            )

    for asset_id, messages in _dict_value(full_report, "DevicesStateErrors").items():
        for message in _messages(messages):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="state_validation",
                    severity=_state_severity(message),
                    description=message,
                )
            )

    return issues


def _nested(payload: object, *keys: str) -> object:
    """Walk nested dict keys, tolerating missing/non-dict nodes (returns None)."""
    node: object = payload
    for key in keys:
        node = _dict_or_empty(node).get(key)
    return node


# Register field -> (observed UDMI location, issue type, severity, description,
# action, observed leaf key(s), canonical dot-path). manufacturer/model/serial/
# firmware read the STATE payload; guid/site/room read METADATA. Paths follow
# UDMI conventions (system.hardware.*, system.serial_no, system.software.firmware,
# system.physical_tag.asset.guid, system.location.{site,section,room}). The leaf
# keys + canonical path drive the misplaced-value diagnostic: when the value is
# absent at the canonical path but present elsewhere (on-site 2026-07-13 a
# publisher nested a second 'system' inside 'system', so every identity read
# "missing" while plainly visible in MQTT Explorer), the issue names WHERE it
# was found instead of claiming it is absent.
_IDENTITY_CHECKS: tuple[tuple[str, Callable[[dict, dict], object], str, str, str, str, tuple[str, ...], str], ...] = (
    ("manufacturer", lambda state, metadata: _nested(state, "system", "hardware", "make"),
     "state_validation", "high",
     "State payload manufacturer does not match the asset register.",
     "Confirm the manufacturer in the MSI schedule and the UDMI state payload.",
     ("make",), "system.hardware.make"),
    ("model", lambda state, metadata: _nested(state, "system", "hardware", "model"),
     "state_validation", "medium",
     "State payload model does not match the asset register.",
     "Check device metadata or update the asset register if the installed model changed.",
     ("model",), "system.hardware.model"),
    ("serial", lambda state, metadata: _nested(state, "system", "serial_no"),
     "state_validation", "medium",
     "State payload serial number does not match the asset register.",
     "Confirm the device serial number in the schedule and the UDMI state payload.",
     ("serial_no",), "system.serial_no"),
    ("firmware", lambda state, metadata: _nested(state, "system", "software", "firmware"),
     "state_validation", "low",
     "State payload firmware version does not match the asset register.",
     "Confirm the expected firmware version or update the device firmware.",
     ("firmware",), "system.software.firmware"),
    ("guid", lambda state, metadata: _nested(metadata, "system", "physical_tag", "asset", "guid"),
     "metadata_validation", "high",
     "Metadata GUID does not match the asset register.",
     "Correct the UDMI metadata asset GUID or the imported register.",
     ("guid",), "system.physical_tag.asset.guid"),
    ("site", lambda state, metadata: _nested(metadata, "system", "location", "site"),
     "metadata_validation", "low",
     "Metadata site does not match the asset register.",
     "Confirm the site in the schedule and the UDMI metadata location.",
     ("site",), "system.location.site"),
    # Devices legitimately publish the room under location.section OR
    # location.room (both exist in the UDMI system model), so the register's
    # Room column matches either: the getter returns ALL present candidates and
    # the check passes when ANY equals the register value — a device carrying
    # both fields (section = building subdivision, room = the register's room)
    # must not read as a mismatch against section alone.
    ("room", lambda state, metadata: [
        value
        for value in (
            _nested(metadata, "system", "location", "section"),
            _nested(metadata, "system", "location", "room"),
        )
        if value
    ],
     "metadata_validation", "low",
     "Metadata room/section does not match the asset register.",
     "Confirm the room/section in the schedule and the UDMI metadata location.",
     ("section", "room"), "system.location.section (or system.location.room)"),
)


def _find_key_paths(
    node: object,
    keys: tuple[str, ...],
    prefix: tuple[str, ...] = (),
    depth: int = 6,
) -> list[str]:
    """Dot-paths of scalar values stored under any of ``keys``, at any nesting.

    Powers the misplaced-value diagnostic: a publisher that wraps UDMI content
    one level too deep still holds the real value somewhere — naming that path
    beats reporting the field as missing.
    """
    if depth < 0 or not isinstance(node, dict):
        return []
    paths: list[str] = []
    for key, value in node.items():
        path = (*prefix, str(key))
        if str(key) in keys and not isinstance(value, (dict, list)):
            paths.append(".".join(path))
        paths.extend(_find_key_paths(value, keys, path, depth - 1))
    return paths


def _find_misplaced_metadata_points(
    payload: dict,
    prefix: tuple[str, ...] = (),
    depth: int = 5,
) -> tuple[str, dict]:
    """First NON-canonical ``pointset.points`` map anywhere in the metadata.

    The canonical location is the payload top level (empty prefix — that case
    is read directly by the caller); anything deeper is a publisher nesting
    error whose dot-path is returned alongside the points map so the register
    comparison can still run against the real content.
    """
    if depth < 0 or not isinstance(payload, dict):
        return "", {}
    for key, value in payload.items():
        path = (*prefix, str(key))
        if (
            key == "pointset"
            and prefix
            and isinstance(value, dict)
            and isinstance(value.get("points"), dict)
        ):
            return ".".join((*path, "points")), value["points"]
        if isinstance(value, dict):
            found_path, found_points = _find_misplaced_metadata_points(value, path, depth - 1)
            if found_points:
                return found_path, found_points
    return "", {}


def _misplaced_value_detail(
    payload: dict,
    leaf_keys: tuple[str, ...],
    canonical_path: str,
) -> str:
    """One sentence naming where a canonical field's value actually sits, or ''."""
    found = [path for path in _find_key_paths(payload, leaf_keys) if path not in canonical_path]
    if not found:
        return ""
    locations = ", ".join(found[:2])
    return (
        f" A '{'/'.join(leaf_keys)}' value was found at {locations} — UDMI expects it at "
        f"{canonical_path}; fix the publisher's payload nesting."
    )


def _review_all_payload_issues(
    parameters: dict[str, object],
    existing_issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Fan _review_payload_issues out across a multi-asset ``assets`` list.

    When ``parameters["assets"]`` is a non-empty list, each entry carries its
    own ``expected_schedule``/``*_payload`` keys; run the single-asset reviewer
    once per entry and aggregate. The single top-level path stays back-compatible.

    Uploaded nonpub schema sets are embedded ONCE at run creation, at the top
    level of the RUN parameters — never inside per-asset entries — so they are
    resolved here and passed down to every reviewer call.
    """
    uploaded_schemas = _nonpub_schema_sets(parameters)
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        issues = [*existing_issues]
        first_new_issue = len(issues)
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            issues.extend(_review_payload_issues(entry, issues, uploaded_schemas=uploaded_schemas))
        return issues[first_new_issue:]
    return _review_payload_issues(parameters, existing_issues, uploaded_schemas=uploaded_schemas)


def _review_payload_issues(
    parameters: dict[str, object],
    existing_issues: list[ValidationIssueRecord],
    *,
    uploaded_schemas: dict[str, dict[str, dict]] | None = None,
) -> list[ValidationIssueRecord]:
    expected = _dict_or_empty(parameters.get("expected_schedule"))
    if not expected:
        return []

    issues = [*existing_issues]
    first_new_issue = len(issues)
    asset_id = str(expected.get("asset_id") or "UDMI asset")
    state_payload = _dict_or_empty(parameters.get("state_payload"))
    metadata_payload = _dict_or_empty(parameters.get("metadata_payload"))
    pointset_payload = _dict_or_empty(parameters.get("pointset_payload"))
    raw_evidence_uri = str(parameters.get("raw_evidence_uri") or "runtime://udmi-validation/review-payloads")
    if uploaded_schemas is None:
        uploaded_schemas = _nonpub_schema_sets(parameters)

    # Register identity values that can never fit canonical UDMI are reported
    # by name; the template embeds a schema-valid placeholder for them instead
    # of failing wholesale (see _METADATA_REGISTER_FIELDS).
    issues.extend(
        _register_canonical_notes(expected, issues, asset_id=asset_id, raw_evidence_uri=raw_evidence_uri)
    )

    # The expected side is a real UDMI-shaped template, not a copy of an
    # observation. Report invalid register constraints before comparing a
    # captured payload, otherwise a malformed register value would look valid.
    for payload_type in ("state", "metadata", "pointset"):
        expected_template = _expected_payload_facet(expected, payload_type)
        template_version = declared_version(expected_template or {})
        if template_version and is_nonpub_version(template_version):
            # Template facets are built in the canonical-1.5.2 shape with
            # placeholders, so they can never be judged against an operator's
            # nonpub schema (uploaded or not) — the payload-side loop below is
            # the sole nonpub judge, and it reports a missing set exactly once.
            continue
        for finding in structural_issues(payload_type, expected_template or {}, uploaded_schemas):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=_PAYLOAD_ISSUE_TYPES[payload_type],
                    severity=finding.severity,
                    description=(
                        f"Expected register values cannot form a valid UDMI {payload_type} template: "
                        f"{finding.description}"
                    ),
                    point_name=finding.point_name,
                    expected_value=finding.expected_value,
                    observed_value=finding.observed_value,
                    suggested_action="Correct the imported register value so it conforms to the expected UDMI payload.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    # Version gate first (workbench contract, Pete 2026-07-09): the register's
    # Expected schema version must equal each payload's declared top-level
    # version. A mismatch is reported immediately and that payload's structure
    # is NOT checked against the wrong schema; on a match (or when the register
    # carries no version) the structure is checked against the declared version.
    expected_version = str(expected.get("udmi_version") or expected.get("schema_version") or "").strip()
    for payload_type, payload, present in (
        ("state", state_payload, "state_payload" in parameters),
        ("metadata", metadata_payload, "metadata_payload" in parameters),
        ("pointset", pointset_payload, "pointset_payload" in parameters),
    ):
        if not present:
            continue
        issue_type = _PAYLOAD_ISSUE_TYPES[payload_type]
        payload_version = declared_version(payload)
        if payload_version is None:
            if expected_version:
                issues.append(
                    _issue(
                        issues,
                        asset_id=asset_id,
                        issue_type=issue_type,
                        severity="high",
                        description=(
                            f"The {payload_type} payload does not declare a UDMI version; "
                            f"the register expects {expected_version}."
                        ),
                        expected_value=expected_version,
                        observed_value="missing",
                        suggested_action="Fix the publisher so every UDMI payload carries its schema version.",
                        raw_evidence_uri=raw_evidence_uri,
                    )
                )
            continue
        if expected_version and not versions_match(expected_version, payload_version):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=issue_type,
                    severity="critical",
                    description=(
                        f"Expected schema version does not match the {payload_type} payload version."
                    ),
                    expected_value=expected_version,
                    observed_value=payload_version,
                    suggested_action=(
                        "Align the register's Expected schema version with the device's UDMI version."
                    ),
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
            continue
        for finding in structural_issues(payload_type, payload, uploaded_schemas):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=issue_type,
                    severity=finding.severity,
                    description=finding.description,
                    point_name=finding.point_name,
                    expected_value=finding.expected_value,
                    observed_value=finding.observed_value,
                    suggested_action=finding.suggested_action,
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    # manufacturer/model/serial/firmware/guid/site/room: flag missing or
    # differing expected values when the corresponding payload was captured.
    for expected_key, observed_getter, issue_type, severity, description, action, leaf_keys, canonical_path in _IDENTITY_CHECKS:
        expected_value = expected.get(expected_key)
        observed = observed_getter(state_payload, metadata_payload)
        # A getter may return one value or a list of candidate values (the
        # register value matches when ANY candidate equals it — e.g. room in
        # location.section or location.room).
        candidates = [value for value in (observed if isinstance(observed, list) else [observed]) if value]
        observed_value = candidates[0] if len(candidates) == 1 else " / ".join(str(value) for value in candidates)
        source_payload = state_payload if issue_type == "state_validation" else metadata_payload
        payload_present = bool(source_payload)
        if expected_value and payload_present and not candidates:
            # "Missing" alone misleads when the value sits at a wrong path
            # (e.g. a second 'system' nesting level): name where it was found.
            misplaced_detail = _misplaced_value_detail(source_payload, leaf_keys, canonical_path)
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=issue_type,
                    severity=severity,
                    description=(
                        f"Expected {expected_key} is missing from the "
                        f"{issue_type.removesuffix('_validation')} payload at {canonical_path}."
                        f"{misplaced_detail}"
                    ),
                    expected_value=str(expected_value),
                    observed_value="missing",
                    suggested_action=action,
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
        elif expected_value and candidates and expected_value not in candidates:
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=issue_type,
                    severity=severity,
                    description=description,
                    expected_value=str(expected_value),
                    observed_value=str(observed_value),
                    suggested_action=action,
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    # Tolerate malformed shapes (pointset/points as a non-object) so a bad
    # payload yields structural issues above instead of crashing the run.
    metadata_points = _dict_or_empty(_dict_or_empty(metadata_payload.get("pointset")).get("points")) if metadata_payload else {}
    if metadata_payload and not metadata_points:
        # On-site 2026-07-13: a publisher nested the whole pointset under
        # 'system', so every register point read "not defined in the metadata
        # pointset" while plainly visible in MQTT Explorer. Report the wrong
        # nesting ONCE, then compare against the misplaced copy so the
        # per-point issues below reflect real content differences (missing
        # points, typos, wrong units) instead of one false "missing" per point.
        misplaced_path, misplaced_points = _find_misplaced_metadata_points(metadata_payload)
        if misplaced_points:
            metadata_points = misplaced_points
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=(
                        f"The metadata payload nests its pointset at {misplaced_path} — UDMI "
                        "expects 'pointset.points' at the payload top level. The register "
                        "point/unit comparison used the nested copy so content is still checked."
                    ),
                    expected_value="pointset.points at the payload top level",
                    observed_value=misplaced_path,
                    suggested_action="Move the pointset object to the metadata payload's top level in the publisher.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
    pointset_points = _dict_or_empty(pointset_payload.get("points")) or _dict_or_empty(
        _dict_or_empty(pointset_payload.get("pointset")).get("points")
    )
    expected_units = _dict_or_empty(expected.get("units"))
    for point_name, expected_unit in expected_units.items():
        metadata_unit = _dict_or_empty(metadata_points.get(point_name)).get("units")
        # Workbench contract: the register's expected unit must MATCH the
        # metadata payload's unit (after alias/format normalisation), not merely
        # be a recognisable UDMI unit.
        expected_canonical = _canonical_unit(expected_unit)
        observed_canonical = _canonical_unit(metadata_unit)
        if expected_canonical and metadata_payload and point_name in metadata_points and not observed_canonical:
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=(
                        f"Metadata point {point_name} does not declare units; "
                        f"the register expects {expected_unit}."
                    ),
                    point_name=str(point_name),
                    expected_value=str(expected_unit),
                    observed_value="missing",
                    suggested_action="Add the expected units to the device metadata point definition.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

        if expected_canonical and observed_canonical and expected_canonical != observed_canonical:
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=f"Metadata unit for {point_name} does not match the expected register unit.",
                    point_name=str(point_name),
                    expected_value=str(expected_unit),
                    observed_value=str(metadata_unit),
                    suggested_action="Correct the device metadata units or the register's Expected units.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
        unit_to_check = metadata_unit or expected_unit
        canonical_to_check = observed_canonical or expected_canonical
        if canonical_to_check and canonical_to_check not in _KNOWN_CANONICAL_UNITS:
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=f"Metadata unit '{unit_to_check}' for {point_name} is not a supported UDMI unit.",
                    point_name=str(point_name),
                    expected_value="known UDMI unit",
                    observed_value=str(unit_to_check),
                    suggested_action="Use a valid UDMI unit such as degrees_celsius or parts_per_million.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

        present_value = _dict_or_empty(pointset_points.get(point_name)).get("present_value")
        if observed_canonical in _NUMERIC_CANONICAL_UNITS and present_value is not None and not isinstance(present_value, int | float):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="pointset_validation",
                    severity="critical",
                    description=f"Pointset payload value for {point_name} should be numeric for unit {observed_canonical}.",
                    point_name=str(point_name),
                    expected_value=f"numeric {observed_canonical}",
                    observed_value=f"{type(present_value).__name__}: {present_value}",
                    suggested_action="Fix the publisher so present_value type matches the expected unit.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    freshness_issue = _pointset_freshness_issue(
        parameters=parameters,
        expected=expected,
        pointset_payload=pointset_payload,
        issues=issues,
        asset_id=asset_id,
        raw_evidence_uri=raw_evidence_uri,
    )
    if freshness_issue is not None:
        issues.append(freshness_issue)

    # ``points`` is the register's Expected points column. Older API callers
    # supplied only ``units``, whose keys remain a compatible point fallback.
    expected_point_values = expected.get("points")
    if expected_point_values is None:
        expected_point_values = expected_units
    expected_points = (
        {str(point) for point in expected_point_values}
        if isinstance(expected_point_values, (dict, list, tuple))
        else set()
    )
    observed_points = set(str(point) for point in pointset_points)
    for point_name in sorted(expected_points - observed_points):
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="pointset_validation",
                severity="high",
                description=f"Expected point {point_name} was not received in the pointset payload.",
                point_name=point_name,
                expected_value="present",
                observed_value="missing",
                suggested_action="Check the publisher mapping and pointset topic.",
                raw_evidence_uri=raw_evidence_uri,
            )
        )
    for point_name in sorted(observed_points - expected_points):
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="pointset_validation",
                severity="medium",
                description=f"Received point {point_name} was not found in the expected schedule.",
                point_name=point_name,
                expected_value="absent",
                observed_value="present",
                suggested_action="Confirm whether this is a valid new point or a publisher mapping error.",
                raw_evidence_uri=raw_evidence_uri,
            )
        )

    # The register's expected point names must also exist in the metadata
    # pointset definition, not only in the live pointset events. Checked only
    # when a metadata payload was actually supplied/captured, so a missing
    # payload is reported once (capture/not-publishing) rather than per point.
    if metadata_payload:
        metadata_point_names = set(str(point) for point in metadata_points)
        for point_name in sorted(expected_points - metadata_point_names):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=f"Expected point {point_name} is not defined in the metadata pointset.",
                    point_name=point_name,
                    expected_value="present",
                    observed_value="missing",
                    suggested_action="Add the point to the device metadata or correct the register.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
        for point_name in sorted(metadata_point_names - expected_points):
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="medium",
                    description=f"Metadata defines point {point_name} that is not in the expected schedule.",
                    point_name=point_name,
                    expected_value="absent",
                    observed_value="present",
                    suggested_action="Confirm whether this is a valid new point or a register omission.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    return issues[first_new_issue:]


def _pointset_freshness_issue(
    *,
    parameters: dict[str, object],
    expected: dict[str, Any],
    pointset_payload: dict[str, Any],
    issues: list[ValidationIssueRecord],
    asset_id: str,
    raw_evidence_uri: str,
) -> ValidationIssueRecord | None:
    """Enforce the register cadence against the captured pointset timestamp."""
    try:
        interval_seconds = float(expected.get("reporting_interval_seconds", 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(interval_seconds) or interval_seconds <= 0 or not pointset_payload:
        return None

    timestamp = pointset_payload.get("timestamp")
    if not isinstance(timestamp, str):
        return None  # Structural validation reports missing/invalid timestamps.
    try:
        normalized_timestamp = timestamp[:-1] + "+00:00" if timestamp.endswith(("Z", "z")) else timestamp
        payload_time = datetime.fromisoformat(normalized_timestamp)
        observed_raw = parameters.get("pointset_payload_received_at") or parameters.get(
            "capture_observed_at"
        )
        observed_time = (
            datetime.fromisoformat((str(observed_raw)[:-1] + "+00:00") if str(observed_raw).endswith(("Z", "z")) else str(observed_raw))
            if observed_raw
            else datetime.now(UTC)
        )
    except ValueError:
        return None
    if payload_time.tzinfo is None or observed_time.tzinfo is None:
        return None

    age_seconds = (observed_time.astimezone(UTC) - payload_time.astimezone(UTC)).total_seconds()
    if age_seconds < -interval_seconds or age_seconds <= interval_seconds:
        if age_seconds < -interval_seconds:
            return _issue(issues, asset_id=asset_id, issue_type="pointset_validation", severity="high", description="Pointset payload timestamp is too far in the future for the capture clock.", expected_value="current device time", observed_value=f"{age_seconds:.1f}s age", suggested_action="Synchronize device and commissioning host clocks.", raw_evidence_uri=raw_evidence_uri)
        return None

    retained = parse_bool(parameters.get("pointset_payload_retained"))
    retained_detail = " It was delivered as a retained MQTT message." if retained else ""
    return _issue(
        issues,
        asset_id=asset_id,
        issue_type="pointset_validation",
        severity="high",
        description=(
            "Pointset payload timestamp exceeds the register's Expected reporting interval "
            f"({age_seconds:.1f}s old; expected at most {interval_seconds:g}s)."
            f"{retained_detail}"
        ),
        expected_value=f"at most {interval_seconds:g} seconds old",
        observed_value=f"{age_seconds:.1f} seconds old" + (" (retained)" if retained else ""),
        suggested_action="Wait for a fresh pointset publish and verify the device reporting cadence.",
        raw_evidence_uri=raw_evidence_uri,
    )


def _capture_window(parameters: dict[str, object], cancel_check: CancelCheck | None) -> tuple[float | None, str]:
    """Resolve the (timeout_seconds, capture_mode) pair for a live capture.

    Blank/0/negative ``capture_seconds`` means indefinite: run until every
    expected topic has reported, cancellation, or the message cap. An
    indefinite capture with NO cancel path would be unkillable if a device
    never publishes, so it is bounded to the default window instead — and the
    downgrade is recorded honestly in ``capture_mode`` rather than hidden.
    """
    seconds = parse_capture_seconds(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS)
    if seconds is None and cancel_check is None:
        return DEFAULT_CAPTURE_SECONDS, "indefinite_bounded_no_cancel"
    return seconds, ("indefinite" if seconds is None else "bounded")


def _capture_topic_groups(topics: list[str]) -> list[list[str]]:
    """Group one asset's subscribed topics into the distinct payloads to see.

    Topics routing to the same payload slot are aliases (a register wildcard
    subscribes both ``…/events/pointset`` and the legacy ``…/event/pointset``);
    a payload on EITHER satisfies the slot, so requiring every literal topic
    would never complete on a single-convention site. A topic with no payload
    slot (e.g. a hand-entered wildcard) forms its own group.
    """
    slots: dict[str, list[str]] = {}
    groups: list[list[str]] = []
    for topic in topics:
        key = _payload_key_for_topic(topic)
        if key is None:
            groups.append([topic])
        else:
            slots.setdefault(key, []).append(topic)
    groups.extend(slots.values())
    return groups


def _unseen_groups(groups: list[list[str]], seen_topics: set[str]) -> list[list[str]]:
    """The topic groups no captured topic has matched yet (wildcard-aware)."""
    return [
        group
        for group in groups
        if not any(_topic_matches_filter(topic, topic_filter) for topic in seen_topics for topic_filter in group)
    ]


def _capture_stop_when(groups: list[list[str]]) -> Callable[[list[MqttMessage]], bool]:
    """Completion predicate: True once every expected topic group has a payload.

    Counts DISTINCT captured topics, never raw message count, so duplicate
    publishes on one chatty topic cannot end the capture early.
    """

    def _complete(messages: list[MqttMessage]) -> bool:
        return not _unseen_groups(groups, _valid_payload_topics(messages))

    return _complete


def _valid_payload_messages(messages: list[MqttMessage]) -> list[MqttMessage]:
    """Messages usable as UDMI evidence: UTF-8 JSON objects, not scalars/lists."""
    return [message for message in messages if isinstance(message.json_payload(), dict)]


def _route_latest_payloads(parameters: dict[str, object], messages: list[MqttMessage]) -> None:
    latest: dict[str, MqttMessage] = {}
    for message in messages:
        if not isinstance(message.json_payload(), dict):
            continue
        key = _payload_key_for_topic(message.topic)
        if key and (key not in latest or message.received_at >= latest[key].received_at):
            latest[key] = message
    for key, message in latest.items():
        parameters[key] = message.json_payload()
        parameters[f"{key}_retained"] = message.retained
        parameters[f"{key}_received_at"] = message.received_at.isoformat()


def _valid_payload_topics(messages: list[MqttMessage]) -> set[str]:
    return {message.topic for message in _valid_payload_messages(messages)}


def _ordered_valid_payload_topics(messages: list[MqttMessage]) -> list[str]:
    return list(dict.fromkeys(message.topic for message in _valid_payload_messages(messages)))


def _missing_topics_issue(*, asset_id: str, missing: list[list[str]], got_any: bool) -> ValidationIssueRecord:
    """Real not_publishing issue naming WHICH expected topics never reported."""
    topics_text = ", ".join(group[0] for group in missing)
    if got_any:
        description = f"Capture ended before every expected topic reported. No payload was seen for: {topics_text}."
    else:
        description = "No UDMI payloads were captured from the live broker during the capture window." + (
            f" Expected topic(s): {topics_text}." if topics_text else ""
        )
    return _issue(
        [],
        asset_id=asset_id,
        issue_type="not_publishing",
        severity="high",
        description=description,
        suggested_action="Confirm the device is publishing and widen the capture window if needed.",
    )


def _capture_error_issue(*, asset_id: str, status_detail: str) -> ValidationIssueRecord:
    return _issue(
        [],
        asset_id=asset_id,
        issue_type="payload_error",
        severity="critical",
        description=f"Live MQTT capture failed ({status_detail}).",
        suggested_action="Check broker reachability, credentials, TLS configuration, and topic filters.",
    )


def _invalid_payload_issue(
    *,
    asset_id: str,
    messages: list[MqttMessage],
    missing: list[list[str]],
) -> ValidationIssueRecord:
    invalid_topics = sorted(
        {message.topic for message in messages if not isinstance(message.json_payload(), dict)}
    )
    required_topics = ", ".join(group[0] for group in missing)
    return _issue(
        [],
        asset_id=asset_id,
        issue_type="payload_error",
        severity="critical",
        description=(
            "MQTT messages arrived but were not valid JSON objects on: "
            f"{', '.join(invalid_topics)}. Required payload group(s) remain unusable: "
            f"{required_topics}."
        ),
        suggested_action="Fix the publisher so every required UDMI topic carries a JSON object.",
    )


def _capture_live_payloads(
    parameters: dict[str, object],
    *,
    live_capture: LiveCapture | None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, object]:
    if not parse_bool(parameters.get("use_live_broker")):
        return {
            "attempted": False,
            "status_detail": "live_broker_not_requested",
            "captured_topics": [],
            "issue": None,
        }

    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        return _capture_live_payloads_per_asset(parameters, assets, live_capture=live_capture, cancel_check=cancel_check)

    if live_capture is None:
        return {
            "attempted": True,
            "status_detail": "live_capture_unavailable",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                issue_type="payload_error",
                severity="high",
                description="Live MQTT capture is not available in this execution context.",
                suggested_action="Run live UDMI validation from a service with broker access, or supply captured payloads directly.",
            ),
        }

    topics = _capture_topics(parameters)
    if not topics:
        return {
            "attempted": True,
            "status_detail": "missing_capture_topics",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                issue_type="payload_error",
                severity="high",
                description="Live UDMI validation requires at least one state, metadata, or pointset topic.",
                suggested_action="Enter the device state, metadata, or events/pointset topic before starting live capture.",
            ),
        }

    timeout_seconds, capture_mode = _capture_window(parameters, cancel_check)
    groups = _capture_topic_groups(topics)
    parameters["subscribed_topics"] = list(topics)
    capture_error_status: str | None = None
    try:
        messages = live_capture(
            build_mqtt_connection_settings(parameters),
            topics=topics,
            timeout_seconds=timeout_seconds,
            max_messages=parse_int(parameters.get("max_messages"), default=DEFAULT_MAX_MESSAGES),
            qos=parse_int(parameters.get("qos"), default=0),
            cancel_check=cancel_check,
            stop_when=_capture_stop_when(groups),
        )
    except MqttCaptureInterrupted as error:
        messages = error.messages
        capture_error_status = _broker_error_status(error.cause)
    except (MqttTransportError, OSError, ValueError) as error:
        # Use the coarse status label only; the raw exception text may carry
        # credentials (connection URL / auth detail) and this description is
        # returned to the frontend.
        broker_status_detail = _broker_error_status(error)
        return {
            "attempted": True,
            "status_detail": broker_status_detail,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "captured_topics": [],
            "issue": _capture_error_issue(
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                status_detail=broker_status_detail,
            ),
        }

    capture_observed_at = datetime.now(UTC).isoformat()
    parameters["capture_observed_at"] = capture_observed_at
    parameters["messages"] = [
        {
            "topic": message.topic,
            "payload": message.json_payload(),
            "retained": message.retained,
            "received_at": message.received_at.isoformat(),
        }
        for message in messages
    ]
    _route_latest_payloads(parameters, messages)

    # Without a transport failure, "captured" is claimed only when EVERY
    # expected topic supplied a usable JSON object; malformed/scalar payloads
    # remain raw evidence but cannot satisfy completion or canonical checks.
    valid_messages = _valid_payload_messages(messages)
    valid_topics = _ordered_valid_payload_topics(messages)
    missing = _unseen_groups(groups, {message.topic for message in valid_messages})
    if capture_error_status:
        return {
            "attempted": True,
            "status_detail": capture_error_status,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "captured_topics": valid_topics,
            "subscribed_topics": list(topics),
            "issue": _capture_error_issue(
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                status_detail=capture_error_status,
            ),
        }
    return {
        "attempted": True,
        "status_detail": (
            "live_payloads_captured" if valid_messages and not missing else "live_capture_timeout"
        ),
        "capture_mode": capture_mode,
        "capture_window_seconds": timeout_seconds,
        "captured_topics": valid_topics,
        "subscribed_topics": list(topics),
        "issue": None
        if valid_messages and not missing
        else (
            _invalid_payload_issue(
                asset_id=str(
                    _dict_or_empty(parameters.get("expected_schedule")).get("asset_id")
                    or "UDMI asset"
                ),
                messages=messages,
                missing=missing,
            )
            if len(valid_messages) != len(messages)
            else _missing_topics_issue(
                asset_id=str(
                    _dict_or_empty(parameters.get("expected_schedule")).get("asset_id")
                    or "UDMI asset"
                ),
                missing=missing,
                got_any=bool(valid_messages),
            )
        ),
    }


def _capture_live_payloads_per_asset(
    parameters: dict[str, object],
    assets: list,
    *,
    live_capture: LiveCapture | None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, object]:
    """Capture live payloads for every asset entry in ONE shared subscription.

    Each entry carries its own state/metadata/pointset topics + expected_schedule;
    the broker connection settings are shared (top level). A single capture
    subscribes the union of every entry's topics and routes each message back to
    the entries whose topics match, so quiet assets are not starved behind chatty
    ones and an indefinite run genuinely waits for ALL assets (the old
    sequential per-asset windows would block asset 2..N behind asset 1 forever
    in indefinite mode).
    """
    if live_capture is None:
        return {
            "attempted": True,
            "status_detail": "live_capture_unavailable",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id="UDMI assets",
                issue_type="payload_error",
                severity="high",
                description="Live MQTT capture is not available in this execution context.",
                suggested_action="Run live UDMI validation from a service with broker access, or supply captured payloads directly.",
            ),
        }

    entries = [entry for entry in assets if isinstance(entry, dict)]
    per_entry_topics = [_capture_topics(entry) for entry in entries]
    topics: list[str] = []
    for entry_topics in per_entry_topics:
        for topic in entry_topics:
            if topic not in topics:
                topics.append(topic)
    if not topics:
        return {
            "attempted": True,
            "status_detail": "missing_capture_topics",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id="UDMI assets",
                issue_type="payload_error",
                severity="high",
                description="Live UDMI validation requires at least one state, metadata, or pointset topic.",
                suggested_action="Import a register with Expected topics, or enter capture topics, before starting live capture.",
            ),
        }

    groups: list[list[str]] = []
    for entry_topics in per_entry_topics:
        groups.extend(_capture_topic_groups(entry_topics))
    timeout_seconds, capture_mode = _capture_window(parameters, cancel_check)
    parameters["subscribed_topics"] = list(topics)
    capture_error_status: str | None = None
    try:
        messages = live_capture(
            build_mqtt_connection_settings(parameters),
            topics=topics,
            timeout_seconds=timeout_seconds,
            max_messages=parse_int(parameters.get("max_messages"), default=DEFAULT_MAX_MESSAGES),
            qos=parse_int(parameters.get("qos"), default=0),
            cancel_check=cancel_check,
            stop_when=_capture_stop_when(groups),
        )
    except MqttCaptureInterrupted as error:
        messages = error.messages
        capture_error_status = _broker_error_status(error.cause)
    except (MqttTransportError, OSError, ValueError) as error:
        # Coarse status label only — raw broker error text may carry credentials.
        broker_status_detail = _broker_error_status(error)
        return {
            "attempted": True,
            "status_detail": broker_status_detail,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "captured_topics": [],
            "issue": _capture_error_issue(asset_id="UDMI assets", status_detail=broker_status_detail),
        }

    capture_observed_at = datetime.now(UTC).isoformat()

    # Route every message back to each entry whose subscribed topics match it,
    # mirroring the single-asset routing (last payload per slot wins).
    for entry, entry_topics in zip(entries, per_entry_topics, strict=True):
        entry["capture_observed_at"] = capture_observed_at
        # Kept for the per-asset not-publishing diagnostics: an asset with no
        # payload can then say WHICH topics were subscribed and what (if
        # anything) actually arrived under them.
        entry["subscribed_topics"] = list(entry_topics)
        entry_messages = [
            message
            for message in messages
            if any(_topic_matches_filter(message.topic, topic) for topic in entry_topics)
        ]
        entry["messages"] = [
            {
                "topic": message.topic,
                "payload": message.json_payload(),
                "retained": message.retained,
                "received_at": message.received_at.isoformat(),
            }
            for message in entry_messages
        ]
        _route_latest_payloads(entry, entry_messages)

    valid_messages = _valid_payload_messages(messages)
    valid_topics = _ordered_valid_payload_topics(messages)
    missing = _unseen_groups(groups, {message.topic for message in valid_messages})
    if capture_error_status:
        return {
            "attempted": True,
            "status_detail": capture_error_status,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "captured_topics": valid_topics,
            "subscribed_topics": list(topics),
            "issue": _capture_error_issue(asset_id="UDMI assets", status_detail=capture_error_status),
        }
    return {
        "attempted": True,
        "status_detail": (
            "live_payloads_captured" if valid_messages and not missing else "live_capture_timeout"
        ),
        "capture_mode": capture_mode,
        "capture_window_seconds": timeout_seconds,
        "captured_topics": valid_topics,
        "subscribed_topics": list(topics),
        "issue": None
        if valid_messages and not missing
        else (
            _invalid_payload_issue(
                asset_id="UDMI assets",
                messages=messages,
                missing=missing,
            )
            if len(valid_messages) != len(messages)
            else _missing_topics_issue(
                asset_id="UDMI assets",
                missing=missing,
                got_any=bool(valid_messages),
            )
        ),
    }


def _capture_topics(parameters: dict[str, object]) -> list[str]:
    topics = [
        _string(parameters.get("state_topic")),
        _string(parameters.get("metadata_topic")),
        _string(parameters.get("pointset_topic")),
    ]
    # Optional additional subscriptions (e.g. the legacy singular
    # "<prefix>/event/pointset" alongside "<prefix>/events/pointset") so a
    # register wildcard captures whichever suffix convention the site uses.
    extra = parameters.get("extra_capture_topics")
    if isinstance(extra, list):
        topics.extend(_string(topic) for topic in extra)
    register_filter = _string(parameters.get("register_topic_filter"))
    if register_filter:
        topics.append(register_filter)
    unique: list[str] = []
    for topic in topics:
        if topic and topic not in unique:
            unique.append(topic)
    return unique


def _payload_key_for_topic(topic: str) -> str | None:
    if topic.endswith("/state"):
        return "state_payload"
    if topic.endswith("/metadata"):
        return "metadata_payload"
    if topic.endswith("/pointset"):
        return "pointset_payload"
    return None


def _uses_direct_payload_inputs(parameters: dict[str, object]) -> bool:
    return any(
        key in parameters
        for key in ("expected_schedule", "assets", "state_payload", "metadata_payload", "pointset_payload", "messages")
    ) and not (parameters.get("full_report_path") or parameters.get("fixture_path"))


def _inline_full_report(parameters: dict[str, object]) -> dict[str, object]:
    report: dict[str, object] = {
        "DeviceList": [],
        "DevicesNotPublishing": [],
        "DevicesNotExpected": {},
        "DevicePayloadErrors": {},
        "DevicePointsetErrors": {},
        "DevicesStateErrors": {},
        "DevicesPointsetValid": [],
        "DevicesStateValid": [],
        # asset_id -> one-line capture diagnostic for a not-publishing asset
        # (subscribed topics vs what actually arrived), appended to its issue.
        "DeviceCaptureDetails": {},
    }
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        # Register-driven multi-asset run: every register row is an expected
        # device. An asset is reported not-publishing only when a live capture
        # was actually attempted and delivered nothing for it — with no capture
        # there was no observation, so no publishing claim is made either way.
        capture_attempted = parse_bool(parameters.get("use_live_broker"))
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            expected = _dict_or_empty(entry.get("expected_schedule"))
            asset_id = str(expected.get("asset_id") or "UDMI asset")
            report["DeviceList"].append(asset_id)  # type: ignore[union-attr]
            has_payload = any(
                _dict_or_empty(entry.get(key))
                for key in ("state_payload", "metadata_payload", "pointset_payload")
            )
            if capture_attempted and not has_payload:
                report["DevicesNotPublishing"].append(asset_id)  # type: ignore[union-attr]
                detail = _asset_capture_detail(entry)
                if detail:
                    report["DeviceCaptureDetails"][asset_id] = detail  # type: ignore[index]
        return report
    expected = _dict_or_empty(parameters.get("expected_schedule"))
    asset_id = str(expected.get("asset_id") or "UDMI asset") if expected else "UDMI asset"
    report["DeviceList"] = [asset_id]
    return report


def _register_rejection_issue(
    parameters: dict[str, object],
    issues: list[ValidationIssueRecord],
) -> ValidationIssueRecord | None:
    """Report register rows the import rejected — those assets are NOT validated.

    Without this a partial import silently narrows the expected asset list and
    a publishing device simply never appears in the results (on-site
    2026-07-13). The backend passes the rejection facts from the import record
    the run was built on.
    """
    rejected = parse_int(parameters.get("register_rejected_rows"), default=0)
    if rejected <= 0:
        return None
    details = parameters.get("register_rejected_details")
    detail_text = ""
    if isinstance(details, list) and details:
        detail_text = " " + "; ".join(str(detail) for detail in details) + "."
    filename = str(parameters.get("register_import_filename") or "").strip()
    source = f" '{filename}'" if filename else ""
    return _issue(
        issues,
        asset_id="MQTT register",
        issue_type="register_import",
        severity="high",
        description=(
            f"The MQTT register import{source} rejected {rejected} row(s); those assets were "
            f"not validated and do not appear in these results.{detail_text}"
        ),
        suggested_action=(
            "Open the Imports page, fix the rejected register rows, re-upload the register, "
            "and run the validation again."
        ),
    )


def _register_duplicate_id_issues(
    parameters: dict[str, object],
    issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Report register rows that reuse one Asset ID for different device topics.

    Two devices mislabelled with one ID group under a single asset in the
    results, so one device looks missing while the other shows a doubled issue
    list (on-site 2026-07-13: a publishing device was absent and its neighbour
    carried two payload sets). The backend detects the collision when building
    the assets list and passes it through for honest reporting.
    """
    raw = parameters.get("register_duplicate_asset_ids")
    if not isinstance(raw, list):
        return []
    duplicates: list[ValidationIssueRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "register asset")
        roots = [str(root) for root in item.get("topic_roots") or [] if root]
        duplicates.append(
            _issue(
                [*issues, *duplicates],
                asset_id=asset_id,
                issue_type="register_import",
                severity="high",
                description=(
                    f"The register has multiple rows with Asset ID '{asset_id}' pointing at "
                    f"different device topics ({', '.join(roots)}). Results grouped under "
                    f"'{asset_id}' mix those devices and one of them looks missing."
                ),
                observed_value=", ".join(roots) or None,
                suggested_action=(
                    "Give each device row a unique Asset ID in the register, re-upload it, "
                    "and run the validation again."
                ),
            )
        )
    return duplicates


def _asset_capture_detail(entry: dict) -> str:
    """Why one asset ended up with no payloads after a real capture.

    Three honest cases, in decreasing specificity: messages arrived on a
    state/metadata/pointset topic but were not JSON objects; messages arrived
    only on unrecognised topics (register topic does not match the device's
    actual payload topics); nothing arrived at all on the subscribed topics.
    On site this is the difference between "2001 is not publishing" and
    knowing WHICH topic string to compare against MQTT Explorer.
    """
    subscribed = [str(topic) for topic in entry.get("subscribed_topics") or [] if str(topic)]
    if not subscribed:
        return ""
    raw_messages = entry.get("messages")
    observed = list(
        dict.fromkeys(
            str(message.get("topic"))
            for message in (raw_messages if isinstance(raw_messages, list) else [])
            if isinstance(message, dict) and message.get("topic")
        )
    )
    if observed:
        slot_topics = [topic for topic in observed if _payload_key_for_topic(topic)]
        if slot_topics:
            return (
                f"Messages arrived on {', '.join(slot_topics)} but their payloads "
                "were not JSON objects."
            )
        return (
            f"Messages arrived on {', '.join(observed)} but none is a recognised UDMI "
            "payload topic (ending /state, /metadata, or /pointset); check the "
            "register's Expected topic against the device's actual topics."
        )
    return (
        f"Nothing arrived on the subscribed topics ({', '.join(subscribed)}) during the "
        "capture window. MQTT topics are case-sensitive — compare these against the "
        "broker (e.g. MQTT Explorer) and widen the capture window for slow publishers."
    )


def _issue(
    issues: list[ValidationIssueRecord],
    *,
    asset_id: str,
    issue_type: str,
    severity: str,
    description: str,
    point_name: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    suggested_action: str | None = None,
    raw_evidence_uri: str | None = None,
) -> ValidationIssueRecord:
    prefix = {
        "not_publishing": "UDMI-NP",
        "unexpected_device": "UDMI-UN",
        "payload_error": "UDMI-PL",
        "pointset_validation": "UDMI-PS",
        "state_validation": "UDMI-ST",
        "metadata_validation": "UDMI-MD",
        "register_import": "UDMI-RG",
    }.get(issue_type, "UDMI-IS")
    return make_issue(
        issues,
        prefix,
        asset_id=asset_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
        point_name=point_name,
        expected_value=expected_value,
        observed_value=observed_value,
        suggested_action=suggested_action,
        raw_evidence_uri=raw_evidence_uri,
    )


def _list_value(full_report: dict[str, Any], key: str) -> list[str]:
    value = full_report.get(key, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_value(full_report: dict[str, Any], key: str) -> dict[str, Any]:
    value = full_report.get(key, {})
    if not isinstance(value, dict):
        return {}
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _messages(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _state_severity(message: str) -> str:
    return "high" if "offline" in message.lower() else "medium"


def _template_timestamp() -> str:
    """Template-build time as RFC 3339. The old epoch-zero sentinel read as a
    broken device clock on site (operators saw "1970" and assumed the tool was
    not pulling the correct time); build time conveys "a current timestamp
    belongs here" while staying schema-valid."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Canonical UDMI 1.5.2 patterns for the register-supplied metadata identity
# fields (mirrors the vendored schemas/udmi/1.5.2/model_system.json). The
# expected template embeds the register value only when it fits the canonical
# pattern; otherwise a schema-valid placeholder keeps the template valid and a
# targeted note names the misfit. Previously the raw value was embedded and the
# whole template failed canonical validation with an opaque "cannot form a
# valid UDMI metadata template" message (on-site 2026-07-13).
_METADATA_REGISTER_FIELDS: dict[str, tuple[str, str, re.Pattern[str], str]] = {
    # register key -> (register label, UDMI metadata path, pattern, placeholder)
    "site": ("Site", "system.location.site", re.compile(r"^[A-Z]{2}-[A-Z]{3,4}-[A-Z0-9]{2,9}$"), "ZZ-TEST-000"),
    "room": ("Room", "system.location.section", re.compile(r"^[A-Z0-9-]+$"), "UNSPECIFIED"),
    "guid": ("GUID", "system.physical_tag.asset.guid", re.compile(r"^[a-z]+://[-0-9a-zA-Z_$]+$"), "placeholder://asset"),
    "asset_id": ("Asset ID", "system.physical_tag.asset.name", re.compile(r"^[A-Z]{2,6}-[1-9][0-9]*$"), "ASSET-1"),
}


def _template_metadata_value(expected: dict[str, Any], register_key: str) -> str:
    """The register value when it can appear in canonical UDMI, else the placeholder."""
    _label, _path, pattern, placeholder = _METADATA_REGISTER_FIELDS[register_key]
    value = str(expected.get(register_key) or "")
    return value if pattern.match(value) else placeholder


# model_system.json location.room — laxer than section (mixed case, underscores),
# so a free-text-ish register Room like "2-09_Meter_Room" is still canonical UDMI.
_ROOM_FIELD_PATTERN = re.compile(r"^[-_a-zA-Z0-9]+$")


def _template_location(expected: dict[str, Any]) -> dict[str, str]:
    """The template's system.location: site plus the register Room where it fits.

    The Room value lands in ``section`` when it fits the strict section pattern,
    in ``room`` when it fits only the laxer room pattern (real devices publish
    either field), and as the section placeholder otherwise.
    """
    location = {"site": _template_metadata_value(expected, "site")}
    room_value = str(expected.get("room") or "")
    section_pattern = _METADATA_REGISTER_FIELDS["room"][2]
    if room_value and not section_pattern.match(room_value) and _ROOM_FIELD_PATTERN.match(room_value):
        location["room"] = room_value
    else:
        location["section"] = _template_metadata_value(expected, "room")
    return location


def _register_canonical_notes(
    expected: dict[str, Any],
    issues: list[ValidationIssueRecord],
    *,
    asset_id: str,
    raw_evidence_uri: str,
) -> list[ValidationIssueRecord]:
    """Name each register identity value that can never appear in canonical UDMI.

    These replace the opaque template-invalid errors: the operator learns WHICH
    register column, its value, and the canonical pattern it must fit, while the
    displayed expected template stays schema-valid with a placeholder. The
    register-vs-observed identity comparison still uses the raw register value.
    """
    notes: list[ValidationIssueRecord] = []
    for register_key, (label, path, pattern, placeholder) in _METADATA_REGISTER_FIELDS.items():
        raw = expected.get(register_key)
        if not raw:
            continue
        value = str(raw)
        if pattern.match(value):
            continue
        if register_key == "room" and _ROOM_FIELD_PATTERN.match(value):
            # Not canonical as location.section, but perfectly canonical as
            # location.room — the template embeds it there, nothing to report.
            continue
        room_alternative = (
            f" or as system.location.room ({_ROOM_FIELD_PATTERN.pattern})"
            if register_key == "room"
            else ""
        )
        notes.append(
            _issue(
                [*issues, *notes],
                asset_id=asset_id,
                issue_type="metadata_validation",
                severity="low",
                description=(
                    f"Register {label} '{value}' cannot appear in canonical UDMI metadata "
                    f"({path} must match {pattern.pattern}{room_alternative}); the expected "
                    f"template shows the placeholder '{placeholder}' instead."
                ),
                expected_value=pattern.pattern,
                observed_value=value,
                suggested_action=(
                    f"Use a canonical UDMI value for {label} in the register (or accept that "
                    "this field is compared against the device but never schema-valid)."
                ),
                raw_evidence_uri=raw_evidence_uri,
            )
        )
    return notes


def _expected_payload_header(expected: dict[str, Any]) -> dict[str, Any]:
    """Schema-valid fields shared by display-only UDMI templates."""
    header: dict[str, Any] = {"timestamp": _template_timestamp()}
    if version := expected.get("udmi_version"):
        header["version"] = version
    return header


def _expected_payload_facet(expected: dict[str, Any], payload_type: str) -> dict[str, Any] | None:
    """UDMI-shaped display template with register constraints and explicit placeholders."""
    header = _expected_payload_header(expected)
    points = expected.get("points")
    if points is None:
        points = expected.get("units", {})
    point_names = [str(point) for point in points] if isinstance(points, (dict, list, tuple)) else []
    units = _dict_or_empty(expected.get("units"))
    if payload_type == "state":
        return {
            **header,
            "system": {
                "last_config": header["timestamp"],
                "operation": {"operational": False},
                "serial_no": expected.get("serial") or "<device serial number>",
                "hardware": {
                    "make": expected.get("manufacturer") or "<device manufacturer>",
                    "model": expected.get("model") or "<device model>",
                },
                "software": {"firmware": expected.get("firmware") or "<device firmware>"},
            },
        }
    if payload_type == "metadata":
        return {
            **header,
            "system": {
                "location": _template_location(expected),
                "physical_tag": {
                    "asset": {
                        "guid": _template_metadata_value(expected, "guid"),
                        "name": _template_metadata_value(expected, "asset_id"),
                    }
                },
            },
            "pointset": {"points": {name: ({"units": units[name]} if name in units else {}) for name in point_names}},
        }
    if payload_type == "pointset":
        return {
            **header,
            "points": {name: {"present_value": None} for name in point_names},
        }
    return None


def _asset_payload_view(
    expected: dict[str, Any],
    observed_by_type: dict[str, dict],
    retained_by_type: dict[str, bool],
) -> dict[str, object] | None:
    """Build ONE asset's per-payload-type expected-vs-observed view, or None.

    A payload type is omitted when neither an expected facet nor an observed
    payload exists, so nothing is fabricated.
    """
    payload_types: list[dict[str, object]] = []
    for payload_type in ("state", "metadata", "pointset"):
        observed = observed_by_type[payload_type]
        expected_facet = _expected_payload_facet(expected, payload_type) if expected else None
        if not observed and not expected_facet:
            continue
        payload_types.append(
            {
                "payload_type": payload_type,
                "expected": expected_facet,
                "observed": observed or None,
                "observed_present": bool(observed),
                "retained": retained_by_type[payload_type],
            }
        )
    if not payload_types:
        return None
    asset_id = str(expected.get("asset_id") or "UDMI asset")
    return {"asset_id": asset_id, "payload_types": payload_types}


def _observed_by_type(source: dict[str, object]) -> dict[str, dict]:
    return {
        "state": _dict_or_empty(source.get("state_payload")),
        "metadata": _dict_or_empty(source.get("metadata_payload")),
        "pointset": _dict_or_empty(source.get("pointset_payload")),
    }


def _retained_by_type(source: dict[str, object]) -> dict[str, bool]:
    return {
        payload_type: parse_bool(source.get(f"{payload_type}_payload_retained"))
        for payload_type in ("state", "metadata", "pointset")
    }


def _build_payload_views(parameters: dict[str, object]) -> list[dict[str, object]]:
    """Per-asset, per-payload-type expected-vs-observed payload view (mq9m4bnv).

    Uses only payloads the validator actually holds: ``expected_schedule``
    (expected facets) and the ``state_payload``/``metadata_payload``/
    ``pointset_payload`` observed payloads (pasted by the operator or written in
    by live capture); nothing is fabricated.

    Multi-asset sites: an optional ``assets`` list (each entry
    ``{expected_schedule, state_payload, metadata_payload, pointset_payload}``)
    emits one view per asset, so a real multi-AHU run shows every device's
    payload evidence. The single top-level ``expected_schedule``/``*_payload``
    stays the single-asset back-compat path. NOTE: issue VALIDATION is still
    single-schedule per run; this view simply surfaces all per-asset payloads
    supplied.
    """
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        views: list[dict[str, object]] = []
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            view = _asset_payload_view(
                _dict_or_empty(entry.get("expected_schedule")),
                _observed_by_type(entry),
                _retained_by_type(entry),
            )
            if view is not None:
                views.append(view)
        return views

    view = _asset_payload_view(
        _dict_or_empty(parameters.get("expected_schedule")),
        _observed_by_type(parameters),
        _retained_by_type(parameters),
    )
    return [view] if view is not None else []


def _payload_view_source(*, captured_topics: object, has_views: bool) -> str:
    """Label the origin of the payload views so the UI never implies fabrication.

    Only claim ``live_capture`` when the broker ACTUALLY delivered payloads (a
    non-empty ``captured_topics``). A failed or timed-out capture leaves the
    pasted default payloads in place with an empty ``captured_topics``; labelling
    those "live_capture" would present pasted values as real device data.
    """
    if not has_views:
        return "none"
    if isinstance(captured_topics, (list, tuple)) and captured_topics:
        return "live_capture"
    return "direct_inputs"


def _dict_or_empty(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_count(parameters: dict[str, object]) -> int:
    # Multi-asset runs carry their payloads inside each assets[] entry.
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        return sum(_message_count(entry) for entry in assets if isinstance(entry, dict))
    messages = parameters.get("messages")
    if isinstance(messages, list):
        return len(messages)
    return sum(1 for key in ("state_payload", "metadata_payload", "pointset_payload") if isinstance(parameters.get(key), dict))


def _latest_payload_timestamp(parameters: dict[str, object]) -> str | None:
    timestamps: list[str] = []
    sources: list[dict[str, object]] = [parameters]
    assets = parameters.get("assets")
    if isinstance(assets, list):
        sources.extend(entry for entry in assets if isinstance(entry, dict))
    for source in sources:
        for key in ("state_payload", "metadata_payload", "pointset_payload"):
            payload = _dict_or_empty(source.get(key))
            timestamp = payload.get("timestamp")
            if isinstance(timestamp, str):
                timestamps.append(timestamp)
    if not timestamps:
        return None
    return max(timestamps, key=_parse_timestamp_sort_key)


def _parse_timestamp_sort_key(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min
