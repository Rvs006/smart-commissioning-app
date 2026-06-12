from typing import Any

from smart_commissioning_core.mqtt_transport import subscribe_and_capture
from smart_commissioning_core.run_store import RunStore
from smart_commissioning_core.udmi_validation import LiveCapture, validate_udmi_full_report


def process_udmi_validation_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_store: RunStore,
    execution_mode: str,
    fallback_reason: str | None = None,
    live_capture: LiveCapture | None = subscribe_and_capture,
) -> Any:
    run_store.update_run_status(
        run_id,
        status="running",
        stage="loading_udmi_fixture",
        progress_percent=15,
    )

    try:
        validation_result = validate_udmi_full_report(parameters, live_capture=live_capture)
        result_summary = {
            **validation_result.result_summary,
            "execution_mode": execution_mode,
            "worker_required": execution_mode != "inline_local_fallback",
        }
        if fallback_reason:
            result_summary["fallback_reason"] = fallback_reason

        run_store.update_result_summary(run_id, result_summary, merge=False)
        run_store.replace_issues(run_id, validation_result.issues)
        return run_store.update_run_status(
            run_id,
            status="succeeded",
            stage="udmi_fixture_validation_complete",
            progress_percent=100,
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
            stage="udmi_fixture_validation_failed",
            progress_percent=100,
            error_message=str(error),
        )
