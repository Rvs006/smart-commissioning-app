# How to review this build

A short guide for an engineer picking up the Smart Commissioning App to review.
It covers how to run it, what to look at, and what is in scope for this round.

> Looking for the deeper references instead? See
> [docs/quickstart.md](quickstart.md) (5-minute run + safe smoke test),
> [docs/team-pilot-deployment.md](team-pilot-deployment.md) (pilot boundary),
> and the [README](../README.md).

---

## 1. Access

The repository is **private** — you need to be added as a collaborator
(GitHub → repo → **Settings → Collaborators**) before you can clone it.

```bash
git clone https://github.com/Rvs006/smart-commissioning-app.git
cd smart-commissioning-app
```

---

## 2. Run it

Three run paths below — pick one. For a review, **Run path 3 (full app locally,
no key) is recommended**. Run path 1 is the fastest if you only want to see the
UI.

> ⚠️ These **"Run path"** numbers are specific to this guide. The numbered
> **Option A / B / C** in the main [README](../README.md#quickstart) are a
> *different* list (deployment profiles) — to review the app, just follow the run
> paths here and ignore the README's option letters.

> **Do you need an API key?** No shared/secret key is committed to this repo (by
> design). For **local review you need no real key** — the backend trusts
> loopback (`127.0.0.1`) as admin; to enable the Run / Publish / Export buttons
> in local mode, set a harmless placeholder in the browser console once:
> `localStorage.setItem('sc.apiKey','local-dev')` (the value is ignored on
> loopback). Only the **Docker path (Run path 2)** needs a real key, which you
> generate yourself — see its sign-in step.

### Run path 1 — Frontend only (quickest UI look, no backend)

Requires **Node 22**.

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173
```

This renders the whole UI (theme, navigation, Brief, Learning, every module
page). Calls to `/api` will show errors because no backend is running — that is
**expected**, not a bug.

### Run path 2 — Full stack with Docker (live data, needs a key)

Brings up frontend + API + worker + Postgres + Redis. Requires **Docker**.

First create a real env file from the template and fill in `API_KEY` and
`POSTGRES_PASSWORD` with generated secrets. **Do not run with `--env-file
infra/.env.example`** — its placeholder `POSTGRES_PASSWORD` would be baked into
the Postgres data volume on first start, so a later switch to real secrets
breaks DB auth until the volume is recreated.

```bash
# from the repo root
cp infra/.env.example infra/.env
# set real secrets in infra/.env (edit the file, or use sed):
#   API_KEY=...           (openssl rand -hex 32)
#   POSTGRES_PASSWORD=... (openssl rand -hex 32)

docker compose -f infra/docker-compose.yml --env-file infra/.env up --build
```

> On Windows, edit `infra/.env` in an editor and paste the output of
> `openssl rand -hex 32` into `API_KEY` and `POSTGRES_PASSWORD`.

Then open **http://127.0.0.1:8080** (nginx serves the UI and proxies `/api`).
Full hosted runbook: [infra/README.md](../infra/README.md).

**Reset the stack** (drop the DB volume so Postgres re-initialises cleanly — use
this if you change `POSTGRES_PASSWORD` after the first run):

```bash
docker compose -f infra/docker-compose.yml down -v
```

**Sign in:** click **"Set API key"** at the top-right of the header, paste your
`API_KEY` (read it back with `grep API_KEY infra/.env`), and Save. The page
reloads and shows your role. To give each engineer their own key instead of
sharing the admin key, see [docs/team-pilot-deployment.md](team-pilot-deployment.md).

> The local and portable deployment profiles auto-trust `127.0.0.1`, so no key
> is needed there — only this Docker path needs one.

### Run path 3 — Full app locally, no key (recommended for review)

The fastest way to exercise the **real** workflow — discovery, validation,
imports, reports — without Docker and **without any committed secret**. In local
mode the backend trusts loopback (`127.0.0.1`) as **admin**, so no real key is
needed. Requires **Python 3.12** and **Node 22**.

```bash
# 1) install the Python packages + frontend
pip install -e ./core -e ./backend -e ./worker
cd frontend && npm ci && cd ..

# 2) backend API in local mode (terminal 1) — loopback is trusted as admin
cd backend
AUTH_MODE=local JOB_EXECUTION_MODE=inline DEPLOYMENT_ROLE=hub \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) seed demo data + run the frontend (terminal 2)
python scripts/seed_demo.py --base-url http://127.0.0.1:8000
npm --prefix frontend run dev      # http://localhost:5173 (proxies /api -> 8000)
```

Then enable the action buttons once in the browser console (F12). The value is a
**non-secret placeholder** — it works only because loopback is already trusted,
so it is safe to share and is not a real credential:

```js
localStorage.setItem('sc.apiKey', 'local-dev');
location.reload();
```

Now Run / Publish / Export / Generate all work. This is full functional testing
of the safe paths (configure, import, fixture/dry-run validation, reports) with
nothing secret committed to the repo. One-command offline smoke test:
`scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000`.

---

## 3. What to review

- **Theme** — the Electracom-branded console, with a **light/dark toggle** in
  the header (try both).
- **Workflow-stage navigation** — the module tabs are grouped by stage:
  **Configure → Discover → Validate → Report → Operate**, so the nav follows the
  order of the job.
- **Step-based module pages** — each module is a **Setup → Run → Results** flow
  (a segmented control at the top), one screen per task instead of a long scroll.
  The step advances automatically as a run is queued and completes.
- **Product Brief** — `/#/brief` — Basics, Key Features, Section Reference, and a
  role-based Guided Tour.
- **Learning** — `/#/learning` — pick-your-role walkthroughs.
- **Safe end-to-end paths (Run path 2 or 3)** — configure the site, import
  registers, run fixture / dry-run validation, and generate reports.

---

## 4. Scope for this round

- **In scope:** UI review and safe functional testing — configure, import
  registers, fixture/dry-run validation, and reports.
- **Not validated yet:** live-network testing — real BACnet/MQTT scans against
  site hardware, a live broker, and scale — is gated on the planned **on-site
  validation phase** (see [docs/phase5-onsite-validation.md](phase5-onsite-validation.md)).
  Please do not treat live scans as production-ready.

---

## 5. Where to read more

- [README.md](../README.md) — run options and feature overview.
- [docs/what-is-this.md](what-is-this.md) — plain-English explanation of the app.
- [CHANGELOG.md](../CHANGELOG.md), "Unreleased" — the full list of recent changes.
- [docs/v1-review-checklist.md](v1-review-checklist.md) — V1 review notes mapped to
  the implementation.
