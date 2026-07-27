"""Lease renewal for one claimed inline run.

The portable application executes jobs on daemon threads inside the API process.
Those jobs need the same independent liveness signal as hosted worker jobs:
progress writes and broker traffic are deliberately not treated as ownership
heartbeats.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from smart_commissioning_core.owned_run_store import OwnedRunStore

logger = logging.getLogger(__name__)


class InlineRunHeartbeat:
    """Renew one inline owner's lease until execution has fully finalized."""

    def __init__(
        self,
        owned_store: OwnedRunStore,
        *,
        lease_seconds: int = 60,
        interval_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        lease_seconds = int(lease_seconds)
        interval_seconds = float(interval_seconds)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one second")
        if lease_seconds > 300:
            raise ValueError("lease_seconds must be no more than 300 seconds")
        if (
            not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or interval_seconds >= lease_seconds
        ):
            raise ValueError("interval_seconds must be positive and below lease_seconds")
        self._owned_store = owned_store
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
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
            raise RuntimeError("inline heartbeat already started")
        run_id = self._owned_store.lease.run_id
        self._thread = threading.Thread(
            target=self._run,
            name=f"inline-heartbeat-{run_id}",
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            raise
        logger.info("inline heartbeat started for run_id=%s", run_id)

    def stop_and_join(self) -> None:
        """Stop promptly and prove the renewal thread did not outlive the run."""

        self._stop.set()
        if self._thread is None:
            return
        # SQLite's configured busy timeout is five seconds. Allow a margin past
        # that bound so a stopped heartbeat cannot be left behind at finalization.
        self._thread.join(
            timeout=max(2.0, min(float(self._lease_seconds), 7.0))
        )
        run_id = self._owned_store.lease.run_id
        if self._thread.is_alive():
            logger.error("inline heartbeat did not stop promptly for run_id=%s", run_id)
        else:
            logger.info("inline heartbeat stopped for run_id=%s", run_id)

    def _run(self) -> None:
        run_id = self._owned_store.lease.run_id
        delay = self._interval_seconds
        consecutive_failures = 0
        warned_low_window = False
        while not self._stop.wait(delay):
            try:
                renewed = self._owned_store.heartbeat(
                    lease_seconds=self._lease_seconds
                )
            except Exception as error:
                consecutive_failures += 1
                remaining = max(
                    0.0,
                    self._lease_seconds - (self._monotonic() - self._last_success),
                )
                if consecutive_failures == 1:
                    # Do not include the exception message or traceback. Database
                    # connection errors may carry credentials or local paths.
                    logger.warning(
                        "inline heartbeat refresh failed for run_id=%s "
                        "exception_type=%s; retrying",
                        run_id,
                        type(error).__name__,
                    )
                if remaining <= self._interval_seconds and not warned_low_window:
                    warned_low_window = True
                    logger.warning(
                        "inline heartbeat renewal window is low for run_id=%s; "
                        "continuing bounded retries",
                        run_id,
                    )
                # A brief SQLite lock must get another chance well before the
                # lease boundary. Keep retries bounded to avoid a lock storm.
                delay = self._retry_delay(remaining)
                continue

            if self._stop.is_set():
                break
            if not renewed:
                outcome = self._owned_store.terminal_outcome
                if outcome is not None and not outcome.conflict:
                    # Finalization cleared the lease just before this heartbeat
                    # transaction. That is normal completion, not ownership loss.
                    break
                self._owned_store.mark_ownership_lost()
                self._ownership_lost.set()
                logger.warning(
                    "inline heartbeat confirmed ownership loss for run_id=%s; "
                    "the stale executor is fenced from further writes",
                    run_id,
                )
                break

            self._last_success = self._monotonic()
            if consecutive_failures:
                logger.info(
                    "inline heartbeat recovered for run_id=%s after %d failed attempt(s)",
                    run_id,
                    consecutive_failures,
                )
            consecutive_failures = 0
            warned_low_window = False
            delay = self._interval_seconds

    def _retry_delay(self, remaining_seconds: float) -> float:
        if remaining_seconds <= 0:
            # Once the last known lease window is exhausted, ownership is
            # uncertain until a heartbeat succeeds or returns False. Keep
            # checking, but never turn an outage into a 20-write-per-second
            # pressure loop.
            return max(1.0, min(self._interval_seconds, 5.0))
        return min(
            self._interval_seconds,
            max(0.05, min(1.0, remaining_seconds / 2.0)),
        )
