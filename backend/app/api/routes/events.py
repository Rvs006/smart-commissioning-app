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
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from app.schemas.jobs import JobStatus, RunRecord
from app.services.run_service import RunService

router = APIRouter()
service = RunService()

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


def _progress_payload(run: RunRecord) -> dict[str, object]:
    """The status/stage/progress slice the frontend live-updates from."""
    return {
        "run_id": run.run_id,
        "job_type": run.job_type,
        "status": run.status,
        "stage": run.stage,
        "progress_percent": run.progress_percent,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "error_message": run.error_message,
    }


async def _run_event_stream(run_id: str) -> AsyncIterator[str]:
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
                run = service.get_run(run_id)
            except FileNotFoundError:
                # The run vanished mid-stream (e.g. retention purge). Tell the
                # client and stop rather than spinning.
                yield _format_sse({"run_id": run_id, "status": "gone"}, event="gone")
                return

            payload = _progress_payload(run)
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
async def stream_run_events(run_id: str) -> StreamingResponse:
    """Stream a run's progress as Server-Sent Events.

    404 if the run does not exist at open. Otherwise returns a
    ``text/event-stream`` that emits status/stage/progress and closes on the
    first terminal status (or the wall-clock cap). Auth is enforced by the
    parent protected router (app.api.router).
    """
    try:
        service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error

    headers = {
        "Cache-Control": "no-cache",
        # Disable proxy buffering (nginx) so frames flush immediately.
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _run_event_stream(run_id),
        media_type="text/event-stream",
        headers=headers,
    )
