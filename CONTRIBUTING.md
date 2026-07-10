# Contributing

Thanks for working on the Smart Commissioning App. This guide covers local
setup, how to run the checks CI runs, and the conventions we follow. The most
important rule is at the bottom: **live-infrastructure paths must stay honestly
marked** — we do not fabricate test results for paths that have not been run
against real hardware.

## Project layout

- `core/` — `smart_commissioning_core`, shared UDMI validation, MQTT logic, DB
  models/repositories, and Alembic migrations (Python).
- `backend/` — FastAPI HTTP API (`smart-commissioning-api`, Python).
- `worker/` — Dramatiq background jobs (`smart-commissioning-worker`, Python).
- `frontend/` — React + TypeScript + Vite operator UI.
- `infra/` — Docker Compose stack (API, worker, Postgres, Redis, object storage).
- `docs/` — architecture, runbook, security posture, and the on-site validation
  checklist.

## Setup

Requires **Python 3.12** and **Node 22** (which ships npm 10/11 — see the
lockfile note below).

Python (install the three editable packages):

```bash
pip install -e ./core -e ./backend -e ./worker
```

For linting/typing locally, also install the dev tooling CI uses:

```bash
pip install ruff mypy
```

Frontend:

```bash
cd frontend && npm ci
```

## Running tests

Python unit tests use the standard-library `unittest` runner (no pytest
required), matching CI:

```bash
# Core
python -m unittest discover -s core/tests

# Backend (run from the backend/ directory)
cd backend && python -m unittest discover -s tests
```

Frontend tests use Vitest:

```bash
cd frontend && npm test          # watch mode
cd frontend && npm test -- --run # single run, as CI runs it
```

## Linting and typechecking

Python — Ruff is the lint gate; mypy is informational:

```bash
ruff check backend worker core
mypy backend/app   # informational only (does not block CI)
```

Frontend — ESLint and the TypeScript compiler:

```bash
cd frontend && npm run lint
cd frontend && npm run typecheck
cd frontend && npm run build
```

## CI gates

CI (`.github/workflows/ci.yml`) runs three jobs on every pull request and push to `main`:

- **`python`** — installs `core`/`backend`/`worker`, runs `ruff check`, then the
  core and backend `unittest` suites. mypy runs per package but is
  `continue-on-error` (informational).
- **`frontend`** — `npm ci`, `npm run lint`, `npm run typecheck`,
  `npm test -- --run`, and `npm run build`.
- **`sbom`** — generates a CycloneDX SBOM + license inventory and runs the
  allowlist license gate. Currently `continue-on-error` (non-blocking) because
  the inventory depends on unpinned transitive versions; treat its warnings
  seriously even though they do not turn the build red.

A PR should be green on `python` and `frontend` before review. Run the relevant
commands locally before pushing.

### npm lockfile lesson

Regenerate `frontend/package-lock.json` with **npm 10** when you change
dependencies. npm 11 writes the optional-dependency tree in a format that npm 10
reads differently and rejects under `npm ci`, which previously broke the
frontend CI job. Keeping lockfiles npm-10-compatible avoids cross-npm install
failures.

## Branch and PR conventions

- Branch off `main`; never commit directly to `main`.
- Use short, descriptive branch names (e.g. `feature/edge-hub-sync`,
  `fix/secret-corruption`).
- Keep commits focused and write imperative-mood subjects ("Add edge-to-hub run
  synchronization"), consistent with the existing history.
- Push each verified feature-branch commit so the remote is the shared source
  of truth; merging and releasing remain separate deliberate actions.
- Open a pull request using the PR template and complete its checklist.
- Update `CHANGELOG.md` (the `[Unreleased]` section) and any affected `docs/`
  when your change is user- or operator-visible.

## Honesty rule: live-infrastructure paths

Several paths in this project were built without access to the real
infrastructure they target — active network scanning against a live BMS/OT
network, a real MQTT broker, Postgres/Redis, a remote sync hub, and the Docker
image build. These are tracked in
[docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md).

When you contribute:

- **Do not claim a live path passed unless it was actually run against real
  infrastructure.** Mark such paths as live-untested / simulated, the way the
  existing docs do (see `docs/protocol-conformance.md`).
- **Do not fabricate test output, broker captures, or scan results.** A passing
  unit test against a fixture is not the same as a passing live run — say which
  one you ran.
- If you do validate a live path on real hardware, record the evidence and check
  off the corresponding item in `docs/phase5-onsite-validation.md`.

Honest status is a feature of this tool. Keep it that way.
