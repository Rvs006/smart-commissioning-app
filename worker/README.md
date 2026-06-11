# Smart Commissioning Worker

This worker owns long-running jobs for:

- IP discovery
- BACnet discovery
- MQTT discovery
- UDMI validation
- BACnet validation
- BACnet to MQTT mapping validation
- report generation

UDMI validation and MQTT config publish validation are implemented by the shared
`smart-commissioning-core` package (`../core`), so the worker produces the same
issue records as the API. The worker currently runs the validate-only paths: it
passes `live_capture=None` / `broker_publisher=None` because it has no broker
configuration access yet, so it never publishes to or captures from a live
broker. The remaining actors are placeholders that define the queue boundary and
payload shape.

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
