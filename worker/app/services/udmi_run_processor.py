from app.services.run_store import FileRunStore
from app.services.udmi_validation import validate_udmi_full_report


def process_udmi_validation_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_store: FileRunStore | None = None,
) -> dict[str, object]:
    store = run_store or FileRunStore()
    store.update_status(
        run_id,
        status="running",
        stage="loading_udmi_fixture",
        progress_percent=15,
    )

    try:
        validation_result = validate_udmi_full_report(parameters)
        store.replace_result(
            run_id,
            result_summary={
                **validation_result.result_summary,
                "execution_mode": "dramatiq_worker",
                "worker_required": True,
            },
            issues=validation_result.issues,
        )
        return store.update_status(
            run_id,
            status="succeeded",
            stage="udmi_fixture_validation_complete",
            progress_percent=100,
        )
    except Exception as error:
        store.update_summary(
            run_id,
            {
                "execution_mode": "dramatiq_worker",
                "worker_required": True,
            },
        )
        return store.update_status(
            run_id,
            status="failed",
            stage="udmi_fixture_validation_failed",
            progress_percent=100,
            error_message=str(error),
        )
