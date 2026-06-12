from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from smart_commissioning_core.db.engine import default_sqlite_url

# Same SQLite default as the backend (backend/runtime/smart_commissioning.db)
# so backend and worker hit the same database file in local development and
# compose setups that share a volume. Postgres deployments set DATABASE_URL.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class WorkerSettings(BaseSettings):
    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = default_sqlite_url(_REPOSITORY_ROOT / "backend" / "runtime")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
