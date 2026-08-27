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
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qs

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

  // config-in (pipe 1): SCT pushes saved config so the tool starts pre-set. IP:
  // pre-select the NIC that matches SCT's Source Interface, then dispatch a real
  // change event - the vendored scan reads the range that change prefills, not the
  // adapter. Pure DOM, so app.js is untouched; a no-op on apps without #adapterSelect.
  function applyMqttConfig(m) {
    function setField(id, val) {
      var el = document.getElementById(id);
      if (el && val != null && val !== '' && !el.value) el.value = val;
    }
    setField('cHost', m.host);
    setField('cPort', m.port);
    setField('cClientId', m.clientId);
    setField('cUser', m.username);
    var proto = document.getElementById('cProto');
    if (proto && typeof m.tls === 'boolean') proto.value = m.tls ? 'mqtts' : 'mqtt';
    var qos = document.getElementById('cQos');
    if (qos && m.qos) qos.value = m.qos;
  }
  function applyConfigIn(cfg) {
    if (!cfg) return;
    if (cfg.mqtt) applyMqttConfig(cfg.mqtt);   // MQTT: prefill broker modal (secrets left blank)
    var cidr = cfg.sourceInterface;
    if (!cidr) return;
    var ip = String(cidr).split('/')[0];
    var sel = document.getElementById('adapterSelect');
    if (!sel) return;
    function pick() {
      var opts = sel.options || [];
      for (var i = 0; i < opts.length; i++) {
        if ((opts[i].textContent || '').indexOf(ip) >= 0) {
          if (sel.value !== String(opts[i].value)) {
            sel.value = opts[i].value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
          }
          return true;
        }
      }
      return false;
    }
    if (pick()) return;
    var obs = new MutationObserver(function () { if (pick()) obs.disconnect(); });
    obs.observe(sel, { childList: true });
    setTimeout(function () { obs.disconnect(); }, 10000);
  }
  window.addEventListener('message', function (ev) {
    var d = ev.data || {};
    if (d.type === 'sct-config-in') applyConfigIn(d);
  });
  try { parent.postMessage({ type: 'sct-bridge-ready' }, '*'); } catch (e) {}
})();
"""


# SCT (Electracom) theme skin served to each vendored scanner (the <link> tag is
# added by the embed rewrite and loaded after the app's own styles.css, so it
# wins the cascade). One sheet covers all three apps: the shared :root + body
# apply everywhere, and each per-app block matches only its own selectors. It
# remaps the apps' palette variables and body font to the SCT brand tokens (warm
# cream + teal); no JS or markup is touched, so every scanner function is
# preserved. Light theme (the vendored apps have no dark toggle).
_THEME_CSS = """:root {
  --bg: #faf7f2;
  --surface: #ffffff;
  --surface-2: #fdf9f3;
  --surface-3: #eeebe5;
  --card: #ffffff;
  --border: #e5dfd6;
  --border-soft: #eeebe5;
  --border-strong: #d8d0c4;
  --ink: #2c2a28;
  --text: #6b6560;
  --muted: #6f6963;
  --muted-2: #8a847d;
  --accent: #26718f;
  --accent-hover: #226986;
  --accent-bg: #e4f2f7;
  --on-accent: #ffffff;
  --blue: #26718f;
  --blue-bg: #e4f2f7;
  --green: #216f42;
  --green-bg: #e8f5ee;
  --amber: #865d0b;
  --amber-bg: #faf1df;
  --orange: #865d0b;
  --orange-bg: #faf1df;
  --red: #c0392b;
  --red-bg: #fbeae7;
  --purple: #8b5c9b;
  --purple-bg: #f1e9f5;
  --pink: #8b5c9b;
  --pink-bg: #f1e9f5;
  --shadow: 0 1px 2px rgba(44, 42, 40, 0.05);
  --radius: 10px;
  --radius-sm: 8px;
  --control-height: 44px;
}
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif; font-size: 13px; color: var(--ink); background: var(--bg); }

/* ===== IP scanner (network-ip-scanner) ===== */
.brand-mark { background: var(--accent); border-radius: var(--radius-sm); }
.brand-title { color: var(--ink); font-weight: 760; }
.brand-sub { color: var(--muted); }
.banner { background: var(--accent-bg); border: 1px solid var(--border-soft); color: var(--text); border-radius: var(--radius); }
.banner strong { color: var(--ink); }
.banner-icon { color: var(--accent); }
.config-card, .summary-card, .results-panel, .detail-panel { border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
.config-label { color: var(--muted); font-size: 10px; font-weight: 760; letter-spacing: 0.02em; text-transform: uppercase; }
.config-input, .search-input { min-height: var(--control-height); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--ink); }
.config-input:focus, .search-input:focus { border-color: var(--accent); outline: 2px solid var(--accent); outline-offset: 2px; }
.summary-title { color: var(--muted); font-size: 10px; font-weight: 760; letter-spacing: 0.02em; text-transform: uppercase; }
.summary-title .i { color: var(--muted-2); }
.summary-num { font-size: 28px; font-weight: 800; }
.summary-num.blue { color: var(--accent); }
.summary-sub { color: var(--muted); }
.btn:not(.sm) { min-height: var(--control-height); padding: 8px 15px; border: 1px solid var(--border); border-radius: var(--radius-sm); font-size: 12px; font-weight: 700; }
.btn.primary { border-color: var(--accent); background: var(--accent); color: var(--on-accent); }
.btn.primary:hover { border-color: var(--accent-hover); background: var(--accent-hover); }
.btn.ghost { background: var(--surface); color: var(--text); border-color: var(--border); }
.btn.ghost:hover { border-color: var(--accent-hover); background: var(--accent-bg); color: var(--accent-hover); }
.link-btn { min-height: 32px; padding: 6px 11px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font-weight: 650; }
.link-btn:hover { border-color: var(--accent-hover); background: var(--accent-bg); color: var(--accent-hover); }
.link-btn.danger { color: var(--red); background: var(--surface); border-color: var(--border); }
.link-btn.danger:hover { border-color: var(--red); background: var(--red-bg); color: var(--red); }
.chip { border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: 980px; padding: 6px 11px; font-size: 11px; font-weight: 700; }
.chip.active { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.scan-state { border-radius: 980px; font-size: 10px; font-weight: 760; padding: 5px 10px; }
.scan-state.idle { background: var(--surface-2); color: var(--muted); }
.scan-state.running { background: var(--accent-bg); color: var(--accent); }
.scan-state.done { background: var(--green-bg); color: var(--green); }
.scan-state.error { background: var(--red-bg); color: var(--red); }
.results-title { color: var(--ink); font-weight: 700; }
.results-table thead th { background: var(--surface-2); color: var(--muted); font-size: 10px; font-weight: 760; letter-spacing: 0.03em; text-transform: uppercase; border-bottom: 1px solid var(--border); }
.results-table tbody td { border-bottom: 1px solid var(--border-soft); }
.results-table tbody tr:hover { background: var(--surface-2); }
.results-table tbody tr.selected { background: var(--accent-bg); }
.ip-cell { color: var(--accent); }
.badge { border-radius: 980px; font-size: 10px; font-weight: 760; padding: 5px 9px; }
.progress-fill { background: var(--accent); }
.toast { background: var(--ink); border-radius: var(--radius-sm); }

/* ===== BACnet scanner (bacnet-scanner) ===== */
.brand-mark { background: var(--accent); color: var(--on-accent); }
.scan-state.idle { background: var(--surface-3); color: var(--muted); }
.config-label { font-size: 10px; font-weight: 760; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
.link-btn { border-color: var(--border); }
.link-btn:hover { background: var(--accent-bg); border-color: var(--accent-hover); color: var(--accent-hover); }
.link-btn.danger { border-color: var(--red-bg); }
.link-btn.danger:hover { background: var(--red-bg); border-color: var(--red); color: var(--red); }
.btn.active-toggle { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.progress-bar { background: var(--surface-3); }
.results-table tbody tr.rag-red-row,
.results-table tbody tr.rag-red-row:hover { background: var(--red-bg); }
.badge.grey { background: var(--surface-3); color: var(--muted); }
.detail-backdrop { background: rgba(44, 42, 40, 0.45); }
.detail-panel.expanded { box-shadow: 0 24px 70px rgba(44, 42, 40, 0.28); }
.detail-section h4 { font-size: 10px; font-weight: 760; letter-spacing: 0.06em; color: var(--muted); }
.svc-tag { background: var(--surface-3); }
.svc-tag.udp { background: var(--accent-bg); color: var(--accent); }
.svc-proto { background: var(--surface-3); }
.svc-proto.udp { background: var(--accent-bg); color: var(--accent); }
.export-bar { border-color: var(--border); }
.export-info { color: var(--ink); }
.networks-bar { background: var(--accent-bg); border-color: var(--border); color: var(--ink); }
.net-chip { background: var(--accent-bg); color: var(--accent); font-size: 10px; font-weight: 760; padding: 5px 9px; border-radius: 980px; }
.obj-row { border-bottom: 1px solid var(--border-soft); }
.obj-row:nth-child(even) { background: var(--surface-2); }

/* ===== MQTT discovery (mqtt-discovery) ===== */
.topnav { background: var(--surface); border-bottom: 1px solid var(--border); }
.nav-tab { background: var(--surface); border-radius: var(--radius-sm); }
.nav-tab:hover { background: var(--surface-2); color: var(--ink); }
.nav-tab.active { background: var(--accent); color: var(--on-accent); border-color: var(--accent); box-shadow: none; }
.mini-btn { border-color: var(--border); border-radius: var(--radius-sm); }
.mini-btn:hover { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.mini-btn.primary { color: var(--on-accent); border-color: var(--accent); }
.mini-btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.filter-input,
.fld input,
.fld select,
.fld textarea { min-height: var(--control-height); background: var(--surface); color: var(--ink); border-radius: var(--radius-sm); }
.cfg-editor { background: var(--surface); color: var(--ink); }
.filter-input:focus,
.fld input:focus,
.fld select:focus,
.fld textarea:focus,
.cfg-editor:focus { outline: 2px solid var(--accent); outline-offset: 2px; border-color: var(--accent); }
.list-table thead th { background: var(--surface-2); }
.list-table tbody td { border-bottom-color: var(--border-soft); }
.list-table tbody tr:not(.selected):hover { background: var(--surface-2); }
.points-table td { border-bottom-color: var(--border-soft); }
.btn.sm { border-radius: var(--radius-sm); font-size: 12px; font-weight: 700; }
.json-view { background: var(--surface-3); color: var(--ink); border: 1px solid var(--border-soft); }
.json-view .jk { color: var(--accent); }
.json-view .js { color: var(--green); }
.json-view .jn { color: var(--amber); }
.json-view .jb { color: var(--accent-hover); }
.cmp-bar { background: var(--surface-3); }
.modal-overlay { background: rgba(44, 42, 40, 0.45); }
.modal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 10px 30px rgba(44, 42, 40, 0.16); }
.certs .cert-hint code { background: var(--surface-3); }
.file-btn { border-color: var(--border); }
.file-btn:hover { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.conn-error { background: var(--red-bg); border-color: var(--red); color: var(--red); }
.cfg-warn { background: var(--amber-bg); border-color: var(--amber); color: var(--amber); }
.sort-btn { background: var(--surface); }
.sort-btn.active { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.trow:not(.selected):hover { background: var(--surface-2); }
.tname.device { color: var(--accent); }
.topic-line { background: var(--surface-2); }
.hist-chip { background: var(--surface-3); }
.hist-chip:hover { background: var(--accent-bg); }
.hist-chip.active { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.cfg-topic code { background: var(--surface-3); color: var(--ink); }
.toast { background: var(--ink); box-shadow: 0 6px 20px rgba(44, 42, 40, 0.22); }
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


async def _relay(
    client: httpx.AsyncClient,
    upstream: httpx.Response,
    *,
    cap: bool,
    on_complete: Callable[[bytes], None] | None = None,
) -> AsyncIterator[bytes]:
    """Stream the sidecar's response body to the browser. SSE responses get the
    wall-clock cap; finite responses (JSON, file downloads) stream to completion.
    Honest close on upstream failure or client disconnect - never fabricates.

    When ``on_complete`` is set the body is teed into a buffer and, ONLY on a
    clean end-of-stream (not a cap timeout, upstream failure, or client
    disconnect), handed to ``on_complete`` off-thread. This is the results-out
    capture: a truncated or failed stream must never persist a partial run."""
    deadline = time.monotonic() + MAX_STREAM_SECONDS
    captured = bytearray() if on_complete is not None else None
    completed = False
    try:
        # aiter_bytes yields content-decoded bytes, so we deliberately do NOT copy
        # the sidecar's content-encoding header - the browser gets plain bytes.
        async for chunk in upstream.aiter_bytes():
            if cap and time.monotonic() > deadline:
                return
            if captured is not None:
                captured.extend(chunk)
            yield chunk
        completed = True
    except httpx.HTTPError:
        return  # upstream died mid-stream; end the stream (browser sees it close)
    except asyncio.CancelledError:
        return  # client disconnected; exit quietly
    finally:
        await upstream.aclose()
        await client.aclose()
    if completed and on_complete is not None and captured is not None:
        await asyncio.to_thread(on_complete, bytes(captured))


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
    if session.proto != proto:
        # A panel session is bound to the protocol it was opened for. A cookie
        # from another protocol's panel must never authorize this one's write
        # (REV-7): reject before minting a token.
        raise HTTPException(
            status_code=403,
            detail="This panel session is for a different scanner. Reopen the panel for this protocol.",
        )
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


def _parse_scan_result(raw: bytes) -> dict | None:
    """Recover the final SSE ``result`` frame (``{rows, summary}``) from a captured
    ``/api/scan`` body. Mirrors the sidecar's own SSE parse (``_stream_scan``)."""
    result: dict | None = None
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[len("data:") :].strip())
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def _parse_compare_result(raw: bytes) -> dict | None:
    """Recover ``{rows, summary}`` from a captured ``/api/compare`` body. Unlike
    ``/api/scan`` this is a finite JSON response (the sidecar's ``compare()`` output
    returned whole), so a plain JSON parse - not the SSE frame walk - is correct."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _min_row_ipv4(rows: list) -> str | None:
    """The smallest real IPv4 among compare rows, used as the recorded scan-range
    start for a compare-capture run. ``/api/compare`` carries no scan range (it
    re-RAGs the last scan's devices), so the honest range we can prove is the span
    of the rows themselves; the captured replay does no I/O, so this is metadata
    only. Skips placeholder/missing IPs (``—``, blank).
    ponytail: the operator's original range is not recoverable on a bare compare -
    the row span is the faithful reconstruction, not a guess at their input."""
    best: ipaddress.IPv4Address | None = None
    for row in rows:
        ip = row.get("ip") if isinstance(row, dict) else None
        try:
            addr = ipaddress.IPv4Address(str(ip))
        except (ipaddress.AddressValueError, ValueError):
            continue
        if best is None or addr < best:
            best = addr
    return str(best) if best is not None else None


def _persist_ip_scan_result(
    session: scanner_raw_session.PanelSession,
    principal: AuthPrincipal,
    query: str,
    raw: bytes,
) -> None:
    """Results-out (pipe 3): after a real IP scan completes in the panel, persist
    its results as a genuine ``ip_scanner`` discovery run so the Results tab, run
    history, and reports light up - reusing the sidecar engine as the parser via a
    one-shot client (no network I/O). Best-effort: never raised into the browser
    stream, and only fires when the SSE carried a final ``result`` frame with rows."""
    try:
        result = _parse_scan_result(raw)
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            return
        start_values = parse_qs(query).get("start")
        start_ip = start_values[0] if start_values else None
        if not start_ip:
            return
        # Lazy import avoids any route-module import cycle at load time.
        from app.api.routes.scanners import dispatch_captured_ip_scanner_run

        dispatch_captured_ip_scanner_run(
            project_id=session.project_id,
            site_id=session.site_id,
            principal=principal,
            start_ip=start_ip,
            captured={"rows": rows, "summary": result.get("summary") or {}},
        )
    except Exception:  # noqa: BLE001 - results-out is best-effort, never breaks the scan
        logger.warning("results-out persistence failed: %s", session.session_id, exc_info=True)


def _persist_bacnet_scan_result(
    session: scanner_raw_session.PanelSession,
    principal: AuthPrincipal,
    raw: bytes,
) -> None:
    """Results-out for BACnet: persist a completed panel scan as a real
    ``bacnet_scanner`` run. The scan's SSE ``result`` frame has the same shape as IP;
    the source NIC is resolved from SCT config in the dispatch helper (BACnet needs
    it and the panel query does not carry it). Best-effort."""
    try:
        result = _parse_scan_result(raw)
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            return
        from app.api.routes.scanners import dispatch_captured_bacnet_scanner_run

        dispatch_captured_bacnet_scanner_run(
            project_id=session.project_id,
            site_id=session.site_id,
            principal=principal,
            captured={
                "rows": rows,
                "summary": result.get("summary") or {},
                "device_files": [],
                "routers": [],
            },
        )
    except Exception:  # noqa: BLE001 - results-out is best-effort, never breaks the scan
        logger.warning("bacnet results-out persistence failed: %s", session.session_id, exc_info=True)


def _persist_mqtt_scan_result(
    session: scanner_raw_session.PanelSession,
    principal: AuthPrincipal,
    raw: bytes,
) -> None:
    """Results-out for MQTT: persist a completed panel export-archive as a real
    ``mqtt_scanner`` run. MQTT is a live stream, so the capture point is the finite
    export ZIP (never per message); its manifest + payloads replay through the
    engine. Best-effort; skips an empty capture."""
    try:
        from smart_commissioning_core.engines.mqtt_scanner_sidecar import _read_export_zip

        manifest, payloads = _read_export_zip(raw)
        if not payloads:
            return
        from app.api.routes.scanners import dispatch_captured_mqtt_scanner_run

        dispatch_captured_mqtt_scanner_run(
            project_id=session.project_id,
            site_id=session.site_id,
            principal=principal,
            captured={"manifest": manifest, "payloads": payloads},
        )
    except Exception:  # noqa: BLE001 - results-out is best-effort, never breaks the export
        logger.warning("mqtt results-out persistence failed: %s", session.session_id, exc_info=True)


def _persist_ip_compare_result(
    session: scanner_raw_session.PanelSession,
    principal: AuthPrincipal,
    raw: bytes,
) -> None:
    """Results-out for an IP re-compare: after the operator loads/changes a register
    in the panel and re-RAGs the last scan (``/api/compare``), persist that verdict
    as a real ``ip_scanner`` run so the register-aware RAG the operator saw lands in
    SCT - not just the raw scan captured earlier. Same ``{rows, summary}`` the scan
    produces, so it replays through the same captured dispatch; the scan-range start
    is reconstructed from the rows (compare carries none). Best-effort."""
    try:
        result = _parse_compare_result(raw)
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            return
        start_ip = _min_row_ipv4(rows)
        if not start_ip:
            return
        from app.api.routes.scanners import dispatch_captured_ip_scanner_run

        dispatch_captured_ip_scanner_run(
            project_id=session.project_id,
            site_id=session.site_id,
            principal=principal,
            start_ip=start_ip,
            captured={"rows": rows, "summary": result.get("summary") or {}},
        )
    except Exception:  # noqa: BLE001 - results-out is best-effort, never breaks the compare
        logger.warning("ip compare results-out persistence failed: %s", session.session_id, exc_info=True)


def _persist_bacnet_compare_result(
    session: scanner_raw_session.PanelSession,
    principal: AuthPrincipal,
    raw: bytes,
) -> None:
    """Results-out for a BACnet re-compare: persist a ``/api/compare`` verdict as a
    real ``bacnet_scanner`` run, mirroring the BACnet scan capture. BACnet needs no
    scan-range start (the dispatch helper freezes the source NIC from SCT config),
    so the compare ``{rows, summary}`` replays directly. Best-effort."""
    try:
        result = _parse_compare_result(raw)
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            return
        from app.api.routes.scanners import dispatch_captured_bacnet_scanner_run

        dispatch_captured_bacnet_scanner_run(
            project_id=session.project_id,
            site_id=session.site_id,
            principal=principal,
            captured={
                "rows": rows,
                "summary": result.get("summary") or {},
                "device_files": [],
                "routers": [],
            },
        )
    except Exception:  # noqa: BLE001 - results-out is best-effort, never breaks the compare
        logger.warning("bacnet compare results-out persistence failed: %s", session.session_id, exc_info=True)


@router.api_route(
    "/{proto}/raw/{path:path}",
    methods=["GET", "POST", "DELETE", "HEAD"],
    dependencies=[Depends(require_engineer)],
)
async def proxy_scanner_raw(
    proto: str, path: str, request: Request, principal: AuthPrincipal = Depends(get_principal)
) -> Response:
    name = SIDECAR_BY_PROTO.get(proto)
    if name is None:
        raise HTTPException(status_code=404, detail="Unknown scanner protocol.")
    if path == "sct-bridge.js":
        return Response(_BRIDGE_JS, media_type="text/javascript")
    if path == "sct-theme.css":
        return Response(_THEME_CSS, media_type="text/css")

    body = await request.body()
    session = scanner_raw_session.resolve(request.cookies.get(COOKIE_NAME))
    if session is not None and session.proto != proto:
        # Defense in depth for REV-7: a session is valid only for the protocol it
        # was opened for. A cookie from another protocol's panel must not authorize
        # this one's writes or be attributed here - drop it, so a write fails closed
        # (no valid token) and nothing is recorded under the wrong protocol. Reads
        # still proxy (role-gated), just unattributed.
        session = None
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

    # Results-out: capture the completed body and persist it as a real run where the
    # protocol has a discrete "done" signal - IP/BACnet scan SSE result frame, the
    # IP/BACnet re-compare JSON, MQTT the finite export-archive ZIP. Needs a session;
    # the scan captures are GET SSE, compare is a POST with a finite JSON body.
    on_complete: Callable[[bytes], None] | None = None
    if session is not None:
        capture_session, capture_principal, capture_query = session, principal, request.url.query
        if request.method == "GET" and proto == "ip" and path == "api/scan" and is_sse:

            def on_complete_ip(raw: bytes) -> None:
                _persist_ip_scan_result(capture_session, capture_principal, capture_query, raw)

            on_complete = on_complete_ip
        elif request.method == "GET" and proto == "bacnet" and path == "api/scan" and is_sse:

            def on_complete_bacnet(raw: bytes) -> None:
                _persist_bacnet_scan_result(capture_session, capture_principal, raw)

            on_complete = on_complete_bacnet
        elif request.method == "GET" and proto == "mqtt" and path == "api/export-archive":

            def on_complete_mqtt(raw: bytes) -> None:
                _persist_mqtt_scan_result(capture_session, capture_principal, raw)

            on_complete = on_complete_mqtt
        elif request.method == "POST" and proto == "ip" and path == "api/compare":

            def on_complete_ip_compare(raw: bytes) -> None:
                _persist_ip_compare_result(capture_session, capture_principal, raw)

            on_complete = on_complete_ip_compare
        elif request.method == "POST" and proto == "bacnet" and path == "api/compare":

            def on_complete_bacnet_compare(raw: bytes) -> None:
                _persist_bacnet_compare_result(capture_session, capture_principal, raw)

            on_complete = on_complete_bacnet_compare

    return StreamingResponse(
        _relay(client, upstream, cap=is_sse, on_complete=on_complete),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
