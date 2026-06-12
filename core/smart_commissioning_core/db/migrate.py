"""Programmatic Alembic migrations (backend startup calls upgrade_to_head)."""

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config

_CORE_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = _CORE_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_PATH = _CORE_ROOT / "alembic"


def build_alembic_config(url: str | None = None) -> Config:
    """Build an Alembic Config pointing at the core migration scripts.

    The URL is passed as ``-x db_url=...`` so it takes the same top precedence
    as on the alembic CLI (see core/alembic/env.py).
    """
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    if url is not None:
        config.cmd_opts = Namespace(x=[f"db_url={url}"])
    return config


def upgrade_to_head(url: str) -> None:
    """Create/upgrade the schema at ``url`` to the latest revision.

    Idempotent: running against an already-migrated database is a no-op.
    """
    command.upgrade(build_alembic_config(url), "head")
