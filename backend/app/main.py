import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Smart Commissioning Tool API",
    version="0.1.0",
    summary="Production scaffold for commissioning configuration, discovery, validation, and reporting.",
)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
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
