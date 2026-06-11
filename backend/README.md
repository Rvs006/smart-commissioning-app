# Smart Commissioning API

This service is the HTTP boundary for:

- configuration
- import workflows
- discovery runs
- validation runs
- reports

The current implementation is a scaffold with typed contracts and placeholder responses. It is intended to be expanded against the specification and the architecture document in `../docs/production-architecture.md`.

## Quickstart

The API depends on the shared `smart-commissioning-core` package in `../core`
(UDMI validation, MQTT transport, and the run processors). It is not published
to PyPI and `pyproject.toml` cannot declare a portable path dependency, so the
install order matters — install core first, then the API:

```bash
# from the repository root
pip install -e ./core -e ./backend

# run the API
cd backend
uvicorn app.main:app --reload
```

Run the tests with core installed (or on `PYTHONPATH`):

```bash
cd backend
python -m unittest discover -s tests
```

## Database

Run, import, and configuration records are persisted through the shared
`smart_commissioning_core.db` layer. By default the API uses a local SQLite
file at `backend/runtime/smart_commissioning.db`; set `DATABASE_URL`
(for example `postgresql+psycopg://...`, see `../infra/.env.example`) to use
Postgres instead. The API owns the schema and applies Alembic migrations on
startup; set `AUTO_MIGRATE=false` to disable that (for example when migrations
are applied out of band).

Uploaded import files and secret material stay on disk under
`backend/runtime/` — only `secret://` references are stored in the database.

### Migrating pre-database runtime state

If you have runtime state from before the database persistence layer
(`runtime/runs/*.json`, `runtime/configuration.json`, `runtime/imports/*.json`),
import it once with:

```bash
cd backend
python -m app.scripts.import_runtime_state
```

The script applies migrations first and is idempotent: runs/imports whose ids
already exist are skipped, and the configuration is only seeded when the
target project/site (defaults: `demo-project`/`demo-site`, override with
`--project-id`/`--site-id`) has no snapshot yet.
