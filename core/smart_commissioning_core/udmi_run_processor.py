import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from smart_commissioning_core.capture_provenance import capture_acceptance_eligible
from smart_commissioning_core.engines.base import make_cancel_checker
from smart_commissioning_core.mqtt_settings import parse_bool, parse_capture_seconds
from smart_commissioning_core.mqtt_transport import subscribe_and_capture_with_outcome
from smart_commissioning_core.run_store import RunStore
from smart_commissioning_core.udmi_validation import (
    DEFAULT_CAPTURE_SECONDS,
    LiveCapture,
    UdmiValidationResult,
    validate_udmi_full_report,
)

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
_PROGRESS_PERSIST_INTERVAL_SECONDS = 1.0
_PROGRESS_WORK_BACKOFF_MULTIPLIER = 4.0
_PROGRESS_SHUTDOWN_TIMEOUT_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)


class _CoalescedProgressWriter:
    """Persist provisional validation snapshots away from the MQTT reader.

    A large register can take several seconds to rebuild and replace its live
    result rows. Doing that work in the MQTT ``on_message`` callback stops the
    socket reader for the whole rebuild, so the capture deadline can expire
    while broker messages are still queued. This single background writer keeps
    only the newest pending snapshot factory. It also backs off in proportion to
    the previous write cost so a large run cannot spend nearly all of its time
    rebuilding provisional results.

    The terminal snapshot is still written synchronously by the caller after
    :meth:`close`, so a provisional writer can never overwrite final evidence.
    """

    def __init__(
        self,
        write: Callable[[Callable[[], UdmiValidationResult]], bool],
        *,
        interval_seconds: float,
        thread_name: str,
    ) -> None:
        self._write = write
        self._interval_seconds = max(0.0, interval_seconds)
        self._condition = threading.Condition()
        self._pending: Callable[[], UdmiValidationResult] | None = None
        self._closing = False
        self._drain_on_close = False
        self._submitted = 0
        self._attempted = 0
        self._persisted = 0
        self._coalesced = 0
        self._shutdown_timed_out = False
        self._detached = False
        self._late_completion: Callable[[], None] | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    def submit(self, factory: Callable[[], UdmiValidationResult]) -> None:
        """Queue the latest progress view and return without building it."""

        with self._condition:
            if self._closing:
                return
            self._submitted += 1
            if self._pending is not None:
                self._coalesced += 1
            self._pending = factory
            self._condition.notify_all()

    def close(
        self,
        *,
        drain: bool,
        timeout_seconds: float | None = None,
        late_completion: Callable[[], None] | None = None,
    ) -> bool:
        """Request shutdown without letting a stuck progress write block a run.

        Returns ``True`` when the worker stopped inside the deadline. When an
        in-flight build or store call remains blocked, the daemon is detached
        and ``late_completion`` runs after it eventually returns. Successful
        validation uses that hook to restore terminal evidence if a stale
        provisional write completed after the terminal write.
        """

        with self._condition:
            if self._closing:
                thread = self._thread
            else:
                self._closing = True
                self._drain_on_close = drain
                if not drain and self._pending is not None:
                    self._coalesced += 1
                    self._pending = None
                self._condition.notify_all()
                thread = self._thread
        shutdown_timeout = (
            _PROGRESS_SHUTDOWN_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        thread.join(timeout=max(0.0, shutdown_timeout))
        if not thread.is_alive():
            return True
        with self._condition:
            self._shutdown_timed_out = True
            self._detached = True
            self._late_completion = late_completion
        _LOGGER.warning(
            "UDMI progress writer exceeded its shutdown deadline; "
            "terminal processing continues."
        )
        return False

    def stats(self) -> dict[str, bool | int | str]:
        with self._condition:
            return {
                "mode": "coalesced_background",
                "submitted": self._submitted,
                "attempted": self._attempted,
                "persisted": self._persisted,
                "coalesced": self._coalesced,
                "shutdown_timed_out": self._shutdown_timed_out,
            }

    def _run(self) -> None:
        next_allowed_at = 0.0
        while True:
            with self._condition:
                while self._pending is None and not self._closing:
                    self._condition.wait()
                if self._closing and (
                    not self._drain_on_close or self._pending is None
                ):
                    return

                delay = next_allowed_at - time.monotonic()
                if delay > 0 and not (self._closing and self._drain_on_close):
                    # New submissions replace ``_pending`` while this wait is in
                    # progress. When the delay elapses, only the freshest view is
                    # built and written.
                    self._condition.wait(timeout=delay)
                    continue
                factory = self._pending
                self._pending = None

            if factory is None:  # Defensive; the condition lock owns this state.
                continue
            started_at = time.monotonic()
            persisted = False
            try:
                persisted = self._write(factory)
            except Exception as error:  # pragma: no cover - write already guards
                _LOGGER.warning(
                    "UDMI progress writer failed (%s); capture continues.",
                    type(error).__name__,
                )
            completed_at = time.monotonic()
            write_seconds = max(0.0, completed_at - started_at)
            with self._condition:
                self._attempted += 1
                if persisted:
                    self._persisted += 1
                late_completion = (
                    self._late_completion
                    if (
                        self._detached
                        and not (self._drain_on_close and self._pending is not None)
                    )
                    else None
                )
                if late_completion is not None:
                    self._late_completion = None
            if late_completion is not None:
                try:
                    late_completion()
                except Exception as error:  # pragma: no cover - store-specific
                    _LOGGER.warning(
                        "UDMI terminal snapshot repair failed (%s).",
                        type(error).__name__,
                    )
            next_allowed_at = completed_at + max(
                self._interval_seconds,
                write_seconds * _PROGRESS_WORK_BACKOFF_MULTIPLIER,
            )


def process_udmi_validation_run(
    run_id: str,
    parameters: dict[str, object],
    *,
    run_store: RunStore,
    execution_mode: str,
    fallback_reason: str | None = None,
    live_capture: LiveCapture | None = subscribe_and_capture_with_outcome,
    run_is_backgrounded: bool = True,
) -> Any:
    run_store.update_run_status(
        run_id,
        status="running",
        stage="loading_udmi_fixture",
        progress_percent=15,
    )

    parameters = dict(parameters)

    # Cooperative stop: Stop run button -> POST /runs/{id}/cancel -> DB flag,
    # observed by the capture loop via this checker. Only claim a cancel path
    # when the store actually advertises one — the engine bounds an indefinite
    # capture itself (udmi_validation._capture_window) when cancel_check is None,
    # so a run can never wait forever with no way to stop it.
    cancel_check = (
        make_cancel_checker(run_store, run_id)
        if callable(getattr(run_store, "is_cancel_requested", None))
        else None
    )
    # A blank/0 Run time is honoured as a true indefinite capture only when the
    # operator can actually reach Stop run: a cancel path is wired AND the run is
    # BACKGROUNDED (worker, or async inline). It is bounded to the default window
    # when either fails. The no-cancel-path case is bounded by
    # udmi_validation._capture_window itself (which also labels capture_mode). The
    # NEW case is a run WITH a cancel path that is NOT backgrounded — a synchronous
    # inline run blocks the HTTP request until it finishes, so the client never
    # receives a run_id and cannot stop it; _capture_window would otherwise honour
    # it as indefinite, so coerce the window here. Bounding capture_seconds (not
    # just the summary flag) is what keeps a silent broker from holding the request
    # thread + broker socket up to the 48h backstop.
    indefinite_requested = (
        parse_capture_seconds(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS) is None
    )
    if indefinite_requested and cancel_check is not None and not run_is_backgrounded:
        parameters["capture_seconds"] = DEFAULT_CAPTURE_SECONDS
    indefinite_bounded_inline = indefinite_requested and (cancel_check is None or not run_is_backgrounded)
    try:
        if parse_bool(parameters.get("use_live_broker")):
            run_store.update_run_status(
                run_id,
                status="running",
                stage="capturing_live_mqtt",
                progress_percent=25,
            )

        def persist_progress_snapshot(
            progress_factory: Callable[[], UdmiValidationResult],
        ) -> bool:
            try:
                progress_result = progress_factory()
                progress_summary = {
                    **progress_result.result_summary,
                    "execution_mode": execution_mode,
                    "worker_required": execution_mode != "inline_local_fallback",
                    "indefinite_bounded_inline": indefinite_bounded_inline,
                }
                if fallback_reason:
                    progress_summary["fallback_reason"] = fallback_reason
                run_store.update_result_summary(run_id, progress_summary, merge=False)
                run_store.replace_issues(run_id, progress_result.issues)
                return True
            except Exception as error:
                # A progress write is observability, not validation evidence
                # collection. Keep the MQTT capture alive and avoid logging the
                # exception text, which may contain database or field data.
                _LOGGER.warning(
                    "UDMI progress snapshot update failed (%s); capture continues.",
                    type(error).__name__,
                )
                return False

        progress_writer = _CoalescedProgressWriter(
            persist_progress_snapshot,
            interval_seconds=_PROGRESS_PERSIST_INTERVAL_SECONDS,
            thread_name=f"udmi-progress-{run_id[-12:]}",
        )
        terminal_summary: dict[str, object] | None = None
        terminal_issues: list[Any] | None = None
        failure_terminal_summary: dict[str, object] = {
            "execution_mode": execution_mode,
            "worker_required": execution_mode != "inline_local_fallback",
            "indefinite_bounded_inline": indefinite_bounded_inline,
            "provisional": False,
            "validation_incomplete": True,
        }
        if fallback_reason:
            failure_terminal_summary["fallback_reason"] = fallback_reason

        def repair_terminal_snapshot_after_late_progress() -> None:
            """Make a detached stale progress write self-healing.

            Lifecycle-v2 and database stores fence writes after terminal status.
            Simple/in-memory stores do not necessarily do so, therefore a late
            writer repeats the already-computed terminal evidence once its stale
            call returns. This hook never delays the terminal path.
            """

            if terminal_summary is None or terminal_issues is None:
                # The progress call returned before the caller began its terminal
                # writes, so those writes will naturally win the ordering race.
                return
            run_store.update_result_summary(run_id, terminal_summary, merge=False)
            run_store.replace_issues(run_id, terminal_issues)

        def repair_failure_snapshot_after_late_progress() -> None:
            # Preserve the drained partial evidence while making it unambiguous
            # that the run itself has reached a failed terminal outcome.
            run_store.update_result_summary(
                run_id,
                failure_terminal_summary,
                merge=True,
            )

        try:
            validation_result = validate_udmi_full_report(
                parameters,
                live_capture=live_capture,
                cancel_check=cancel_check,
                progress_callback=progress_writer.submit,
            )
        except Exception:
            # Preserve the freshest partial evidence when validation itself
            # fails, but never let a stuck report build or database call prevent
            # the run from reaching its failed terminal state.
            progress_writer.close(
                drain=True,
                late_completion=repair_failure_snapshot_after_late_progress,
            )
            raise
        # A successful terminal result supersedes any queued provisional view.
        # Wait only to a firm deadline. If a store call is stuck, the late repair
        # hook restores this terminal snapshot after that stale call returns.
        progress_writer.close(
            drain=False,
            late_completion=repair_terminal_snapshot_after_late_progress,
        )
        result_summary = {
            **validation_result.result_summary,
            "execution_mode": execution_mode,
            "worker_required": execution_mode != "inline_local_fallback",
            "indefinite_bounded_inline": indefinite_bounded_inline,
            "progress_snapshot_metrics": progress_writer.stats(),
        }
        if fallback_reason:
            result_summary["fallback_reason"] = fallback_reason

        cancel_observed = cancel_check is not None and cancel_check()
        if cancel_observed:
            result_summary["validation_incomplete"] = True

        broker_status_detail = str(
            result_summary.get("broker_status_detail") or "capture_failed"
        )
        termination_reason = str(result_summary.get("termination_reason") or "")
        capture_failed = (
            result_summary.get("broker_capture_attempted")
            and (
                broker_status_detail not in _CAPTURE_COMPLETED_STATUSES
                or termination_reason
                in {
                    "broker_interruption",
                    "message_cap",
                    "byte_cap",
                    "backstop_elapsed",
                    "capture_outcome_unavailable",
                }
                or (
                    result_summary.get("capture_mode") == "bounded"
                    and not (
                        result_summary.get("window_completed") is True
                        and termination_reason == "window_elapsed"
                    )
                )
            )
        )
        terminal_status = (
            "cancelled" if cancel_observed else "failed" if capture_failed else "succeeded"
        )
        result_summary["acceptance_eligible"] = capture_acceptance_eligible(
            terminal_status,
            result_summary,
        )

        terminal_summary = result_summary
        terminal_issues = list(validation_result.issues)
        run_store.update_result_summary(run_id, result_summary, merge=False)
        run_store.replace_issues(run_id, terminal_issues)
        if not cancel_observed and cancel_check is not None and cancel_check():
            cancel_observed = True
            result_summary["validation_incomplete"] = True
            result_summary["acceptance_eligible"] = False
            run_store.update_result_summary(run_id, result_summary, merge=False)
        if cancel_observed:
            # Cancel observed during the run: keep the real partial results but
            # finish under a cancelled status — the cancel route only sets the
            # flag; the observing engine flips the terminal status.
            return run_store.update_run_status(
                run_id,
                status="cancelled",
                stage="udmi_validation_cancelled",
                progress_percent=100,
            )
        if capture_failed:
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
                "indefinite_bounded_inline": indefinite_bounded_inline,
                "provisional": False,
                "validation_incomplete": True,
                **(
                    {"fallback_reason": fallback_reason}
                    if fallback_reason
                    else {}
                ),
            },
        )
        return run_store.update_run_status(
            run_id,
            status="failed",
            stage="udmi_fixture_validation_failed",
            progress_percent=100,
            error_message=_SANITIZED_FAILURE_MESSAGE,
        )
