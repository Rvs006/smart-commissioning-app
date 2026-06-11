# Smart Commissioning App

This repository currently contains:

- the functional specification in `Smart Commissioning Tool Specification.pdf`
- the original UI prototypes in `smart_commissiong_tool_ui.jsx`, `preview.html`, and `smart_commissioning_tool_FIXED_fast.html`
- the standalone UDMI payload validator in `device_udmi_payload_validation/`
- the first production scaffold in `frontend/`, `backend/`, `worker/`, `infra/`, and `docs/`

## Production Direction

The real application should be built as a multi-service system:

- `frontend/`: React + TypeScript + Vite operator UI
- `backend/`: FastAPI HTTP API and domain contracts
- `worker/`: background discovery and validation jobs
- `infra/`: local Docker Compose stack for API, worker, Postgres, Redis, and object storage
- `docs/`: product and architecture decisions

The existing HTML and JSX files remain reference material for the product workflow and visual design. They are not the production runtime.

## Repository Layout

```text
docs/
frontend/
backend/
worker/
infra/
device_udmi_payload_validation/
preview.html
smart_commissiong_tool_ui.jsx
Smart Commissioning Tool Specification.pdf
```

## Local Startup Plan

The scaffold is intended to become runnable with the following flow:

1. Start infrastructure:
   `docker compose -f infra/docker-compose.yml --env-file infra/.env.example up --build`
2. Frontend:
   `cd frontend && npm install && npm run dev`
3. Backend:
   `cd backend && python -m pip install -e . && uvicorn app.main:app --reload`
4. Worker:
   `cd worker && python -m pip install -e . && dramatiq app.tasks`

## What Exists Today

- `frontend/` is a clean shell for the real React app.
- `backend/` exposes the initial API surface and contracts.
- `backend/` now persists configuration and import metadata in `backend/runtime/` as a bootstrap storage layer before the PostgreSQL repository is introduced.
- `worker/` exposes the initial job names for discovery, UDMI validation, mapping validation, and reporting.
- `docs/production-architecture.md` maps the specification to the production build.
