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
python -m unittest tests.test_v1_review_contracts
```
