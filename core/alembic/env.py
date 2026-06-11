"""Alembic environment for the smart_commissioning_core schema."""

import os
from logging.config import fileConfig

from alembic import context

# Importing models registers all tables on Base.metadata for autogenerate.
from smart_commissioning_core.db import models  # noqa: F401
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Resolve the database URL: -x db_url, then DATABASE_URL, then alembic.ini."""
    x_arguments = context.get_x_argument(as_dictionary=True)
    if x_arguments.get("db_url"):
        return x_arguments["db_url"]
    environment_url = os.environ.get("DATABASE_URL")
    if environment_url:
        return environment_url
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    engine = create_engine_from_url(_resolve_url())

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Batch mode so future ALTERs work on SQLite as well as Postgres.
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
