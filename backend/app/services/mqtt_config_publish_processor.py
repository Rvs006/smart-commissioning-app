from app.schemas.jobs import RunRecord
from app.services.mqtt_config_publish import validate_and_publish_config
from app.services.run_service import RunService


def process_mqtt_config_publish_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_service: RunService,
    execution_mode: str,
) -> RunRecord:
    run_service.update_run_status(
        run_id,
        status="running",
        stage="validating_mqtt_config_publish",
        progress_percent=25,
    )

    try:
        result = validate_and_publish_config(parameters)
        run_service.update_result_summary(
            run_id,
            {
                **result.result_summary,
                "execution_mode": execution_mode,
                "worker_required": execution_mode != "inline_local_fallback",
            },
            merge=False,
        )
        run_service.replace_issues(run_id, result.issues)
        return run_service.update_run_status(
            run_id,
            status="failed" if result.issues else "succeeded",
            stage="mqtt_config_publish_complete",
            progress_percent=100,
            error_message=None if not result.issues else "MQTT config publish validation failed.",
        )
    except Exception as error:
        run_service.update_result_summary(
            run_id,
            {
                "execution_mode": execution_mode,
                "worker_required": execution_mode != "inline_local_fallback",
            },
        )
        return run_service.update_run_status(
            run_id,
            status="failed",
            stage="mqtt_config_publish_failed",
            progress_percent=100,
            error_message=str(error),
        )
