import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from smart_commissioning_core.db.migrate import upgrade_to_head
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import (
    REQUEST_ID_HEADER,
    configure_file_logging,
    configure_logging,
    new_request_id,
    propagate_uvicorn_loggers,
    reset_request_id,
    set_request_id,
)
from app.core.observability import (
    HTTP_REQUESTS_IN_PROGRESS,
    observe_request,
    render_latest,
    route_template,
)
from app.core.runtime import ensure_runtime_directories
from app.services.log_service import (
    LOG_DIR,
    apply_logging_settings,
    purge_old_logs,
    retention_days,
)

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Re-read settings at startup time (not module import time) so test
    # harnesses that override the environment per test class are honored.
    startup_settings = get_settings()
    # Install the structured JSON log formatter + correlation filter before
    # anything else logs during startup.
    startup_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(startup_level)
    # Always install the local rotating file handler with env/INFO defaults, so
    # file logging exists even if the configured settings below cannot be read
    # (fresh or unmigrated DB). apply_logging_settings re-points it on success.
    configure_file_logging(LOG_DIR, startup_level)
    # Re-point uvicorn's loggers to propagate to root, so unhandled-500
    # tracebacks (logged by uvicorn.error, whose handler does NOT reach root by
    # default) land in app.log on both the portable exe and the dev uvicorn path.
    # Runs here deliberately: uvicorn has already configured these loggers before
    # the app lifespan starts.
    propagate_uvicorn_loggers()
    if startup_settings.auth_mode == "api_key" and not (startup_settings.api_key or "").strip():
        logger.warning(
            "AUTH_MODE is 'api_key' but API_KEY is not set; "
            "all authenticated API requests will be rejected (fail closed).",
        )
    if startup_settings.auto_migrate:
        # The backend owns the schema: create/upgrade it before serving.
        ensure_runtime_directories()
        upgrade_to_head(startup_settings.database_url)
    # Apply the operator-configured logging level and run the startup retention
    # purge. Fully guarded (the metrics scrape / crash-logger precedent: startup
    # side-effects must never block boot); the DB read must follow the migration
    # above. When auto_migrate is False on a virgin DB this simply falls through
    # to the console+file defaults installed above.
    try:
        from app.services.configuration_service import ConfigurationService

        logging_values = ConfigurationService().load(mask_secrets=False).logging.values
        apply_logging_settings(logging_values)
        purge_old_logs(LOG_DIR, retention_days(logging_values))
    except Exception:  # noqa: BLE001 (configured logging is best-effort; never block startup)
        logger.debug("Could not apply configured logging settings.", exc_info=True)
    # Orphan-run sweep: a run left at status "running" by an application restart
    # (or a crash mid-persist) never reaches a terminal status on its own, so the
    # UI shows a run that spins forever. Mark such interrupted runs failed with an
    # actionable message. Only inline runs are swept — a run handed to the worker
    # queue may still be executing on a worker in the hosted deployment. Guarded
    # like every startup side-effect above: it must never block boot.
    try:
        from app.services.run_service import RunService

        swept = RunService().sweep_interrupted_runs()
        if swept:
            logger.warning(
                "Startup swept %d interrupted run(s) to failed: %s",
                len(swept),
                ", ".join(swept),
            )
    except Exception:  # noqa: BLE001 (orphan sweep is best-effort; never block startup)
        logger.debug("Orphan-run sweep failed.", exc_info=True)
    yield


app = FastAPI(
    title="Smart Commissioning Tool API",
    version="0.1.0",
    summary="Production scaffold for commissioning configuration, discovery, validation, and reporting.",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Log the full traceback for any unhandled error, then return the standard 500.

    Belt-and-braces alongside the uvicorn.error re-pointing: this logs through an
    app logger (which reaches the rotating file handler) BEFORE the response goes
    out, so a 500 traceback lands in app.log even if uvicorn's own error logging
    is ever reconfigured. The response body is byte-for-byte FastAPI's default
    500, so clients see no change. Starlette still re-raises after this returns,
    so nothing else in the error path changes.
    """
    logger.error(
        "Unhandled exception handling %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


@app.exception_handler(StarletteHTTPException)
async def log_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """Log 4xx/5xx HTTPException rejections, then defer to FastAPI's default handler.

    Adds a WARNING breadcrumb (method, path, status, detail) for client-error
    rejections — an RBAC 403, a missing-target 400, a 404 — so a log bundle tells
    the session story; 5xx HTTPExceptions (e.g. a 503 when the queue is down) log
    at ERROR. The response is unchanged: the FastAPI default handler builds it.
    """
    if 400 <= exc.status_code < 500:
        logger.warning(
            "HTTP %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail
        )
    elif exc.status_code >= 500:
        logger.error(
            "HTTP %s on %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail
        )
    return await http_exception_handler(request, exc)

# Interactive docs and the OpenAPI schema disclose the full API surface, so
# they are only served in local (loopback-only) mode; hosted api_key
# deployments answer 404 for unauthenticated schema endpoints. Checked per
# request (not at import) so test harnesses overriding AUTH_MODE per class
# are honored.
_SCHEMA_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


@app.middleware("http")
async def _gate_schema_endpoints(request: Request, call_next):  # noqa: ANN001, ANN201
    if request.url.path in _SCHEMA_PATHS and get_settings().auth_mode == "api_key":
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


@app.middleware("http")
async def _record_request_metrics(request: Request, call_next):  # noqa: ANN001, ANN201
    # /metrics must not measure itself (and is not auth/schema gated).
    if request.url.path == "/metrics":
        return await call_next(request)
    start = time.perf_counter()
    in_progress = HTTP_REQUESTS_IN_PROGRESS.labels(method=request.method)
    in_progress.inc()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        in_progress.dec()
        observe_request(request.method, route_template(request), status_code, time.perf_counter() - start)


@app.middleware("http")
async def _bind_request_id(request: Request, call_next):  # noqa: ANN001, ANN201
    # Outermost middleware (registered last): accept an inbound X-Request-ID or
    # mint a fresh uuid, bind it to the logging contextvar so every downstream
    # log line carries it, and echo it on the response. Ordered BEFORE the
    # schema-gate so even gated 404s are correlated.
    inbound = request.headers.get(REQUEST_ID_HEADER)
    request_id = inbound.strip() if inbound and inbound.strip() else new_request_id()
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response

# The API uses header-based auth only (X-API-Key / Authorization), never
# cookies or sessions, so credentialed CORS stays disabled.
app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_origins=settings.cors_origin_list,
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus exposition endpoint.

    Exposed at the APP level (not under /api/v1), and intentionally exempt from
    auth and the schema-gate: scrapers are unauthenticated infrastructure, so in
    production this MUST be bound to an internal network / not exposed publicly.
    Runs-by-status is refreshed cheaply at scrape time from the run store.
    """
    _refresh_runs_by_status()
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


def _refresh_runs_by_status() -> None:
    """Populate the runs-by-status gauge; never fails the scrape on DB error."""
    from app.core.observability import set_runs_by_status

    try:
        from smart_commissioning_core.db.models import Run
        from sqlalchemy import func, select

        from app.core.db import get_engine

        with get_engine().connect() as connection:
            rows = connection.execute(
                select(Run.status, func.count()).group_by(Run.status),
            ).all()
        set_runs_by_status({str(status): int(count) for status, count in rows})
    except Exception:  # noqa: BLE001 (metrics scrape must never 500 on a DB hiccup)
        logger.debug("Could not refresh runs-by-status gauge.", exc_info=True)


FRONTEND_DIST = Path(
    os.environ.get(
        "SCT_FRONTEND_DIST",
        Path(__file__).resolve().parents[2] / "frontend" / "dist",
    ),
)

if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")


@app.get("/", response_model=None)
def root():
    index_path = FRONTEND_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)

    return {
        "service": app.title,
        "version": app.version or "0.1.0",
        "environment": settings.environment,
    }


def _resolve_frontend_file(spa_path: str) -> Path | None:
    """Map a request path to a real file inside FRONTEND_DIST, or None for SPA routes.

    Vite copies frontend/public/ to the dist ROOT (not into dist/assets/), so the
    /assets mount never sees files like electracom-logo.png; without this they
    fell through to the SPA fallback and the browser got index.html for a PNG.

    Any path that escapes the dist root -- '../' traversal, an absolute or
    drive-qualified path -- raises 404 and is never served. The is_relative_to
    check is load-bearing rather than just '..' screening: joining an absolute
    component (e.g. 'C:/Windows/win.ini') REPLACES the base.

    Junk that stays inside the root (e.g. an embedded NUL, which pathlib reports
    as simply "not a file") is not special-cased: it resolves to no file and so
    gets index.html like any other unknown SPA route.
    """
    dist_root = FRONTEND_DIST.resolve()
    try:
        candidate = (dist_root / spa_path).resolve()
        inside = candidate.is_relative_to(dist_root)
        found = inside and candidate.is_file()
    except (OSError, ValueError):
        # Defensive: resolve() can still raise on some malformed Windows paths.
        raise HTTPException(status_code=404, detail="Route not found.") from None
    if not inside:
        raise HTTPException(status_code=404, detail="Route not found.")
    return candidate if found else None


@app.get("/{spa_path:path}", include_in_schema=False, response_model=None)
def spa_fallback(spa_path: str):
    index_path = FRONTEND_DIST / "index.html"
    if not index_path.exists() or spa_path.startswith("api/"):
        # Backend-only deployments (no built dist) and unknown /api/* paths keep
        # answering 404; the api/ gate stays AHEAD of file resolution.
        raise HTTPException(status_code=404, detail="Route not found.")
    static_file = _resolve_frontend_file(spa_path)
    if static_file is not None:
        # No media_type: starlette infers it from the extension (.png -> image/png).
        return FileResponse(static_file)
    return FileResponse(index_path)
