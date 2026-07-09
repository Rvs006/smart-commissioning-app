# Smart Commissioning Worker

This worker owns long-running jobs for:

- IP discovery
- BACnet discovery
- MQTT discovery
- UDMI validation
- BACnet validation
- BACnet to MQTT mapping validation

UDMI validation and MQTT config publish validation are implemented by the shared
`smart-commissioning-core` package (`../core`), so the worker produces the same
issue records as the API. The worker registers an MQTT configuration provider
at import, so live UDMI capture and MQTT config publish resolve a broker host
from stored configuration or run parameters (see `app/mqtt_config_provider.py`
for the honest limits on mutual-TLS material). The real-broker capture/publish
behaviour is untested here and remains on-site-validation surface.

## Database

Run records are read and written through the shared database layer
(`smart_commissioning_core.db.DbRunStore`). The worker uses the same
`DATABASE_URL` as the API and defaults to the same SQLite file
(`../backend/runtime/smart_commissioning.db`) so both processes hit the same
database in local development.

The worker does NOT run migrations: the backend owns the schema and applies
Alembic migrations on startup. Start the API (or run its migrations) before
pointing a worker at a fresh database.

## Quickstart

`smart-commissioning-core` is not published to PyPI and `pyproject.toml` cannot
declare a portable path dependency, so the install order matters — install core
first, then the worker:

```bash
# from the repository root
pip install -e ./core -e ./worker

# run the worker
cd worker
dramatiq app.tasks
```
