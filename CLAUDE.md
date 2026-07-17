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

- **Model routing (Claude Code)**: do planning, architecture, sizing, and
  root-cause investigation on **Fable (`claude-fable-5`)**; write the code on
  **Opus 4.8 (`claude-opus-4-8`)** — switch model for the implementation phase
  or delegate implementation subagents with `model: claude-opus-4-8`.
- **Current handoff**: status as of 2026-07-17 (evening) — **v0.1.11 through
  v0.1.16 are released** (`main`, CI green, workflow-built boot-smoked
  portable bundles attached; exe SHA-256 lives in each Release's notes,
  deliberately not in repo files; v0.1.16 is cut from this commit and
  publishes immediately after it). v0.1.16 is the post-field-session fix
  bundle: a 2026-07-17 live session on a real flat (no-BBMD) BACnet network
  showed every real discovery run returning HTTP 500 and freezing at
  "running" while the scan completed on the wire — raw bacpypes3 values
  hitting JSON persistence plus a double-fault in the run wrapper — and the
  log bundle came back empty because 500 tracebacks never reached `app.log`.
  Fixed: JSON-safe value normalization at the read boundary,
  terminal-status-on-every-path in `engines/base.py`, uvicorn-to-`app.log`
  routing + run-lifecycle breadcrumbs, and a startup sweep for runs orphaned
  at "running" (worker-queued runs excluded). **A BBMD is optional per site**
  — recorded as a hard requirement in `docs/protocol-conformance.md` §3;
  "local broadcast only" is the healthy flat-network state. Open questions:
  whether the 2026-07-20 lab network has a BBMD (the runbook rewrite hinges
  on it), and the BACnet pre-flight card planned for v0.1.17 after the lab.
  The punchlist's §4 deferred items and §5 open Pete decisions remain the
  live backlog, alongside GitHub issue #4 (production gates). Next hard date:
  the **2026-07-20** live lab session — `docs/lab-day-2026-07-20-runbook.md`.
  Update or supersede this block when the state changes.
- **This repo is PUBLIC.** Keep site names, real network addresses, device ids,
  personnel, and commercial detail out of code, docs, and commit messages.
  Technical root causes with file:line evidence are the point; operational
  specifics belong in private notes.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`). Log notable changes in `CHANGELOG.md`. See `CONTRIBUTING.md`.
- **Sync**: after a verified commit, push its feature branch unless the user
  explicitly asks to keep it local or the remote is unavailable. Never merge or
  publish a release without the user's authorization.
- **Portable releases**: give `packaging/windows_portable/build.ps1` an explicit
  `-Version vX.Y.Z` when producing a release. It writes that version into both
  `README_FIRST.txt` and the Windows EXE Properties → Details metadata. Publish
  with `scripts/release-portable.ps1` (Windows PowerShell 5.1-safe): it ships
  the CI artifact archive as the release zip, proves the version from inside
  the bundle, fills `{{EXE_SHA256}}` / `{{ZIP_SHA256}}` / `{{COMMIT}}` tokens
  in the notes file, and cross-checks GitHub's recorded asset digest after
  upload. `-VerifyExisting` re-verifies a published release read-only. Its
  header documents the PS 5.1 traps it exists to avoid — do not hand-roll the
  download/zip steps again.
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
- **Locked-down (ThreatLocker/WDAC) machines: no local Python, but ruff still
  works via WASM.** On managed corporate laptops the application allowlist
  denies `ruff.exe` and ringfences `python.exe` so it cannot even *read* `.py`
  files in this repo (`PermissionError`) — so `ruff check`, `unittest`, and
  running the backend are all impossible locally, and **CI on a pushed branch is
  the only Python validation path**. Node is not ringfenced, so ruff's WASM build
  gives you a real lint gate:

  ```bash
  # in a scratch dir, NOT the repo — do not add this to any package.json
  npm install @astral-sh/ruff-wasm-nodejs
  ```

  Then `new Workspace({...})` mirroring `ruff.toml` (`select`, `line-length`,
  and the `flake8-bugbear.extend-immutable-calls` list — omit it and every
  FastAPI `= Depends(...)` reports a false `B008`), and feed it each file's
  source read with node's `fs`. Caveat: the Workspace gets no `src` setting, so
  **`I001` is unreliable** for first-party `app.*` imports (they lint clean in
  real CI) — but **`invalid-syntax` findings are config-independent and
  trustworthy**. This is worth doing before any push: a stray tool-call XML tag
  left in a test file once reddened `main` as a plain syntax error, and because
  ruff runs before the unit tests it blocked the whole suite.
