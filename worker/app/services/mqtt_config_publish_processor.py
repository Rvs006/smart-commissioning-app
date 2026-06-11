import json

from app.services.run_store import FileRunStore


def process_mqtt_config_publish_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_store: FileRunStore | None = None,
) -> dict[str, object]:
    store = run_store or FileRunStore()
    store.update_status(
        run_id,
        status="running",
        stage="validating_mqtt_config_publish",
        progress_percent=25,
    )

    topic = str(parameters.get("topic") or "").strip()
    payload_text = str(parameters.get("payload") or "").strip()
    confirmed = bool(parameters.get("confirmed"))
    issues: list[dict[str, object]] = []

    if not confirmed:
        issues.append(_issue(len(issues), "publish_not_confirmed", "Config payload publish requires operator confirmation."))
    if not topic.endswith("/config"):
        issues.append(_issue(len(issues), "invalid_config_topic", "Config payload topic must end in /config."))
    try:
        json.loads(payload_text)
    except json.JSONDecodeError:
        issues.append(_issue(len(issues), "invalid_config_payload", "Config payload must be valid JSON."))

    result = store.replace_result(
        run_id,
        result_summary={
            "topic": topic,
            "publish_confirmed": confirmed,
            "payload_bytes": len(payload_text.encode("utf-8")),
            "status": "failed" if issues else "succeeded",
            "execution_mode": "dramatiq_worker",
            "worker_required": True,
        },
        issues=issues,
    )
    return store.update_status(
        run_id,
        status="failed" if issues else "succeeded",
        stage="mqtt_config_publish_complete",
        progress_percent=100,
        error_message=None if not issues else "MQTT config publish validation failed.",
    )


def _issue(index: int, issue_type: str, description: str) -> dict[str, object]:
    return {
        "issue_id": f"MQTT-CFG-{index + 1:04d}",
        "asset_id": None,
        "issue_type": issue_type,
        "severity": "high",
        "description": description,
        "status_detail": issue_type,
        "raw_evidence_uri": "runtime://mqtt-config-publish/latest",
    }
