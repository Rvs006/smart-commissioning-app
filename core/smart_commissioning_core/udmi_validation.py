import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from smart_commissioning_core.engines.comparison_common import make_issue
from smart_commissioning_core.mqtt_settings import (
    _broker_error_status,
    _string,
    build_mqtt_connection_settings,
    parse_bool,
    parse_float,
    parse_int,
)
from smart_commissioning_core.mqtt_transport import MqttMessage, MqttTransportError, subscribe_and_capture
from smart_commissioning_core.records import ValidationIssueRecord

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


@dataclass(frozen=True)
class UdmiValidationResult:
    result_summary: dict[str, object]
    issues: list[ValidationIssueRecord]
    source_fixture: str


LiveCapture = Callable[..., list[MqttMessage]]


def validate_udmi_full_report(
    parameters: dict[str, object] | None = None,
    *,
    live_capture: LiveCapture | None = subscribe_and_capture,
) -> UdmiValidationResult:
    parameters = dict(parameters or {})
    capture_issues: list[ValidationIssueRecord] = []
    capture_summary = _capture_live_payloads(parameters, live_capture=live_capture)
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
    issues.extend(_review_all_payload_issues(parameters or {}, issues))
    expected_devices = _list_value(full_report, "DeviceList")
    not_publishing = _list_value(full_report, "DevicesNotPublishing")
    latest_payload = _latest_payload_timestamp(parameters or {})
    # Per-asset, per-payload-type expected-vs-observed view for the results UI
    # (mq9m4bnv). Built only from real payloads the validator already has
    # (pasted inputs or live capture); the fixture path carries no payload JSON,
    # so it returns [] and is labelled 'none' rather than fabricating content.
    payload_views = _build_payload_views(parameters or {})
    result_summary: dict[str, object] = {
        "expected_devices": len(expected_devices),
        "publishing_seen": max(0, len(expected_devices) - len(not_publishing)),
        "not_publishing": len(not_publishing),
        "pointset_valid": len(_list_value(full_report, "DevicesPointsetValid")),
        "state_valid": len(_list_value(full_report, "DevicesStateValid")),
        "issue_count": len(issues),
        "message_count": _message_count(parameters or {}),
        "payload_last_seen": latest_payload,
        "source": source,
        "source_fixture": source_fixture,
        "broker_capture_attempted": capture_summary["attempted"],
        "broker_status_detail": capture_summary["status_detail"],
        "captured_topics": capture_summary["captured_topics"],
        "payload_views": payload_views,
        "payload_view_source": _payload_view_source(
            parameters or {},
            capture_attempted=bool(capture_summary["attempted"]),
            has_views=bool(payload_views),
        ),
    }
    return UdmiValidationResult(
        result_summary=result_summary,
        issues=issues,
        source_fixture=source_fixture,
    )


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

    for asset_id in _list_value(full_report, "DevicesNotPublishing"):
        issues.append(
            _issue(
                issues,
                asset_id=asset_id,
                issue_type="not_publishing",
                severity="high",
                description=f"Expected device {asset_id} did not publish during the validation window.",
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
# action). manufacturer/model/serial/firmware read the STATE payload; guid/site/
# room read METADATA. Paths follow UDMI conventions (system.hardware.*,
# system.serial_no, system.software.firmware, system.physical_tag.asset.guid,
# system.location.{site,section}); confirm on a real device if one never matches.
_IDENTITY_CHECKS: tuple[tuple[str, Callable[[dict, dict], object], str, str, str, str], ...] = (
    ("manufacturer", lambda state, metadata: _nested(state, "system", "hardware", "make"),
     "state_validation", "high",
     "State payload manufacturer does not match the asset register.",
     "Confirm the manufacturer in the MSI schedule and the UDMI state payload."),
    ("model", lambda state, metadata: _nested(state, "system", "hardware", "model"),
     "state_validation", "medium",
     "State payload model does not match the asset register.",
     "Check device metadata or update the asset register if the installed model changed."),
    ("serial", lambda state, metadata: _nested(state, "system", "serial_no"),
     "state_validation", "medium",
     "State payload serial number does not match the asset register.",
     "Confirm the device serial number in the schedule and the UDMI state payload."),
    ("firmware", lambda state, metadata: _nested(state, "system", "software", "firmware"),
     "state_validation", "low",
     "State payload firmware version does not match the asset register.",
     "Confirm the expected firmware version or update the device firmware."),
    ("guid", lambda state, metadata: _nested(metadata, "system", "physical_tag", "asset", "guid"),
     "metadata_validation", "high",
     "Metadata GUID does not match the asset register.",
     "Correct the UDMI metadata asset GUID or the imported register."),
    ("site", lambda state, metadata: _nested(metadata, "system", "location", "site"),
     "metadata_validation", "low",
     "Metadata site does not match the asset register.",
     "Confirm the site in the schedule and the UDMI metadata location."),
    ("room", lambda state, metadata: _nested(metadata, "system", "location", "section"),
     "metadata_validation", "low",
     "Metadata room/section does not match the asset register.",
     "Confirm the room/section in the schedule and the UDMI metadata location."),
)


def _review_all_payload_issues(
    parameters: dict[str, object],
    existing_issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    """Fan _review_payload_issues out across a multi-asset ``assets`` list.

    When ``parameters["assets"]`` is a non-empty list, each entry carries its
    own ``expected_schedule``/``*_payload`` keys; run the single-asset reviewer
    once per entry and aggregate. The single top-level path stays back-compatible.
    """
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        issues: list[ValidationIssueRecord] = []
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            issues.extend(_review_payload_issues(entry, [*existing_issues, *issues]))
        return issues
    return _review_payload_issues(parameters, existing_issues)


def _review_payload_issues(
    parameters: dict[str, object],
    existing_issues: list[ValidationIssueRecord],
) -> list[ValidationIssueRecord]:
    expected = _dict_or_empty(parameters.get("expected_schedule"))
    if not expected:
        return []

    issues: list[ValidationIssueRecord] = []
    asset_id = str(expected.get("asset_id") or "UDMI asset")
    state_payload = _dict_or_empty(parameters.get("state_payload"))
    metadata_payload = _dict_or_empty(parameters.get("metadata_payload"))
    pointset_payload = _dict_or_empty(parameters.get("pointset_payload"))
    raw_evidence_uri = str(parameters.get("raw_evidence_uri") or "runtime://udmi-validation/review-payloads")

    # manufacturer/model/serial/firmware/guid/site/room: flag any expected value
    # that is present in both register and payload but differs (see _IDENTITY_CHECKS).
    for expected_key, observed_getter, issue_type, severity, description, action in _IDENTITY_CHECKS:
        expected_value = expected.get(expected_key)
        observed_value = observed_getter(state_payload, metadata_payload)
        if expected_value and observed_value and expected_value != observed_value:
            issues.append(
                _issue(
                    [*existing_issues, *issues],
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

    metadata_points = metadata_payload.get("pointset", {}).get("points", {}) if metadata_payload else {}
    pointset_points = pointset_payload.get("points", {}) or pointset_payload.get("pointset", {}).get("points", {})
    expected_units = _dict_or_empty(expected.get("units"))
    for point_name, expected_unit in expected_units.items():
        metadata_unit = _dict_or_empty(metadata_points.get(point_name)).get("units")
        unit_to_check = metadata_unit or expected_unit
        if unit_to_check and str(unit_to_check) not in KNOWN_UDMI_UNITS:
            issues.append(
                _issue(
                    [*existing_issues, *issues],
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
        if unit_to_check in NUMERIC_UDMI_UNITS and present_value is not None and not isinstance(present_value, int | float):
            issues.append(
                _issue(
                    [*existing_issues, *issues],
                    asset_id=asset_id,
                    issue_type="pointset_validation",
                    severity="critical",
                    description=f"Pointset payload value for {point_name} should be numeric for unit {unit_to_check}.",
                    point_name=str(point_name),
                    expected_value=f"numeric {unit_to_check}",
                    observed_value=f"{type(present_value).__name__}: {present_value}",
                    suggested_action="Fix the publisher so present_value type matches the expected unit.",
                    raw_evidence_uri=raw_evidence_uri,
                )
            )

    expected_points = set(str(point) for point in expected_units)
    observed_points = set(str(point) for point in pointset_points)
    for point_name in sorted(expected_points - observed_points):
        issues.append(
            _issue(
                [*existing_issues, *issues],
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
                [*existing_issues, *issues],
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

    return issues


def _capture_live_payloads(
    parameters: dict[str, object],
    *,
    live_capture: LiveCapture | None,
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
        return _capture_live_payloads_per_asset(parameters, assets, live_capture=live_capture)

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

    try:
        messages = live_capture(
            build_mqtt_connection_settings(parameters),
            topics=topics,
            timeout_seconds=parse_float(parameters.get("capture_seconds"), default=5.0),
            max_messages=parse_int(parameters.get("max_messages"), default=len(topics)),
            qos=parse_int(parameters.get("qos"), default=0),
        )
    except (MqttTransportError, OSError, ValueError) as error:
        # Use the coarse status label only; the raw exception text may carry
        # credentials (connection URL / auth detail) and this description is
        # returned to the frontend.
        broker_status_detail = _broker_error_status(error)
        return {
            "attempted": True,
            "status_detail": broker_status_detail,
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                issue_type="payload_error",
                severity="critical",
                description=f"Live MQTT capture failed ({broker_status_detail}).",
                suggested_action="Check broker reachability, credentials, TLS configuration, and topic filters.",
            ),
        }

    parameters["messages"] = [
        {"topic": message.topic, "payload": message.json_payload()} for message in messages
    ]
    for message in messages:
        payload = message.json_payload()
        if not isinstance(payload, dict):
            continue
        key = _payload_key_for_topic(message.topic)
        if key:
            parameters[key] = payload

    return {
        "attempted": True,
        "status_detail": "live_payloads_captured" if messages else "live_capture_timeout",
        "captured_topics": [message.topic for message in messages],
        "issue": None
        if messages
        else _issue(
            [],
            asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
            issue_type="not_publishing",
            severity="high",
            description="No UDMI payloads were captured from the live broker during the capture window.",
            suggested_action="Confirm the device is publishing and widen the capture window if needed.",
        ),
    }


def _capture_live_payloads_per_asset(
    parameters: dict[str, object],
    assets: list,
    *,
    live_capture: LiveCapture | None,
) -> dict[str, object]:
    """Capture live payloads for each asset entry into that entry's *_payload keys.

    Each entry carries its own state/metadata/pointset topics + expected_schedule;
    the broker connection settings are shared (top level). Reuses the single-asset
    capture per entry so routing/parsing stays in one place.

    ponytail: sequential, one short window per asset. Fine when each asset
    publishes within the window; concurrent capture / long (COV, daily) windows —
    Dhilen's "run 2-3 validations at once" — are a separate change.
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

    connection = {key: value for key, value in parameters.items() if key != "assets"}
    captured_topics: list[str] = []
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        merged = {**connection, **entry}
        summary = _capture_live_payloads(merged, live_capture=live_capture)
        # Copy the captured payloads back so the per-asset reviewer sees them.
        for key in ("messages", "state_payload", "metadata_payload", "pointset_payload"):
            if key in merged:
                entry[key] = merged[key]
        captured_topics.extend(summary.get("captured_topics") or [])
    return {
        "attempted": True,
        "status_detail": "live_payloads_captured" if captured_topics else "live_capture_timeout",
        "captured_topics": captured_topics,
        "issue": None,
    }


def _capture_topics(parameters: dict[str, object]) -> list[str]:
    topics = [
        _string(parameters.get("state_topic")),
        _string(parameters.get("metadata_topic")),
        _string(parameters.get("pointset_topic")),
    ]
    return [topic for topic in topics if topic]


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
        for key in ("expected_schedule", "state_payload", "metadata_payload", "pointset_payload", "messages")
    ) and not (parameters.get("full_report_path") or parameters.get("fixture_path"))


def _inline_full_report(parameters: dict[str, object]) -> dict[str, object]:
    expected = _dict_or_empty(parameters.get("expected_schedule"))
    asset_id = str(expected.get("asset_id") or "UDMI asset") if expected else "UDMI asset"
    return {
        "DeviceList": [asset_id],
        "DevicesNotPublishing": [],
        "DevicesNotExpected": {},
        "DevicePayloadErrors": {},
        "DevicePointsetErrors": {},
        "DevicesStateErrors": {},
        "DevicesPointsetValid": [],
        "DevicesStateValid": [],
    }


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


def _expected_payload_facet(expected: dict[str, Any], payload_type: str) -> dict[str, Any] | None:
    """Slice of the expected schedule that maps to a payload type.

    Mirrors the facets _review_payload_issues compares so the per-type view's
    "expected" column stays consistent with the issue logic: manufacturer/model
    -> state, guid/units -> metadata, units -> pointset.
    """
    if payload_type == "state":
        facet = {key: expected[key] for key in ("manufacturer", "model") if expected.get(key) is not None}
    elif payload_type == "metadata":
        facet = {key: expected[key] for key in ("guid", "units") if expected.get(key) is not None}
    elif payload_type == "pointset":
        facet = {key: expected[key] for key in ("units",) if expected.get(key) is not None}
    else:
        facet = {}
    return facet or None


def _asset_payload_view(expected: dict[str, Any], observed_by_type: dict[str, dict]) -> dict[str, object] | None:
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
            view = _asset_payload_view(_dict_or_empty(entry.get("expected_schedule")), _observed_by_type(entry))
            if view is not None:
                views.append(view)
        return views

    view = _asset_payload_view(_dict_or_empty(parameters.get("expected_schedule")), _observed_by_type(parameters))
    return [view] if view is not None else []


def _payload_view_source(
    parameters: dict[str, object], *, capture_attempted: bool, has_views: bool
) -> str:
    """Label the origin of the payload views so the UI never implies fabrication."""
    if not has_views:
        return "none"
    has_observed = any(
        isinstance(parameters.get(key), dict) and parameters.get(key)
        for key in ("state_payload", "metadata_payload", "pointset_payload")
    )
    if capture_attempted and has_observed:
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
    messages = parameters.get("messages")
    if isinstance(messages, list):
        return len(messages)
    return sum(1 for key in ("state_payload", "metadata_payload", "pointset_payload") if isinstance(parameters.get(key), dict))


def _latest_payload_timestamp(parameters: dict[str, object]) -> str | None:
    timestamps: list[str] = []
    for key in ("state_payload", "metadata_payload", "pointset_payload"):
        payload = _dict_or_empty(parameters.get(key))
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
