"""In-process lease for the single MQTT live broker session (M4a).

The vendored ``mqtt-discovery`` sidecar holds ONE broker connection in
process-global memory (its ``/api/connect`` resets all state). This service
mirrors that single-tenancy with one in-process lease so SCT can admit exactly
one live session at a time, arbitrate it against a bounded capture run (both
lanes drive the same sidecar), and reclaim it when the operator walks away.

Nothing is persisted: open / close / takeover / reap are log lines. The lease is
SCT's admission policy only; the sidecar's own ``connection.status`` is always
the truth. On backend shutdown the lifespan kills the sidecar
(``supervisor.stop_all``), so the broker socket dies with the process and there
is no lifespan hook to run here.

# ponytail: process-global single session + a daemon reaper thread. Per-project
# sessions only if the sidecar ever grows past single-tenancy.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def _noop() -> None:
    return None


@dataclass
class LiveSession:
    session_id: str
    owner: str
    project_id: str
    site_id: str
    since: datetime
    acquired_mono: float
    last_stream_mono: float
    attached_streams: int = 0
    # Best-effort sidecar disconnect, bound to the sidecar base_url at acquire.
    disconnect: Callable[[], None] = field(default=_noop)


class MqttLiveSessionService:
    """One live session, guarded by one non-reentrant lock."""

    IDLE_GRACE_SECONDS = 120.0
    ABSOLUTE_CAP_SECONDS = 14_400.0  # 4h hard ceiling regardless of activity
    REAPER_INTERVAL_SECONDS = 15.0
    MAX_ATTACHED_STREAMS = 5

    def __init__(self) -> None:
        # PUBLIC: the create-run guard in scanners.py serialises on this same lock
        # so a live session and a capture run can never both start.
        self.lock = threading.Lock()
        self._session: LiveSession | None = None
        self._reaper: threading.Thread | None = None

    # -- reads ----------------------------------------------------------------

    def current(self) -> LiveSession | None:
        with self.lock:
            return self._session

    def current_locked(self) -> LiveSession | None:
        """Read the session when the caller ALREADY holds ``self.lock``.

        The lock is non-reentrant, so the create-run guard (which holds the lock
        to close the run-vs-live race) must not call ``current()``.
        """
        return self._session

    # -- lifecycle ------------------------------------------------------------

    def acquire(
        self,
        *,
        owner: str,
        project_id: str,
        site_id: str,
        disconnect: Callable[[], None],
        take_over: bool,
    ) -> LiveSession:
        """Take the lease. Caller MUST already hold ``self.lock``.

        A takeover just replaces the lease; the caller's subsequent
        ``/api/connect`` tears down the previous broker connection (the sidecar's
        ``doConnect`` resets its single-tenant state), so no disconnect I/O runs
        under the lock here.
        """
        if self._session is not None and take_over:
            logger.info(
                "mqtt live session takeover: %s replacing %s",
                owner,
                self._session.owner,
            )
        now_mono = time.monotonic()
        session = LiveSession(
            session_id=uuid.uuid4().hex,
            owner=owner,
            project_id=project_id,
            site_id=site_id,
            since=datetime.now(UTC),
            acquired_mono=now_mono,
            last_stream_mono=now_mono,
            disconnect=disconnect,
        )
        self._session = session
        self._ensure_reaper()
        logger.info(
            "mqtt live session opened session=%s project=%s site=%s by=%s",
            session.session_id,
            project_id,
            site_id,
            owner,
        )
        return session

    def release(self, session_id: str | None) -> bool:
        """Release the lease (``None`` releases whatever is held). Idempotent.

        The sidecar disconnect runs OUTSIDE the lock so a slow best-effort POST
        never stalls connect / create-run / the relay.
        """
        disconnect: Callable[[], None] | None = None
        released = False
        with self.lock:
            held = self._session
            if held is not None and (session_id is None or held.session_id == session_id):
                disconnect = held.disconnect
                self._session = None
                released = True
                logger.info("mqtt live session closed session=%s by=owner-stop", held.session_id)
        if disconnect is not None:
            _best_effort(disconnect)
        return released

    def reap_if_stale(self, *, now_mono: float | None = None) -> str | None:
        """Close the lease if idle past the grace or past the absolute cap.

        Returns the reason (``"idle"`` / ``"cap"``) when it reaped, else ``None``.
        Injectable clock so the reaper logic is unit-testable without a thread.
        """
        now = time.monotonic() if now_mono is None else now_mono
        disconnect: Callable[[], None] | None = None
        reason: str | None = None
        with self.lock:
            held = self._session
            if held is not None:
                if now - held.acquired_mono > self.ABSOLUTE_CAP_SECONDS:
                    reason = "cap"
                elif held.attached_streams <= 0 and (
                    now - max(held.acquired_mono, held.last_stream_mono) > self.IDLE_GRACE_SECONDS
                ):
                    reason = "idle"
                if reason is not None:
                    disconnect = held.disconnect
                    self._session = None
                    logger.info("mqtt live session reaped session=%s reason=%s", held.session_id, reason)
        if disconnect is not None:
            _best_effort(disconnect)
        return reason

    # -- stream attachment ----------------------------------------------------

    def stream_attach(self, session_id: str) -> bool:
        with self.lock:
            held = self._session
            if held is None or held.session_id != session_id:
                return False
            if held.attached_streams >= self.MAX_ATTACHED_STREAMS:
                return False
            held.attached_streams += 1
            held.last_stream_mono = time.monotonic()
            return True

    def stream_detach(self, session_id: str) -> None:
        with self.lock:
            held = self._session
            if held is not None and held.session_id == session_id:
                held.attached_streams = max(0, held.attached_streams - 1)
                held.last_stream_mono = time.monotonic()

    def touch(self, session_id: str) -> bool:
        with self.lock:
            held = self._session
            if held is None or held.session_id != session_id:
                return False
            held.last_stream_mono = time.monotonic()
            return True

    # -- reaper thread --------------------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        thread = threading.Thread(target=self._reaper_loop, name="mqtt-live-session-reaper", daemon=True)
        self._reaper = thread
        thread.start()

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(self.REAPER_INTERVAL_SECONDS)
            try:
                self.reap_if_stale()
            except Exception:  # noqa: BLE001 - a reaper must never die on one bad tick
                logger.warning("mqtt live session reaper tick failed", exc_info=True)


def _best_effort(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - best-effort cleanup never masks the caller's result
        logger.debug("mqtt live session best-effort disconnect failed", exc_info=True)


# Module-level singleton (mirrors ``service = RunService()`` in discovery.py).
service = MqttLiveSessionService()
