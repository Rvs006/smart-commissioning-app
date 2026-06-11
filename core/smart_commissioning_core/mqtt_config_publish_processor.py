from typing import Any

from smart_commissioning_core.mqtt_config_publish import BrokerPublisher, validate_and_publish_config
from smart_commissioning_core.mqtt_transport import publish_config_and_wait_for_pointset
from smart_commissioning_core.run_store import RunStore


def process_mqtt_config_publish_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_store: RunStore,
    execution_mode: str,
    broker_publisher: BrokerPublisher | None = publish_config_and_wait_for_pointset,
) -> Any:
    run_store.update_run_status(
        run_id,
        status="running",
        stage="validating_mqtt_config_publish",
        progress_percent=25,
    )

    try:
        result = validate_and_publish_config(parameters, broker_publisher=broker_publisher)
        run_store.update_result_summary(
            run_id,
            {
                **result.result_summary,
                "execution_mode": execution_mode,
                "worker_required": execution_mode != "inline_local_fallback",
            },
            merge=False,
        )
        run_store.replace_issues(run_id, result.issues)
        return run_store.update_run_status(
            run_id,
            status="failed" if result.issues else "succeeded",
            stage="mqtt_config_publish_complete",
            progress_percent=100,
            error_message=None if not result.issues else "MQTT config publish validation failed.",
        )
    except Exception as error:
        run_store.update_result_summary(
            run_id,
            {
                "execution_mode": execution_mode,
                "worker_required": execution_mode != "inline_local_fallback",
            },
        )
        return run_store.update_run_status(
            run_id,
            status="failed",
            stage="mqtt_config_publish_failed",
            progress_percent=100,
            error_message=str(error),
        )
