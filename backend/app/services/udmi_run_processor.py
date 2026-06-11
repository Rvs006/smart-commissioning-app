from app.schemas.jobs import RunRecord
from app.services.run_service import RunService
from app.services.udmi_validation import validate_udmi_full_report


def process_udmi_validation_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_service: RunService,
    execution_mode: str,
    fallback_reason: str | None = None,
) -> RunRecord:
    run_service.update_run_status(
        run_id,
        status="running",
        stage="loading_udmi_fixture",
        progress_percent=15,
    )

    try:
        validation_result = validate_udmi_full_report(parameters)
        result_summary = {
            **validation_result.result_summary,
            "execution_mode": execution_mode,
            "worker_required": execution_mode != "inline_local_fallback",
        }
        if fallback_reason:
            result_summary["fallback_reason"] = fallback_reason

        run_service.update_result_summary(run_id, result_summary, merge=False)
        run_service.replace_issues(run_id, validation_result.issues)
        return run_service.update_run_status(
            run_id,
            status="succeeded",
            stage="udmi_fixture_validation_complete",
            progress_percent=100,
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
            stage="udmi_fixture_validation_failed",
            progress_percent=100,
            error_message=str(error),
        )
