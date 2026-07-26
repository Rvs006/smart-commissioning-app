"""Worker settings from environment variables (stdlib only, no .env file)."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from smart_commissioning_core.db.engine import default_sqlite_url

# Same SQLite default as the backend (backend/runtime/smart_commissioning.db)
# so backend and worker hit the same database file in local development and
# compose setups that share a volume. Postgres deployments set DATABASE_URL.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class WorkerSettings:
    redis_url: str
    database_url: str
    deployment_id: str
    lease_seconds: int
    heartbeat_seconds: float


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        database_url=os.getenv(
            "DATABASE_URL", default_sqlite_url(_REPOSITORY_ROOT / "backend" / "runtime")
        ),
        deployment_id=os.getenv("SMART_COMMISSIONING_DEPLOYMENT_ID", "local-worker"),
        lease_seconds=max(15, int(os.getenv("RUN_LEASE_SECONDS", "60"))),
        heartbeat_seconds=max(1.0, float(os.getenv("RUN_HEARTBEAT_SECONDS", "15"))),
    )
