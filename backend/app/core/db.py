"""Process-wide SQLAlchemy engine for the API service."""

from functools import lru_cache

from smart_commissioning_core.db.engine import create_engine_from_url
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from app.core.runtime import ensure_runtime_directories


@lru_cache
def get_engine() -> Engine:
    """Return the shared engine built from settings.database_url.

    Runtime directories are ensured first so the default SQLite database file
    can be created under the runtime root on first connect.
    """
    ensure_runtime_directories()
    return create_engine_from_url(get_settings().database_url)
