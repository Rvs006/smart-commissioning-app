"""Server-Sent Events (SSE) run-progress streaming.

Route:
  * GET /api/v1/runs/{run_id}/events  — a ``text/event-stream`` that emits the
    run's status/stage/progress as SSE ``data:`` JSON events, polling the run
    store on a short server-side interval and CLOSING the stream once the run
    reaches a terminal status (succeeded/failed/cancelled). A final event is
    sent, then the generator stops.

This is the SSE-first job-progress channel the production architecture calls
for. The frontend keeps the proven 1.5s polling as a fallback when SSE is
unavailable or errors (see frontend/src/api/client.ts + ModulePage/Dashboard).

Design notes / honesty:
  * No Redis/broker/worker is required. The stream polls the SAME run store
    (DbRunStore via RunService) the polling endpoints read, so it works against
    a plain SQLite database in inline mode. Long-running streams that follow a
    real queued worker over the network are the only thing not exercised by the
    in-process tests; see ``live_untested`` in the task report.
  * Auth: this route is mounted on the parent protected router
    (app.api.router), so it is behind the SAME ``require_auth`` as every other
    /api/v1 route. The browser ``EventSource`` API cannot attach custom headers,
    so the frontend consumes this stream via ``fetch()`` + a ReadableStream
    reader, which DOES send ``X-API-Key`` through the existing ``withApiKey``
    path. We deliberately do NOT accept the key as a query parameter (keys in
    URLs get logged). In local/loopback mode no key is needed at all.
  * Cancel-safety: the generator catches ``asyncio.CancelledError`` (raised when
    the client disconnects) and exits cleanly so no generator is leaked. A hard
    wall-clock cap (``MAX_STREAM_SECONDS``) bounds the lifetime even if a run
    never reaches a terminal status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.rbac import Role
from starlette.responses import StreamingResponse

from app.core.auth import AuthPrincipal, require_role
from app.core.scopes import load_scoped_run
from app.schemas.jobs import JobStatus, RunRecord
from app.services.run_service import RunService

logger = logging.getLogger(__name__)

router = APIRouter()
service = RunService()
lifecycle_repository = RunLifecycleRepository(service.engine)
require_viewer = require_role(Role.VIEWER)

# Terminal statuses close the stream. Kept in lockstep with the frontend
# runFormat.terminalStatuses and the JobStatus literal.
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({"succeeded", "failed", "cancelled"})

# Server-side poll cadence. Short (1s) so live progress is responsive, but
# coarser than the 1.5s client fallback would be on its own.
POLL_INTERVAL_SECONDS = 1.0

# Hard wall-clock cap on a single stream so a run that never terminates (or a
# wedged client) cannot leak a generator forever.
MAX_STREAM_SECONDS = 600.0  # 10 minutes


def _format_sse(payload: dict[str, object], *, event: str | None = None) -> str:
    """Serialize one SSE message. ``data:`` carries compact JSON."""
    lines = []
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'), default=str)}")
    # SSE frames are terminated by a blank line.
    return "\n".join(lines) + "\n\n"


def _progress_payload(
    run: RunRecord,
    *,
    scoped_project_id: str | None = None,
    scoped_site_id: str | None = None,
) -> dict[str, object]:
    """The status/stage/progress slice the frontend live-updates from."""
    payload: dict[str, object] = {
        "run_id": run.run_id,
        "job_type": run.job_type,
        "status": run.status,
        "stage": run.stage,
        "progress_percent": run.progress_percent,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "error_message": run.error_message,
    }
    if run.job_type in {"ip_discovery", "bacnet_discovery"}:
        attempt = service.get_run_attempt_read_only(run.run_id)
        if attempt < 1:
            payload.update(
                {
                    "observation_attempt": None,
                    "latest_observation_cursor": 0,
                    "progressive_counts": {"observations": 0},
                }
            )
            return payload
        cutoff = lifecycle_repository.get_discovery_observation_cutoff(
            run.run_id,
            attempt,
            project_id=scoped_project_id,
            site_id=scoped_site_id,
        )
        terminal = service.get_terminal_observation_marker_read_only(run.run_id)
        payload.update(
            {
                "observation_attempt": cutoff.attempt,
                "latest_observation_cursor": (
                    int(terminal["terminal_cursor"]) if terminal is not None else cutoff.terminal_cursor
                ),
                "progressive_counts": {
                    "observations": (
                        int(terminal["observation_count"]) if terminal is not None else cutoff.observation_count
                    )
                },
            }
        )
    return payload


async def _run_event_stream(
    run_id: str,
    principal: AuthPrincipal,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``run_id`` until it is terminal or the cap is hit.

    The current run state is fetched once at open (the route already 404s if it
    is missing), then re-polled every ``POLL_INTERVAL_SECONDS``. Only changed
    states (or the first observation) are emitted to keep the channel quiet,
    and a final frame is always sent for a terminal run before closing.
    """
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    last_serialized: str | None = None
    try:
        while True:
            try:
                # Scope is mutable control state. Recheck it on EVERY database
                # poll so user deactivation, role demotion, or grant revocation
                # closes an already-open stream before another progress read.
                scoped = await asyncio.to_thread(
                    load_scoped_run,
                    run_id,
                    principal,
                    engine=service.engine,
                    query_only=True,
                )
                # Keep the synchronous query-only read off the event loop so a
                # busy database cannot delay Stop or other async request handling.
                run = await asyncio.to_thread(service.get_run_read_only, run_id)
            except HTTPException:
                # Missing, purged, foreign, and newly revoked all close through
                # one indistinguishable marker. Never keep polling after access
                # control denies the captured principal.
                yield _format_sse({"run_id": run_id, "status": "closed"}, event="closed")
                return
            except FileNotFoundError:
                yield _format_sse({"run_id": run_id, "status": "closed"}, event="closed")
                return
            except Exception:  # noqa: BLE001 - control-store loss must close, not grant access
                logger.warning("SSE run/control poll failed for %s; closing", run_id, exc_info=True)
                yield _format_sse(
                    {"run_id": run_id, "status": "unavailable"},
                    event="unavailable",
                )
                return

            try:
                payload = await asyncio.to_thread(
                    _progress_payload,
                    run,
                    scoped_project_id=scoped.project_id,
                    scoped_site_id=scoped.site_id,
                )
            except Exception:  # noqa: BLE001 - cursor/control reads also fail closed
                logger.warning(
                    "SSE observation/control poll failed for %s; closing",
                    run_id,
                    exc_info=True,
                )
                yield _format_sse(
                    {"run_id": run_id, "status": "unavailable"},
                    event="unavailable",
                )
                return
            serialized = json.dumps(payload, sort_keys=True, default=str)
            if serialized != last_serialized:
                last_serialized = serialized
                yield _format_sse(payload)

            if run.status in TERMINAL_STATUSES:
                # Explicit terminal marker so the client can stop reading even
                # if it dedupes identical progress frames.
                yield _format_sse(payload, event="terminal")
                return

            if time.monotonic() >= deadline:
                # Cap reached: emit a timeout marker and close cleanly so the
                # frontend can decide to fall back to polling.
                yield _format_sse(payload, event="timeout")
                return

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        # Client disconnected: exit quietly so the generator is not leaked.
        # Re-raising is unnecessary (and noisy) for a streaming generator.
        return


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    principal: AuthPrincipal = Depends(require_viewer),
) -> StreamingResponse:
    """Stream a run's progress as Server-Sent Events.

    404 if the run does not exist at open. Otherwise returns a
    ``text/event-stream`` that emits status/stage/progress and closes on the
    first terminal status (or the wall-clock cap). Auth is enforced by the
    parent protected router (app.api.router).
    """
    try:
        await asyncio.to_thread(
            load_scoped_run,
            run_id,
            principal,
            engine=service.engine,
            query_only=True,
        )
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - fail closed if control state cannot be read
        logger.warning("SSE open control check failed for %s", run_id, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Run state is temporarily unavailable.",
        ) from error

    headers = {
        "Cache-Control": "no-store",
        # Disable proxy buffering (nginx) so frames flush immediately.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _run_event_stream(run_id, principal),
        media_type="text/event-stream",
        headers=headers,
    )
