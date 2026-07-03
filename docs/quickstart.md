# Quickstart — validate a running stack in 5 minutes

> **Just want to use the app?** Follow **Get it running** in the
> [README](../README.md#get-it-running-pick-one-path) — you do not need anything
> on this page. This page is for verifying a deployment (smoke test) and for
> developers running from source.

This is the fast path to a **running** Smart Commissioning App plus a one-command
**smoke test** that proves the stack works end-to-end before you go on-site. It
covers both deployment profiles (plus a [developer run-from-source
profile](#c-developer-profile--run-from-source)):

- **Hosted (Docker Compose)** — `infra/docker-compose.yml`: nginx frontend,
  FastAPI API, Dramatiq worker, Postgres, password-protected Redis.
  `AUTH_MODE=api_key`. Use when several users share a server.
- **Portable / local** — the launcher under `packaging/windows_portable/`:
  binds `127.0.0.1` only, SQLite, jobs run inline, `AUTH_MODE=local` (no API
  key). Use on a technician laptop near the site network.

The smoke test only exercises **safe, side-effect-free** paths — health,
readiness, metrics, configuration, a UDMI validation against a **bundled fixture
(no network)**, and a **dry-run** IP discovery (a plan, no packets sent). It
never triggers a real (non-dry-run) active scan or a live broker publish.

---

## A. Hosted profile (Docker Compose)

Real ports: API on `127.0.0.1:8000`, frontend + `/api` proxy on
`127.0.0.1:8080`. Everything binds to loopback only.

### 1. Configure secrets

```sh
cd infra
cp .env.example .env
```

Edit `infra/.env` and replace every `CHANGE_ME` placeholder. Generate each
secret with `openssl rand -hex 32`:

| Variable | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Postgres password. |
| `REDIS_PASSWORD` | Redis `requirepass` password. |
| `API_KEY` | Shared key clients send as `X-API-Key` (because `AUTH_MODE=api_key`). |

`POSTGRES_DB` / `POSTGRES_USER` already have sane defaults; `DATABASE_URL` and
`REDIS_URL` are assembled inside `docker-compose.yml` — do not set them
yourself. Optional: `FRONTEND_PORT` (default 8080), `API_PORT` (default 8000).

Compose **fails fast** if any required secret is missing.

### 2. Bring the stack up

```sh
docker compose -f infra/docker-compose.yml up -d --build
```

Startup order is handled by healthchecks: Postgres and Redis become healthy, the
api applies Alembic migrations on startup, then the worker starts. Open the app
at <http://127.0.0.1:8080>.

### 3. Smoke-test it

> Optional — proves a deployment works; not needed for day-to-day use.

Use the **same `API_KEY`** you put in `infra/.env`.

Linux / macOS / CI (bash):

```sh
SC_API_KEY="<your API_KEY>" scripts/smoke_local.sh http://127.0.0.1:8000
```

Windows (PowerShell):

```powershell
$env:SC_API_KEY = '<your API_KEY>'
pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000
```

You can also point the smoke test at the nginx `/api` proxy on the frontend port
(`-BaseUrl http://127.0.0.1:8080`); both reach the same API. The script prints
`PASS`/`FAIL` per check and exits non-zero if anything fails.

> If `/api/v1/ready` returns 503, a required dependency is down — in hosted mode
> that is Postgres or Redis. Check `docker compose -f infra/docker-compose.yml ps`.

### 4. Seed demo data (optional)

Give the UI something to show — the demo configuration, a real UDMI validation
run with issues, and a dry-run discovery plan:

```sh
SC_API_KEY="<your API_KEY>" python scripts/seed_demo.py --base-url http://127.0.0.1:8000
```

`scripts/seed_demo.py` drives the real API (stdlib only, no extra deps) and is
safe to run more than once.

---

## B. Portable / local profile

This profile binds `127.0.0.1` only, uses SQLite, runs jobs inline, and defaults
to `AUTH_MODE=local` — so **no API key** is needed for loopback clients.

### 1. Start the app

**Shipped portable app (engineers):** download
`SmartCommissioningApp_Windows_Portable.zip` from the
[latest release](https://github.com/Rvs006/smart-commissioning-app/releases/latest),
right-click → Extract All, and double-click `SmartCommissioningApp.exe`. It picks
a free port starting at 8000, applies migrations on startup, opens a browser,
and prints the chosen URL in the console, e.g. `http://127.0.0.1:8000/` — always
use the printed URL.

**From source (developers):** run the same launcher directly (identical
behaviour to the packaged `.exe`):

```sh
python packaging/windows_portable/run_smart_commissioning_app.py
```

Alternatively, run the API directly with uvicorn in local mode against a temp
SQLite DB:

```sh
AUTH_MODE=local JOB_EXECUTION_MODE=inline \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

(Run from the `backend/` directory, or with `backend/` on `PYTHONPATH`.)

### 2. Smoke-test it — no key

> Optional — proves a deployment works; not needed for day-to-day use.

Leave `SC_API_KEY` **unset** so the script sends no header (loopback is trusted
in local mode). Use whatever port the launcher reported.

bash:

```sh
scripts/smoke_local.sh http://127.0.0.1:8000
```

PowerShell:

```powershell
pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000
```

### 3. Seed demo data (optional)

```sh
python scripts/seed_demo.py --base-url http://127.0.0.1:8000
```

No `--api-key` needed in local mode.

---

## C. Developer profile — run from source

Single-user loopback profile: SQLite, jobs inline, auth bypassed for
`127.0.0.1`. Requires **Python 3.12** and **Node 22**. (This is the old README
"Option B"; it now lives here.)

```bash
# 1) install the three editable Python packages + the frontend
pip install -e ./core -e ./backend -e ./worker
cd frontend && npm ci && cd ..

# 2) backend API (terminal 1)
cd backend
AUTH_MODE=local JOB_EXECUTION_MODE=inline DEPLOYMENT_ROLE=hub \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) seed demo data + frontend (terminal 2)
python scripts/seed_demo.py --base-url http://127.0.0.1:8000
npm --prefix frontend run dev      # http://localhost:5173, proxies /api -> 8000
```

> **Engineer action buttons work automatically here.** With the backend running on
> loopback, the app recognises the trusted `127.0.0.1` admin, so Run / Publish /
> Export are enabled with no key and no console step. (The old
> `localStorage.setItem('sc.apiKey','local-dev')` trick is now only needed for the
> *backend-less* frontend-only preview, where there is no `/me` to grant admin.)
> One-command offline smoke: `scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000`.

---

## What the smoke test checks

> Optional — proves a deployment works; not needed for day-to-day use.

`scripts/smoke_local.sh` and `scripts/smoke_local.ps1` are equivalent and run
the same checks against `BASE_URL` (default `http://127.0.0.1:8000`):

| # | Check | Expectation |
| --- | --- | --- |
| 1 | `GET /api/v1/health` | `200`, `status: "ok"` |
| 2 | `GET /api/v1/ready` | `200`, `status: "ready"` |
| 3 | `GET /metrics` | Prometheus exposition text (app root, not under `/api/v1`) |
| 4 | `GET /api/v1/configuration` | demo-project / demo-site snapshot |
| 5 | `POST /api/v1/validation/udmi/runs` + poll `runs/{id}` + `runs/{id}/issues` | run reaches `succeeded`; issues returned. Validates the **bundled UDMI fixture — no network** |
| 6 | `POST /api/v1/discovery/ip/runs` with `parameters.dry_run=true` | run `succeeded` with a `dry_run_plan` (targets), **no scan, no authorization needed** |

Auth handling: the scripts attach `X-API-Key` only when `SC_API_KEY` is set
(hosted), and omit it otherwise (local/loopback). Tunables via env:
`SC_CURL_TIMEOUT`, `SC_POLL_ATTEMPTS`, `SC_POLL_INTERVAL`.

### Verifying the scripts (syntax only)

```sh
bash -n scripts/smoke_local.sh
python -m py_compile scripts/seed_demo.py
pwsh -NoProfile -Command "[scriptblock]::Create((Get-Content -Raw scripts/smoke_local.ps1)) | Out-Null"
```

---

## Live-only / on-site paths (NOT covered here)

Everything above is fixture- and dry-run-only. The real-hardware paths — active
IP/BACnet scans against a live segment, live MQTT discovery/config publish, real
broker/Redis/Postgres behaviour, and edge→hub sync — can only be validated on
real infrastructure. Run those from:

- **Operations runbook** — [runbook.md](runbook.md) (deploy, health/readiness
  and metrics checks, secret rotation, upgrades, incident triage).
- **Phase 5 on-site validation checklist** —
  [phase5-onsite-validation.md](phase5-onsite-validation.md) (the live-network /
  real-infrastructure work, with safety guidance: dry-run first, then a lab
  segment, then the real building with authorization).

> Safety: active scanning and live config publishing can disturb a live BMS/OT
> network. Always dry-run first. See
> [security-posture.md](security-posture.md).
