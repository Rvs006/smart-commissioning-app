import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from smart_commissioning_core.db.migrate import upgrade_to_head

from app.api.router import api_router
from app.core.config import get_settings
from app.core.runtime import ensure_runtime_directories

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Re-read settings at startup time (not module import time) so test
    # harnesses that override the environment per test class are honored.
    startup_settings = get_settings()
    if startup_settings.auth_mode == "api_key" and not (startup_settings.api_key or "").strip():
        logger.warning(
            "AUTH_MODE is 'api_key' but API_KEY is not set; "
            "all authenticated API requests will be rejected (fail closed).",
        )
    if startup_settings.auto_migrate:
        # The backend owns the schema: create/upgrade it before serving.
        ensure_runtime_directories()
        upgrade_to_head(startup_settings.database_url)
    yield


app = FastAPI(
    title="Smart Commissioning Tool API",
    version="0.1.0",
    summary="Production scaffold for commissioning configuration, discovery, validation, and reporting.",
    lifespan=lifespan,
)

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


@app.get("/{spa_path:path}", include_in_schema=False, response_model=None)
def spa_fallback(spa_path: str):
    index_path = FRONTEND_DIST / "index.html"
    if index_path.exists() and not spa_path.startswith("api/"):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Route not found.")
