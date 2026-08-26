"""Advanced-panel reverse proxy: serve a vendored standalone scanner app's own
web UI inside SCT, with SCT on top.

The browser iframe talks ONLY to SCT (``/api/v1/scanners/{proto}/raw/...``); this
route forwards to the loopback sidecar the supervisor owns. It is the single
choke point where SCT gates by role and (later milestones) guards writes and
records evidence, without modifying the vendored app.

Reads flow free (role check only, aligned with the frictionless mode). Writes
are classified fail-closed (``scanner_raw_policy``) and, until the write-guard
milestone, rejected outright - the panel never silently forwards a device write.

Auth: every proxied request runs the protected router's auth + ``require_engineer``.
In local/portable mode a loopback request already resolves to a principal, so the
iframe's cookieless subresource requests authenticate for free. A browser-native
credential for AUTH_MODE=api_key is deferred.
ponytail: no cookie/session endpoint yet - loopback ADMIN covers the shipped
(portable) and dev deployments; add a panel-session cookie when a keyed hosted
deployment actually needs the Advanced tab.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.api.routes.discovery import require_engineer, service
from app.core.auth import AuthPrincipal, get_principal
from app.core.scopes import require_project_site_access
from app.schemas.jobs import JobCreateRequest
from app.services import scanner_raw_session
from app.services.scanner_raw_policy import SIDECAR_BY_PROTO, classify, should_record, write_digest
from app.services.scanner_raw_session import COOKIE_NAME
from app.services.sidecar_supervisor import SidecarUnavailable

logger = logging.getLogger(__name__)

router = APIRouter()

# Wall-clock cap on one browser stream (matches events.py / the MQTT live relay);
# the vendored UI reconnects its EventSource on close.
MAX_STREAM_SECONDS = 600.0

# Copied response headers (never hop-by-hop headers). content-disposition carries
# the download filename for /api/template and /api/export(-archive).
_COPIED_HEADERS = ("content-type", "content-disposition", "cache-control")

# SCT-owned script the vendored index.html loads (tag injected by the embed
# rewrite). Wraps the iframe's fetch so a device write pauses and asks the SCT
# parent to confirm: the parent shows the exact topic/value, mints a hash-bound
# token, and the write is re-sent with it. Non-writes pass straight through.
_BRIDGE_JS = """(function () {
  var WRITES = ['/api/publish', '/api/config'];
  function writePath(method, url) {
    if ((method || 'GET').toUpperCase() === 'GET') return null;
    try {
      var abs = new URL(url, location.href).pathname;
      var rel = abs.replace(/^.*\\/raw\\//, '');            // e.g. "api/publish"
      var norm = '/' + rel.split('?')[0].replace(/^\\/+/, '');
      return WRITES.indexOf(norm) >= 0 ? rel : null;
    } catch (e) { return null; }
  }
  var orig = window.fetch;
  var seq = 0;
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === 'string' ? input : input.url;
    var method = init.method || (typeof input !== 'string' && input.method) || 'GET';
    var path = writePath(method, url);
    if (!path) return orig(input, init);
    var body = init.body != null ? String(init.body) : '';
    var id = 'w' + (++seq);
    return new Promise(function (resolve, reject) {
      function onMsg(ev) {
        var d = ev.data || {};
        if (d.type !== 'sct-write-decision' || d.id !== id) return;
        window.removeEventListener('message', onMsg);
        if (!d.token) { reject(new Error('Write cancelled')); return; }
        var headers = Object.assign({}, init.headers || {}, { 'X-SCT-Write-Confirm': d.token });
        resolve(orig(url, Object.assign({}, init, { headers: headers, body: body })));
      }
      window.addEventListener('message', onMsg);
      parent.postMessage({ type: 'sct-write-request', id: id, method: method, path: path, body: body }, '*');
    });
  };
})();
"""


# SCT theme skin injected into each vendored scanner (the <link> tag is added by
# the embed rewrite and loaded after the app's own styles.css, so it wins the
# cascade). It remaps the app's palette variables and body font to the SCT
# tokens; no JS or markup is touched, so every scanner function is preserved. The
# IP scanner is fully skinned here; the sibling apps already pick up the shared
# font and any common tokens, and get their remaining selectors as they are
# brought over.
_THEME_CSS = """:root {
  --bg: #f5f5f7;
  --border: rgba(0, 0, 0, 0.08);
  --border-strong: rgba(0, 0, 0, 0.16);
  --text: #1d1d1f;
  --muted: #86868b;
  --muted-2: #a1a1a6;
  --blue: #0071e3;
  --blue-bg: rgba(0, 113, 227, 0.06);
  --green: #248a3d;
  --green-bg: rgba(52, 199, 89, 0.08);
  --amber: #c77c00;
  --amber-bg: rgba(255, 159, 10, 0.08);
  --red: #d70015;
  --red-bg: rgba(255, 59, 48, 0.08);
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  --radius: 10px;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
}
.brand-mark { background: var(--blue); }
.progress-fill { background: var(--blue); }
.btn.primary:hover { background: #0077ed; }
.chip.active { background: var(--blue); border-color: var(--blue); }
.config-input:focus,
.search-input:focus { outline: 2px solid rgba(0, 113, 227, 0.35); border-color: var(--blue); }
.results-table thead th { background: #f2f2f7; }
"""


def _proxy_client() -> httpx.AsyncClient:
    # Rebindable at module scope so tests inject an httpx.MockTransport. No read
    # timeout: an SSE stream is idle between frames; the wall-clock cap bounds it.
    return httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=None))


def _resolve_base_url(request: Request, name: str) -> str:
    supervisor = getattr(request.app.state, "sidecar_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="Scanner sidecar is not available.")
    try:
        return str(supervisor.base_url_for(name)).rstrip("/")
    except SidecarUnavailable as error:
        raise HTTPException(status_code=503, detail="Scanner sidecar is not available.") from error


async def _relay(client: httpx.AsyncClient, upstream: httpx.Response, *, cap: bool) -> AsyncIterator[bytes]:
    """Stream the sidecar's response body to the browser. SSE responses get the
    wall-clock cap; finite responses (JSON, file downloads) stream to completion.
    Honest close on upstream failure or client disconnect - never fabricates."""
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    try:
        # aiter_bytes yields content-decoded bytes, so we deliberately do NOT copy
        # the sidecar's content-encoding header - the browser gets plain bytes.
        async for chunk in upstream.aiter_bytes():
            if cap and time.monotonic() > deadline:
                return
            yield chunk
    except httpx.HTTPError:
        return  # upstream died mid-stream; end the stream (browser sees it close)
    except asyncio.CancelledError:
        return  # client disconnected; exit quietly
    finally:
        await upstream.aclose()
        await client.aclose()


class PanelSessionRequest(BaseModel):
    project_id: str
    site_id: str


# Registered BEFORE the catch-all so POST .../raw/session resolves here, not as a
# proxied sidecar path.
@router.post("/{proto}/raw/session", dependencies=[Depends(require_engineer)])
def open_panel_session(
    proto: str,
    body: PanelSessionRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JSONResponse:
    """Open an Advanced-panel session: bind project/site + owner and set the
    attribution cookie the proxy reads on the iframe's subresource requests."""
    if proto not in SIDECAR_BY_PROTO:
        raise HTTPException(status_code=404, detail="Unknown scanner protocol.")
    require_project_site_access(principal, body.project_id, body.site_id, engine=service.engine)
    session = scanner_raw_session.create(
        owner=principal.username, project_id=body.project_id, site_id=body.site_id, proto=proto
    )
    response = JSONResponse(
        {"session_id": session.session_id, "project_id": body.project_id, "site_id": body.site_id}
    )
    response.set_cookie(
        COOKIE_NAME, session.session_id, httponly=True, samesite="strict", path="/api/v1/scanners/"
    )
    return response


def _record_action(
    session: scanner_raw_session.PanelSession,
    proto: str,
    method: str,
    subpath: str,
    query: str,
    http_status: int,
) -> None:
    """Best-effort: land one terminal SCT run row for an evidence-worthy panel
    action. Never raises into the proxy - a recording failure must not fail the
    operator's scan. Records what crossed the wire (endpoint + scope + outcome),
    not client-side interactions.
    ponytail: no byte/frame counts yet - add them if the field wants soak metrics."""
    try:
        endpoint = "/" + subpath.split("?", 1)[0].strip("/")
        record_request = JobCreateRequest(
            project_id=session.project_id,
            site_id=session.site_id,
            job_type="scanner_raw_action",
            parameters={
                "source": "advanced_panel",
                "proto": proto,
                "method": method.upper(),
                "endpoint": endpoint,
                "query": query or "",
            },
        )
        run = service.create_job_run(
            record_request, expected_job_type="scanner_raw_action", requesting_principal=session.owner
        )
        status = "succeeded" if 200 <= http_status < 400 else "failed"
        service.update_run_status(run.run_id, status=status, stage="advanced_panel")
        service.update_result_summary(run.run_id, {"http_status": http_status})
    except Exception:  # noqa: BLE001 - evidence is best-effort, never breaks the action
        logger.warning("advanced-panel evidence recording failed: %s %s", method, subpath, exc_info=True)


class WriteConfirmRequest(BaseModel):
    method: str
    path: str
    body: str = ""


@router.post("/{proto}/raw/confirm-write", dependencies=[Depends(require_engineer)])
def confirm_panel_write(proto: str, body: WriteConfirmRequest, request: Request) -> JSONResponse:
    """Mint a single-use, hash-bound token for one device write the panel is about
    to send. The proxy accepts that write only with this token."""
    if proto not in SIDECAR_BY_PROTO:
        raise HTTPException(status_code=404, detail="Unknown scanner protocol.")
    session = scanner_raw_session.resolve(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=403, detail="Open the Advanced panel before confirming a write.")
    if classify(body.method, body.path) != "write":
        raise HTTPException(status_code=400, detail="Not a device-write action.")
    digest = write_digest(body.method, body.path, body.body.encode("utf-8"))
    token = scanner_raw_session.mint_write_token(session_id=session.session_id, digest=digest)
    return JSONResponse({"token": token})


_SECRET_KEY_HINTS = ("password", "secret", "token", "key", "credential")


def _redact_write_body(body: bytes) -> object:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return {"_raw_bytes": len(body)}
    if isinstance(data, dict):
        return {k: ("***" if any(h in k.lower() for h in _SECRET_KEY_HINTS) else v) for k, v in data.items()}
    return data


def _record_write(
    session: scanner_raw_session.PanelSession,
    proto: str,
    method: str,
    subpath: str,
    body: bytes,
    http_status: int,
) -> None:
    """Best-effort: land one terminal scanner_raw_write run for a guarded device
    write, carrying the redacted payload and its hash. Never raises into the proxy."""
    try:
        endpoint = "/" + subpath.split("?", 1)[0].strip("/")
        record_request = JobCreateRequest(
            project_id=session.project_id,
            site_id=session.site_id,
            job_type="scanner_raw_write",
            parameters={"source": "advanced_panel", "proto": proto, "method": method.upper(), "endpoint": endpoint},
        )
        run = service.create_job_run(
            record_request, expected_job_type="scanner_raw_write", requesting_principal=session.owner
        )
        status = "succeeded" if 200 <= http_status < 400 else "failed"
        service.update_run_status(run.run_id, status=status, stage="advanced_panel_write")
        service.update_result_summary(
            run.run_id,
            {
                "http_status": http_status,
                "payload": _redact_write_body(body),
                "payload_sha256": hashlib.sha256(body).hexdigest(),
            },
        )
    except Exception:  # noqa: BLE001 - evidence is best-effort, never breaks the action
        logger.warning("advanced-panel write evidence recording failed: %s %s", method, subpath, exc_info=True)


@router.api_route(
    "/{proto}/raw/{path:path}",
    methods=["GET", "POST", "DELETE", "HEAD"],
    dependencies=[Depends(require_engineer)],
)
async def proxy_scanner_raw(proto: str, path: str, request: Request) -> Response:
    name = SIDECAR_BY_PROTO.get(proto)
    if name is None:
        raise HTTPException(status_code=404, detail="Unknown scanner protocol.")
    if path == "sct-bridge.js":
        return Response(_BRIDGE_JS, media_type="text/javascript")
    if path == "sct-theme.css":
        return Response(_THEME_CSS, media_type="text/css")

    body = await request.body()
    session = scanner_raw_session.resolve(request.cookies.get(COOKIE_NAME))
    is_write = classify(request.method, path) == "write"
    if is_write:
        # A device write is allowed only with a single-use token bound to these
        # exact bytes (minted by /confirm-write after the operator confirms).
        token = request.headers.get("X-SCT-Write-Confirm")
        digest = write_digest(request.method, path, body)
        if session is None or not scanner_raw_session.consume_write_token(
            token, session_id=session.session_id, digest=digest
        ):
            raise HTTPException(
                status_code=403,
                detail="This device write needs SCT confirmation. Confirm it in the panel and try again.",
            )

    base_url = _resolve_base_url(request, name)
    target = f"{base_url}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type

    client = _proxy_client()
    try:
        upstream_request = client.build_request(
            request.method, target, content=body or None, headers=headers
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(status_code=502, detail="The scanner sidecar did not respond.") from error

    if session is not None:
        if is_write:
            await asyncio.to_thread(
                _record_write, session, proto, request.method, path, body, upstream.status_code
            )
        elif should_record(request.method, path):
            await asyncio.to_thread(
                _record_action, session, proto, request.method, path, request.url.query, upstream.status_code
            )

    is_sse = upstream.headers.get("content-type", "").startswith("text/event-stream")
    response_headers = {k: upstream.headers[k] for k in _COPIED_HEADERS if k in upstream.headers}
    if is_sse:
        response_headers["cache-control"] = "no-store"
        response_headers["x-accel-buffering"] = "no"

    return StreamingResponse(
        _relay(client, upstream, cap=is_sse),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
