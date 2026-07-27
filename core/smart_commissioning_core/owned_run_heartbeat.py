"""Shared lease-heartbeat policy and guard for every owned run executor."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from smart_commissioning_core.owned_run_store import OwnedRunStore

logger = logging.getLogger(__name__)

MIN_RUN_LEASE_SECONDS = 15
MAX_RUN_LEASE_SECONDS = 300
MIN_RUN_HEARTBEAT_SECONDS = 1.0

OWNED_CONTEXT_FAILURE_STAGE = "execution_context_unavailable"
OWNED_CONTEXT_FAILURE_MESSAGE = "Frozen execution inputs could not be resolved."
OWNED_EXECUTOR_FAILURE_STAGE = "executor_unhandled_failure"
OWNED_EXECUTOR_FAILURE_MESSAGE = (
    "This run stopped unexpectedly before it could finish, so no results were "
    "saved. Please run it again."
)
OWNED_EXECUTOR_MISSING_TERMINAL_STAGE = "executor_missing_terminal_result"
OWNED_EXECUTOR_MISSING_TERMINAL_MESSAGE = (
    "The execution processor returned without a terminal result. Please run it again."
)
OWNED_HEARTBEAT_START_FAILURE_STAGE = "owned_heartbeat_start_failed"


@dataclass(frozen=True)
class OwnedRunHeartbeatPolicy:
    """Production timing policy shared by API and worker configuration."""

    lease_seconds: int = 60
    interval_seconds: float = 15.0

    def __post_init__(self) -> None:
        lease_seconds = int(self.lease_seconds)
        interval_seconds = float(self.interval_seconds)
        if lease_seconds < MIN_RUN_LEASE_SECONDS:
            raise ValueError(
                f"run_lease_seconds must be at least {MIN_RUN_LEASE_SECONDS}"
            )
        if lease_seconds > MAX_RUN_LEASE_SECONDS:
            raise ValueError(
                f"run_lease_seconds must be no more than {MAX_RUN_LEASE_SECONDS}"
            )
        if not math.isfinite(interval_seconds):
            raise ValueError("run_heartbeat_seconds must be finite")
        if interval_seconds < MIN_RUN_HEARTBEAT_SECONDS:
            raise ValueError(
                f"run_heartbeat_seconds must be at least {MIN_RUN_HEARTBEAT_SECONDS:g}"
            )
        if interval_seconds * 3 > lease_seconds:
            raise ValueError(
                "run_heartbeat_seconds must be no more than one third of "
                "run_lease_seconds"
            )
        object.__setattr__(self, "lease_seconds", lease_seconds)
        object.__setattr__(self, "interval_seconds", interval_seconds)


DEFAULT_OWNED_RUN_HEARTBEAT_POLICY = OwnedRunHeartbeatPolicy()

_active_lock = threading.Lock()
_active_heartbeats: set[OwnedRunHeartbeat] = set()


class OwnedRunHeartbeat:
    """Renew one fenced owner's lease until terminal finalization completes."""

    def __init__(
        self,
        owned_store: OwnedRunStore,
        *,
        lease_seconds: int = 60,
        interval_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
        executor_label: str = "owned-run",
        thread_name_prefix: str = "owned-run-heartbeat",
    ) -> None:
        lease_seconds = int(lease_seconds)
        interval_seconds = float(interval_seconds)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one second")
        if lease_seconds > MAX_RUN_LEASE_SECONDS:
            raise ValueError(
                f"lease_seconds must be no more than {MAX_RUN_LEASE_SECONDS} seconds"
            )
        if (
            not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or interval_seconds * 3 > lease_seconds
        ):
            raise ValueError(
                "interval_seconds must be positive and no more than one third of "
                "lease_seconds"
            )
        self._owned_store = owned_store
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._executor_label = str(executor_label)
        self._thread_name_prefix = str(thread_name_prefix)
        self._stop = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_success = monotonic()

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("owned-run heartbeat already started")
        run_id = self._owned_store.lease.run_id
        self._thread = threading.Thread(
            target=self._run,
            name=f"{self._thread_name_prefix}-{run_id}",
            daemon=True,
        )
        with _active_lock:
            _active_heartbeats.add(self)
        try:
            self._thread.start()
        except Exception:
            with _active_lock:
                _active_heartbeats.discard(self)
            self._thread = None
            raise
        logger.info(
            "owned-run heartbeat started for run_id=%s executor=%s",
            run_id,
            self._executor_label,
        )

    def stop_and_join(self) -> bool:
        """Stop promptly and report whether the renewal thread fully exited."""

        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            # SQLite's configured busy timeout is five seconds. Allow a margin
            # beyond it so a stopped heartbeat is not left behind at finalization.
            thread.join(timeout=max(2.0, min(float(self._lease_seconds), 7.0)))
        stopped = not thread.is_alive()
        run_id = self._owned_store.lease.run_id
        if stopped:
            with _active_lock:
                _active_heartbeats.discard(self)
            logger.info(
                "owned-run heartbeat stopped for run_id=%s executor=%s",
                run_id,
                self._executor_label,
            )
        else:
            logger.error(
                "owned-run heartbeat did not stop promptly for run_id=%s executor=%s",
                run_id,
                self._executor_label,
            )
        return stopped

    def abandon_and_join(self) -> bool:
        """Fence a local executor during process shutdown, then stop its beat."""

        if self._owned_store.terminal_outcome is None:
            self._owned_store.mark_ownership_lost()
            self._ownership_lost.set()
        return self.stop_and_join()

    def _run(self) -> None:
        run_id = self._owned_store.lease.run_id
        delay = self._interval_seconds
        consecutive_failures = 0
        warned_low_window = False
        try:
            while not self._stop.wait(delay):
                try:
                    renewed = self._owned_store.heartbeat(
                        lease_seconds=self._lease_seconds
                    )
                except Exception as error:
                    consecutive_failures += 1
                    remaining = max(
                        0.0,
                        self._lease_seconds
                        - (self._monotonic() - self._last_success),
                    )
                    if consecutive_failures == 1:
                        # Do not include an exception message or traceback. A
                        # connection error may carry credentials or local paths.
                        logger.warning(
                            "owned-run heartbeat refresh failed for run_id=%s "
                            "executor=%s exception_type=%s; retrying",
                            run_id,
                            self._executor_label,
                            type(error).__name__,
                        )
                    if remaining <= self._interval_seconds and not warned_low_window:
                        warned_low_window = True
                        logger.warning(
                            "owned-run heartbeat renewal window is low for run_id=%s "
                            "executor=%s; continuing bounded retries",
                            run_id,
                            self._executor_label,
                        )
                    delay = self._retry_delay(remaining)
                    continue

                if self._stop.is_set():
                    break
                if not renewed:
                    outcome = self._owned_store.terminal_outcome
                    if outcome is not None and not outcome.conflict:
                        break
                    self._owned_store.mark_ownership_lost()
                    self._ownership_lost.set()
                    logger.warning(
                        "owned-run heartbeat confirmed ownership loss for run_id=%s "
                        "executor=%s; the stale executor is fenced from further writes",
                        run_id,
                        self._executor_label,
                    )
                    break

                self._last_success = self._monotonic()
                if consecutive_failures:
                    logger.info(
                        "owned-run heartbeat recovered for run_id=%s executor=%s "
                        "after %d failed attempt(s)",
                        run_id,
                        self._executor_label,
                        consecutive_failures,
                    )
                consecutive_failures = 0
                warned_low_window = False
                delay = self._interval_seconds
        finally:
            with _active_lock:
                _active_heartbeats.discard(self)

    def _retry_delay(self, remaining_seconds: float) -> float:
        if remaining_seconds <= 0:
            return max(1.0, min(self._interval_seconds, 5.0))
        return min(
            self._interval_seconds,
            max(0.05, min(1.0, remaining_seconds / 2.0)),
        )


def abandon_active_owned_run_heartbeats() -> int:
    """Fence and stop every active heartbeat in the current process."""

    with _active_lock:
        active = tuple(_active_heartbeats)
    for heartbeat in active:
        heartbeat.abandon_and_join()
    return len(active)
