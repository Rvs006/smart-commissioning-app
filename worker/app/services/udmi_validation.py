import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_FULL_REPORT_PATH = (
    Path(__file__).resolve().parents[3]
    / "device_udmi_payload_validation"
    / "device_udmi_payload_validation"
    / "full_report.json"
)
PACKAGED_FULL_REPORT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "udmi_full_report.json"


@dataclass(frozen=True)
class UdmiValidationResult:
    result_summary: dict[str, object]
    issues: list[dict[str, object]]
    source_fixture: str


def validate_udmi_full_report(parameters: dict[str, object] | None = None) -> UdmiValidationResult:
    report_path = _resolve_report_path(parameters or {})
    full_report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(full_report, dict):
        raise ValueError("UDMI full report fixture must contain a JSON object.")

    issues = _normalise_issues(full_report)
    expected_devices = _list_value(full_report, "DeviceList")
    not_publishing = _list_value(full_report, "DevicesNotPublishing")
    result_summary: dict[str, object] = {
        "expected_devices": len(expected_devices),
        "publishing_seen": max(0, len(expected_devices) - len(not_publishing)),
        "not_publishing": len(not_publishing),
        "pointset_valid": len(_list_value(full_report, "DevicesPointsetValid")),
        "state_valid": len(_list_value(full_report, "DevicesStateValid")),
        "issue_count": len(issues),
        "source": "udmi_full_report_fixture",
        "source_fixture": str(report_path),
    }
    return UdmiValidationResult(
        result_summary=result_summary,
        issues=issues,
        source_fixture=str(report_path),
    )


def _resolve_report_path(parameters: dict[str, object]) -> Path:
    raw_path = parameters.get("full_report_path") or parameters.get("fixture_path")
    if raw_path is None:
        return DEFAULT_FULL_REPORT_PATH if DEFAULT_FULL_REPORT_PATH.exists() else PACKAGED_FULL_REPORT_PATH
    if not isinstance(raw_path, str):
        raise ValueError("UDMI fixture path parameter must be a string.")

    report_path = Path(raw_path).expanduser()
    if not report_path.is_absolute():
        report_path = Path(__file__).resolve().parents[3] / report_path
    return report_path


def _normalise_issues(full_report: dict[str, Any]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []

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


def _issue(
    issues: list[dict[str, object]],
    *,
    asset_id: str,
    issue_type: str,
    severity: str,
    description: str,
) -> dict[str, object]:
    prefix = {
        "not_publishing": "UDMI-NP",
        "unexpected_device": "UDMI-UN",
        "payload_error": "UDMI-PL",
        "pointset_validation": "UDMI-PS",
        "state_validation": "UDMI-ST",
    }.get(issue_type, "UDMI-IS")
    return {
        "issue_id": f"{prefix}-{len(issues) + 1:04d}",
        "asset_id": asset_id,
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
    }


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
