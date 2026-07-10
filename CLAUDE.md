# AGENTS.md

Guidance for coding agents working in this repo. `CLAUDE.md` is a copy of this
file — edit `AGENTS.md` and copy it over so the two stay identical.

## What this is

Smart Commissioning Tool: a React + TypeScript frontend, a FastAPI backend, a
Dramatiq worker, and a shared `smart_commissioning_core` package, for
commissioning building IP / BACnet / MQTT / UDMI devices. Pre-1.0.

## Layout

- `core/` — `smart_commissioning_core`: engines (ip_scan, bacnet/mqtt discovery,
  validation), UDMI logic, persistence (SQLAlchemy + Alembic). Imported by both
  backend and worker.
- `backend/` — FastAPI app (`app.main:app`): routes → services → repositories.
- `worker/` — Dramatiq actors for queued runs.
- `frontend/` — Vite app; dev server on :5173, proxies `/api` → :8000.
- `infra/` — Docker Compose stack. `docs/` — reference + review guides.

## Setup (Python 3.12, Node 22)

```bash
pip install -e ./core -e ./backend -e ./worker
pip install ruff mypy
cd frontend && npm ci
```

## Tests — match CI (Python uses stdlib `unittest`, not pytest)

```bash
python -m unittest discover -s core/tests
cd backend && python -m unittest discover -s tests
cd frontend && npm test -- --run
```

`pytest` also runs these unittest-style suites if you prefer it, but CI runs
`unittest` — keep that the source of truth.

## Lint / typecheck

```bash
ruff check backend worker core      # lint gate
mypy backend/app                    # informational only, never blocks CI
cd frontend && npm run lint && npm run typecheck && npm run build
```

## Run locally

```bash
# Full stack (Postgres, Redis, API, worker, frontend):
sh scripts/bootstrap-env.sh   # once: writes infra/.env with random secrets (Windows: pwsh scripts/bootstrap-env.ps1)
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build

# Or split: backend then frontend
cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
npm --prefix frontend run dev       # http://localhost:5173
```

## CI gates (`.github/workflows/ci.yml`)

`python` (ruff + core/backend unittest; mypy `continue-on-error`), `frontend`
(npm ci, lint, typecheck, `npm test -- --run`, build), and `sbom`. Default test
collection order is alphabetical — keep it so.

## Conventions

- **Commits**: Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`). Log notable changes in `CHANGELOG.md`. See `CONTRIBUTING.md`.
- **Sync**: after a verified commit, push its feature branch unless the user
  explicitly asks to keep it local or the remote is unavailable. Never merge or
  publish a release without the user's authorization.
- **Portable releases**: give `packaging/windows_portable/build.ps1` an explicit
  `-Version vX.Y.Z` when producing a release. It writes that version into both
  `README_FIRST.txt` and the Windows EXE Properties → Details metadata.
- **Style**: smallest correct change, reuse before adding, stdlib before deps.
  Deliberate shortcuts carry a `ponytail:` comment naming the ceiling.
- **Honesty**: engines never fake success — unreachable broker / unauthorized
  scan record a real status, not a fabricated result.

## UDMI payload views

- **Expected payload panels are templates, not observations.** Render the real
  UDMI state/metadata/pointset shape; use the register's values where known and
  schema-valid sentinel values for device-supplied fields. Never copy observed
  broker values into the expected side.
- **Point contract**: expected points appear in both `metadata.pointset.points`
  and `pointset.points`; expected units apply only to metadata point definitions.
- **Schema and register checks are independent.** A payload can match the
  register yet still fail canonical UDMI structural validation, and both results
  must remain visible to the operator.

## Gotchas

- **Worktree + editable install**: in a `git worktree`, `import
  smart_commissioning_core` resolves to the **main checkout's** installed
  package, not the worktree's `core/`. To exercise worktree `core/` changes,
  run with `PYTHONPATH=<worktree>/core` prepended (or reinstall core from the
  worktree).
- **npm lockfile**: use the npm bundled with Node 22 in this repo/toolchain.
  After regenerating `frontend/package-lock.json`, run `npm ci`, lint,
  typecheck, tests, and build before committing.
- **Real scans need authorization**: discovery/publish engines require
  `parameters.authorized = true` (or `scan_authorization`); a `dry_run` previews
  with no I/O and needs none.
