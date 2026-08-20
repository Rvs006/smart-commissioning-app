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
import hashlib
import json
import logging
import time
import urllib.error
from collections.abc import AsyncIterator, Mapping
from functools import partial

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from smart_commissioning_core.engines.mqtt_publish_sidecar import process_mqtt_publish_run
from smart_commissioning_core.engines.mqtt_scanner_sidecar import (
    _connect_config,
    _get_json,
    _post_disconnect,
    _post_json,
    _root_filter,
)
from smart_commissioning_core.mqtt_settings import MqttSettingsError, build_mqtt_connection_settings
from starlette.responses import StreamingResponse

from app.api.routes.discovery import (
    _bind_control_identity,
    _create_run,
    _dispatch,
    _prepare_scan_parameters,
    _require_legacy_scan_authorization,
    _settings_throttle,
    require_engineer,
    service,
)
from app.core.auth import AuthPrincipal, get_principal
from app.core.config import get_settings
from app.core.scopes import load_scoped_run, require_project_site_access
from app.schemas.jobs import JobAcceptedResponse, JobCreateRequest
from app.schemas.mqtt_live import (
    MqttLiveConnectRequest,
    MqttLiveConnectResponse,
    MqttLiveDisconnectRequest,
    MqttLiveDisconnectResponse,
    MqttLiveFocusRequest,
    MqttLiveFocusResponse,
    MqttLiveSearchRequest,
    MqttLiveSearchResponse,
    MqttLiveSessionInfo,
    MqttLiveStatusResponse,
    MqttLiveSubscribeRequest,
    MqttLiveSubscribeResponse,
)
from app.services.engine_dispatch import is_dry_run
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


@router.post(
    "/mqtt_sidecar/live/focus",
    response_model=MqttLiveFocusResponse,
    dependencies=[Depends(require_engineer)],
)
def focus_mqtt_live(
    request: MqttLiveFocusRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveFocusResponse:
    """Focus one asset in the live session so its live points, per-topic detail,
    and config payload ride the snapshot stream. Read-only; sets the sidecar's
    single-tenant focus (one operator, one session)."""
    session = live_service.current()
    if session is None or session.session_id != request.session_id:
        raise HTTPException(status_code=409, detail="The live session has ended or was taken over. Refresh status.")
    require_project_site_access(principal, session.project_id, session.site_id, engine=service.engine)
    base_url = _resolve_base_url(http_request)
    try:
        result = _post_json(base_url, "/api/focus", {"asset": request.asset})
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(status_code=502, detail="The MQTT discovery sidecar could not be reached.") from error
    ok = bool(isinstance(result, dict) and result.get("ok"))
    focused = result.get("focused") if isinstance(result, dict) else None
    return MqttLiveFocusResponse(ok=ok, focused=focused if isinstance(focused, dict) else None)


def _current_session_or_409(session_id: str, principal: AuthPrincipal):
    """Assert ``session_id`` is the current lease + the caller scopes to it."""
    session = live_service.current()
    if session is None or session.session_id != session_id:
        raise HTTPException(status_code=409, detail="The live session has ended or was taken over. Refresh status.")
    require_project_site_access(principal, session.project_id, session.site_id, engine=service.engine)
    return session


@router.post(
    "/mqtt_sidecar/live/subscribe",
    response_model=MqttLiveSubscribeResponse,
    dependencies=[Depends(require_engineer)],
)
def subscribe_mqtt_live(
    request: MqttLiveSubscribeRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveSubscribeResponse:
    """Change the live subscription (filter and/or qos) on the held session."""
    _current_session_or_409(request.session_id, principal)
    base_url = _resolve_base_url(http_request)
    body: dict[str, object] = {}
    if request.root_filter is not None:
        body["rootFilter"] = request.root_filter
    if request.qos is not None:
        body["qos"] = request.qos
    try:
        result = _post_json(base_url, "/api/subscribe", body)
    except urllib.error.HTTPError as error:
        # The sidecar returns 409 when the session is not connected to the broker.
        raise HTTPException(status_code=409, detail="The live session is not connected to the broker.") from error
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(status_code=502, detail="The MQTT discovery sidecar could not be reached.") from error
    if not (isinstance(result, dict) and result.get("ok")):
        detail = str((result or {}).get("error") or "The subscription change was rejected.")
        raise HTTPException(status_code=409, detail=detail)
    return MqttLiveSubscribeResponse(ok=True)


@router.post(
    "/mqtt_sidecar/live/search",
    response_model=MqttLiveSearchResponse,
    dependencies=[Depends(require_engineer)],
)
def search_mqtt_live(
    request: MqttLiveSearchRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> MqttLiveSearchResponse:
    """Filter the live tree server-side (topic + asset + payload text). The
    filtered result rides the snapshot stream; blank clears the filter."""
    _current_session_or_409(request.session_id, principal)
    base_url = _resolve_base_url(http_request)
    try:
        _post_json(base_url, "/api/search", {"q": request.q, "matchedOnly": request.matched_only})
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(status_code=502, detail="The MQTT discovery sidecar could not be reached.") from error
    return MqttLiveSearchResponse(ok=True)


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


# --------------------------------------------------------------------------
# M5 PR-A: sealed one-message publish through the held live session.
# --------------------------------------------------------------------------

_PUBLISH_ALLOWED_KEYS = frozenset({"dry_run", "topic", "payload", "qos", "retain"})
_PUBLISH_PAYLOAD_MAX_BYTES = 65_536
_PUBLISH_TOPIC_MAX = 1024


def _seal_publish_preview(parameters: dict, principal: AuthPrincipal) -> None:
    """Validate the operator's publish and freeze it into a scan_contract_v1.

    Only ``{dry_run, topic, payload, qos, retain}`` are accepted; the broker
    identity resolves server-side (no browser secrets). The frozen contract's
    ``packet_plan_sha256`` is what an admin approves and what the live send
    replays, so the wire bytes can only ever be these.
    """
    extra = set(parameters) - _PUBLISH_ALLOWED_KEYS
    if extra:
        raise HTTPException(status_code=400, detail=f"Unexpected publish parameters: {', '.join(sorted(extra))}.")
    topic = str(parameters.get("topic") or "").strip()
    if (
        not topic
        or len(topic) > _PUBLISH_TOPIC_MAX
        or "+" in topic
        or "#" in topic
        or any(ord(char) < 32 for char in topic)
    ):
        raise HTTPException(
            status_code=400,
            detail="A publish topic is required and must not contain wildcards (+ or #) or control characters.",
        )
    payload = parameters.get("payload")
    if not isinstance(payload, str):
        raise HTTPException(status_code=400, detail="The publish payload must be a string.")
    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) > _PUBLISH_PAYLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="The publish payload exceeds the 64 KiB limit.")
    qos = parameters.get("qos", 0)
    if qos not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="QoS must be 0, 1, or 2.")
    retain = bool(parameters.get("retain"))
    try:
        settings = build_mqtt_connection_settings({})
    except (MqttSettingsError, ValueError) as error:
        raise HTTPException(
            status_code=400,
            detail=(
                "No MQTT broker is configured. Enter the broker FQDN or IP address "
                "on the Configuration page and save it."
            ),
        ) from error
    parameters["topic"] = topic
    parameters["qos"] = qos
    parameters["retain"] = retain
    parameters["scan_contract_v1"] = {
        "scan_contract_version": "1.0",
        "job_type": "mqtt_publish",
        "mqtt_publish": {
            "topic": topic,
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload_bytes": len(payload_bytes),
            "qos": qos,
            "retain": retain,
            # host/port/tls only; credentials never enter the sealed contract.
            "broker": {"host": settings.host, "port": settings.port, "tls": settings.use_tls},
            "policy": {"dispatch_phase_seconds": 30.0, "run_deadline_seconds": 60.0},
        },
    }
    _bind_control_identity(parameters, principal)


def _assert_session_broker_matches_seal(base_url: str, parameters: Mapping) -> None:
    """The held session must be connected to the SAME broker the seal approved."""
    sealed = ((parameters.get("scan_contract_v1") or {}).get("mqtt_publish") or {}).get("broker") or {}
    try:
        status = _get_json(base_url, "/api/status")
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(status_code=502, detail="The MQTT discovery sidecar could not be reached.") from error
    connection = (status or {}).get("status") if isinstance(status, dict) else {}
    connection = connection if isinstance(connection, dict) else {}
    if connection.get("status") != "connected":
        raise HTTPException(status_code=409, detail="The live session is not connected to the broker.")
    live = (str(connection.get("host") or "").strip().casefold(), int(connection.get("port") or 0), bool(connection.get("tls")))
    seal = (str(sealed.get("host") or "").strip().casefold(), int(sealed.get("port") or 0), bool(sealed.get("tls")))
    if live != seal:
        raise HTTPException(
            status_code=409,
            detail="The live session is connected to a different broker than the sealed preview.",
        )


@router.post(
    "/mqtt_sidecar/live/publish/runs",
    response_model=JobAcceptedResponse,
    dependencies=[Depends(require_engineer)],
)
def create_mqtt_publish_run(
    request: JobCreateRequest,
    http_request: Request,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    """Publish one MQTT message through the sealed preview ceremony.

    Dry run previews the exact bytes with no broker I/O and seals them; an admin
    approves that exact preview (POST /scan-authorizations); the live send replays
    the frozen bytes (empty parameters + the two ids) only if the current live
    session is held by the sender and connected to the sealed broker.
    """
    require_project_site_access(principal, request.project_id, request.site_id, engine=service.engine)
    if get_settings().job_execution_mode != "inline":
        raise HTTPException(
            status_code=503,
            detail=(
                "MQTT publish runs require the local inline executor; queue and "
                "external execution are not available for the loopback sidecar."
            ),
        )
    # _prepare_scan_parameters decides dry-vs-live FIRST (400s a dry preview that
    # carries ids, or a live send with non-empty parameters) before any run lookup.
    parameters, preview = _prepare_scan_parameters(request, expected_job_type="mqtt_publish", principal=principal)
    base_url: str | None = None
    if preview:
        _seal_publish_preview(parameters, principal)
    else:
        load_scoped_run(str(request.preview_run_id), principal, engine=service.engine)  # scope the preview
        base_url = _resolve_base_url(http_request)
        session = live_service.current()
        if session is None:
            raise HTTPException(status_code=409, detail="No MQTT live session is open. Start the live view first.")
        if session.owner != principal.username:
            raise HTTPException(
                status_code=409,
                detail=f"The live session is held by {session.owner}. Only the session owner can send a write.",
            )
        if (session.project_id, session.site_id) != (request.project_id, request.site_id):
            raise HTTPException(status_code=409, detail="The live session belongs to a different workspace.")
        _assert_session_broker_matches_seal(base_url, parameters)

    run = _create_run(request.model_copy(update={"parameters": parameters}), "mqtt_publish", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_mqtt_publish_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            throttle=_settings_throttle(frozen_parameters),
            dry_run=is_dry_run(frozen_parameters),
            sidecar_base_url=base_url,
        )

    return _dispatch(run, enqueue=None, run_inline=run_inline, label="MQTT publish")
