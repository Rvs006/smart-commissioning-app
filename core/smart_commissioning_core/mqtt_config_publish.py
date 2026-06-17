import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from smart_commissioning_core.engines.safety import require_scan_authorization
from smart_commissioning_core.mqtt_settings import (
    build_mqtt_connection_settings,
    parse_bool,
    parse_float,
)
from smart_commissioning_core.mqtt_transport import (
    MqttMessage,
    MqttTransportError,
    publish_config_and_wait_for_pointset,
)
from smart_commissioning_core.records import ValidationIssueRecord


@dataclass(frozen=True)
class MqttConfigPublishResult:
    result_summary: dict[str, object]
    issues: list[ValidationIssueRecord]


BrokerPublisher = Callable[..., MqttMessage | None]


def validate_and_publish_config(
    parameters: dict[str, object],
    *,
    broker_publisher: BrokerPublisher | None = publish_config_and_wait_for_pointset,
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

    # A live config publish actively writes to a real broker / device, so it is
    # an authorized active operation. Gate it in the engine core (not only the
    # API route) so worker / direct callers cannot bypass authorization. A
    # ScanNotAuthorized raised here propagates to the processor, which marks the
    # run failed; the API route maps it to 403. Validate-only (no live broker)
    # stays unauthenticated and side-effect-free.
    if use_live_broker:
        require_scan_authorization(parameters)

    issues: list[ValidationIssueRecord] = []
    now = datetime.now(UTC)
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
        if broker_publisher is None:
            broker_status_detail = "live_publish_unavailable"
            issues.append(
                _issue(
                    issues,
                    issue_type="live_publish_unavailable",
                    severity="high",
                    description="Live MQTT publish is not available in this execution context.",
                    topic=topic or None,
                    suggested_action="Run the config publish from a service with broker access.",
                    status_detail=broker_status_detail,
                    last_seen_at=now,
                )
            )
        else:
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
                # Use the coarse status label only; the raw exception text may
                # carry credentials (e.g. a connection URL or auth detail) and
                # this description is returned to the frontend.
                issues.append(
                    _connection_issue(
                        issues,
                        topic,
                        broker_status_detail,
                        f"Live MQTT publish/subscribe failed ({broker_status_detail}).",
                    )
                )

    previous_config = _capture_previous_config(parameters, topic)

    # Confirm-back covers EVERY written point, not just the primary (mq9n11wi).
    # Prefer the expected_points list; fall back to the legacy singular
    # expected_point/expected_value so older callers and rollbacks are unchanged.
    # One config_override_not_observed issue is raised per point whose
    # present_value did not change to the expected value in the next pointset.
    expected_points = _normalize_expected_points(parameters, expected_point, expected_value)
    confirmed_points: list[dict[str, object]] = []
    for point_name, point_expected in expected_points:
        point_observed = _extract_present_value(next_pointset_payload, point_name)
        confirmed_points.append(
            {
                "point": point_name,
                "expected_value": point_expected,
                "observed_value": point_observed,
                "confirmed": point_observed == point_expected,
            }
        )
        if point_observed != point_expected:
            issues.append(
                _issue(
                    issues,
                    issue_type="config_override_not_observed",
                    severity="high",
                    description=f"Next pointset payload did not show {point_name} present_value changed to {point_expected}.",
                    topic=_pointset_topic_from_config(topic),
                    point_name=point_name,
                    expected_value=str(point_expected),
                    observed_value="missing" if point_observed is None else str(point_observed),
                    suggested_action="Check device command handling and confirm the next pointset event after publishing.",
                    status_detail="override_not_observed",
                    last_seen_at=now,
                )
            )

    # The singular observed_value summary field reflects the primary (first)
    # expected point so existing summary consumers keep working.
    observed_value = confirmed_points[0]["observed_value"] if confirmed_points else None

    status = "failed" if issues else "succeeded"
    summary: dict[str, object] = {
        "topic": topic,
        "publish_confirmed": confirmed,
        "payload_bytes": len(payload_text.encode("utf-8")),
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "expected_point": expected_point,
        "expected_value": expected_value,
        "observed_value": observed_value,
        "expected_points": confirmed_points,
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
        # Rollback support: the prior value on the config topic, captured before
        # the publish. See _capture_previous_config for the honest limitations.
        "previous_config": previous_config,
    }
    return MqttConfigPublishResult(result_summary=summary, issues=issues)


def _capture_previous_config(parameters: dict[str, object], topic: str) -> dict[str, object]:
    """Capture the prior retained config value on ``topic`` for rollback.

    HONESTY: reading a broker's RETAINED message requires a reachable broker,
    which does not exist in this environment. So this captures whatever prior
    value the caller can provide via ``parameters['previous_config_payload']``
    (e.g. a value the operator snapshotted earlier); when none is supplied we
    record ``captured: False`` and leave ``payload`` null. The live capture of
    the retained value off a real broker is on-site-validation surface and is
    listed in the task's ``live_untested`` output.

    Shape: ``{"topic", "payload" (str|None), "captured" (bool), "source"}``.
    """
    supplied = parameters.get("previous_config_payload")
    if supplied is None:
        supplied = parameters.get("previous_config")
    if isinstance(supplied, (dict, list)):
        payload_text: str | None = json.dumps(supplied)
        source = "request_supplied"
        captured = True
    elif isinstance(supplied, str) and supplied.strip():
        payload_text = supplied
        source = "request_supplied"
        captured = True
    else:
        payload_text = None
        source = "not_captured_no_broker_read"
        captured = False
    return {
        "topic": topic,
        "payload": payload_text,
        "captured": captured,
        "source": source,
    }


def rollback_config(
    parameters: dict[str, object],
    previous_config: dict[str, object],
    *,
    broker_publisher: BrokerPublisher | None = publish_config_and_wait_for_pointset,
) -> MqttConfigPublishResult:
    """Republish a previously-captured config payload to roll back a publish.

    Reuses the forward publish path so the SAME publish-confirmation gate and
    live-broker handling apply: the operator must re-confirm
    (``parameters['confirmed']``), and a live broker is contacted only when
    ``use_live_broker`` / ``broker_host`` is set. The payload published is the
    captured previous value, NOT the request's ``payload``.

    HONESTY: the live republish to a real broker is the same untested
    raw-socket path as the forward publish. Without a broker this validates the
    rollback plumbing (gate, topic, captured payload) only.
    """
    # A live rollback republishes to a real broker, so gate it on authorization
    # in the core (not only the API route) — same as the forward publish — so
    # worker / direct callers cannot bypass it. ScanNotAuthorized propagates to
    # the processor (failed run -> 403 at the API). Validate-only stays open.
    use_live_broker = parse_bool(parameters.get("use_live_broker")) or bool(_string(parameters.get("broker_host")))
    if use_live_broker:
        require_scan_authorization(parameters)

    payload_text = previous_config.get("payload")
    if not isinstance(payload_text, str) or not payload_text.strip():
        raise ValueError("rollback requires a captured previous config payload (a JSON string).")

    rollback_parameters = dict(parameters)
    # Publish the captured prior value to the original config topic; keep the
    # broker/auth/confirmation parameters from the original run.
    rollback_parameters["payload"] = payload_text
    if previous_config.get("topic"):
        rollback_parameters["topic"] = previous_config["topic"]
    # A rollback does not assert a new expected override value; drop any
    # forward-publish expectation so the rollback is judged on publish success.
    rollback_parameters.pop("expected_point", None)
    rollback_parameters.pop("expected_value", None)
    # Do not re-capture a "previous of the previous"; mark this as a rollback.
    rollback_parameters.pop("previous_config_payload", None)
    rollback_parameters.pop("previous_config", None)

    result = validate_and_publish_config(rollback_parameters, broker_publisher=broker_publisher)
    summary = dict(result.result_summary)
    summary["rollback"] = True
    summary["rolled_back_payload_bytes"] = len(payload_text.encode("utf-8"))
    return MqttConfigPublishResult(result_summary=summary, issues=result.issues)


def _string(value: object) -> str:
    return str(value or "").strip()


def _normalize_expected_points(
    parameters: dict[str, object],
    primary_point: str,
    primary_value: object,
) -> list[tuple[str, object]]:
    """Expected (point, value) pairs to confirm in the next pointset payload.

    Prefers the ``expected_points`` list (mq9n11wi multi-point confirm-back),
    each entry shaped ``{"point": str, "value": object}``; falls back to the
    legacy singular ``expected_point``/``expected_value`` so older callers and
    rollbacks behave exactly as before. Entries with a blank point name or a
    ``None`` expected value are dropped (nothing to confirm), matching the
    original singular guard.
    """
    raw = parameters.get("expected_points")
    pairs: list[tuple[str, object]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            point = _string(entry.get("point"))
            value = entry.get("value")
            if point and value is not None:
                pairs.append((point, value))
    if pairs:
        return pairs
    if primary_point and primary_value is not None:
        return [(primary_point, primary_value)]
    return []


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
