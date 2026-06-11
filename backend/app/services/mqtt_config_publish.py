import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.schemas.jobs import ValidationIssueRecord
from app.services.mqtt_settings import (
    build_mqtt_connection_settings,
    parse_bool,
    parse_float,
)
from app.services.mqtt_transport import (
    MqttMessage,
    MqttTransportError,
    publish_config_and_wait_for_pointset,
)


@dataclass(frozen=True)
class MqttConfigPublishResult:
    result_summary: dict[str, object]
    issues: list[ValidationIssueRecord]


BrokerPublisher = Callable[..., MqttMessage | None]


def validate_and_publish_config(
    parameters: dict[str, object],
    *,
    broker_publisher: BrokerPublisher = publish_config_and_wait_for_pointset,
) -> MqttConfigPublishResult:
    topic = _string(parameters.get("topic"))
    payload_text = _string(parameters.get("payload"))
    expected_point = _string(parameters.get("expected_point"))
    expected_value = parameters.get("expected_value")
    next_pointset_payload = parameters.get("next_pointset_payload")
    confirmed = parse_bool(parameters.get("confirmed"))
    simulate_error = _string(parameters.get("simulate_error")).casefold()
    use_live_broker = parse_bool(parameters.get("use_live_broker")) or bool(_string(parameters.get("broker_host")))
    pointset_topic = _string(parameters.get("pointset_topic")) or (_pointset_topic_from_config(topic) or "")
    wait_seconds = parse_float(parameters.get("wait_seconds"), default=5.0)

    issues: list[ValidationIssueRecord] = []
    now = datetime.now(timezone.utc)
    broker_attempted = False
    broker_status_detail = "live_broker_not_requested"

    if not confirmed:
        issues.append(
            _issue(
                issues,
                issue_type="publish_not_confirmed",
                severity="high",
                description="Config payload publish was blocked because operator confirmation was not supplied.",
                topic=topic or None,
                suggested_action="Confirm the target topic and payload before publishing.",
                status_detail="publish_blocked",
            )
        )

    if not topic or " " in topic or "/" not in topic or not topic.endswith("/config"):
        issues.append(
            _issue(
                issues,
                issue_type="invalid_config_topic",
                severity="high",
                description="Config payload topic must be a valid MQTT topic ending in /config.",
                topic=topic or None,
                suggested_action="Use the device config topic, for example 334os/b1/ahu-1000001/config.",
                status_detail="invalid_topic",
            )
        )

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        issues.append(
            _issue(
                issues,
                issue_type="invalid_config_payload",
                severity="critical",
                description=f"Config payload is not valid JSON: {error.msg}.",
                topic=topic or None,
                suggested_action="Fix the JSON payload and retry.",
                status_detail="invalid_json",
            )
        )
        payload = {}

    if simulate_error in {"auth", "authentication"}:
        issues.append(_connection_issue(issues, topic, "authentication_error", "MQTT broker rejected the supplied credentials."))
    elif simulate_error == "tls":
        issues.append(_connection_issue(issues, topic, "tls_error", "MQTT TLS handshake failed."))
    elif simulate_error in {"unreachable", "network"}:
        issues.append(_connection_issue(issues, topic, "broker_unreachable", "MQTT broker could not be reached."))

    if use_live_broker and not issues:
        broker_attempted = True
        broker_status_detail = "connecting_to_live_broker"
        try:
            settings = build_mqtt_connection_settings(parameters)
            message = broker_publisher(
                settings,
                config_topic=topic,
                config_payload=payload_text,
                pointset_topic=pointset_topic,
                timeout_seconds=wait_seconds,
            )
            if message is None:
                broker_status_detail = "live_pointset_timeout"
                issues.append(
                    _issue(
                        issues,
                        issue_type="live_pointset_timeout",
                        severity="high",
                        description=f"No pointset payload was received on {pointset_topic} after publishing the config payload.",
                        topic=pointset_topic,
                        suggested_action="Confirm the device publishes an events/pointset message after config commands.",
                        status_detail=broker_status_detail,
                        last_seen_at=now,
                    )
                )
            else:
                broker_status_detail = "live_pointset_received"
                next_pointset_payload = message.json_payload()
                if next_pointset_payload is None:
                    issues.append(
                        _issue(
                            issues,
                            issue_type="invalid_live_pointset_payload",
                            severity="critical",
                            description="The live pointset message was received but was not valid JSON.",
                            topic=message.topic,
                            suggested_action="Fix the publisher payload so the pointset message is valid JSON.",
                            status_detail="invalid_live_json",
                            last_seen_at=now,
                        )
                    )
        except (MqttTransportError, OSError, ValueError) as error:
            broker_status_detail = _broker_error_status(error)
            issues.append(
                _connection_issue(
                    issues,
                    topic,
                    broker_status_detail,
                    f"Live MQTT publish/subscribe failed: {error}",
                )
            )

    observed_value = _extract_present_value(next_pointset_payload, expected_point)
    if expected_point and expected_value is not None and observed_value != expected_value:
        issues.append(
            _issue(
                issues,
                issue_type="config_override_not_observed",
                severity="high",
                description=f"Next pointset payload did not show {expected_point} present_value changed to {expected_value}.",
                topic=_pointset_topic_from_config(topic),
                point_name=expected_point,
                expected_value=str(expected_value),
                observed_value="missing" if observed_value is None else str(observed_value),
                suggested_action="Check device command handling and confirm the next pointset event after publishing.",
                status_detail="override_not_observed",
                last_seen_at=now,
            )
        )

    status = "failed" if issues else "succeeded"
    summary: dict[str, object] = {
        "topic": topic,
        "publish_confirmed": confirmed,
        "payload_bytes": len(payload_text.encode("utf-8")),
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "expected_point": expected_point,
        "expected_value": expected_value,
        "observed_value": observed_value,
        "message_count": 1 if next_pointset_payload else 0,
        "broker_publish_attempted": broker_attempted,
        "pointset_topic": pointset_topic,
        "status": status,
        "status_detail": _summary_status_detail(
            status=status,
            broker_attempted=broker_attempted,
            broker_status_detail=broker_status_detail,
        ),
        "broker_status_detail": broker_status_detail,
        "last_seen_at": now.isoformat(),
        "raw_evidence_uri": "runtime://mqtt-config-publish/latest",
    }
    return MqttConfigPublishResult(result_summary=summary, issues=issues)


def _string(value: object) -> str:
    return str(value or "").strip()


def _extract_present_value(payload: object, point_name: str) -> object | None:
    if not point_name:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    point = payload.get("pointset", {}).get("points", {}).get(point_name)
    if not isinstance(point, dict):
        return None
    return point.get("present_value")


def _pointset_topic_from_config(topic: str) -> str | None:
    if not topic:
        return None
    return topic.removesuffix("/config") + "/events/pointset"


def _broker_error_status(error: Exception) -> str:
    text = str(error).casefold()
    if "tls" in text or "certificate" in text or "ssl" in text:
        return "tls_error"
    if "username" in text or "password" in text or "authorised" in text or "authorized" in text:
        return "authentication_error"
    if "timed out" in text or "timeout" in text:
        return "broker_timeout"
    return "broker_unreachable"


def _summary_status_detail(*, status: str, broker_attempted: bool, broker_status_detail: str) -> str:
    if status != "succeeded":
        return "Config publish validation failed; inspect issues for the precise cause."
    if broker_attempted:
        return f"Live MQTT publish accepted and verified through {broker_status_detail}."
    return "Local pointset verification accepted; live broker publish was not requested."


def _connection_issue(
    issues: list[ValidationIssueRecord],
    topic: str,
    issue_type: str,
    description: str,
) -> ValidationIssueRecord:
    return _issue(
        issues,
        issue_type=issue_type,
        severity="critical",
        description=description,
        topic=topic or None,
        suggested_action="Check MQTT broker reachability, credentials, TLS files, and firewall rules.",
        status_detail=issue_type,
    )


def _issue(
    issues: list[ValidationIssueRecord],
    *,
    issue_type: str,
    severity: str,
    description: str,
    topic: str | None = None,
    point_name: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    suggested_action: str | None = None,
    status_detail: str | None = None,
    last_seen_at: datetime | None = None,
) -> ValidationIssueRecord:
    return ValidationIssueRecord(
        issue_id=f"MQTT-CFG-{len(issues) + 1:04d}",
        asset_id=None,
        issue_type=issue_type,
        severity=severity,
        description=description,
        topic=topic,
        point_name=point_name,
        expected_value=expected_value,
        observed_value=observed_value,
        suggested_action=suggested_action,
        raw_evidence_uri="runtime://mqtt-config-publish/latest",
        status_detail=status_detail,
        last_seen_at=last_seen_at,
    )
