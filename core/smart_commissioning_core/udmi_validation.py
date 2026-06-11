import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from smart_commissioning_core.mqtt_settings import build_mqtt_connection_settings, parse_bool, parse_float, parse_int
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
    issues.extend(_review_payload_issues(parameters or {}, issues))
    expected_devices = _list_value(full_report, "DeviceList")
    not_publishing = _list_value(full_report, "DevicesNotPublishing")
    latest_payload = _latest_payload_timestamp(parameters or {})
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

    expected_make = expected.get("manufacturer")
    observed_make = state_payload.get("system", {}).get("hardware", {}).get("make") if state_payload else None
    if expected_make and observed_make and expected_make != observed_make:
        issues.append(
            _issue(
                [*existing_issues, *issues],
                asset_id=asset_id,
                issue_type="state_validation",
                severity="high",
                description="State payload manufacturer does not match the asset register.",
                expected_value=str(expected_make),
                observed_value=str(observed_make),
                suggested_action="Confirm the manufacturer in the MSI schedule and the UDMI state payload.",
                raw_evidence_uri=raw_evidence_uri,
            )
        )

    expected_model = expected.get("model")
    observed_model = state_payload.get("system", {}).get("hardware", {}).get("model") if state_payload else None
    if expected_model and observed_model and expected_model != observed_model:
        issues.append(
            _issue(
                [*existing_issues, *issues],
                asset_id=asset_id,
                issue_type="state_validation",
                severity="medium",
                description="State payload model does not match the asset register.",
                expected_value=str(expected_model),
                observed_value=str(observed_model),
                suggested_action="Check device metadata or update the asset register if the installed model changed.",
                raw_evidence_uri=raw_evidence_uri,
            )
        )

    expected_guid = expected.get("guid")
    observed_guid = (
        metadata_payload.get("system", {})
        .get("physical_tag", {})
        .get("asset", {})
        .get("guid")
        if metadata_payload
        else None
    )
    if expected_guid and observed_guid and expected_guid != observed_guid:
        issues.append(
            _issue(
                [*existing_issues, *issues],
                asset_id=asset_id,
                issue_type="metadata_validation",
                severity="high",
                description="Metadata GUID does not match the asset register.",
                expected_value=str(expected_guid),
                observed_value=str(observed_guid),
                suggested_action="Correct the UDMI metadata asset GUID or the imported register.",
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
        )
    except (MqttTransportError, OSError, ValueError) as error:
        return {
            "attempted": True,
            "status_detail": _broker_error_status(error),
            "captured_topics": [],
            "issue": _issue(
                [],
                asset_id=str(_dict_or_empty(parameters.get("expected_schedule")).get("asset_id") or "UDMI asset"),
                issue_type="payload_error",
                severity="critical",
                description=f"Live MQTT capture failed: {error}",
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


def _string(value: object) -> str:
    return str(value or "").strip()


def _broker_error_status(error: Exception) -> str:
    text = str(error).casefold()
    if "tls" in text or "certificate" in text or "ssl" in text:
        return "tls_error"
    if "username" in text or "password" in text or "authorised" in text or "authorized" in text:
        return "authentication_error"
    if "timed out" in text or "timeout" in text:
        return "broker_timeout"
    return "broker_unreachable"


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
    return ValidationIssueRecord(
        issue_id=f"{prefix}-{len(issues) + 1:04d}",
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
