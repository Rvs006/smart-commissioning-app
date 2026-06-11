"""SQLAlchemy engine for the worker process.

The worker never runs migrations — the backend owns the schema and applies
Alembic migrations on startup (see worker/README.md).
"""

from functools import lru_cache
from pathlib import Path

from smart_commissioning_core.db.engine import create_engine_from_url
from sqlalchemy.engine import Engine, make_url

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database:
        # Ensure the runtime directory exists so SQLite can open the file.
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    return create_engine_from_url(url)
