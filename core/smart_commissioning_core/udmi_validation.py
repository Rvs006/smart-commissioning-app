import hashlib
import json
import math
import re
import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from smart_commissioning_core.dbo_units import (
    KNOWN_CANONICAL_UNITS,
    KNOWN_UNIT_NAMES,
    NUMERIC_CANONICAL_UNITS,
    NUMERIC_UNIT_NAMES,
    canonical_unit,
)
from smart_commissioning_core.engines.comparison_common import make_issue
from smart_commissioning_core.mqtt_settings import (
    INDEFINITE_BACKSTOP_SECONDS,
    _broker_error_status,
    _string,
    build_mqtt_connection_settings,
    parse_bool,
    parse_capture_seconds,
    parse_int,
)
from smart_commissioning_core.mqtt_transport import (
    DEFAULT_PRIMARY_RETAINED_BYTES,
    DEFAULT_SECONDARY_RETAINED_BYTES,
    MqttCaptureInterrupted,
    MqttCaptureOutcome,
    MqttMessage,
    MqttTransportError,
    _topic_matches_filter,
    subscribe_and_capture_with_outcome,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_results import build_validation_summary_v1
from smart_commissioning_core.udmi_schema import (
    _is_rfc3339_datetime,
    declared_version,
    is_nonpub_version,
    nonpub_version_key,
    structural_issues,
    structural_version_available,
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

# Back-compatible public names now sourced from the pinned, shared DBO module.
NUMERIC_UDMI_UNITS = NUMERIC_UNIT_NAMES
KNOWN_UDMI_UNITS = KNOWN_UNIT_NAMES
_KNOWN_CANONICAL_UNITS = KNOWN_CANONICAL_UNITS
_NUMERIC_CANONICAL_UNITS = NUMERIC_CANONICAL_UNITS

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
    return canonical_unit(value)


def _is_blank_value(value: object) -> bool:
    """True when a present field is blank: None, or an empty/whitespace string.

    0, 0.0 and False are real observations, never blank — so this deliberately
    does NO falsiness check (a numeric zero or a boolean must not read as empty).
    """
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class UdmiValidationResult:
    result_summary: dict[str, object]
    issues: list[ValidationIssueRecord]
    source_fixture: str


@dataclass
class _AssetTopicDiscoveryState:
    """Bounded topic-only evidence for one uniquely identified register asset."""

    expected_topics: dict[str, dict[str, object]]
    alternate_topics: dict[str, dict[str, object]]
    expected_seen: bool = False
    alternate_seen: bool = False
    matched_message_count: int = 0
    topic_limit_reached: bool = False


LiveCapture = Callable[..., list[MqttMessage] | MqttCaptureOutcome]
CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[Callable[[], UdmiValidationResult]], None]

# Capture defaults: the window matches mqtt_discovery's DEFAULT_CAPTURE_SECONDS;
# the message cap is a SAFETY ceiling only — completion is decided by
# _capture_stop_when (a payload seen for every expected topic), never by raw
# message count, so duplicate publishes on one chatty topic cannot end a
# capture before the quiet topics report.
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_MAX_MESSAGES = 500
# Raw evidence is deliberately bounded independently of validation metrics. A
# terminal run may retain every message delivered by the primary validation
# lanes, but a pathological broker must never turn a run record into an
# unbounded payload store.
MAX_RAW_EVIDENCE_RECORDS = 10_000
MAX_RAW_EVIDENCE_BYTES = 10 * 1024 * 1024
# Opt-in forensic topic tracing keeps no payload bodies and holds at most this
# many distinct topic records for one registered asset. The cap is deliberately
# small: a broad MQTT subscription may carry unrelated high-volume traffic.
DEFAULT_ASSET_TOPIC_DISCOVERY_LIMIT = 20
MAX_ASSET_TOPIC_DISCOVERY_LIMIT = 100


def validate_udmi_full_report(
    parameters: dict[str, object] | None = None,
    *,
    live_capture: LiveCapture | None = subscribe_and_capture_with_outcome,
    cancel_check: CancelCheck | None = None,
    progress_callback: ProgressCallback | None = None,
) -> UdmiValidationResult:
    parameters = dict(parameters or {})
    capture_issues: list[ValidationIssueRecord] = []
    capture_started_at = datetime.now(UTC)
    capture_summary = _capture_live_payloads(
        parameters,
        live_capture=live_capture,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    capture_ended_at = datetime.now(UTC)
    if capture_summary.get("attempted") is True:
        capture_summary = {
            **capture_summary,
            "capture_started_at": capture_started_at.isoformat(),
            "capture_ended_at": capture_ended_at.isoformat(),
            "capture_duration_seconds": max(
                0.0, (capture_ended_at - capture_started_at).total_seconds()
            ),
        }
    capture_issue = capture_summary["issue"]
    if isinstance(capture_issue, ValidationIssueRecord):
        capture_issues.append(capture_issue)

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
    issues.extend(_wrong_topic_issues(parameters, issues))
    issues = _unique_udmi_issue_ids(issues)
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
        "capture_started_at": capture_summary.get("capture_started_at"),
        "capture_ended_at": capture_summary.get("capture_ended_at"),
        "capture_duration_seconds": capture_summary.get("capture_duration_seconds"),
        "window_completed": capture_summary.get("window_completed"),
        "termination_reason": capture_summary.get("termination_reason"),
        "capture_retention": capture_summary.get("capture_retention"),
        "captured_topics": capture_summary["captured_topics"],
        "subscribed_topics": capture_summary.get("subscribed_topics", []),
        "unexpected_device_count": len(_unexpected_devices(parameters)),
        "unexpected_devices": _unexpected_devices(parameters),
        "unexpected_devices_measured": bool(
            parameters.get("unexpected_devices_measured")
        ),
        "unexpected_devices_measurement_scope": parameters.get(
            "unexpected_devices_measurement_scope"
        ),
        "wrong_topic_asset_count": len(_wrong_topic_assets(parameters)),
        "wrong_topic_assets": _wrong_topic_assets(parameters),
        "payload_views": payload_views,
        "raw_evidence": _raw_evidence_from_parameters(parameters or {}),
        "payload_view_source": _payload_view_source(
            captured_topics=capture_summary["captured_topics"],
            has_views=bool(payload_views),
        ),
        # A terminal validator return always replaces any in-progress snapshots
        # written by ``progress_callback``. Consumers can therefore distinguish
        # a live projection from the final evidence without inferring from
        # counts that may still change.
        "provisional": False,
    }
    asset_topic_discovery = parameters.get("asset_topic_discovery")
    if isinstance(asset_topic_discovery, dict):
        # Optional diagnostic evidence. It is deliberately separate from the
        # validation metrics: seeing an asset ID on another MQTT topic is useful
        # forensic evidence, not proof that its required UDMI payload arrived.
        result_summary["asset_topic_discovery"] = asset_topic_discovery
    result_summary["validation_summary_v1"] = build_validation_summary_v1(
        parameters,
        issues,
        fallback_expected_asset_ids=expected_devices,
        fallback_observed_asset_ids=[
            device for device in expected_devices if str(device) not in {str(item) for item in not_publishing}
        ],
    )
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
    expected_devices: Sequence[object],
    not_publishing: Sequence[object],
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

    # ``DevicesNotExpected`` was represented as a validation issue by early
    # fixture imports. Unexpected publishers are supporting measurement
    # evidence, not faults against the registered validation schedule, so they
    # must never enter issue/fault metrics. Current live runs persist them under
    # ``unexpected_devices`` instead (with topics, last-seen and measurement
    # completeness); legacy fixture keys are deliberately not promoted to an
    # issue here.

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


_STATE_SHAPED_METADATA_KEYS = frozenset({"operational", "last_config"})


def _state_shaped_metadata_evidence(payload: dict[str, Any]) -> list[str]:
    """Return state-only key paths found in a payload assigned to metadata.

    ``operational`` and ``last_config`` are valid evidence of state content but
    are not metadata fields in the canonical UDMI payload. Keeping the paths
    rather than the whole body gives the operator an actionable routing hint
    without copying private payload content into the issue row.
    """

    matches: list[str] = []

    def visit(node: object, path: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            key_text = str(key)
            current_path = (*path, key_text)
            if key_text in _STATE_SHAPED_METADATA_KEYS:
                rendered = ".".join(current_path)
                if isinstance(value, bool):
                    rendered += f"={str(value).lower()}"
                elif value is not None and not isinstance(value, (dict, list)):
                    rendered += f"={value}"
                matches.append(rendered)
            if isinstance(value, dict):
                visit(value, current_path)

    visit(payload)
    return sorted(set(matches))


def _metadata_topic_hint(parameters: dict[str, object], expected: dict[str, Any]) -> str:
    """Return the raw/expected metadata topic when the caller recorded one."""

    for source in (parameters, expected):
        for key in ("metadata_topic", "raw_topic"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        expected_topic = source.get("expected_topic")
        if isinstance(expected_topic, str):
            cleaned_topic = expected_topic.strip()
            if cleaned_topic.casefold().rstrip("/").endswith("/metadata"):
                return cleaned_topic
    return ""


def _state_on_metadata_issue(
    *,
    parameters: dict[str, object],
    expected: dict[str, Any],
    metadata_payload: dict[str, Any],
    asset_id: str,
    raw_evidence_uri: str,
) -> ValidationIssueRecord | None:
    evidence = _state_shaped_metadata_evidence(metadata_payload)
    if not evidence:
        return None
    actual_topic_value = parameters.get("metadata_payload_topic")
    actual_topic = actual_topic_value.strip() if isinstance(actual_topic_value, str) else ""
    configured_topic_value = parameters.get("metadata_topic") or expected.get("metadata_topic")
    configured_topic = configured_topic_value.strip() if isinstance(configured_topic_value, str) else ""
    raw_topic = actual_topic or _metadata_topic_hint(parameters, expected)
    # A direct payload fixture can legitimately use the metadata slot while
    # omitting topic provenance. Without a raw/expected metadata topic there is
    # not enough evidence to call the shape a routing error.
    if not raw_topic:
        return None
    expected_topic = configured_topic or raw_topic
    return _issue(
        [],
        asset_id=asset_id,
        issue_type="payload_routing",
        severity="high",
        description=(
            "The raw payload assigned to the metadata slot is state-shaped, which "
            "usually means the publisher routed a state payload to the metadata "
            f"topic. Raw payload type: metadata; raw topic: {raw_topic or 'not recorded'}; "
            f"expected topic: {expected_topic}; key evidence: {', '.join(evidence)}."
        ),
        topic=raw_topic or None,
        expected_value=expected_topic,
        observed_value="; ".join(evidence),
        match_basis="state_on_metadata",
        suggested_action=(
            "Publish the metadata payload on the register's metadata topic and keep "
            "state fields such as operational and last_config on the state topic."
        ),
        raw_evidence_uri=raw_evidence_uri,
    )


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
    # New templates use location.room. Devices that already publish the value
    # under location.section remain compatible: the getter returns all present candidates and
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
     ("section", "room"), "system.location.room (or legacy system.location.section)"),
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


# difflib ratio above which a missing-expected point and an unexpected-received
# point are treated as one misnamed point rather than two independent faults. A
# single-letter slip in a typical snake_case point name scores ~0.98
# (phase2_line_current_sensor vs phas2_line_current_sensor = 50/51 ~ 0.980);
# genuinely different names (supply_air_temperature vs return_air_temperature ~
# 0.77) stay below it and remain two issues.
_MISNAME_RATIO_THRESHOLD = 0.8

_DIGIT_RUN = re.compile(r"\d+")


def _differ_only_by_index(expected: str, received: str) -> bool:
    """True when two names are identical apart from their numeric indices.

    ``phase1_line_current_sensor`` and ``phase2_line_current_sensor`` (or
    ``zone1_temp`` / ``zone2_temp``) score ~0.96 on SequenceMatcher, but they are
    DISTINCT indexed points — different physical measurements — not one
    misspelling. Merging them would drop two real faults to one and steer the
    operator to rename phase-2 data onto a phase-1 register row. A genuine typo
    perturbs letters and leaves the digits alone, so its digit-stripped skeleton
    differs and this guard does not fire.
    """
    return expected != received and _DIGIT_RUN.sub("", expected) == _DIGIT_RUN.sub("", received)


def _probable_misname_pairs(missing: list[str], unexpected: list[str]) -> list[tuple[str, str]]:
    """One-to-one (expected, received) pairs whose names are near-identical.

    Greedy highest-ratio pairing, ties broken alphabetically, each name
    consumed at most once; anything under the threshold stays two issues.
    Indexed siblings (see :func:`_differ_only_by_index`) are excluded even when
    they clear the ratio, so distinct numbered points remain two faults.
    """
    scored = sorted(
        (
            (SequenceMatcher(None, expected, received).ratio(), expected, received)
            for expected in missing
            for received in unexpected
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    pairs: list[tuple[str, str]] = []
    used_expected: set[str] = set()
    used_received: set[str] = set()
    for ratio, expected, received in scored:
        if ratio < _MISNAME_RATIO_THRESHOLD:
            break
        if expected in used_expected or received in used_received:
            continue
        if _differ_only_by_index(expected, received):
            continue
        pairs.append((expected, received))
        used_expected.add(expected)
        used_received.add(received)
    return pairs


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


def _applicable_payload_types(expected: dict[str, Any]) -> set[str]:
    raw_types = expected.get("payload_types")
    status = _string(expected.get("payload_applicability_status")).casefold()
    if isinstance(raw_types, list):
        return {
            str(payload_type).strip().casefold()
            for payload_type in raw_types
            if str(payload_type).strip().casefold() in _PAYLOAD_ISSUE_TYPES
        }
    if status in {"unresolved", "invalid"}:
        return set()
    # Direct fixture inputs from before the register applicability contract
    # retain their historical shape; imported register entries always carry
    # payload_types plus a status.
    return set(_PAYLOAD_ISSUE_TYPES)


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
    applicable_payload_types = _applicable_payload_types(expected)
    state_payload = _dict_or_empty(parameters.get("state_payload"))
    metadata_payload = _dict_or_empty(parameters.get("metadata_payload"))
    pointset_payload = _dict_or_empty(parameters.get("pointset_payload"))
    raw_evidence_uri = str(parameters.get("raw_evidence_uri") or "runtime://udmi-validation/review-payloads")
    if uploaded_schemas is None:
        uploaded_schemas = _nonpub_schema_sets(parameters)

    applicability_status = _string(expected.get("payload_applicability_status")).casefold()
    if applicability_status in {"unresolved", "invalid"}:
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="payload_applicability",
                severity="high",
                description=(
                    "The register does not contain an approved payload applicability matrix; "
                    "expected payload types were not invented."
                ),
                expected_value="an explicit subset of state, metadata, pointset",
                observed_value=applicability_status,
                suggested_action=(
                    "Obtain the approved payload applicability decision and rerun validation "
                    "with Payload applicability populated."
                ),
                raw_evidence_uri=raw_evidence_uri,
            )
        )

    state_on_metadata = _state_on_metadata_issue(
        parameters=parameters,
        expected=expected,
        metadata_payload=metadata_payload,
        asset_id=asset_id,
        raw_evidence_uri=raw_evidence_uri,
    )
    if state_on_metadata is not None and "metadata" in applicable_payload_types:
        issues.append(state_on_metadata)

    # Register identity values that can never fit canonical UDMI are reported
    # by name; the template embeds a schema-valid placeholder for them instead
    # of failing wholesale (see _METADATA_REGISTER_FIELDS).
    issues.extend(
        _register_canonical_notes(expected, issues, asset_id=asset_id, raw_evidence_uri=raw_evidence_uri)
    )

    # The expected side is a real UDMI-shaped template, not a copy of an
    # observation. Report invalid register constraints before comparing a
    # captured payload, otherwise a malformed register value would look valid.
    for payload_type in applicable_payload_types:
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

    # Version gate first (workbench contract, field engineer 2026-07-09): the register's
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
        if payload_type not in applicable_payload_types:
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="payload_not_applicable",
                    severity="high",
                    description=(
                        f"A {payload_type} payload was observed, but that payload type is not "
                        "approved for this asset."
                    ),
                    expected_value="not applicable",
                    observed_value="received",
                    suggested_action="Confirm the applicability matrix or remove the unapproved publisher output.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )
            continue
        if payload_type == "metadata" and state_on_metadata is not None:
            # The raw metadata evidence is retained by the grouped routing
            # issue. Running it through the metadata schema as well would turn
            # one likely publisher mistake into a noisy list of secondary
            # property/point findings.
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
                # The payload omitted its authority, but the imported register
                # can still authorize required-field checks when its expected
                # version is pinned locally (or supplied as an uploaded nonpub
                # set). Validate a copy so the original evidence remains an
                # honest unversioned payload and the missing-version issue stays.
                if structural_version_available(expected_version, uploaded_schemas):
                    shadow_version = (
                        nonpub_version_key(expected_version)
                        if is_nonpub_version(expected_version)
                        else expected_version
                    )
                    shadow_payload = {**payload, "version": shadow_version}
                    for finding in structural_issues(
                        payload_type, shadow_payload, uploaded_schemas
                    ):
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

    # ``location`` is optional in canonical UDMI 1.5.2, but it becomes a
    # register-policy requirement when Site or Room was imported. Report the
    # missing container separately from canonical ``system`` and from leaf
    # identity mismatches so the operator can fix the hierarchy in one move.
    metadata_present = "metadata_payload" in parameters
    expected_location_fields = [
        field for field in ("site", "room") if expected.get(field)
    ]
    if metadata_present and expected_location_fields and state_on_metadata is None:
        system_value = metadata_payload.get("system")
        location_value = (
            system_value.get("location") if isinstance(system_value, dict) else None
        )
        if not isinstance(location_value, dict):
            if "system" not in metadata_payload:
                observed_location = "system missing"
            elif not isinstance(system_value, dict):
                observed_location = f"system is {type(system_value).__name__}"
            elif "location" not in system_value:
                observed_location = "location missing"
            elif location_value is None:
                observed_location = "location null"
            else:
                observed_location = f"location is {type(location_value).__name__}"
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="metadata_validation",
                    severity="high",
                    description=(
                        "Expected metadata system.location is missing or is not an object; "
                        "the asset register supplies "
                        f"{', '.join(expected_location_fields)}."
                    ),
                    expected_value=(
                        "system.location object containing "
                        + ", ".join(expected_location_fields)
                    ),
                    observed_value=observed_location,
                    suggested_action=(
                        "Publish location under metadata.system.location, with Site in "
                        "location.site and Room in location.room."
                    ),
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    # manufacturer/model/serial/firmware/guid/site/room: flag missing or
    # differing expected values when the corresponding payload was captured.
    for expected_key, observed_getter, issue_type, severity, description, action, leaf_keys, canonical_path in _IDENTITY_CHECKS:
        if issue_type == "metadata_validation" and state_on_metadata is not None:
            continue
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
    if metadata_payload and not metadata_points and state_on_metadata is None:
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
        metadata_point_entry = _dict_or_empty(metadata_points.get(point_name))
        metadata_unit = metadata_point_entry.get("units")
        # Workbench contract: the register's expected unit must MATCH the
        # metadata payload's unit (after alias/format normalisation), not merely
        # be a recognisable UDMI unit.
        expected_canonical = _canonical_unit(expected_unit)
        observed_canonical = _canonical_unit(metadata_unit)
        # A blank-but-PRESENT units field ("", null, whitespace) routes to the
        # empty-value pass below, not here — "does not declare units" is the
        # truly-absent case only (units key missing from the point entry).
        if (
            expected_canonical
            and metadata_payload
            and point_name in metadata_points
            and not observed_canonical
            and "units" not in metadata_point_entry
        ):
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
                    match_basis="units",
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
                    match_basis="units",
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
                    description=f"Metadata unit '{unit_to_check}' for {point_name} is not a recognized DBO unit.",
                    point_name=str(point_name),
                    expected_value="recognized DBO unit",
                    observed_value=str(unit_to_check),
                    match_basis="units",
                    suggested_action=(
                        "Correct the unit spelling or add the intended unit to the pinned "
                        "Digital Buildings Ontology vocabulary after review."
                    ),
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

        present_value = _dict_or_empty(pointset_points.get(point_name)).get("present_value")
        # A blank-but-present value routes to the empty-value pass below (the
        # accurate "never linked" fact), not to this numeric-type complaint.
        if (
            observed_canonical in _NUMERIC_CANONICAL_UNITS
            and present_value is not None
            and not _is_blank_value(present_value)
            and not isinstance(present_value, int | float)
        ):
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

    # Empty-value pass (field review 2026-07-20, doc item 9): a units or
    # present_value field that is PRESENT but blank ("", null, or whitespace) is
    # a real fault — a point never linked to its data source — distinct from an
    # ABSENT field, which the missing-field checks above/below still handle
    # unchanged. Each field is swept ONLY in the payload where canonical UDMI
    # 1.5.2 permits it: units in metadata point models, present_value in pointset
    # event points (model_pointset_point.json / events_pointset_point.json, both
    # additionalProperties:false). A blank field in the OTHER payload is an
    # illegal property the structural pass already flags with the correct
    # "remove it" remediation — re-flagging it here would double the blocking
    # count with contradictory register/publisher advice.
    for payload_label, empty_issue_type, points_map, payload_present, field in (
        ("Metadata", "metadata_validation", metadata_points, bool(metadata_payload), "units"),
        ("Pointset", "pointset_validation", pointset_points, bool(pointset_payload), "present_value"),
    ):
        if not payload_present:
            continue
        for empty_point in sorted(points_map):
            entry = points_map[empty_point]
            if not isinstance(entry, dict):
                continue  # non-dict point entries are already reported structurally
            if field not in entry or not _is_blank_value(entry[field]):
                continue
            value = entry[field]
            description = (
                f"{payload_label} point {empty_point} carries an empty {field} value; "
                "the field is present but blank."
            )
            expected_value = "a non-empty value"
            if field == "units":
                register_unit = expected_units.get(empty_point)
                if register_unit and _canonical_unit(register_unit):
                    description += f" The register expects {register_unit}."
                    expected_value = str(register_unit)
                suggested_action = (
                    "Set the point's units in the device metadata, or remove the point "
                    "if it is not configured."
                )
            else:
                suggested_action = (
                    "Link the point to its data source so the publisher reports a real "
                    "value, then re-run."
                )
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type=empty_issue_type,
                    severity="high",
                    description=description,
                    point_name=str(empty_point),
                    expected_value=expected_value,
                    observed_value="null" if value is None else f'"{value}"',
                    match_basis="units" if field == "units" else None,
                    suggested_action=suggested_action,
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
    # A single misnamed point lands one spelling in ``missing`` and its near-twin
    # in ``unexpected``; pair those into one issue naming both spellings so a
    # typo is not double-counted as an absent point AND an unexpected point. Only
    # leftovers after that pairing stay as the two independent messages below.
    # When no pointset payload was captured ``unexpected_points`` is empty, so
    # the pairing is a no-op and an offline device never reads as a rename.
    # Broker silence is one payload-level absence, not one missing-point fault
    # per register row. An explicitly present empty JSON object is different:
    # it is captured evidence and must still fail the expected-point checks.
    pointset_present = "pointset_payload" in parameters
    missing_points = sorted(expected_points - observed_points) if pointset_present else []
    unexpected_points = sorted(observed_points - expected_points) if pointset_present else []
    misname_pairs = _probable_misname_pairs(missing_points, unexpected_points)
    paired_expected = {expected_name for expected_name, _ in misname_pairs}
    paired_received = {received_name for _, received_name in misname_pairs}
    for expected_name, received_name in misname_pairs:
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="pointset_validation",
                severity="high",
                description=(
                    f"Expected point {expected_name} was not received; the pointset instead "
                    f"carries the similarly named {received_name} — probably a single misnamed "
                    "point. One issue is reported for both spellings."
                ),
                point_name=expected_name,
                expected_value=expected_name,
                observed_value=received_name,
                suggested_action=(
                    "Align the point name between the register and the publisher "
                    "(correct whichever spelling is wrong), then re-run."
                ),
                raw_evidence_uri=raw_evidence_uri,
            )
        )
    for point_name in missing_points:
        if point_name in paired_expected:
            continue
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
    for point_name in unexpected_points:
        if point_name in paired_received:
            continue
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
    if metadata_payload and state_on_metadata is None:
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
    if not _is_rfc3339_datetime(timestamp):
        return None  # Structural validation owns malformed timestamp findings.
    payload_time = _parse_rfc3339_datetime(timestamp)
    observed_raw = parameters.get("pointset_payload_received_at") or parameters.get(
        "capture_observed_at"
    )
    # A pasted payload without a broker receive/capture timestamp has no
    # defensible age. Do not substitute the current wall clock, which turns an
    # old fixture into a false stale finding (or a future fixture into a false
    # clock finding). Structural timestamp validation still reports malformed
    # or missing payload timestamps separately.
    if not observed_raw:
        return None
    observed_time = _parse_rfc3339_datetime(observed_raw)
    if payload_time is None or observed_time is None:
        return None
    if payload_time.tzinfo is None or observed_time.tzinfo is None:
        return None

    age_seconds = (observed_time.astimezone(UTC) - payload_time.astimezone(UTC)).total_seconds()
    # Whole-hour clock-labelling signature: a device stamping LOCAL wall time
    # (e.g. BST = UTC+1) but labelling it "Z" reads ~N*3600s off, firing this as
    # a bogus HIGH cadence miss and burying real data faults. Diagnose it in the
    # wording (still reporting it: a mislabelled clock IS a conformance fault).
    # abs(hour_offset)<=14 bounds it to real-world UTC offsets. The residual to
    # the nearest whole hour is <=1800s (half the 3600s rounding unit), so an
    # uncapped max(interval, 300) tolerance fires on EVERY stale payload once the
    # cadence is >=30 min (the UK half-hourly fleet), misreading a dead device as
    # a clock artifact. Cap the window at 600s so the signature only claims ages
    # sitting genuinely NEAR a whole hour, not any stale one.
    hour_offset = round(age_seconds / 3600)
    tz_tolerance = min(max(interval_seconds, 300.0), 600.0)
    tz_signature = (
        hour_offset != 0
        and abs(hour_offset) <= 14
        and abs(age_seconds - hour_offset * 3600) <= tz_tolerance
    )
    if tz_signature and age_seconds < 0:
        # A future payload cannot be explained by missed cadence, so a whole-hour
        # offset is strong clock-labelling evidence.
        tz_hint = (
            f" This looks like a whole-hour clock-labelling offset, about "
            f"{abs(hour_offset)} hour(s); check that the device stamps UTC."
        )
    elif tz_signature:
        # A past payload exactly one hour old is observationally identical to a
        # device that genuinely stopped publishing one hour ago. Preserve both
        # diagnoses instead of steering the operator to the clock alone.
        tz_hint = (
            f" The age is close to a whole-hour offset, about {abs(hour_offset)} "
            "hour(s). This can be a clock-labelling error or genuine stale "
            "publishing; check both UTC labelling and the publish cadence."
        )
    else:
        tz_hint = ""
    if age_seconds < -interval_seconds or age_seconds <= interval_seconds:
        if age_seconds < -interval_seconds:
            return _issue(issues, asset_id=asset_id, issue_type="pointset_timestamp", severity="high", description="Pointset payload timestamp is too far in the future for the capture clock." + tz_hint, expected_value="current device time", observed_value=f"{age_seconds:.1f}s age", suggested_action="Synchronize device and commissioning host clocks.", raw_evidence_uri=raw_evidence_uri)
        return None

    retained = parse_bool(parameters.get("pointset_payload_retained"))
    retained_detail = " It was delivered as a retained MQTT message." if retained else ""
    return _issue(
        issues,
        asset_id=asset_id,
        issue_type="pointset_timestamp",
        severity="high",
        description=(
            "Pointset payload timestamp exceeds the register's Expected reporting interval "
            f"({age_seconds:.1f}s old; expected at most {interval_seconds:g}s)."
            f"{retained_detail}{tz_hint}"
        ),
        expected_value=f"at most {interval_seconds:g} seconds old",
        observed_value=f"{age_seconds:.1f} seconds old" + (" (retained)" if retained else ""),
        suggested_action="Wait for a fresh pointset publish and verify the device reporting cadence.",
        raw_evidence_uri=raw_evidence_uri,
    )


def _parse_rfc3339_datetime(value: object) -> datetime | None:
    if not _is_rfc3339_datetime(value):
        return None
    assert isinstance(value, str)
    return datetime.fromisoformat(value.upper().replace("Z", "+00:00"))


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
        # An unresolved applicability matrix has no expected validation groups.
        # Keep broker observation alive for the configured window instead of
        # treating "nothing is approved" as "everything is complete".
        if not groups:
            return False
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
        parameters[f"{key}_topic"] = message.topic
        parameters[f"{key}_retained"] = message.retained
        parameters[f"{key}_received_at"] = message.received_at.isoformat()


def _valid_payload_topics(messages: list[MqttMessage]) -> set[str]:
    return {message.topic for message in _valid_payload_messages(messages)}


def _ordered_valid_payload_topics(messages: list[MqttMessage]) -> list[str]:
    return list(dict.fromkeys(message.topic for message in _valid_payload_messages(messages)))


def _missing_topics_issue(*, asset_id: str | None, missing: list[list[str]], got_any: bool) -> ValidationIssueRecord:
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


_CAPTURE_ERROR_ACTIONS = {
    "broker_not_configured": (
        "Enter the MQTT broker FQDN or IP address on the Configuration page "
        "and save it."
    ),
    "dns_resolution_failed": (
        "Check the broker FQDN or IP address on the Configuration page. The "
        "configured hostname did not resolve in DNS."
    ),
}
_DEFAULT_CAPTURE_ERROR_ACTION = "Check broker reachability, credentials, TLS configuration, and topic filters."


def _capture_error_issue(*, asset_id: str | None, status_detail: str) -> ValidationIssueRecord:
    return _issue(
        [],
        asset_id=asset_id,
        issue_type="payload_error",
        severity="high",
        description=f"Live MQTT capture failed ({status_detail}).",
        suggested_action=_CAPTURE_ERROR_ACTIONS.get(status_detail, _DEFAULT_CAPTURE_ERROR_ACTION),
    )


def _invalid_payload_issue(
    *,
    asset_id: str | None,
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


_CAPTURE_SECRET_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "connection_string",
    "cookie",
    "credential",
    "database_url",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "session_key",
    "token",
)


def _redact_capture_payload(value: object, *, key: object = "") -> tuple[object, bool]:
    """Keep raw JSON useful without persisting credential-shaped values."""
    key_text = str(key).casefold().replace("-", "_").replace(" ", "_")
    if key_text in {"pwd", "passwd"} or any(
        part in key_text or part.replace("_", "") in key_text.replace("_", "")
        for part in _CAPTURE_SECRET_PARTS
    ):
        return "********", value not in (None, "")
    if isinstance(value, dict):
        redacted = False
        result: dict[str, object] = {}
        for child_key, child_value in value.items():
            clean, child_redacted = _redact_capture_payload(child_value, key=child_key)
            result[str(child_key)] = clean
            redacted = redacted or child_redacted
        return result, redacted
    if isinstance(value, list):
        redacted = False
        result = []
        for item in value:
            clean, item_redacted = _redact_capture_payload(item)
            result.append(clean)
            redacted = redacted or item_redacted
        return result, redacted
    if isinstance(value, str) and "-----begin" in value.casefold() and "private key-----" in value.casefold():
        return "********", True
    return value, False


def _serialise_capture_message(
    message: MqttMessage,
    *,
    asset_id: str | None = None,
) -> dict[str, object]:
    """Persistable message evidence for progress, terminal results, and export."""
    payload = message.json_payload()
    clean_payload, redacted = _redact_capture_payload(payload)
    payload_type = _payload_key_for_topic(message.topic)
    if payload_type:
        payload_type = payload_type.removesuffix("_payload")
    payload_timestamp = (
        payload.get("timestamp")
        if isinstance(payload, dict) and isinstance(payload.get("timestamp"), str)
        else None
    )
    body_bytes = len(message.payload)
    return {
        "asset_id": asset_id,
        "payload_type": payload_type,
        "topic": message.topic,
        "payload": clean_payload if isinstance(payload, dict) else None,
        "payload_encoding": "json" if isinstance(payload, dict) else "omitted_non_json",
        "payload_size_bytes": body_bytes,
        "content_sha256": hashlib.sha256(message.payload).hexdigest(),
        "payload_timestamp": payload_timestamp,
        "retained": message.retained,
        "received_at": message.received_at.isoformat(),
        "broker_received_at": message.received_at.isoformat(),
        "qos": message.qos,
        "redaction_status": "redacted" if redacted else "none",
    }


def _raw_evidence_from_parameters(parameters: dict[str, object]) -> dict[str, object]:
    """Return bounded portable records from the terminal capture projection."""
    records: list[dict[str, object]] = []
    total_bytes = 0
    truncated = False
    assets = parameters.get("assets")
    sources: list[tuple[str | None, object]] = []
    if isinstance(assets, list) and assets:
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            expected = _dict_or_empty(entry.get("expected_schedule"))
            sources.append((str(expected.get("asset_id") or "") or None, entry.get("messages")))
    else:
        expected = _dict_or_empty(parameters.get("expected_schedule"))
        sources.append((str(expected.get("asset_id") or "") or None, parameters.get("messages")))
    for asset_id, raw_messages in sources:
        if not isinstance(raw_messages, list):
            continue
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            record = dict(raw)
            if not record.get("asset_id"):
                record["asset_id"] = asset_id
            size = parse_int(record.get("payload_size_bytes"), default=0)
            if len(records) >= MAX_RAW_EVIDENCE_RECORDS or total_bytes + max(0, size) > MAX_RAW_EVIDENCE_BYTES:
                truncated = True
                continue
            records.append(record)
            total_bytes += max(0, size)
    return {
        "records": records,
        "record_count": len(records),
        "captured_record_count": len(records),
        "truncated": truncated,
        "retained_bytes": total_bytes,
    }


def _route_capture_messages_to_assets(
    entries: list[dict],
    per_entry_subscribed_topics: list[list[str]],
    per_entry_validation_topics: list[list[str]],
    messages: list[MqttMessage],
    *,
    observed_at: str,
    wrong_topic_routes: dict[str, int] | None = None,
) -> None:
    """Route the latest live evidence into every matching register asset."""
    wrong_topic_routes = wrong_topic_routes or {}
    for entry_index, (entry, subscribed_topics, validation_topics) in enumerate(
        zip(
            entries,
            per_entry_subscribed_topics,
            per_entry_validation_topics,
            strict=True,
        )
    ):
        entry["capture_observed_at"] = observed_at
        entry["subscribed_topics"] = list(subscribed_topics)
        publisher_root = _entry_publisher_root(entry, subscribed_topics)
        diagnostic_messages = [
            message
            for message in messages
            if wrong_topic_routes.get(message.topic) == entry_index
            or (
                any(
                    _topic_matches_filter(message.topic, topic)
                    for topic in subscribed_topics
                )
                and (
                    any(
                        _topic_matches_filter(message.topic, topic)
                        for topic in validation_topics
                    )
                    or (
                        publisher_root is not None
                        and _topic_within_publisher_root(message.topic, publisher_root)
                    )
                )
            )
        ]
        entry["observed_topics"] = list(
            dict.fromkeys(message.topic for message in diagnostic_messages)
        )
        entry_messages = [
            message
            for message in messages
            if wrong_topic_routes.get(message.topic) == entry_index
            or any(
                _topic_matches_filter(message.topic, topic)
                for topic in validation_topics
            )
        ]
        expected = _dict_or_empty(entry.get("expected_schedule"))
        entry["messages"] = [
            _serialise_capture_message(
                message,
                asset_id=str(expected.get("asset_id") or "") or None,
            )
            for message in entry_messages
        ]
        _route_latest_payloads(entry, entry_messages)


def _progressive_invalid_payload_issues(
    messages: list[MqttMessage],
    existing_issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Describe malformed payloads already observed without claiming future silence."""
    issues = [*existing_issues]
    first_new_issue = len(issues)
    for topic in sorted(
        {message.topic for message in messages if not isinstance(message.json_payload(), dict)}
    ):
        issues.append(
            _issue(
                issues,
                asset_id=None,
                issue_type="payload_error",
                severity="critical",
                description=f"MQTT message on {topic} is not a valid JSON object.",
                observed_value="invalid JSON object",
                suggested_action="Fix the publisher so the topic carries a JSON object.",
            )
        )
    return issues[first_new_issue:]


def _build_progressive_result(
    parameters: dict[str, object],
    messages: list[MqttMessage],
    *,
    capture_mode: str,
    capture_window_seconds: float | None,
    subscribed_topics: list[str],
    message_count: int,
) -> UdmiValidationResult:
    """Build an honest in-progress projection from evidence received so far.

    Silence is deliberately not an issue until the capture window closes. The
    projection can report missing expected payloads as a current count, while
    ``provisional`` makes clear that later messages can still change it.
    """
    # ``_inline_full_report`` only creates not-publishing findings when a live
    # capture is complete. Use a shallow top-level copy with that final-window
    # inference disabled while retaining the real, already-routed asset data.
    provisional_parameters = dict(parameters)
    provisional_parameters["use_live_broker"] = False
    full_report = _inline_full_report(provisional_parameters)
    issues = _normalise_issues(full_report)
    register_rejection = _register_rejection_issue(parameters, issues)
    if register_rejection is not None:
        issues.append(register_rejection)
    issues.extend(_register_duplicate_id_issues(parameters, issues))
    issues.extend(_review_all_payload_issues(parameters, issues))
    issues.extend(_wrong_topic_issues(parameters, issues))
    issues.extend(_progressive_invalid_payload_issues(messages, issues))
    issues = _unique_udmi_issue_ids(issues)

    expected_devices = _list_value(full_report, "DeviceList")
    payload_views = _build_payload_views(parameters)
    observed_asset_ids = []
    for view in payload_views:
        payload_types = view.get("payload_types")
        if not isinstance(payload_types, list):
            continue
        if any(
            bool(payload.get("observed_present"))
            for payload in payload_types
            if isinstance(payload, dict)
        ):
            observed_asset_ids.append(str(view.get("asset_id")))
    captured_topics = _ordered_valid_payload_topics(messages)
    conformance = _conformance_fields(expected_devices, [], issues)
    result_summary: dict[str, object] = {
        "expected_devices": len(expected_devices),
        "publishing_seen": len(set(observed_asset_ids)),
        "not_publishing": 0,
        "not_publishing_devices": [],
        "pointset_valid": 0,
        "state_valid": 0,
        "issue_count": len(issues),
        "blocking_issue_count": conformance["blocking_issue_count"],
        "payload_conformance_percent": conformance["payload_conformance_percent"],
        "message_count": message_count,
        "payload_last_seen": _latest_payload_timestamp(parameters),
        "source": "schedule_payload_inputs",
        "source_fixture": "live_capture_in_progress",
        "broker_capture_attempted": True,
        "broker_status_detail": "live_capture_in_progress",
        "capture_mode": capture_mode,
        "capture_window_seconds": capture_window_seconds,
        "captured_topics": captured_topics,
        "subscribed_topics": list(subscribed_topics),
        "unexpected_device_count": len(_unexpected_devices(parameters)),
        "unexpected_devices": _unexpected_devices(parameters),
        "unexpected_devices_measured": bool(
            parameters.get("unexpected_devices_measured")
        ),
        "unexpected_devices_measurement_scope": parameters.get(
            "unexpected_devices_measurement_scope"
        ),
        "wrong_topic_asset_count": len(_wrong_topic_assets(parameters)),
        "wrong_topic_assets": _wrong_topic_assets(parameters),
        "payload_views": payload_views,
        "raw_evidence": _raw_evidence_from_parameters(parameters),
        "payload_view_source": _payload_view_source(
            captured_topics=captured_topics,
            has_views=bool(payload_views),
        ),
        "provisional": True,
    }
    result_summary["validation_summary_v1"] = build_validation_summary_v1(
        parameters,
        issues,
        fallback_expected_asset_ids=expected_devices,
        fallback_observed_asset_ids=observed_asset_ids,
    )
    return UdmiValidationResult(
        result_summary=result_summary,
        issues=issues,
        source_fixture="live_capture_in_progress",
    )


def _snapshot_progress_parameters(
    parameters: dict[str, object],
) -> dict[str, object]:
    """Freeze the mutable capture surface before a progress report is built.

    Live capture replaces dynamic entry values (messages, observed topics and
    payloads) rather than mutating those values in place. A top-level copy plus
    one shallow copy per asset therefore gives the background reporter a stable
    point-in-time view without deep-copying payload JSON on the MQTT reader.
    """

    snapshot = dict(parameters)
    assets = parameters.get("assets")
    if isinstance(assets, list):
        snapshot["assets"] = [
            dict(asset) if isinstance(asset, dict) else asset for asset in assets
        ]
    return snapshot


def _capture_result_details(
    result: list[MqttMessage] | MqttCaptureOutcome,
    *,
    finite_window: bool,
) -> tuple[list[MqttMessage], str, bool, dict[str, object]]:
    """Translate transport truth into the public validation contract.

    A legacy list-only adapter deliberately cannot prove that a finite capture
    reached its deadline.  It remains injectable for older tests and callers,
    but is incomplete rather than silently successful.
    """
    if not isinstance(result, MqttCaptureOutcome):
        return (
            list(result),
            "capture_outcome_unavailable" if finite_window else "required_topics_received",
            False,
            {"capture_outcome_available": False},
        )
    external_reason = {
        "deadline_elapsed": "window_elapsed",
        "cancelled": "cancelled",
        "broker_interruption": "broker_interruption",
        "primary_topic_cap": "message_cap",
        "primary_byte_cap": "byte_cap",
        "required_topics_received": "required_topics_received",
        "indefinite_backstop": "backstop_elapsed",
    }.get(result.termination, "capture_outcome_unavailable")
    deadline_proven = (
        finite_window
        and result.termination == "deadline_elapsed"
        and result.deadline_requested
        and result.deadline_elapsed
        and not result.cancelled
        and not result.primary_cap_reached
        and not result.primary_byte_cap_reached
        and result.interruption_cause is None
    )
    if result.termination == "deadline_elapsed" and not deadline_proven:
        # An indefinite capture receives a transport backstop for safety. An
        # inconsistent adapter outcome is likewise not proof of completion.
        external_reason = "backstop_elapsed" if not finite_window else "capture_outcome_unavailable"
    telemetry = {
        "capture_outcome_available": True,
        "capture_termination_internal": result.termination,
        "primary_cap_reached": result.primary_cap_reached,
        "primary_byte_cap_reached": result.primary_byte_cap_reached,
        "secondary_truncated": result.secondary_truncated,
        "secondary_count_truncated": result.secondary_count_truncated,
        "secondary_byte_truncated": result.secondary_byte_truncated,
        "primary_retained_count": result.primary_retained_count,
        "secondary_retained_count": result.secondary_retained_count,
        "primary_retained_bytes": result.primary_retained_bytes,
        "secondary_retained_bytes": result.secondary_retained_bytes,
        "deadline_requested": result.deadline_requested,
        "deadline_elapsed": result.deadline_elapsed,
    }
    return (
        list(result.messages),
        external_reason,
        deadline_proven,
        telemetry,
    )


def _capture_live_payloads(
    parameters: dict[str, object],
    *,
    live_capture: LiveCapture | None,
    cancel_check: CancelCheck | None = None,
    progress_callback: ProgressCallback | None = None,
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
        return _capture_live_payloads_per_asset(
            parameters,
            assets,
            live_capture=live_capture,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )

    if live_capture is None:
        return {
            "attempted": True,
            "status_detail": "live_capture_unavailable",
            "window_completed": False,
            "termination_reason": "capture_unavailable",
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
            "window_completed": False,
            "termination_reason": "missing_capture_topics",
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
    publisher_root = _entry_publisher_root(parameters, topics)
    validation_topics = _capture_validation_topics(parameters, topics, publisher_root)
    groups = _capture_topic_groups(validation_topics)
    parameters["subscribed_topics"] = list(topics)
    capture_error_status: str | None = None
    capture_cancelled = False
    latest_progress_messages: dict[str, MqttMessage] = {}
    progress_message_count = 0
    progress_state_lock = threading.Lock()

    def build_progress_snapshot() -> UdmiValidationResult:
        with progress_state_lock:
            snapshot_parameters = _snapshot_progress_parameters(parameters)
            snapshot_messages = list(latest_progress_messages.values())
            snapshot_message_count = progress_message_count
        return _build_progressive_result(
            snapshot_parameters,
            snapshot_messages,
            capture_mode=capture_mode,
            capture_window_seconds=timeout_seconds,
            subscribed_topics=topics,
            message_count=snapshot_message_count,
        )

    def on_message(message: MqttMessage) -> None:
        nonlocal progress_message_count
        with progress_state_lock:
            progress_message_count += 1
            latest_progress_messages[message.topic] = message
            progress_messages = list(latest_progress_messages.values())
            parameters["capture_observed_at"] = message.received_at.isoformat()
            parameters["messages"] = [
                _serialise_capture_message(captured) for captured in progress_messages
            ]
            _route_latest_payloads(parameters, progress_messages)
        if progress_callback is not None:
            progress_callback(build_progress_snapshot)

    def capture_cancel_check() -> bool:
        nonlocal capture_cancelled
        if not capture_cancelled and cancel_check is not None:
            capture_cancelled = bool(cancel_check())
        return capture_cancelled

    try:
        capture_result = live_capture(
            build_mqtt_connection_settings(parameters),
            topics=topics,
            # timeout_seconds stays None in the summary (capture_mode "indefinite");
            # the transport gets the 48h backstop so an indefinite capture still
            # ends with its data rather than hanging on a never-publishing device.
            timeout_seconds=timeout_seconds if timeout_seconds is not None else INDEFINITE_BACKSTOP_SECONDS,
            max_messages=parse_int(parameters.get("max_messages"), default=DEFAULT_MAX_MESSAGES),
            qos=parse_int(parameters.get("qos"), default=0),
            cancel_check=capture_cancel_check if cancel_check is not None else None,
            stop_when=(
                _capture_stop_when(groups)
                if timeout_seconds is None and groups
                else (lambda _messages: False)
            ),
            on_message=on_message,
        )
    except MqttCaptureInterrupted as error:
        messages = error.messages
        capture_result = messages
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
            "window_completed": False,
            "termination_reason": "broker_interruption",
            "captured_topics": [],
            "issue": _capture_error_issue(
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                status_detail=broker_status_detail,
            ),
        }

    messages, transport_termination, transport_window_completed, transport_telemetry = _capture_result_details(
        capture_result,
        finite_window=timeout_seconds is not None,
    )
    if transport_termination == "broker_interruption" and isinstance(capture_result, MqttCaptureOutcome):
        capture_error_status = _broker_error_status(
            capture_result.interruption_cause or MqttTransportError("MQTT capture interrupted.")
        )
    capture_observed_at = datetime.now(UTC).isoformat()
    with progress_state_lock:
        parameters["capture_observed_at"] = capture_observed_at
        parameters["messages"] = [
            _serialise_capture_message(message) for message in messages
        ]
        _route_latest_payloads(parameters, messages)

    # Without a transport failure, "captured" is claimed only when EVERY
    # expected topic supplied a usable JSON object; malformed/scalar payloads
    # remain raw evidence but cannot satisfy completion or canonical checks.
    valid_messages = _valid_payload_messages(messages)
    valid_topics = _ordered_valid_payload_topics(messages)
    missing = _unseen_groups(groups, {message.topic for message in valid_messages})
    termination_reason = "cancelled" if capture_cancelled else transport_termination
    if timeout_seconds is None and termination_reason == "required_topics_received" and missing:
        termination_reason = "backstop_elapsed"
    window_completed = transport_window_completed and not capture_cancelled
    if capture_error_status:
        return {
            "attempted": True,
            "status_detail": capture_error_status,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "window_completed": False,
            "termination_reason": "broker_interruption",
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
        "window_completed": window_completed,
        "termination_reason": termination_reason,
        "capture_retention": transport_telemetry,
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
    progress_callback: ProgressCallback | None = None,
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
            "window_completed": False,
            "termination_reason": "capture_unavailable",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=None,
                issue_type="payload_error",
                severity="high",
                description="Live MQTT capture is not available in this execution context.",
                suggested_action="Run live UDMI validation from a service with broker access, or supply captured payloads directly.",
            ),
        }

    entries = [entry for entry in assets if isinstance(entry, dict)]
    per_entry_topics = [_capture_topics(entry) for entry in entries]
    expected_publisher_roots = [
        _entry_publisher_root(entry, entry_topics)
        for entry, entry_topics in zip(entries, per_entry_topics, strict=True)
    ]
    measurement_scope = _unexpected_measurement_scope(expected_publisher_roots)
    per_entry_validation_topics = [
        _capture_validation_topics(entry, entry_topics, publisher_root)
        for entry, entry_topics, publisher_root in zip(
            entries,
            per_entry_topics,
            expected_publisher_roots,
            strict=True,
        )
    ]
    registered_asset_routes = _registered_asset_routes(entries)
    diagnostic_enabled = parse_bool(parameters.get("topic_discovery_enabled"))
    diagnostic_scope: str | None = None
    diagnostic_scope_source = "disabled"
    diagnostic_scope_error: str | None = None
    diagnostic_topic_limit = max(
        1,
        min(
            parse_int(
                parameters.get("topic_discovery_max_topics_per_asset"),
                default=DEFAULT_ASSET_TOPIC_DISCOVERY_LIMIT,
            ),
            MAX_ASSET_TOPIC_DISCOVERY_LIMIT,
        ),
    )
    if diagnostic_enabled:
        (
            diagnostic_scope,
            diagnostic_scope_source,
            diagnostic_scope_error,
        ) = _asset_topic_discovery_scope(parameters, measurement_scope)
    diagnostic_states = (
        [_AssetTopicDiscoveryState({}, {}) for _ in entries]
        if diagnostic_enabled
        else []
    )
    parameters["unexpected_devices"] = []
    parameters["unexpected_devices_measured"] = False
    parameters["unexpected_devices_measurement_scope"] = measurement_scope
    parameters["wrong_topic_assets"] = []
    parameters["wrong_topic_asset_count"] = 0
    if diagnostic_enabled and diagnostic_scope_error:
        # Never turn a missing confirmation into a broader broker subscription.
        # The API rejects this before a run is created; this retained core guard
        # keeps injected/direct callers equally fail-safe.
        status_detail = f"asset_topic_discovery_{diagnostic_scope_error}"
        parameters["asset_topic_discovery"] = _build_asset_topic_discovery(
            entries,
            per_entry_validation_topics,
            expected_publisher_roots,
            registered_asset_routes,
            diagnostic_states,
            scope=None,
            scope_source=diagnostic_scope_source,
            scope_error=diagnostic_scope_error,
            topic_limit=diagnostic_topic_limit,
            capture_complete=False,
            capture_status=status_detail,
        )
        return {
            "attempted": False,
            "status_detail": status_detail,
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=None,
                issue_type="payload_error",
                severity="high",
                description=(
                    "Asset-topic discovery requested all MQTT topics without the required "
                    "all-scope confirmation."
                    if diagnostic_scope_error == "all_scope_confirmation_required"
                    else "Asset-topic discovery scope must be 'bounded' or 'all'."
                ),
                suggested_action=(
                    "Use the bounded register scope, or explicitly confirm the all-topic "
                    "capture before retrying."
                ),
            ),
        }
    topics: list[str] = []
    for entry_topics in per_entry_topics:
        for topic in entry_topics:
            if topic not in topics:
                topics.append(topic)
    validation_topics = list(
        dict.fromkeys(
            topic
            for entry_topics in per_entry_validation_topics
            for topic in entry_topics
        )
    )
    # Expected validation topics remain in the protected primary transport
    # lane. The old bounded measurement scope is the only normal secondary
    # lane. When an explicitly confirmed diagnostic ``#`` subscription is
    # added below, a delivery in neither lane is diagnostic-only metadata,
    # never ordinary validation evidence.
    normal_secondary_topic_filters = [measurement_scope] if measurement_scope else []
    if (
        measurement_scope
        and measurement_scope not in topics
        and diagnostic_scope != "#"
    ):
        # The measurement subscription is observational only. It never enters
        # ``groups`` below, so an absent unexpected publisher cannot block an
        # expected-device capture from completing.
        topics.append(measurement_scope)
    if diagnostic_enabled and diagnostic_scope and diagnostic_scope not in topics:
        # ``diagnostic_scope`` is either the existing bounded measurement scope
        # or an explicitly confirmed ``#`` override. It never changes the
        # expected validation groups below.
        topics.append(diagnostic_scope)
    if not topics:
        return {
            "attempted": True,
            "status_detail": "missing_capture_topics",
            "window_completed": False,
            "termination_reason": "missing_capture_topics",
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=None,
                issue_type="payload_error",
                severity="high",
                description="Live UDMI validation requires at least one state, metadata, or pointset topic.",
                suggested_action="Import a register with Expected topics, or enter capture topics, before starting live capture.",
            ),
        }

    groups: list[list[str]] = []
    for entry_topics in per_entry_validation_topics:
        groups.extend(_capture_topic_groups(entry_topics))
    timeout_seconds, capture_mode = _capture_window(parameters, cancel_check)
    configured_max_messages = parse_int(parameters.get("max_messages"), default=0)
    # The transport retains one latest message per distinct expected topic and
    # stops when this ceiling is reached. A fixed 500-topic default truncates a
    # large register before every expected payload can be retained: 554 assets
    # produce 2,216 concrete validation filters. Keep explicit operator limits,
    # otherwise size the safety ceiling to the actual shared subscription.
    max_messages = configured_max_messages or max(
        DEFAULT_MAX_MESSAGES,
        len(validation_topics),
    )
    unexpected_max_messages = parse_int(
        parameters.get("unexpected_max_messages"),
        default=max_messages,
    )
    parameters["subscribed_topics"] = list(topics)
    capture_error_status: str | None = None
    capture_cancelled = False
    latest_progress_messages: dict[str, MqttMessage] = {}
    latest_progress_validation_messages: dict[str, MqttMessage] = {}
    latest_progress_messages_by_entry: list[dict[str, MqttMessage]] = [
        {} for _ in entries
    ]
    progress_wrong_topic_messages: dict[str, MqttMessage] = {}
    progress_wrong_topic_routes: dict[str, int] = {}
    exact_topic_routes: dict[str, set[int]] = {}
    wildcard_topic_routes: list[tuple[int, str]] = []
    for entry_index, entry_topics in enumerate(per_entry_validation_topics):
        for topic_filter in entry_topics:
            if "+" in topic_filter or "#" in topic_filter:
                wildcard_topic_routes.append((entry_index, topic_filter))
            else:
                exact_topic_routes.setdefault(topic_filter, set()).add(entry_index)
    progress_message_count = 0
    progress_state_lock = threading.Lock()

    def matching_validation_entries(topic: str) -> set[int]:
        matching_entries = set(exact_topic_routes.get(topic, ()))
        for entry_index, topic_filter in wildcard_topic_routes:
            if _topic_matches_filter(topic, topic_filter):
                matching_entries.add(entry_index)
        return matching_entries

    def topic_matches_validation_filter(topic: str) -> bool:
        return bool(matching_validation_entries(topic))

    def matches_validation_topic(message: MqttMessage) -> bool:
        return topic_matches_validation_filter(message.topic)

    def matches_normal_capture_scope(message: MqttMessage) -> bool:
        """Whether a delivery belongs to the normal validation subscription.

        Bounded discovery reuses the normal measurement scope, so it deliberately
        keeps the historical behavior. Only a confirmed all-topic diagnostic
        adds ``#``; traffic outside the expected validation filters and the
        original bounded measurement scope is diagnostic-only in that mode.
        """

        if diagnostic_scope != "#":
            return True
        return matches_validation_topic(message) or bool(
            measurement_scope
            and _topic_matches_filter(message.topic, measurement_scope)
        )

    diagnostic_callback_count = 0

    def on_observed_message(message: MqttMessage) -> None:
        """Index an exact registered asset ID without retaining its payload body."""

        nonlocal diagnostic_callback_count
        if not diagnostic_enabled or diagnostic_scope is None:
            return
        diagnostic_callback_count += 1
        entry_index = _registered_asset_entry_index_from_topic(
            message.topic,
            registered_asset_routes,
        )
        if entry_index is None:
            return
        _record_asset_topic_discovery_message(
            diagnostic_states[entry_index],
            message,
            is_expected_topic=(entry_index in matching_validation_entries(message.topic)),
            topic_limit=diagnostic_topic_limit,
        )

    def capture_cancel_check() -> bool:
        """Latch cancellation observed by the capture loop.

        ``subscribe_and_capture`` returns partial messages normally when its
        cancellation callback becomes true. Remember that termination reason
        here so a cancelled partial window cannot be reported as a completed
        unexpected-publisher measurement.
        """
        nonlocal capture_cancelled
        if not capture_cancelled and cancel_check is not None:
            capture_cancelled = bool(cancel_check())
        return capture_cancelled

    def persist_asset_topic_discovery(
        *,
        capture_complete: bool,
        capture_status: str,
    ) -> None:
        if not diagnostic_enabled:
            return
        parameters["asset_topic_discovery"] = _build_asset_topic_discovery(
            entries,
            per_entry_validation_topics,
            expected_publisher_roots,
            registered_asset_routes,
            diagnostic_states,
            scope=diagnostic_scope,
            scope_source=diagnostic_scope_source,
            scope_error=diagnostic_scope_error,
            topic_limit=diagnostic_topic_limit,
            capture_complete=capture_complete,
            capture_status=capture_status,
        )

    def build_progress_snapshot() -> UdmiValidationResult:
        """Build one coherent provisional view away from the socket reader."""

        with progress_state_lock:
            snapshot_parameters = _snapshot_progress_parameters(parameters)
            snapshot_entries = [
                entry
                for entry in snapshot_parameters.get("assets", [])
                if isinstance(entry, dict)
            ]
            snapshot_messages = list(latest_progress_messages.values())
            snapshot_validation_messages = list(
                latest_progress_validation_messages.values()
            )
            snapshot_wrong_topic_messages = list(
                progress_wrong_topic_messages.values()
            )
            snapshot_wrong_topic_routes = dict(progress_wrong_topic_routes)
            snapshot_message_count = progress_message_count

        # Wrong-topic and unexpected-publisher aggregation grows with both the
        # register and captured-topic sets. Keep it on the coalesced reporter,
        # never on the MQTT reader thread.
        _record_wrong_topic_assets(
            snapshot_parameters,
            snapshot_entries,
            expected_publisher_roots,
            per_entry_validation_topics,
            registered_asset_routes,
            snapshot_wrong_topic_messages,
            topic_matches_validation_filter,
        )
        _measure_unexpected_publishers(
            snapshot_parameters,
            snapshot_messages,
            expected_publisher_roots,
            measurement_scope,
            registered_wrong_topic_routes=snapshot_wrong_topic_routes,
            measured=False,
        )
        return _build_progressive_result(
            snapshot_parameters,
            snapshot_validation_messages,
            capture_mode=capture_mode,
            capture_window_seconds=timeout_seconds,
            subscribed_topics=topics,
            message_count=snapshot_message_count,
        )

    def on_message(message: MqttMessage) -> None:
        nonlocal progress_message_count
        if not matches_normal_capture_scope(message):
            # The transport already offered this broker delivery to the
            # topic-only ``on_observed_message`` ledger. Do not let a message
            # delivered solely by the diagnostic ``#`` subscription affect an
            # in-progress validation projection.
            return
        with progress_state_lock:
            latest_progress_messages[message.topic] = message
            matching_entries = matching_validation_entries(message.topic)
            wrong_topic_entry = None
            if not matching_entries:
                wrong_topic_entry = _registered_wrong_topic_entry_index(
                    message.topic,
                    expected_publisher_roots,
                    registered_asset_routes,
                    topic_matches_validation_filter,
                )
            if wrong_topic_entry is not None:
                progress_wrong_topic_messages[message.topic] = message
                progress_wrong_topic_routes[message.topic] = wrong_topic_entry
            is_registered_validation_message = bool(matching_entries) or (
                wrong_topic_entry is not None
            )
            if is_registered_validation_message:
                progress_message_count += 1
                latest_progress_validation_messages[message.topic] = message
            if wrong_topic_entry is not None:
                matching_entries.add(wrong_topic_entry)
            observed_at = message.received_at.isoformat()
            # Update only entries that can receive this topic. Re-routing the
            # full capture through every asset for each broker message makes a
            # chatty expected topic quadratic in register size and topic count.
            for entry_index in matching_entries:
                entry_messages = latest_progress_messages_by_entry[entry_index]
                entry_messages[message.topic] = message
                routed_messages = list(entry_messages.values())
                entry = entries[entry_index]
                entry["capture_observed_at"] = observed_at
                entry["subscribed_topics"] = list(per_entry_topics[entry_index])
                expected = _dict_or_empty(entry.get("expected_schedule"))
                entry["messages"] = [
                    _serialise_capture_message(
                        captured,
                        asset_id=str(expected.get("asset_id") or "") or None,
                    )
                    for captured in routed_messages
                ]
                entry["observed_topics"] = list(
                    dict.fromkeys(captured.topic for captured in routed_messages)
                )
                _route_latest_payloads(entry, routed_messages)
        if progress_callback is not None:
            progress_callback(build_progress_snapshot)

    try:
        expected_complete = _capture_stop_when(groups)
        # Finite validation is a measurement window, never an early-success
        # check. Expected-topic completeness remains useful for indefinite
        # operator captures only.
        stop_when = expected_complete if capture_mode == "indefinite" else (lambda _messages: False)
        capture_options: dict[str, object] = {
            "topics": topics,
            # timeout_seconds stays None in the summary (capture_mode "indefinite");
            # the transport gets the 48h backstop so an indefinite capture still
            # ends with its data rather than hanging on a never-publishing device.
            "timeout_seconds": (
                timeout_seconds
                if timeout_seconds is not None
                else INDEFINITE_BACKSTOP_SECONDS
            ),
            "max_messages": max_messages,
            "qos": parse_int(parameters.get("qos"), default=0),
            "cancel_check": (
                capture_cancel_check if cancel_check is not None else None
            ),
            "stop_when": stop_when,
            "on_message": on_message,
        }
        if diagnostic_enabled:
            # This hook is invoked before observational secondary-topic retention
            # drops a new topic. It retains only exact registered asset-ID topic
            # metadata in its own small per-asset ledger.
            capture_options["on_observed_message"] = on_observed_message
        if validation_topics:
            # Reserve the configured cap for the concrete expected payload
            # filters. Register wildcards and other observation-only traffic
            # get a separate bounded budget, so neither sibling publishers nor
            # unrelated topics beneath an expected root can consume a slot.
            capture_options["primary_topics"] = validation_topics
            capture_options["secondary_max_messages"] = unexpected_max_messages
            # Make the separate retention budgets part of the validation
            # contract instead of relying on the transport's implementation
            # defaults.  Expected payload evidence is deliberately much larger
            # than the bounded discovery lane.
            capture_options["primary_max_bytes"] = DEFAULT_PRIMARY_RETAINED_BYTES
            capture_options["secondary_max_bytes"] = DEFAULT_SECONDARY_RETAINED_BYTES
            if diagnostic_scope == "#":
                # Prefer the bounded measurement lane when one exists. With no
                # common bounded scope, the operator's explicit all-topic
                # confirmation must retain ``#`` in the independent secondary
                # lane; an empty filter list would discard every diagnostic.
                capture_options["secondary_topic_filters"] = (
                    normal_secondary_topic_filters or ["#"]
                )
        capture_result = live_capture(
            build_mqtt_connection_settings(parameters),
            **capture_options,
        )
    except MqttCaptureInterrupted as error:
        messages = error.messages
        capture_result = messages
        capture_error_status = _broker_error_status(error.cause)
    except (MqttTransportError, OSError, ValueError) as error:
        # Coarse status label only — raw broker error text may carry credentials.
        broker_status_detail = _broker_error_status(error)
        persist_asset_topic_discovery(
            capture_complete=False,
            capture_status=broker_status_detail,
        )
        return {
            "attempted": True,
            "status_detail": broker_status_detail,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "window_completed": False,
            "termination_reason": "broker_interruption",
            "captured_topics": [],
            "issue": _capture_error_issue(asset_id=None, status_detail=broker_status_detail),
        }

    messages, transport_termination, transport_window_completed, transport_telemetry = _capture_result_details(
        capture_result,
        finite_window=timeout_seconds is not None,
    )
    if transport_termination == "broker_interruption" and isinstance(capture_result, MqttCaptureOutcome):
        capture_error_status = _broker_error_status(
            capture_result.interruption_cause or MqttTransportError("MQTT capture interrupted.")
        )

    # Fakes and third-party transport adapters predating ``on_observed_message``
    # may return evidence without invoking the hook. Preserve deterministic
    # diagnostic results for those adapters without double-counting the real
    # transport, which always invokes it before returning.
    if diagnostic_enabled and diagnostic_callback_count == 0:
        for message in messages:
            on_observed_message(message)

    # Third-party/fake capture adapters may not yet enforce
    # ``secondary_topic_filters``. Preserve the same isolation at the terminal
    # result boundary: broad diagnostic-only traffic belongs in the topic ledger
    # and nowhere in the ordinary validation evidence path.
    normal_messages = [
        message for message in messages if matches_normal_capture_scope(message)
    ]

    capture_observed_at = datetime.now(UTC).isoformat()

    with progress_state_lock:
        expected_messages = [
            message
            for message in normal_messages
            if matches_validation_topic(message)
        ]
        wrong_topic_routes = _record_wrong_topic_assets(
            parameters,
            entries,
            expected_publisher_roots,
            per_entry_validation_topics,
            registered_asset_routes,
            normal_messages,
            topic_matches_validation_filter,
        )
        validation_messages = [
            message
            for message in normal_messages
            if matches_validation_topic(message) or message.topic in wrong_topic_routes
        ]
        expected_topic_count = len({message.topic for message in expected_messages})
        observation_topic_count = len(
            {
                message.topic
                for message in normal_messages
                if not matches_validation_topic(message)
            }
        )

        # Route every message back to each entry whose subscribed topics match
        # it, mirroring single-asset routing (latest payload per slot wins).
        _route_capture_messages_to_assets(
            entries,
            per_entry_topics,
            per_entry_validation_topics,
            normal_messages,
            observed_at=capture_observed_at,
            wrong_topic_routes=wrong_topic_routes,
        )
        _measure_unexpected_publishers(
            parameters,
            normal_messages,
            expected_publisher_roots,
            measurement_scope,
            registered_wrong_topic_routes=wrong_topic_routes,
            measured=(
                capture_error_status is None
                and not capture_cancelled
                and transport_window_completed
                and expected_topic_count < max_messages
                and observation_topic_count < unexpected_max_messages
                and not transport_telemetry.get("primary_cap_reached", False)
                and not transport_telemetry.get("primary_byte_cap_reached", False)
                and not transport_telemetry.get("secondary_truncated", False)
            ),
        )

    diagnostic_capture_complete = (
        capture_error_status is None
        and not capture_cancelled
        and transport_window_completed
        and not transport_telemetry.get("primary_cap_reached", False)
        and not transport_telemetry.get("primary_byte_cap_reached", False)
        and not transport_telemetry.get("secondary_truncated", False)
    )
    diagnostic_capture_status = (
        capture_error_status
        or ("cancelled" if capture_cancelled else None)
        # A topic ledger may contain useful observations from an old list-only
        # adapter or from an indefinite transport backstop, but neither result
        # proves that the requested measurement window completed.  Do not mark
        # the diagnostic capture as ``completed`` merely because it avoided a
        # retention cap.
        or (None if transport_window_completed else transport_termination)
        or (
            "primary_topic_limit_reached"
            if transport_telemetry.get("primary_cap_reached", False)
            else (
                "primary_byte_limit_reached"
                if transport_telemetry.get("primary_byte_cap_reached", False)
                else (
                    "secondary_byte_limit_reached"
                    if transport_telemetry.get("secondary_byte_truncated", False)
                    else (
                        "secondary_topic_limit_reached"
                        if transport_telemetry.get("secondary_count_truncated", False)
                        else "completed"
                    )
                )
            )
        )
    )
    # Measurement-only wildcard traffic must never become a validation issue.
    # It is retained solely for the separate unexpected-device summary above.
    valid_messages = _valid_payload_messages(validation_messages)
    valid_topics = _ordered_valid_payload_topics(validation_messages)
    missing = _unseen_groups(groups, {message.topic for message in valid_messages})
    if capture_error_status:
        termination_reason = "broker_interruption"
    elif capture_cancelled:
        termination_reason = "cancelled"
    else:
        termination_reason = transport_termination
    if timeout_seconds is None and termination_reason == "required_topics_received" and missing:
        termination_reason = "backstop_elapsed"
    window_completed = transport_window_completed and not capture_cancelled and capture_error_status is None
    persist_asset_topic_discovery(
        capture_complete=diagnostic_capture_complete,
        capture_status=diagnostic_capture_status,
    )

    if capture_error_status:
        return {
            "attempted": True,
            "status_detail": capture_error_status,
            "capture_mode": capture_mode,
            "capture_window_seconds": timeout_seconds,
            "window_completed": False,
            "termination_reason": termination_reason,
            "captured_topics": valid_topics,
            "subscribed_topics": list(topics),
            "issue": _capture_error_issue(asset_id=None, status_detail=capture_error_status),
        }
    return {
        "attempted": True,
        "status_detail": (
            "live_payloads_captured" if valid_messages and not missing else "live_capture_timeout"
        ),
        "capture_mode": capture_mode,
        "capture_window_seconds": timeout_seconds,
        "window_completed": window_completed,
        "termination_reason": termination_reason,
        "capture_retention": transport_telemetry,
        "captured_topics": valid_topics,
        "subscribed_topics": list(topics),
        "issue": None
        if valid_messages and not missing
        else (
            _invalid_payload_issue(
                asset_id=None,
                messages=validation_messages,
                missing=missing,
            )
            if len(valid_messages) != len(validation_messages)
            else _missing_topics_issue(
                asset_id=None,
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


def _capture_validation_topics(
    parameters: dict[str, object],
    subscribed_topics: list[str],
    publisher_root: str | None,
) -> list[str]:
    """Payload filters that may affect validation for one register asset.

    ``register_topic_filter`` remains in the broker subscription for evidence,
    but it is deliberately excluded here because a broad parent wildcard can
    also match siblings or unrelated device topics. The backend normally
    derives concrete payload siblings from every register wildcard. The root
    fallback keeps hand-built inputs safe when those concrete fields are absent.
    """
    candidates_by_type = {
        "state": _string(parameters.get("state_topic")),
        "metadata": _string(parameters.get("metadata_topic")),
        "pointset": _string(parameters.get("pointset_topic")),
    }
    expected = _dict_or_empty(parameters.get("expected_schedule"))
    # The schedule is the merged per-asset contract. Its ordered effective
    # types must win over a first register row's top-level convenience fields.
    raw_payload_types = expected.get("payload_types")
    applicability_status = _string(expected.get("payload_applicability_status")).casefold()
    if not isinstance(raw_payload_types, list):
        raw_payload_types = parameters.get("payload_types")
        if not applicability_status:
            applicability_status = _string(
                parameters.get("payload_applicability_status")
            ).casefold()
    if applicability_status in {"unresolved", "invalid"}:
        # The register may still be subscribed broadly for evidence, but no
        # facet is an expected validation topic until the external matrix is
        # approved.
        return []
    elif isinstance(raw_payload_types, list):
        allowed = {
            str(payload_type).strip().casefold()
            for payload_type in raw_payload_types
            if str(payload_type).strip().casefold() in candidates_by_type
        }
        candidates_by_type = {
            payload_type: topic
            for payload_type, topic in candidates_by_type.items()
            if payload_type in allowed
        }
    candidates = list(candidates_by_type.values())
    extra = parameters.get("extra_capture_topics")
    if isinstance(extra, list):
        candidates.extend(_string(topic) for topic in extra)
    validation_topics = [
        topic for topic in candidates if topic and _payload_key_for_topic(topic)
    ]
    if not validation_topics and publisher_root and applicability_status not in {"unresolved", "invalid"}:
        validation_topics = [
            f"{publisher_root}/state",
            f"{publisher_root}/metadata",
            f"{publisher_root}/events/pointset",
            f"{publisher_root}/event/pointset",
        ]
    if not validation_topics:
        validation_topics = list(subscribed_topics)
    return list(dict.fromkeys(validation_topics))


def _entry_publisher_root(entry: dict, entry_topics: list[str]) -> str | None:
    """One literal publisher root for an expected asset, when safely derivable."""
    primary_topics = [
        _string(entry.get("state_topic")),
        _string(entry.get("metadata_topic")),
        _string(entry.get("pointset_topic")),
    ]
    extra = entry.get("extra_capture_topics")
    if isinstance(extra, list):
        primary_topics.extend(_string(topic) for topic in extra)
    candidates = [
        root
        for topic in primary_topics
        if topic
        if (root := _publisher_root_from_filter(topic)) is not None
    ]
    if not candidates:
        register_filter = _string(entry.get("register_topic_filter"))
        if register_filter:
            root = _publisher_root_from_filter(register_filter)
            return root
        candidates = [
            root
            for topic in entry_topics
            if (root := _publisher_root_from_filter(topic)) is not None
        ]
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def _publisher_root_from_filter(topic_filter: str) -> str | None:
    """Literal device root from one expected topic/filter, rejecting ambiguity."""
    text = topic_filter.strip().strip("/")
    if not text:
        return None
    wildcard_root = text.endswith("/#")
    if wildcard_root:
        text = text[:-2]
    elif "+" in text or "#" in text:
        return None
    if not wildcard_root:
        for suffix in ("/events/pointset", "/event/pointset", "/metadata", "/state", "/pointset"):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                break
        else:
            if "/" not in text:
                return None
            text = text.rsplit("/", 1)[0]
    parts = text.split("/")
    if not parts or any(not part or part in {"+", "#"} for part in parts):
        return None
    return "/".join(parts)


def _topic_within_publisher_root(topic: str, publisher_root: str) -> bool:
    """Whether a concrete broker topic belongs to one literal publisher root."""
    concrete = topic.strip().strip("/")
    root = publisher_root.strip().strip("/")
    return bool(root and (concrete == root or concrete.startswith(f"{root}/")))


def _unexpected_measurement_scope(expected_roots: list[str | None]) -> str | None:
    """A bounded common-ancestor filter, never an unbounded bare ``#``.

    The strict common parent can only see sibling publishers in the same
    register branch. Step up one additional parent when that remains bounded,
    so a registered asset moved between adjacent site/floor branches can still
    be identified by its exact MQTT topic segment.
    """
    if not expected_roots or any(root is None for root in expected_roots):
        return None
    roots = [str(root) for root in expected_roots]
    split_roots = [root.split("/") for root in roots]
    common: list[str] = []
    for parts in zip(*split_roots, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    # The scope must be a PARENT of every expected publisher. When all roots
    # are identical (including the one-asset case), step up one level to make
    # sibling discovery possible.
    if common and any(len(common) >= len(parts) for parts in split_roots):
        common.pop()
    if len(common) > 1:
        common.pop()
    if not common or any(part in {"", "+", "#"} for part in common):
        return None
    return "/".join(common) + "/#"


def _registered_asset_routes(entries: list[dict]) -> dict[str, int]:
    """Map only unique, non-blank register asset IDs to their entry index."""
    indexes: dict[str, list[int]] = defaultdict(list)
    for entry_index, entry in enumerate(entries):
        expected = _dict_or_empty(entry.get("expected_schedule"))
        asset_id = str(expected.get("asset_id") or "").strip()
        if asset_id:
            indexes[asset_id].append(entry_index)
    return {
        asset_id: entry_indexes[0]
        for asset_id, entry_indexes in indexes.items()
        if len(entry_indexes) == 1
    }


def _registered_asset_entry_index_from_topic(
    topic: str,
    registered_asset_routes: dict[str, int],
) -> int | None:
    """Return one exact, case-sensitive registered asset-ID segment, if safe.

    This is intentionally stricter than a substring search. ``AHU-1-copy`` is
    not ``AHU-1``; a topic naming two registered asset IDs is ambiguous; and a
    duplicate register ID has already been removed from ``registered_asset_routes``.
    Those cases remain unclassified instead of attaching broker traffic to the
    wrong asset.
    """

    entry_indexes = {
        registered_asset_routes[segment]
        for segment in topic.strip().strip("/").split("/")
        if segment in registered_asset_routes
    }
    return next(iter(entry_indexes)) if len(entry_indexes) == 1 else None


def _asset_topic_discovery_scope(
    parameters: dict[str, object],
    measurement_scope: str | None,
) -> tuple[str | None, str, str | None]:
    """Resolve the opt-in topic-trace scope without ever widening by default.

    The normal diagnostic lane observes only the existing bounded common
    ancestor (``hv/#`` for the field register). A bare ``#`` is possible only
    through an explicit request plus confirmation. The API rejects that request
    early; retaining this defensive resolver keeps direct core callers fail-safe.
    """

    requested = _string(parameters.get("topic_discovery_scope")).casefold()
    if requested in {"", "bounded"}:
        if measurement_scope is None:
            return None, "unavailable", None
        return measurement_scope, "register_common_ancestor", None
    if requested == "all":
        if parse_bool(parameters.get("topic_discovery_all_scope_confirmed")):
            return "#", "all", None
        return None, "all", "all_scope_confirmation_required"
    return None, "invalid", "invalid_scope"


def _record_asset_topic_discovery_message(
    state: _AssetTopicDiscoveryState,
    message: MqttMessage,
    *,
    is_expected_topic: bool,
    topic_limit: int,
) -> None:
    """Keep bounded, payload-free evidence for one asset-ID topic match.

    Expected-topic evidence wins over alternate-topic evidence when the cap is
    full. The status flags still record that an unretained alternate topic was
    seen, while ``topic_limit_reached`` prevents callers from treating the
    retained lists as a complete broker inventory.
    """

    state.matched_message_count += 1
    if is_expected_topic:
        state.expected_seen = True
        bucket = state.expected_topics
    else:
        state.alternate_seen = True
        bucket = state.alternate_topics

    existing = bucket.get(message.topic)
    if existing is not None:
        existing["message_count"] = int(existing["message_count"]) + 1
        existing["last_seen"] = message.received_at.isoformat()
        return

    retained_count = len(state.expected_topics) + len(state.alternate_topics)
    if retained_count >= topic_limit:
        if is_expected_topic and state.alternate_topics:
            # Keep concrete evidence that an expected payload topic arrived;
            # alternate discoveries can be unbounded below a broad wildcard.
            # Dictionaries preserve insertion order, so this is the oldest
            # retained alternate topic without adding a per-message sort to the
            # MQTT reader path.
            evicted = next(iter(state.alternate_topics))
            del state.alternate_topics[evicted]
        else:
            state.topic_limit_reached = True
            return
        state.topic_limit_reached = True

    bucket[message.topic] = {
        "topic": message.topic,
        "message_count": 1,
        # This is the local receive clock, not an MQTT publish timestamp.
        "last_seen": message.received_at.isoformat(),
    }


def _asset_topic_evidence_rows(
    topics: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    return [dict(topics[topic]) for topic in sorted(topics)]


def _build_asset_topic_discovery(
    entries: list[dict],
    per_entry_validation_topics: list[list[str]],
    expected_publisher_roots: list[str | None],
    registered_asset_routes: dict[str, int],
    states: list[_AssetTopicDiscoveryState],
    *,
    scope: str | None,
    scope_source: str,
    scope_error: str | None,
    topic_limit: int,
    capture_complete: bool,
    capture_status: str,
) -> dict[str, object]:
    """Build one stable, topic-only discovery ledger for the validation export."""

    uniquely_routable_entries = set(registered_asset_routes.values())
    rows: list[dict[str, object]] = []
    for entry_index, entry in enumerate(entries):
        expected = _dict_or_empty(entry.get("expected_schedule"))
        asset_id = str(expected.get("asset_id") or "").strip()
        state = states[entry_index]
        if not asset_id:
            status = "missing_asset_id"
        elif entry_index not in uniquely_routable_entries:
            status = "ambiguous_asset_id"
        elif scope_error:
            status = "scope_configuration_error"
        elif scope is None:
            status = "scope_unavailable"
        elif state.expected_seen:
            status = "expected_topic_observed"
        elif state.alternate_seen:
            status = "alternate_topic_observed"
        elif capture_complete:
            # This deliberately says only what this capture observed. It is not
            # evidence that the physical device is absent or offline.
            status = "no_matching_asset_id_topic_observed"
        else:
            status = "capture_incomplete"
        rows.append(
            {
                "asset_id": asset_id,
                "system": str(expected.get("system") or "").strip() or "Unspecified",
                "expected_topic_root": str(expected_publisher_roots[entry_index] or ""),
                "expected_topics": list(per_entry_validation_topics[entry_index]),
                "observed_expected_topics": _asset_topic_evidence_rows(
                    state.expected_topics
                ),
                "observed_alternate_topics": _asset_topic_evidence_rows(
                    state.alternate_topics
                ),
                "matched_message_count": state.matched_message_count,
                "topic_limit_reached": state.topic_limit_reached,
                "status": status,
            }
        )

    rows.sort(key=lambda row: (str(row["asset_id"]).casefold(), str(row["asset_id"])))
    status_counts = {
        status: sum(1 for row in rows if row["status"] == status)
        for status in (
            "expected_topic_observed",
            "alternate_topic_observed",
            "no_matching_asset_id_topic_observed",
            "capture_incomplete",
            "ambiguous_asset_id",
            "missing_asset_id",
            "scope_unavailable",
            "scope_configuration_error",
        )
    }
    return {
        "enabled": True,
        "scope": scope,
        "scope_source": scope_source,
        "scope_error": scope_error,
        "topic_limit_per_asset": topic_limit,
        "capture_complete": capture_complete,
        "capture_status": capture_status,
        "asset_results": rows,
        "status_counts": status_counts,
    }


def _registered_wrong_topic_entry_index(
    topic: str,
    expected_roots: list[str | None],
    registered_asset_routes: dict[str, int],
    validation_topic_matcher: Callable[[str], bool],
) -> int | None:
    """Identify one registered asset on a different literal publisher root.

    MQTT segments and register IDs are compared exactly and case-sensitively.
    A topic naming no registered ID, more than one registered ID, or a duplicate
    register ID is left as observational traffic rather than guessed.
    """
    if _payload_key_for_topic(topic) is None:
        return None
    if validation_topic_matcher(topic):
        return None
    entry_index = _registered_asset_entry_index_from_topic(topic, registered_asset_routes)
    if entry_index is None:
        return None
    expected_root = expected_roots[entry_index]
    if not expected_root:
        return None
    actual_root = _publisher_root_from_message_topic(topic)
    if actual_root == expected_root:
        return None
    return entry_index


def _expected_topic_for_payload(
    entry: dict,
    validation_topics: list[str],
    payload_type: str,
) -> str:
    direct = _string(entry.get(f"{payload_type}_topic"))
    if direct:
        return direct
    payload_key = f"{payload_type}_payload"
    return next(
        (
            topic
            for topic in validation_topics
            if _payload_key_for_topic(topic) == payload_key
        ),
        "",
    )


def _record_wrong_topic_assets(
    parameters: dict[str, object],
    entries: list[dict],
    expected_roots: list[str | None],
    per_entry_validation_topics: list[list[str]],
    registered_asset_routes: dict[str, int],
    messages: list[MqttMessage],
    validation_topic_matcher: Callable[[str], bool],
) -> dict[str, int]:
    """Persist one deterministic expected-vs-actual topic row per asset."""
    routes: dict[str, int] = {}
    # Keep the latest message for every distinct actual topic. One registered
    # asset can publish the same payload type below several wrong roots during a
    # capture; collapsing by payload type alone loses evidence needed by the
    # annotated register and wrong-topic report detail.
    latest_by_topic: dict[tuple[int, str, str], MqttMessage] = {}
    messages_by_entry: dict[int, list[MqttMessage]] = defaultdict(list)
    for message in messages:
        entry_index = _registered_wrong_topic_entry_index(
            message.topic,
            expected_roots,
            registered_asset_routes,
            validation_topic_matcher,
        )
        if entry_index is None:
            continue
        payload_key = _payload_key_for_topic(message.topic)
        if payload_key is None:
            continue
        payload_type = payload_key.removesuffix("_payload")
        routes[message.topic] = entry_index
        messages_by_entry[entry_index].append(message)
        key = (entry_index, payload_type, message.topic)
        current = latest_by_topic.get(key)
        if current is None or (message.received_at, message.topic) >= (
            current.received_at,
            current.topic,
        ):
            latest_by_topic[key] = message

    payload_order = {"state": 0, "metadata": 1, "pointset": 2}
    rows: list[dict[str, object]] = []
    for entry_index, entry_messages in messages_by_entry.items():
        entry = entries[entry_index]
        expected = _dict_or_empty(entry.get("expected_schedule"))
        latest = max(
            entry_messages,
            key=lambda message: (message.received_at, message.topic),
        )
        payloads: list[dict[str, str]] = []
        for (payload_entry_index, payload_type, _actual_topic), message in sorted(
            latest_by_topic.items(),
            key=lambda item: (
                item[0][0],
                payload_order.get(item[0][1], 99),
                item[0][2],
            ),
        ):
            if payload_entry_index != entry_index:
                continue
            payloads.append(
                {
                    "payload_type": payload_type,
                    "expected_topic": _expected_topic_for_payload(
                        entry,
                        per_entry_validation_topics[entry_index],
                        payload_type,
                    ),
                    "actual_topic": message.topic,
                }
            )
        rows.append(
            {
                "asset_id": str(expected.get("asset_id") or "").strip(),
                "system": str(expected.get("system") or "").strip()
                or "Unspecified",
                "expected_topic_root": str(expected_roots[entry_index] or ""),
                "actual_topic_root": _publisher_root_from_message_topic(
                    latest.topic
                ),
                "payloads": payloads,
                "last_seen": latest.received_at.isoformat(),
            }
        )
    rows.sort(
        key=lambda row: (
            str(row["asset_id"]).casefold(),
            str(row["expected_topic_root"]),
            str(row["actual_topic_root"]),
        )
    )
    parameters["wrong_topic_assets"] = rows
    parameters["wrong_topic_asset_count"] = len(rows)
    return routes


def _measure_unexpected_publishers(
    parameters: dict[str, object],
    messages: list[MqttMessage],
    expected_roots: list[str | None],
    measurement_scope: str | None,
    *,
    registered_wrong_topic_routes: dict[str, int] | None = None,
    measured: bool,
) -> None:
    """Persist a deterministic, deduplicated inventory of unexpected siblings."""
    parameters["unexpected_devices_measured"] = bool(measurement_scope and measured)
    parameters["unexpected_devices_measurement_scope"] = measurement_scope
    if not measurement_scope:
        parameters["unexpected_devices"] = []
        return
    literal_expected_roots = [root for root in expected_roots if root]
    registered_wrong_topic_routes = registered_wrong_topic_routes or {}
    registered_wrong_topic_roots = {
        _publisher_root_from_message_topic(topic)
        for topic in registered_wrong_topic_routes
    }
    grouped: dict[str, list[MqttMessage]] = defaultdict(list)
    for message in messages:
        if not _topic_matches_filter(message.topic, measurement_scope):
            continue
        if any(_topic_belongs_to_root(message.topic, root) for root in literal_expected_roots):
            continue
        if any(
            _topic_belongs_to_root(message.topic, root)
            for root in registered_wrong_topic_roots
        ):
            continue
        publisher_root = _publisher_root_from_message_topic(message.topic)
        grouped[publisher_root].append(message)

    unexpected: list[dict[str, object]] = []
    for topic_root in sorted(grouped):
        publisher_messages = grouped[topic_root]
        latest = max(publisher_messages, key=lambda message: message.received_at.timestamp())
        unexpected.append(
            {
                "id": "unexpected-"
                + hashlib.sha256(topic_root.encode("utf-8")).hexdigest()[:12],
                "topic_root": topic_root,
                "topics": sorted({message.topic for message in publisher_messages}),
                "last_seen": latest.received_at.isoformat(),
            }
        )
    parameters["unexpected_devices"] = unexpected


def _topic_belongs_to_root(topic: str, root: str) -> bool:
    return topic == root or topic.startswith(root + "/")


def _publisher_root_from_message_topic(topic: str) -> str:
    text = topic.strip().strip("/")
    for marker in ("/events/", "/event/"):
        if marker in text:
            return text.split(marker, 1)[0]
    for suffix in ("/metadata", "/state", "/pointset", "/config"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text.rsplit("/", 1)[0] if "/" in text else text


def _unexpected_devices(parameters: dict[str, object]) -> list[dict[str, object]]:
    value = parameters.get("unexpected_devices")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _wrong_topic_assets(parameters: dict[str, object]) -> list[dict[str, object]]:
    value = parameters.get("wrong_topic_assets")
    if not isinstance(value, list):
        return []
    payload_order = {"state": 0, "metadata": 1, "pointset": 2}
    rows: dict[str, dict[str, object]] = {}
    payloads_by_asset: dict[
        str, dict[tuple[str, str, str], dict[str, str]]
    ] = defaultdict(dict)
    for item in value:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id:
            continue
        raw_payloads = item.get("payloads")
        for payload in raw_payloads if isinstance(raw_payloads, list) else []:
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("payload_type") or "").strip()
            expected_topic = str(payload.get("expected_topic") or "").strip()
            actual_topic = str(payload.get("actual_topic") or "").strip()
            if payload_type not in _PAYLOAD_ISSUE_TYPES or not actual_topic:
                continue
            key = (payload_type, expected_topic, actual_topic)
            payloads_by_asset[asset_id][key] = {
                "payload_type": payload_type,
                "expected_topic": expected_topic,
                "actual_topic": actual_topic,
            }
        row: dict[str, object] = {
            "asset_id": asset_id,
            "system": str(item.get("system") or "").strip() or "Unspecified",
            "expected_topic_root": str(
                item.get("expected_topic_root") or ""
            ).strip(),
            "actual_topic_root": str(
                item.get("actual_topic_root") or ""
            ).strip(),
            "last_seen": str(item.get("last_seen") or "").strip() or None,
        }
        existing = rows.get(asset_id)
        last_seen = str(row["last_seen"] or "")
        row_rank = (
            *_parse_timestamp_sort_key(last_seen),
            last_seen,
            str(row["system"]),
            str(row["expected_topic_root"]),
            str(row["actual_topic_root"]),
        )
        if existing is None:
            existing_rank = None
        else:
            existing_last_seen = str(existing["last_seen"] or "")
            existing_rank = (
                *_parse_timestamp_sort_key(existing_last_seen),
                existing_last_seen,
                str(existing["system"]),
                str(existing["expected_topic_root"]),
                str(existing["actual_topic_root"]),
            )
        if existing_rank is None or row_rank > existing_rank:
            rows[asset_id] = row
    for asset_id, row in rows.items():
        row["payloads"] = sorted(
            payloads_by_asset[asset_id].values(),
            key=lambda payload: (
                payload_order.get(payload["payload_type"], 99),
                payload["actual_topic"],
                payload["expected_topic"],
            ),
        )
    return [
        rows[asset_id]
        for asset_id in sorted(rows, key=lambda item: (item.casefold(), item))
    ]


def _wrong_topic_issues(
    parameters: dict[str, object],
    existing_issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Turn registered topic mismatches into separate blocking topic faults."""
    issues = [*existing_issues]
    first_new_issue = len(issues)
    for row in _wrong_topic_assets(parameters):
        asset_id = str(row["asset_id"] or "UDMI asset")
        payloads = row.get("payloads")
        topic_evidence: dict[str, dict[str, set[str]]] = {}
        for payload in payloads if isinstance(payloads, list) else []:
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("payload_type") or "")
            expected_topic = str(payload.get("expected_topic") or "")
            actual_topic = str(payload.get("actual_topic") or "")
            if (
                payload_type not in _PAYLOAD_ISSUE_TYPES
                or not actual_topic
                or actual_topic == expected_topic
            ):
                continue
            bucket = topic_evidence.setdefault(
                payload_type,
                {"expected_topics": set(), "actual_topics": set()},
            )
            if expected_topic:
                bucket["expected_topics"].add(expected_topic)
            bucket["actual_topics"].add(actual_topic)

        # One deterministic finding per affected payload type, while the
        # supporting wrong-topic collection keeps every distinct actual topic.
        for payload_type in _PAYLOAD_ISSUE_TYPES:
            evidence = topic_evidence.get(payload_type)
            if evidence is None:
                continue
            expected_topics = sorted(evidence["expected_topics"])
            actual_topics = sorted(evidence["actual_topics"])
            expected_value = ", ".join(expected_topics)
            observed_value = ", ".join(actual_topics)
            if len(actual_topics) == 1:
                description = (
                    f"Registered asset {asset_id} published its {payload_type} "
                    f"payload on {observed_value}; the register expects "
                    f"{expected_value or 'a different topic'}."
                )
            else:
                description = (
                    f"Registered asset {asset_id} published its {payload_type} "
                    f"payload on these topics: {observed_value}; the register expects "
                    f"{expected_value or 'a different topic'}."
                )
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="topic_mismatch",
                    severity="high",
                    description=description,
                    topic=actual_topics[0] if len(actual_topics) == 1 else None,
                    expected_value=expected_value or None,
                    observed_value=observed_value,
                    match_basis=payload_type,
                    suggested_action=(
                        "Correct the publisher topic or update the register after "
                        "confirming the intended device path."
                    ),
                )
            )
    return issues[first_new_issue:]


def _payload_key_for_topic(topic: str) -> str | None:
    normalised = str(topic or "").casefold().rstrip("/")
    if normalised.endswith("/state"):
        return "state_payload"
    if normalised.endswith("/metadata"):
        return "metadata_payload"
    if normalised.endswith("/pointset"):
        return "pointset_payload"
    return None


def _uses_direct_payload_inputs(parameters: dict[str, object]) -> bool:
    return any(
        key in parameters
        for key in ("expected_schedule", "assets", "state_payload", "metadata_payload", "pointset_payload", "messages")
    ) and not (parameters.get("full_report_path") or parameters.get("fixture_path"))


def _inline_full_report(parameters: dict[str, object]) -> dict[str, object]:
    device_list: list[str] = []
    not_publishing: list[str] = []
    capture_details: dict[str, str] = {}
    report: dict[str, object] = {
        "DeviceList": device_list,
        "DevicesNotPublishing": not_publishing,
        "DevicesNotExpected": {},
        "DevicePayloadErrors": {},
        "DevicePointsetErrors": {},
        "DevicesStateErrors": {},
        "DevicesPointsetValid": [],
        "DevicesStateValid": [],
        # asset_id -> one-line capture diagnostic for a not-publishing asset
        # (subscribed topics vs what actually arrived), appended to its issue.
        "DeviceCaptureDetails": capture_details,
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
            device_list.append(asset_id)
            # An empty JSON object is still an observed payload (albeit one that
            # will fail structural validation), so presence cannot use bool({}).
            has_payload = any(
                key in entry and isinstance(entry.get(key), (dict, str))
                for key in ("state_payload", "metadata_payload", "pointset_payload")
            )
            if capture_attempted and not has_payload:
                not_publishing.append(asset_id)
                detail = _asset_capture_detail(entry)
                if detail:
                    capture_details[asset_id] = detail
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
    validation_observed = list(
        dict.fromkeys(
            str(message.get("topic"))
            for message in (raw_messages if isinstance(raw_messages, list) else [])
            if isinstance(message, dict) and message.get("topic")
        )
    )
    raw_observed_topics = entry.get("observed_topics")
    if isinstance(raw_observed_topics, list):
        observed = list(dict.fromkeys(str(topic) for topic in raw_observed_topics if str(topic)))
    else:
        # Backward-compatible fallback for persisted/imported runs produced
        # before diagnostic observations were kept separate from validation.
        observed = list(validation_observed)
    if validation_observed:
        return (
            f"Messages arrived on {', '.join(validation_observed)} but their payloads "
            "were not JSON objects."
        )
    if observed:
        return (
            f"Messages arrived on {', '.join(observed)} but none matches this asset's "
            "expected state, metadata, or pointset topic; check the register's Expected "
            "topics against the device's actual topics."
        )
    return (
        f"Nothing arrived on the subscribed topics ({', '.join(subscribed)}) during the "
        "capture window. MQTT topics are case-sensitive — compare these against the "
        "broker (e.g. MQTT Explorer) and widen the capture window for slow publishers."
    )


def _issue(
    issues: list[ValidationIssueRecord],
    *,
    asset_id: str | None,
    issue_type: str,
    severity: str,
    description: str,
    point_name: str | None = None,
    topic: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    match_basis: str | None = None,
    suggested_action: str | None = None,
    raw_evidence_uri: str | None = None,
) -> ValidationIssueRecord:
    prefix = {
        "not_publishing": "UDMI-NP",
        "unexpected_device": "UDMI-UN",
        "payload_error": "UDMI-PL",
        "pointset_validation": "UDMI-PS",
        "pointset_timestamp": "UDMI-TS",
        "state_validation": "UDMI-ST",
        "metadata_validation": "UDMI-MD",
        "payload_routing": "UDMI-RT",
        "topic_mismatch": "UDMI-TP",
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
        topic=topic,
        expected_value=expected_value,
        observed_value=observed_value,
        match_basis=match_basis,
        suggested_action=suggested_action,
        raw_evidence_uri=raw_evidence_uri,
    )


def _unique_udmi_issue_ids(
    issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Deterministically renumber every UDMI prefix in the final issue order.

    Some capture helpers are deliberately usable in isolation and therefore
    create ``...-0001`` against an empty list. Once those records are combined
    with fixture and structural findings, this final pass prevents duplicate
    primary identifiers without changing categories or issue order.
    """
    counts: dict[str, int] = defaultdict(int)
    unique: list[ValidationIssueRecord] = []
    used: set[str] = set()
    for issue in issues:
        match = re.match(r"^(UDMI-[A-Z]+)-\d+$", issue.issue_id)
        if match is None:
            candidate = issue.issue_id
            if candidate in used:
                suffix = 2
                while f"{candidate}-{suffix}" in used:
                    suffix += 1
                candidate = f"{candidate}-{suffix}"
        else:
            prefix = match.group(1)
            counts[prefix] += 1
            candidate = f"{prefix}-{counts[prefix]:04d}"
            while candidate in used:
                counts[prefix] += 1
                candidate = f"{prefix}-{counts[prefix]:04d}"
        used.add(candidate)
        unique.append(
            issue if candidate == issue.issue_id else issue.model_copy(update={"issue_id": candidate})
        )
    return unique


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


_ROOM_FIELD_PATTERN = re.compile(r"^[-_a-zA-Z0-9]+$")


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
    "room": ("Room", "system.location.room", _ROOM_FIELD_PATTERN, "UNSPECIFIED"),
    "guid": ("GUID", "system.physical_tag.asset.guid", re.compile(r"^[a-z]+://[-0-9a-zA-Z_$]+$"), "placeholder://asset"),
    "asset_id": ("Asset ID", "system.physical_tag.asset.name", re.compile(r"^[A-Z]{2,6}-[1-9][0-9]*$"), "ASSET-1"),
}


def _template_metadata_value(expected: dict[str, Any], register_key: str) -> str:
    """The register value when it can appear in canonical UDMI, else the placeholder."""
    _label, _path, pattern, placeholder = _METADATA_REGISTER_FIELDS[register_key]
    value = str(expected.get(register_key) or "")
    return value if pattern.match(value) else placeholder


def _template_location(expected: dict[str, Any]) -> dict[str, str]:
    """The template's system.location, using the register's Room as ``room``."""
    return {
        "site": _template_metadata_value(expected, "site"),
        "room": _template_metadata_value(expected, "room"),
    }


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
        notes.append(
            _issue(
                [*issues, *notes],
                asset_id=asset_id,
                issue_type="metadata_validation",
                severity="low",
                description=(
                    f"Register {label} '{value}' cannot appear in canonical UDMI metadata "
                    f"({path} must match {pattern.pattern}); the expected "
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


def _expected_payload_header(
    expected: dict[str, Any],
    *,
    template_timestamp: str | None = None,
) -> dict[str, Any]:
    """Schema-valid fields shared by display-only UDMI templates."""
    header: dict[str, Any] = {"timestamp": template_timestamp or _template_timestamp()}
    if version := expected.get("udmi_version"):
        header["version"] = version
    return header


def _expected_payload_facet(
    expected: dict[str, Any],
    payload_type: str,
    *,
    template_timestamp: str | None = None,
) -> dict[str, Any] | None:
    """UDMI-shaped display template with register constraints and explicit placeholders."""
    header = _expected_payload_header(expected, template_timestamp=template_timestamp)
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
    observed_present_by_type: dict[str, bool],
    retained_by_type: dict[str, bool],
    received_at_by_type: dict[str, str | None],
    topic_by_type: dict[str, str | None],
    *,
    template_timestamp: str,
) -> dict[str, object] | None:
    """Build ONE asset's per-payload-type expected-vs-observed view, or None.

    A payload type is omitted when neither an expected facet nor an observed
    payload exists, so nothing is fabricated.
    """
    raw_payload_types = expected.get("payload_types")
    applicability_status = _string(expected.get("payload_applicability_status")).casefold()
    if isinstance(raw_payload_types, list):
        expected_payload_types = {
            str(payload_type).strip().casefold()
            for payload_type in raw_payload_types
            if str(payload_type).strip().casefold() in {"state", "metadata", "pointset"}
        }
    elif applicability_status in {"unresolved", "invalid"}:
        expected_payload_types = set()
    else:
        # Direct fixture inputs predating the applicability contract retain
        # their legacy display shape; register-driven runs always carry the
        # explicit list/status above.
        expected_payload_types = {"state", "metadata", "pointset"}
    payload_types: list[dict[str, object]] = []
    for payload_type in ("state", "metadata", "pointset"):
        observed = observed_by_type[payload_type]
        expected_facet = (
            _expected_payload_facet(
                expected,
                payload_type,
                template_timestamp=template_timestamp,
            )
            if expected and payload_type in expected_payload_types
            else None
        )
        observed_present = observed_present_by_type[payload_type]
        if not observed_present and not expected_facet:
            continue
        payload_types.append(
            {
                "payload_type": payload_type,
                "expected": expected_facet,
                "observed": observed or None,
                "observed_present": observed_present,
                "retained": retained_by_type[payload_type],
                "received_at": received_at_by_type[payload_type],
                "topic": topic_by_type[payload_type],
            }
        )
    if not payload_types:
        return None
    asset_id = str(expected.get("asset_id") or "UDMI asset")
    system = str(expected.get("system") or "").strip() or "Unspecified"
    return {"asset_id": asset_id, "system": system, "payload_types": payload_types}


def _observed_by_type(source: dict[str, object]) -> dict[str, dict]:
    return {
        "state": _dict_or_empty(source.get("state_payload")),
        "metadata": _dict_or_empty(source.get("metadata_payload")),
        "pointset": _dict_or_empty(source.get("pointset_payload")),
    }


def _observed_present_by_type(source: dict[str, object]) -> dict[str, bool]:
    return {
        payload_type: f"{payload_type}_payload" in source
        and isinstance(source.get(f"{payload_type}_payload"), (dict, str))
        for payload_type in ("state", "metadata", "pointset")
    }


def _retained_by_type(source: dict[str, object]) -> dict[str, bool]:
    return {
        payload_type: parse_bool(source.get(f"{payload_type}_payload_retained"))
        for payload_type in ("state", "metadata", "pointset")
    }


def _received_at_by_type(source: dict[str, object]) -> dict[str, str | None]:
    return {
        payload_type: (
            str(source.get(f"{payload_type}_payload_received_at"))
            if source.get(f"{payload_type}_payload_received_at")
            else None
        )
        for payload_type in ("state", "metadata", "pointset")
    }


def _topic_by_type(source: dict[str, object]) -> dict[str, str | None]:
    return {
        payload_type: (
            str(source.get(f"{payload_type}_topic"))
            if source.get(f"{payload_type}_topic")
            else None
        )
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
    # One result snapshot gets one template-build instant. Reusing it across
    # every asset and payload type prevents the expected panels from inventing
    # seconds of drift while a large result is projected. This template value is
    # display/schema metadata only; freshness uses observed payload and receipt
    # timestamps in _pointset_freshness_issue.
    template_timestamp = _template_timestamp()
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        views: list[dict[str, object]] = []
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            view = _asset_payload_view(
                _dict_or_empty(entry.get("expected_schedule")),
                _observed_by_type(entry),
                _observed_present_by_type(entry),
                _retained_by_type(entry),
                _received_at_by_type(entry),
                _topic_by_type(entry),
                template_timestamp=template_timestamp,
            )
            if view is not None:
                views.append(view)
        return views

    view = _asset_payload_view(
        _dict_or_empty(parameters.get("expected_schedule")),
        _observed_by_type(parameters),
        _observed_present_by_type(parameters),
        _retained_by_type(parameters),
        _received_at_by_type(parameters),
        _topic_by_type(parameters),
        template_timestamp=template_timestamp,
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


def _parse_timestamp_sort_key(value: str) -> tuple[int, datetime]:
    # Valid offset-aware timestamps outrank invalid or offset-less values. A
    # naive wall-clock value has no knowable position against UTC; attaching UTC
    # would invent an instant and could falsely call it the latest payload.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (0, datetime.min.replace(tzinfo=UTC))
    if parsed.tzinfo is None:
        return (0, datetime.min.replace(tzinfo=UTC))
    return (1, parsed.astimezone(UTC))
