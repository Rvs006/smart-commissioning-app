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
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_access_key: str = "minioadmin"
    object_storage_secret_key: str = "minioadmin"
    object_storage_bucket: str = "commissioning-evidence"
    job_execution_mode: Literal["auto", "queue", "inline"] = "auto"
    allow_inline_worker_fallback: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
