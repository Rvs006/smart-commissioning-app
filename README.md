# Smart Commissioning App

![CI](https://github.com/Rvs006/smart-commissioning-app/actions/workflows/ci.yml/badge.svg)

## Status

Production-hardened across phases 0–4b: persistence (SQLAlchemy/Alembic),
authentication + secret encryption at rest, real discovery/validation engines
with scan-safety controls, observability/evidence-integrity/backup-restore, and
signed edge → hub run synchronization. CI is green on the blocking `python`
and `frontend` jobs; the `sbom` license gate runs as a non-blocking
(`continue-on-error`) job.

Live-network paths — active scanning against real BMS/OT hardware, a real MQTT
broker, Postgres/Redis, a remote hub, and the Docker image build — were
developed without access to that infrastructure and are **pending on-site
validation**. See [docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md)
for the checklist that must pass before company-wide production rollout.

This repository currently contains:

- the functional specification in `Smart Commissioning Tool Specification.pdf`
- the standalone UDMI payload validator in `device_udmi_payload_validation/`
- the first production scaffold in `frontend/`, `backend/`, `worker/`, `infra/`, and `docs/`

The original UI prototypes (`smart_commissiong_tool_ui.jsx`, `preview.html`, `smart_commissioning_tool_FIXED_fast.html`, and the zip-inspector dev tools) were removed after the baseline commit and remain available in git history (baseline commit `3471050`).

## Production Direction

The real application should be built as a multi-service system:

- `frontend/`: React + TypeScript + Vite operator UI
- `backend/`: FastAPI HTTP API and domain contracts
- `worker/`: background discovery and validation jobs
- `infra/`: local Docker Compose stack for API, worker, Postgres, Redis, and object storage
- `docs/`: product and architecture decisions

The original HTML and JSX prototypes served as reference material for the product workflow and visual design; they were never the production runtime and can be retrieved from git history (baseline commit `3471050`).

## Repository Layout

```text
docs/
frontend/
backend/
worker/
infra/
packaging/
deliverables/
device_udmi_payload_validation/
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

## Docs

| Document | Covers |
| --- | --- |
| [docs/production-architecture.md](docs/production-architecture.md) | System model mapping the specification to the production build |
| [docs/runbook.md](docs/runbook.md) | Deploy, operate, and recover (hosted compose + edge/portable profiles) |
| [docs/security-posture.md](docs/security-posture.md) | Threat model, auth, secret handling, scan-safety, IEC 62443 alignment |
| [docs/sync-architecture.md](docs/sync-architecture.md) | Signed edge → hub run + evidence synchronization |
| [docs/observability.md](docs/observability.md) | Structured logs, Prometheus metrics, alerts/SLOs, crash log |
| [docs/backup-restore.md](docs/backup-restore.md) | Backup/restore + retention, RPO/RTO guidance per profile |
| [docs/protocol-conformance.md](docs/protocol-conformance.md) | UDMI/MQTT/BACnet support: tested vs. simulated vs. live-untested |
| [docs/SBOM.md](docs/SBOM.md) | Python dependency + license inventory (see `docs/SBOM.generated.md`) |
| [docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md) | On-site validation checklist for live-network/infra paths |
| [docs/v1-review-checklist.md](docs/v1-review-checklist.md) | V1 review notes mapped to the production scaffold |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, lint, CI gates, and
branch/PR conventions, and [CHANGELOG.md](CHANGELOG.md) for the change history.
