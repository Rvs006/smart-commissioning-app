"""Database persistence layer shared by the Smart Commissioning API and worker.

Modules:
- base: SQLAlchemy declarative base with an Alembic-friendly naming convention.
- models: ORM models (Project, Site, Run, RunIssue, ConfigurationSnapshot, ImportRecord).
- engine: engine/session factories and the default SQLite URL helper.
- db_run_store: DbRunStore implementing the shared RunStore protocol.
- repositories: ConfigurationRepository and ImportRepository.
- migrate: programmatic Alembic upgrade entrypoint (upgrade_to_head).
"""
