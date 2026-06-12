import logging
from typing import Any

from smart_commissioning_core.mqtt_config_publish import (
    BrokerPublisher,
    rollback_config,
    validate_and_publish_config,
)
from smart_commissioning_core.mqtt_transport import publish_config_and_wait_for_pointset
from smart_commissioning_core.run_store import RunStore

_logger = logging.getLogger(__name__)

# Persisted, user-facing failure message. The raw exception text is NOT
# surfaced: a parameter / transport error can echo back credentials (broker URL,
# username/password, tokens). The raw detail is logged server-side instead.
_SANITIZED_FAILURE_MESSAGE = "MQTT config publish failed; see server logs."


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
    except Exception:
        _logger.exception("MQTT config publish run %s failed", run_id)
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
            error_message=_SANITIZED_FAILURE_MESSAGE,
        )


def process_mqtt_config_rollback_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    previous_config: dict[str, object],
    run_store: RunStore,
    execution_mode: str,
    broker_publisher: BrokerPublisher | None = publish_config_and_wait_for_pointset,
) -> Any:
    """Roll a config publish back by republishing its captured previous value.

    Mirrors :func:`process_mqtt_config_publish_run`: flips the run to running,
    runs :func:`rollback_config` (which reuses the publish gate), persists the
    rollback summary + issues, and sets the terminal status. ``previous_config``
    is the captured value from the original run's ``result_summary``.
    """
    run_store.update_run_status(
        run_id,
        status="running",
        stage="rolling_back_mqtt_config_publish",
        progress_percent=25,
    )

    try:
        result = rollback_config(
            parameters,
            previous_config,
            broker_publisher=broker_publisher,
        )
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
            stage="mqtt_config_rollback_complete",
            progress_percent=100,
            error_message=None if not result.issues else "MQTT config rollback validation failed.",
        )
    except Exception:
        _logger.exception("MQTT config rollback run %s failed", run_id)
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
            stage="mqtt_config_rollback_failed",
            progress_percent=100,
            error_message=_SANITIZED_FAILURE_MESSAGE,
        )
