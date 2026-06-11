from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from smart_commissioning_core.db.engine import default_sqlite_url

from app.core.runtime import RUNTIME_ROOT


class Settings(BaseSettings):
    environment: str = "development"
    # Portable/edge-friendly default: a SQLite file under the runtime root.
    # Deployments that run Postgres set DATABASE_URL explicitly (see infra/).
    database_url: str = default_sqlite_url(RUNTIME_ROOT)
    # Apply Alembic migrations (smart_commissioning_core.db.migrate) on startup.
    auto_migrate: bool = True
    redis_url: str = "redis://localhost:6379/0"
    # API authentication (enforced by app.core.auth.require_auth):
    # - "local" (default): only loopback clients are accepted, matching the
    #   portable desktop deployment where uvicorn binds 127.0.0.1. If api_key
    #   is also set, a valid key is accepted from any client address.
    # - "api_key": every request must present the configured key via the
    #   X-API-Key header or "Authorization: Bearer <key>".
    auth_mode: Literal["local", "api_key"] = "local"
    api_key: str | None = None
    # Comma-separated allowed CORS origins (env CORS_ORIGINS).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Upload limits for /api/v1/imports: cap on the request body / uploaded
    # file size, and a zip-bomb guard on the declared decompressed size of
    # XLSX archives.
    max_upload_bytes: int = 20 * 1024 * 1024
    max_xlsx_decompressed_bytes: int = 200 * 1024 * 1024
    job_execution_mode: Literal["auto", "queue", "inline"] = "auto"
    allow_inline_worker_fallback: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
