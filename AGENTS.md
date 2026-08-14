# AGENTS.md

Guidance for coding agents working in this repo. `CLAUDE.md` is a copy of this
file - edit `AGENTS.md` and copy it over so the two stay identical.

## What this is

Smart Commissioning Tool: a React + TypeScript frontend, a FastAPI backend, a
Dramatiq worker, and a shared `smart_commissioning_core` package, for
commissioning building IP / BACnet / MQTT / UDMI devices. Pre-1.0.

## Layout

- `core/` - `smart_commissioning_core`: engines (ip_scan, bacnet/mqtt discovery,
  validation), UDMI logic, persistence (SQLAlchemy + Alembic). Imported by both
  backend and worker.
- `backend/` - FastAPI app (`app.main:app`): routes → services → repositories.
- `worker/` - Dramatiq actors for queued runs.
- `frontend/` - Vite app; dev server on :5173, proxies `/api` → :8000.
- `infra/` - Docker Compose stack. `docs/` - reference + review guides.

## Setup (Python 3.12, Node 22.22+ or 24)

```bash
pip install -e ./core -e ./backend -e ./worker
pip install ruff mypy
cd frontend && npm ci
```

## Tests - match CI (Python uses stdlib `unittest`, not pytest)

```bash
python -m unittest discover -s core/tests
cd backend && python -m unittest discover -s tests
cd frontend && npm test -- --run
```

`pytest` also runs these unittest-style suites if you prefer it, but CI runs
`unittest` - keep that the source of truth.

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
collection order is alphabetical - keep it so.

## Conventions

- **Model routing (Claude Code)**: do planning, architecture, sizing, and
  root-cause investigation on **Fable (`claude-fable-5`)**; write the code on
  **Opus 4.8 (`claude-opus-4-8`)** - switch model for the implementation phase
  or delegate implementation subagents with `model: claude-opus-4-8`.
- **Current handoff**: status as of 2026-08-14. The v0.1.43 candidate keeps the
  v0.1.42 discovery recovery work and makes the operator Nmap route practical:
  Configuration no longer carries a manual authority form, a global
  administrator can approve one signed local installation from IP Scanner, and
  engineers reuse that recorded approval. The unified portable EXE still
  excludes Nmap/Npcap files; it enables the local approval route only. Field
  acceptance remains open until the approved register, applicability matrix,
  paired collector evidence, and complete cadence run pass
  `docs/v0.1.43-field-acceptance-checklist.md`.
  A one-minute operator Stop is cancellation evidence, not cadence evidence.
  The latest public release is v0.1.42; v0.1.43 is the current candidate. Keep
  source, raw evidence, report bytes, EXE identity, Docker labels, and release
  SHA bound to the same run or commit.
- **Version bookkeeping**: whenever the application version changes, or a
  release is published, update this handoff in both `AGENTS.md` and `CLAUDE.md`
  in the same commit. Keep the two files byte-for-byte identical, and record the
  latest public release, current candidate, and field-acceptance state.
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
  header documents the PS 5.1 traps it exists to avoid - do not hand-roll the
  download/zip steps again.
- **Style**: smallest correct change, reuse before adding, stdlib before deps.
  Deliberate shortcuts carry a `ponytail:` comment naming the ceiling.
- **Honesty**: engines never fake success - unreachable broker / unauthorized
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

## Humanized, anti-AI-slop messages

- Whenever the user asks for a message, email, handoff, release note, status
  update, or engineer-facing summary, apply the Humanizer skill and the
  anti-AI-slop writing rules before showing the draft. Do not wait for the
  user to repeat this instruction.
- Put the ready-to-send message first. Keep the user's casual, direct voice,
  use exact versions, commit IDs, test names, file paths, and observed results,
  and keep claims tied to evidence. Cut stock openers, generic summaries,
  rhetorical questions, fake experience, repeated sentence patterns, banned
  buzzwords, and decorative AI phrasing.
- Include at least one concrete detail and one honest limitation when they
  matter. Prefer plain sentences with varied rhythm. Avoid em dashes, emoji
  bullets, and decorative formatting in professional messages. Technical
  precision takes priority over forced informality.
- For edits, preserve the user's facts and meaning. If useful, provide a
  short violations note after the rewritten message, never before it.

- **Worktree + editable install**: in a `git worktree`, `import
  smart_commissioning_core` resolves to the **main checkout's** installed
  package, not the worktree's `core/`. To exercise worktree `core/` changes,
  run with `PYTHONPATH=<worktree>/core` prepended (or reinstall core from the
  worktree).
- **npm lockfile**: React Router requires Node 22.22 or later. CI and release
  workflows use Node 24, so use Node 24 when regenerating the lockfile for a
  release change.
  After regenerating `frontend/package-lock.json`, run `npm ci`, lint,
  typecheck, tests, and build before committing.
- **Real scans need authorization**: discovery/publish engines require
  `parameters.authorized = true` (or `scan_authorization`); a `dry_run` previews
  with no I/O and needs none.
- **Locked-down (ThreatLocker/WDAC) machines: no local Python, but ruff still
  works via WASM.** On managed corporate laptops the application allowlist
  denies `ruff.exe` and ringfences `python.exe` so it cannot even *read* `.py`
  files in this repo (`PermissionError`) - so `ruff check`, `unittest`, and
  running the backend are all impossible locally, and **CI on a pushed branch is
  the only Python validation path**. Node is not ringfenced, so ruff's WASM build
  gives you a real lint gate:

  ```bash
  # in a scratch dir, NOT the repo - do not add this to any package.json
  npm install @astral-sh/ruff-wasm-nodejs
  ```

  Then `new Workspace({...})` mirroring `ruff.toml` (`select`, `line-length`,
  and the `flake8-bugbear.extend-immutable-calls` list - omit it and every
  FastAPI `= Depends(...)` reports a false `B008`), and feed it each file's
  source read with node's `fs`. Caveat: the Workspace gets no `src` setting, so
  **`I001` is unreliable** for first-party `app.*` imports (they lint clean in
  real CI) - but **`invalid-syntax` findings are config-independent and
  trustworthy**. This is worth doing before any push: a stray tool-call XML tag
  left in a test file once reddened `main` as a plain syntax error, and because
  ruff runs before the unit tests it blocked the whole suite.
