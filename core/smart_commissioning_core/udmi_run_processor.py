from typing import Any

from smart_commissioning_core.engines.base import make_cancel_checker
from smart_commissioning_core.mqtt_settings import parse_capture_seconds
from smart_commissioning_core.mqtt_transport import subscribe_and_capture
from smart_commissioning_core.run_store import RunStore
from smart_commissioning_core.udmi_validation import (
    DEFAULT_CAPTURE_SECONDS,
    LiveCapture,
    validate_udmi_full_report,
)

# ponytail: inline safety ceiling for an indefinite capture. On the queued
# worker, "blank = run until every expected topic reports or Cancel" is
# honoured as a true indefinite capture. On the inline path the run executes
# INSIDE the API request thread and the frontend only learns the run_id when
# that request returns — so the Cancel button never renders while an inline
# run is in flight. An indefinite request there is therefore bounded to this
# ceiling (generous vs the 20s register reporting interval; the all-topics-seen
# stop condition usually ends the run far earlier) and the downgrade is
# recorded as indefinite_bounded_inline in the result summary.
INLINE_INDEFINITE_CEILING_SECONDS = 240.0
_SANITIZED_FAILURE_MESSAGE = "UDMI validation failed; see server logs."

# Capture outcomes where the transport did its WHOLE job: connect, subscribe,
# and run the window to completion. A silent or non-conforming device inside a
# completed window is a RESULT (not_publishing / payload_error issues, red
# rows on the Results step) — never a run failure (field ask 2026-07-15: "it
# can't fail the whole validation just because one device isn't responding").
# Everything else — broker_unreachable / tls_error / authentication_error /
# broker_timeout / live_capture_unavailable / missing_capture_topics / blank —
# stays `failed`: if we never reached the broker we cannot claim we validated
# anything (honesty rule), and an UNKNOWN future status defaults to failed for
# the same reason.
_CAPTURE_COMPLETED_STATUSES = frozenset({"live_payloads_captured", "live_capture_timeout"})
_SILENT_DEVICE_STAGE = "udmi_validation_complete_with_silent_devices"


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

    parameters = dict(parameters)
    indefinite_requested = (
        parse_capture_seconds(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS) is None
    )
    indefinite_bounded_inline = indefinite_requested and execution_mode != "dramatiq_worker"
    if indefinite_bounded_inline:
        parameters["capture_seconds"] = INLINE_INDEFINITE_CEILING_SECONDS

    # Cooperative stop: Cancel run button -> POST /runs/{id}/cancel -> DB flag,
    # observed by the capture loop via this checker. Only claim a cancel path
    # when the store actually advertises one — the engine bounds an indefinite
    # capture itself when cancel_check is None, so a run can never wait forever
    # with no way to stop it.
    cancel_check = (
        make_cancel_checker(run_store, run_id)
        if callable(getattr(run_store, "is_cancel_requested", None))
        else None
    )

    try:
        validation_result = validate_udmi_full_report(parameters, live_capture=live_capture, cancel_check=cancel_check)
        result_summary = {
            **validation_result.result_summary,
            "execution_mode": execution_mode,
            "worker_required": execution_mode != "inline_local_fallback",
            "indefinite_bounded_inline": indefinite_bounded_inline,
        }
        if fallback_reason:
            result_summary["fallback_reason"] = fallback_reason

        run_store.update_result_summary(run_id, result_summary, merge=False)
        run_store.replace_issues(run_id, validation_result.issues)
        if cancel_check is not None and cancel_check():
            # Cancel observed during the run: keep the real partial results but
            # finish under a cancelled status — the cancel route only sets the
            # flag; the observing engine flips the terminal status.
            return run_store.update_run_status(
                run_id,
                status="cancelled",
                stage="udmi_validation_cancelled",
                progress_percent=100,
            )
        broker_status_detail = str(result_summary.get("broker_status_detail") or "capture_failed")
        if (
            result_summary.get("broker_capture_attempted")
            and broker_status_detail not in _CAPTURE_COMPLETED_STATUSES
        ):
            return run_store.update_run_status(
                run_id,
                status="failed",
                stage="udmi_fixture_validation_failed",
                progress_percent=100,
                error_message=(
                    f"Live MQTT capture did not complete ({broker_status_detail}); "
                    "see validation issues."
                ),
            )
        completed_with_silent_devices = (
            bool(result_summary.get("broker_capture_attempted"))
            and broker_status_detail == "live_capture_timeout"
        )
        return run_store.update_run_status(
            run_id,
            status="succeeded",
            stage=_SILENT_DEVICE_STAGE if completed_with_silent_devices else "udmi_fixture_validation_complete",
            progress_percent=100,
        )
    except Exception:
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
            error_message=_SANITIZED_FAILURE_MESSAGE,
        )
