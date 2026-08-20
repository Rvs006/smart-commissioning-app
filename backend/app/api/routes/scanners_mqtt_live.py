"""MQTT live session routes (M4a): a held broker connection streamed to the
browser through the sidecar, distinct from the bounded capture run.

The browser never talks to the sidecar. It opens a session here (connect),
watches a proxied SSE stream of the sidecar's live topic tree, and stops it
(disconnect); RBAC + consent are enforced at the route before any sidecar I/O.
Broker host and credentials resolve server-side (``_connect_config`` ->
``build_mqtt_connection_settings``), so the browser sends no secrets.

The single lease (``app.services.mqtt_live_session``) admits one session at a
time and is arbitrated against a capture run under one lock, because both lanes
drive the sidecar's single connection. Nothing persists; the stream relay copies
events.py's cancel-safety, scope-recheck, and honesty vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
from collections.abc import AsyncIterator
from functools import partial

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from smart_commissioning_core.engines.mqtt_scanner_sidecar import (
    _connect_config,
    _get_json,
    _post_disconnect,
    _post_json,
    _root_filter,
)
from starlette.responses import StreamingResponse

from app.api.routes.discovery import _require_legacy_scan_authorization, require_engineer, service
from app.core.auth import AuthPrincipal, get_principal
from app.core.config import get_settings
from app.core.scopes import require_project_site_access
from app.schemas.mqtt_live import (
    MqttLiveConnectRequest,
    MqttLiveConnectResponse,
    MqttLiveDisconnectRequest,
    MqttLiveDisconnectResponse,
    MqttLiveSessionInfo,
    MqttLiveStatusResponse,
)
from app.services.mqtt_live_session import LiveSession
from app.services.mqtt_live_session import service as live_service
from app.services.sidecar_supervisor import MQTT_SCANNER, SidecarUnavailable

logger = logging.getLogger(__name__)

router = APIRouter()

# Wall-clock cap on one browser stream; the frontend reconnects on timeout (same
# value as events.py MAX_STREAM_SECONDS).
MAX_STREAM_SECONDS = 600.0
# Scope is mutable control state: recheck it this often inside a live stream.
SCOPE_RECHECK_SECONDS = 1.0


def _format_sse(payload: dict[str, object], *, event: str | None = None) -> str:
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


def _stream_client() -> httpx.AsyncClient:
    # Rebindable at module scope so tests inject an httpx.MockTransport.
    return httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=30.0))


def _session_info(session: LiveSession) -> MqttLiveSessionInfo:
    return MqttLiveSessionInfo(
        session_id=session.session_id,
        owner=session.owner,
        project_id=session.project_id,
        site_id=session.site_id,
        since=session.since,
    )


def _resolve_base_url(http_request: Request) -> str:
    supervisor = getattr(http_request.app.state, "sidecar_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="MQTT discovery sidecar is not available.")
    try:
        return str(supervisor.base_url_for(MQTT_SCANNER)).rstrip("/")
    except SidecarUnavailable as error:
        raise HTTPException(status_code=503, detail="MQTT discovery sidecar is not available.") from error


def _sidecar_error_text(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return "the sidecar reported an error"
    if isinstance(body, dict):
        return str((body.get("status") or {}).get("error") or body.get("error") or "the sidecar reported an error")
    return "the sidecar reported an error"


@router.post(
    "/mqtt_sidecar/live/connect",
    response_model=MqttLiveConnectResponse,
    dependencies=[Depends(require_engineer)],
)
def connect_mqtt_live(
    request: MqttLiveConnectRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveConnectResponse:
    """Open (or take over) the single MQTT live broker session.

    Broker host and credentials resolve server-side from the Configuration page;
    the browser sends only root_filter / qos / authorized / take_over. A live
    subscribe is real network I/O, so the same legacy consent gate the capture
    run uses applies.
    """
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "MQTT live sessions require the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )
    _require_legacy_scan_authorization({"authorized": request.authorized})
    base_url = _resolve_base_url(http_request)

    params: dict[str, object] = {"project_id": request.project_id, "site_id": request.site_id}
    if request.qos is not None:
        params["qos"] = request.qos
    root = _root_filter({"root_filter": request.root_filter} if request.root_filter else {})
    try:
        config = _connect_config(params, root)
    except ValueError as error:
        # MqttSettingsError (a ValueError) fires when no broker is configured.
        raise HTTPException(
            status_code=400,
            detail=(
                "No MQTT broker is configured. Enter the broker FQDN or IP address "
                "on the Configuration page and save it."
            ),
        ) from error

    # Close the run-vs-live race under the single lease lock: no mqtt_scanner run
    # may be queued/running, and no live session may already be held (unless
    # taking over). Both lanes drive the sidecar's one connection.
    with live_service.lock:
        active = service.list_runs(job_types={"mqtt_scanner"}, status="running", limit=1) or service.list_runs(
            job_types={"mqtt_scanner"}, status="queued", limit=1
        )
        if active:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An MQTT capture run is in progress (run {active[0].run_id}). Wait for it to "
                    "finish or stop it before starting a live session."
                ),
            )
        held = live_service.current_locked()
        if held is not None and not request.take_over:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An MQTT live session is already open (started by {held.owner} at "
                    f"{held.since.isoformat()}). Use take over to replace it."
                ),
            )
        session = live_service.acquire(
            owner=principal.username,
            project_id=request.project_id,
            site_id=request.site_id,
            disconnect=partial(_post_disconnect, base_url),
            take_over=request.take_over,
        )

    # Lock released before any network I/O. Drive the sidecar connect; on any
    # failure the lease is released so a dead session never wedges the lane.
    try:
        result = _post_json(base_url, "/api/connect", config)
    except urllib.error.HTTPError as error:
        live_service.release(session.session_id)
        raise HTTPException(
            status_code=502,
            detail=f"The MQTT broker connection was refused: {_sidecar_error_text(error)}",
        ) from error
    except (urllib.error.URLError, OSError) as error:
        live_service.release(session.session_id)
        raise HTTPException(status_code=503, detail="MQTT discovery sidecar is not available.") from error

    if isinstance(result, dict) and result.get("ok") is False:
        live_service.release(session.session_id)
        detail = str((result.get("status") or {}).get("error") or result.get("error") or "no detail")
        raise HTTPException(status_code=502, detail=f"The MQTT broker connection was refused: {detail}")

    connection = result.get("status") if isinstance(result, dict) else {}
    return MqttLiveConnectResponse(ok=True, session=_session_info(session), connection=connection or {})


@router.post(
    "/mqtt_sidecar/live/disconnect",
    response_model=MqttLiveDisconnectResponse,
    dependencies=[Depends(require_engineer)],
)
def disconnect_mqtt_live(
    request: MqttLiveDisconnectRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveDisconnectResponse:
    """Stop the live session. Releasing an already-free lease is a 200 no-op so
    an operator Stop is always safe to press."""
    released = live_service.release(request.session_id)
    return MqttLiveDisconnectResponse(ok=True, released=released)


@router.get(
    "/mqtt_sidecar/live/status",
    response_model=MqttLiveStatusResponse,
    dependencies=[Depends(require_engineer)],
)
def mqtt_live_status(
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveStatusResponse:
    """Report who holds the lease and the sidecar's live connection status. Never
    409s: the occupied panel needs this to render an honest in-use state."""
    session = live_service.current()
    info = _session_info(session) if session is not None else None
    try:
        base_url = _resolve_base_url(http_request)
        payload = _get_json(base_url, "/api/status")
    except (HTTPException, urllib.error.URLError, OSError):
        return MqttLiveStatusResponse(session=info, sidecar_available=False)
    payload = payload if isinstance(payload, dict) else {}
    return MqttLiveStatusResponse(
        session=info,
        sidecar_available=True,
        connection=payload.get("status") or {},
        stats=payload.get("stats") or {},
        register_summary=payload.get("register") or {},
    )


@router.get("/mqtt_sidecar/live/stream", dependencies=[Depends(require_engineer)])
async def stream_mqtt_live(
    http_request: Request,
    session_id: str = Query(..., min_length=1, max_length=64),
    principal: AuthPrincipal = Depends(get_principal),
) -> StreamingResponse:
    """Proxy the sidecar's live topic-tree SSE to the browser.

    The session must be the current lease; scope is rechecked at open and on a
    cadence inside the relay. The browser never touches the sidecar.
    """
    session = live_service.current()
    if session is None or session.session_id != session_id:
        raise HTTPException(status_code=409, detail="The live session has ended or was taken over. Refresh status.")
    try:
        await asyncio.to_thread(
            require_project_site_access,
            principal,
            session.project_id,
            session.site_id,
            engine=service.engine,
            query_only=True,
        )
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - fail closed if control state cannot be read
        raise HTTPException(status_code=503, detail="Live session state is temporarily unavailable.") from error
    base_url = _resolve_base_url(http_request)
    if not live_service.stream_attach(session_id):
        raise HTTPException(status_code=409, detail="Too many attached live streams, or the session ended.")
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    return StreamingResponse(
        _live_relay(base_url, session_id, principal, session.project_id, session.site_id),
        media_type="text/event-stream",
        headers=headers,
    )


async def _live_relay(
    base_url: str,
    session_id: str,
    principal: AuthPrincipal,
    project_id: str,
    site_id: str,
) -> AsyncIterator[str]:
    """Forward the sidecar's ``data:`` frames verbatim until the lease is lost,
    scope is revoked, the sidecar dies, or the wall-clock cap is hit. Honest
    control frames only; never fabricates data after an upstream failure."""
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    next_scope_check = 0.0
    try:
        async with _stream_client() as client, client.stream("GET", f"{base_url}/api/stream") as upstream:
            async for line in upstream.aiter_lines():
                if not line.startswith("data: "):
                    continue  # skip the blank inter-frame lines; sidecar frames are single-line
                now = time.monotonic()
                current = live_service.current()
                if current is None or current.session_id != session_id:
                    yield _format_sse({"session_id": session_id, "status": "closed"}, event="closed")
                    return
                if now >= next_scope_check:
                    next_scope_check = now + SCOPE_RECHECK_SECONDS
                    try:
                        await asyncio.to_thread(
                            require_project_site_access,
                            principal,
                            project_id,
                            site_id,
                            engine=service.engine,
                            query_only=True,
                        )
                    except HTTPException:
                        yield _format_sse({"session_id": session_id, "status": "closed"}, event="closed")
                        return
                    except Exception:  # noqa: BLE001 - control loss closes, never grants access
                        yield _format_sse({"session_id": session_id, "status": "unavailable"}, event="unavailable")
                        return
                if now >= deadline:
                    yield _format_sse({"session_id": session_id, "status": "timeout"}, event="timeout")
                    return
                live_service.touch(session_id)
                yield f"{line}\n\n"
    except httpx.HTTPError:
        yield _format_sse({"session_id": session_id, "status": "unavailable"}, event="unavailable")
        return
    except asyncio.CancelledError:
        return  # client disconnected; exit quietly (events.py convention)
    finally:
        live_service.stream_detach(session_id)
