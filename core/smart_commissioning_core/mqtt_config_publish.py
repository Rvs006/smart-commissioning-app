import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from smart_commissioning_core.engines.comparison_common import make_issue
from smart_commissioning_core.engines.safety import require_scan_authorization
from smart_commissioning_core.mqtt_settings import (
    _broker_error_status,
    _string,
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
# Reads the broker's retained config payload for rollback (read-only SUBSCRIBE);
# injectable so tests can supply a fake and so a caller can disable the live read.
ConfigReader = Callable[..., str | None]


def validate_and_publish_config(
    parameters: dict[str, object],
    *,
    broker_publisher: BrokerPublisher | None = publish_config_and_wait_for_pointset,
    broker_reader: ConfigReader | None = None,
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

    # Capture the prior retained config BEFORE publishing so a later rollback can
    # restore the real prior value (a live retained read AFTER the publish would
    # capture the just-published value). Read-only SUBSCRIBE; gated by the same
    # use_live_broker/authorization as the publish (authorization enforced above).
    previous_config = _capture_previous_config(
        parameters,
        topic,
        use_live_broker=use_live_broker,
        broker_reader=broker_reader,
        wait_seconds=wait_seconds,
    )

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

    # Multi-point confirm: build the set of expected (point, value) pairs from
    # (in priority order) an explicit parameters['expected_points'] list, OR by
    # deriving them from the published config payload's pointset.points
    # set_value entries, OR the legacy single expected_point/expected_value pair.
    # Each expected point is checked against the captured next pointset's
    # present_value, with a per-point pass/fail in result_summary and one issue
    # per mismatch. The single-point path stays unchanged for back-compat.
    expected_points = _resolve_expected_points(
        parameters,
        expected_point=expected_point,
        expected_value=expected_value,
        published_payload=payload,
        have_pointset=next_pointset_payload is not None,
    )
    point_checks: list[dict[str, object]] = []
    for expected in expected_points:
        point_name = expected["point"]
        target_value = expected["value"]
        observed = _extract_present_value(next_pointset_payload, point_name)
        matched = observed == target_value
        point_checks.append(
            {
                "point": point_name,
                "expected_value": target_value,
                "observed_value": observed,
                "matched": matched,
            }
        )
        if not matched:
            issues.append(
                _issue(
                    issues,
                    issue_type="config_override_not_observed",
                    severity="high",
                    description=f"Next pointset payload did not show {point_name} present_value changed to {target_value}.",
                    topic=_pointset_topic_from_config(topic),
                    point_name=point_name,
                    expected_value=str(target_value),
                    observed_value="missing" if observed is None else str(observed),
                    suggested_action="Check device command handling and confirm the next pointset event after publishing.",
                    status_detail="override_not_observed",
                    last_seen_at=now,
                )
            )

    # Back-compat single-point summary fields: when exactly one point was
    # checked (the legacy single-point shape, or a single-entry multi-point
    # set), keep reporting expected_point/expected_value/observed_value as
    # before. Multi-point detail always lives in point_checks.
    primary_check = point_checks[0] if len(point_checks) == 1 else None
    summary_expected_point = primary_check["point"] if primary_check else expected_point
    summary_expected_value = primary_check["expected_value"] if primary_check else expected_value
    summary_observed_value = (
        primary_check["observed_value"]
        if primary_check
        else _extract_present_value(next_pointset_payload, expected_point)
    )

    status = "failed" if issues else "succeeded"
    summary: dict[str, object] = {
        "topic": topic,
        "publish_confirmed": confirmed,
        "payload_bytes": len(payload_text.encode("utf-8")),
        "payload_keys": list(payload.keys()) if isinstance(payload, dict) else [],
        "expected_point": summary_expected_point,
        "expected_value": summary_expected_value,
        "observed_value": summary_observed_value,
        "expected_point_count": len(point_checks),
        "matched_point_count": sum(1 for check in point_checks if check["matched"]),
        # "parted": at least one expected point matched but not all of them, so
        # the publish partially confirmed. (status is still "failed" because a
        # mismatch raised an issue.) Lets the UI distinguish a total miss from a
        # partial confirm without re-deriving from point_checks.
        "partial_confirm": (
            len(point_checks) > 1
            and 0 < sum(1 for check in point_checks if check["matched"]) < len(point_checks)
        ),
        "point_checks": point_checks,
        # Back-compat mirror of the per-point results under the original key with
        # a "confirmed" alias (PR #7 summary contract), so consumers/tests that
        # read result_summary["expected_points"][].confirmed keep working
        # alongside the richer point_checks/matched schema above.
        "expected_points": [
            {
                "point": check["point"],
                "expected_value": check["expected_value"],
                "observed_value": check["observed_value"],
                "confirmed": check["matched"],
            }
            for check in point_checks
        ],
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


def _capture_previous_config(
    parameters: dict[str, object],
    topic: str,
    *,
    use_live_broker: bool = False,
    broker_reader: ConfigReader | None = None,
    wait_seconds: float = 5.0,
) -> dict[str, object]:
    """Capture the prior retained config value on ``topic`` for rollback.

    Order of preference: (1) a LIVE retained read off the broker when
    ``use_live_broker`` and a ``broker_reader`` are available — the device's
    actual current /config, subscribed read-only before the forward publish;
    (2) a value the caller snapshotted via ``parameters['previous_config_payload']``;
    (3) nothing (``captured: False``). The live read is the real read-modify-
    restore path; it is on-site-untested (no broker in dev) but no longer a stub.

    Shape: ``{"topic", "payload" (str|None), "captured" (bool), "source"}``.
    """
    if use_live_broker and broker_reader is not None and topic:
        try:
            settings = build_mqtt_connection_settings(parameters)
            retained = broker_reader(settings, config_topic=topic, timeout_seconds=wait_seconds)
        except (MqttTransportError, OSError, ValueError):
            retained = None
        if isinstance(retained, str) and retained.strip():
            return {
                "topic": topic,
                "payload": retained,
                "captured": True,
                "source": "live_retained_read",
            }

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
    # forward-publish expectation (single- AND multi-point) so the rollback is
    # judged on publish success, and suppress deriving an expectation from the
    # republished payload's set_values.
    rollback_parameters.pop("expected_point", None)
    rollback_parameters.pop("expected_value", None)
    rollback_parameters.pop("expected_points", None)
    rollback_parameters["_suppress_expected_point_derivation"] = True
    # Do not re-capture a "previous of the previous"; mark this as a rollback.
    rollback_parameters.pop("previous_config_payload", None)
    rollback_parameters.pop("previous_config", None)

    # A rollback restores a known prior value, so it does not capture a "previous
    # of the previous" — skip the live retained read on the rollback republish.
    result = validate_and_publish_config(
        rollback_parameters, broker_publisher=broker_publisher, broker_reader=None
    )
    summary = dict(result.result_summary)
    summary["rollback"] = True
    summary["rolled_back_payload_bytes"] = len(payload_text.encode("utf-8"))
    return MqttConfigPublishResult(result_summary=summary, issues=result.issues)


def _resolve_expected_points(
    parameters: dict[str, object],
    *,
    expected_point: str,
    expected_value: object,
    published_payload: object,
    have_pointset: bool,
) -> list[dict[str, object]]:
    """Build the list of expected ``{point, value}`` pairs to confirm.

    Priority:

    1. ``parameters['expected_points']`` — an explicit list of
       ``{"point": <name>, "value": <set_value>}`` mappings (the multi-point
       contract; the frontend can send the full expected set directly). Always
       honored when supplied.
    2. Otherwise, DERIVE the expected set from the published config payload's
       ``pointset.points.<name>.set_value`` entries (the multi-point payload the
       frontend now publishes). Each point's set_value is what the next pointset
       should report as present_value. Derivation only runs when there is a next
       pointset to verify against (a captured live pointset or a supplied
       ``next_pointset_payload``) or when the caller opts in with
       ``confirm_published_points`` — so a fire-and-forget publish with no
       captured pointset does NOT manufacture spurious mismatches (back-compat).
    3. Otherwise fall back to the legacy single ``expected_point`` /
       ``expected_value`` pair (unchanged back-compat).

    A point is only kept when it has a non-empty name and a non-None value, so a
    payload without set_values (or an empty expected list) yields no checks —
    identical to today's behavior when no expectation is supplied.
    """
    explicit = parameters.get("expected_points")
    if isinstance(explicit, list):
        resolved: list[dict[str, object]] = []
        for entry in explicit:
            if not isinstance(entry, dict):
                continue
            name = _string(entry.get("point") or entry.get("name"))
            value = entry["value"] if "value" in entry else entry.get("set_value")
            if name and value is not None:
                resolved.append({"point": name, "value": value})
        if resolved:
            return resolved

    should_derive = (
        not parse_bool(parameters.get("_suppress_expected_point_derivation"))
        and (have_pointset or parse_bool(parameters.get("confirm_published_points")))
    )
    if should_derive:
        derived = _derive_expected_from_payload(published_payload)
        if derived:
            return derived

    if expected_point and expected_value is not None:
        return [{"point": expected_point, "value": expected_value}]
    return []


def _derive_expected_from_payload(payload: object) -> list[dict[str, object]]:
    """Derive expected ``{point, value}`` pairs from a config payload's set_values.

    Reads ``pointset.points.<name>.set_value`` from the published config payload;
    each set_value is the value the next pointset's present_value should match.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []
    points = payload.get("pointset", {})
    points = points.get("points", {}) if isinstance(points, dict) else {}
    if not isinstance(points, dict):
        return []
    resolved: list[dict[str, object]] = []
    for name, point in points.items():
        if not isinstance(point, dict) or "set_value" not in point:
            continue
        value = point.get("set_value")
        if value is not None:
            resolved.append({"point": str(name), "value": value})
    return resolved


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
    return make_issue(
        issues,
        "MQTT-CFG",
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
