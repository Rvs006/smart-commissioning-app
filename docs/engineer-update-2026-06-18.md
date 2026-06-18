# Engineer update — Review Comments widget restored (2026-06-18)

PR #17 (squash `ec4ac3b`) is merged to main. The in-app **Review Comments** feedback widget now ships in every production build (it was previously dev-only, so it vanished from the hosted/packaged app). Also fixed: the README `API_KEY` command now has a copy-safe PowerShell form. Pick **one** channel below to get the change.

## Windows portable bundle

A fresh bundle was built from main (commit `ec4ac3b`) via:

```sh
pwsh packaging/windows_portable/build.ps1 -SkipFrontend -Clean
```

- **Output:** `build/Smart_Commissioning_App_Windows_Portable/` (~114 MB: `SmartCommissioningApp.exe` 18 MB + `_internal/` + `backend/` + `core/` + `frontend/dist/` + `README_FIRST.txt`).
- **Verified:** the bundled `frontend/dist` carries the restored Review Comments widget (grep-confirmed in the shipped JS).
- **Run it:** double-click `SmartCommissioningApp.exe`, keep the console open (it prints the local URL and opens your browser), Ctrl+C to stop. Unsigned build, so SmartScreen may warn → **More info → Run anyway**.
- **Exe boot smoke** (`scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000`) is the documented build-box / on-site verification step — not run in this build environment.

> Binaries are not committed to git, so the maintainer shares the built folder/zip directly (the engineer cannot `git pull` this).

## Hosted Docker instance

Run these on the **Docker host** (the server running the compose stack), from the repository root. Commands use `docker compose` with the hosted profile file `infra/docker-compose.yml`. `API_KEY` (and the other secrets) must already be set in `infra/.env` from the original deploy — this is a frontend-only redeploy, so no `.env` changes are needed.

```sh
# 0. From the repo root on the Docker host
cd /path/to/Smart-Commissioning-App

# 1. Pull the merged change on main
git checkout main
git pull --ff-only origin main

# 2. Rebuild ONLY the frontend image (no cache so the new Vite bundle is baked in).
#    The frontend Dockerfile runs `npm run build` at image-build time, so a
#    restart alone will NOT pick up the new bundle — a rebuild is required.
#    api and worker are unchanged by this frontend-only change and do NOT need rebuilding.
docker compose -f infra/docker-compose.yml build --no-cache frontend

# 3. Recreate the frontend container from the freshly built image
#    (api / worker / postgres / redis keep running untouched)
docker compose -f infra/docker-compose.yml up -d --no-deps frontend
```

### Verify

Open `http://127.0.0.1:8080` (or your `FRONTEND_PORT`) and confirm the new **Review Comments** button appears in the bottom-right corner.

```sh
# Optional sanity check that the container came up healthy
docker compose -f infra/docker-compose.yml ps frontend
```

> The app binds to loopback (`127.0.0.1:8080`) on the host; if you reach it through a TLS reverse proxy, open the public URL instead and look for the same bottom-right Review Comments button. Hard-refresh (Ctrl/Cmd+Shift+R) if your browser cached the old bundle.

## Standalone HTML review build

**Verdict: No edit to the standalone HTML is warranted. The engineer can keep using `deliverables/Smart_Commissioning_App_Standalone_HTML_Review.html` unchanged.**

### Why the merged fix does not apply to this file

The merged change is commit `7d3c69d` ("fix(frontend): ship engineer review-comments widget in production builds"). It touched exactly four files, none of which is the standalone HTML:

- `frontend/src/app/App.tsx`
- `README.md`
- `docs/portable-bundle-rebuild.md`
- `docs/team-pilot-deployment.md`

The bug it fixed was specific to the **React app**: the `<ReviewFeedback />` component in `App.tsx` was gated behind `import.meta.env.DEV`, so it only rendered under `npm run dev` and disappeared in production builds (`npm run build`). The fix re-gates it on `import.meta.env.VITE_REVIEW_COMMENTS !== "false"` so it ships by default. That is a Vite/build-time concern that has no analogue in the standalone file.

### The standalone HTML's review widget was never affected

I verified directly in the file:

- The widget logic is fully self-contained inline JS: `renderDrawer` (L801), `toggleFeedback` (L819), `addComment` (L824), `deleteComment` (L838), `exportComments` (L844).
- There is **no DEV gate**. A `grep` for `DEV`, `import.meta`, `process.env`, and `NODE_ENV` returns zero matches in the file.
- The "Review Comments" button (`#feedbackButton`, L241) is always rendered, and it is unconditionally wired on load: `document.getElementById("feedbackButton").addEventListener("click", () => toggleFeedback(true));` (L863).
- Comments persist to `localStorage` and are exportable as JSON/CSV (L844-852) — same behavior the React fix is now bringing to the packaged app.

In short, the React component was a *port of* this standalone widget; the standalone original already does the right thing.

### The README PowerShell fix also does not apply

The README change splits the `API_KEY` generation command into separate bash and PowerShell blocks. That concerns running the **hosted Docker stack** (where Windows users pasting `export API_KEY=...` into PowerShell hit an error). The standalone HTML has no such workflow — it runs by simply opening the file in a browser, with no API key, server, or shell command involved. Nothing to change.

### Conclusion

The standalone HTML is already in the desired end state that the merged commit brought the React app up to. No edit is needed, and no edit would improve it relative to this change.

## Build from main yourself

These are copy-paste commands for an engineer with repo access to pull the merged change and run it locally. They follow the profiles documented in `README.md` (Option B local dev, and a no-backend prod-bundle check). In every profile, the success check is the same: **the bottom-right "Review Comments" button is visible** in the app.

> Prereqs: **Python 3.12** and **Node 22**. Run all commands from the repository root (`smart-commissioning-app/`) unless a step says otherwise. The repo is private — you must already be a collaborator and have cloned it.

---

### 0. Get the merged change (all profiles)

```bash
git checkout main
git pull
```

---

### 1. Local dev profile (Option B) — full app with backend

SQLite, jobs run inline, auth bypassed for `127.0.0.1`. No broker / Postgres / Redis needed. Uses **two terminals**.

**One-time install** (both Python packages and the frontend):

```bash
pip install -e ./core -e ./backend -e ./worker
npm --prefix frontend ci
```

**Terminal 1 — backend API.** The env-var line is the part that trips people up: bash uses `VAR=value` prefixes, PowerShell needs `$env:VAR = "value"` set first. Use the block for **your** shell.

```bash
# bash / macOS / Linux  (run from the backend/ folder)
cd backend
AUTH_MODE=local JOB_EXECUTION_MODE=inline DEPLOYMENT_ROLE=hub \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
# Windows PowerShell  (run from the backend/ folder)
cd backend
$env:AUTH_MODE = "local"; $env:JOB_EXECUTION_MODE = "inline"; $env:DEPLOYMENT_ROLE = "hub"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> Why this differs by shell: in bash, `AUTH_MODE=local ... python ...` sets the variables for that one process. In PowerShell there is no inline `VAR=value cmd` syntax — `AUTH_MODE=local` (or `export AUTH_MODE=local`) errors with `'export'/'AUTH_MODE' is not recognized`. You must set `$env:AUTH_MODE = "local"` on its own first, then run `python ...`.

**Terminal 2 — seed demo data, then start the frontend** (run from the repo root):

```bash
python scripts/seed_demo.py --base-url http://127.0.0.1:8000
npm --prefix frontend run dev
```

Open **http://localhost:5173** (Vite proxies `/api` → `http://127.0.0.1:8000`).

**Verify:** the **"Review Comments"** button is visible in the **bottom-right** corner of the page.

> Optional — enable the Run / Publish / Export action buttons (gated on an API key even in local mode): open the app, press **F12 → Console**, run `localStorage.setItem('sc.apiKey','local-dev')`, then reload. The Review Comments button does **not** require this.

---

### 2. Quick prod-bundle check — no backend

This builds the production frontend bundle and serves it with Vite preview. No Python, no backend, no API needed — the **Review Comments feature is fully client-side (it stores comments in `localStorage`)**, so it works in this standalone bundle. Run from the repo root:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend run preview
```

Open **http://localhost:4173**.

**Verify:** the **"Review Comments"** button is visible in the **bottom-right** corner. Click it, add a comment, and reload — it persists (proof the localStorage path works with no backend).

> API-backed pages (scans, validation, reports) will show errors in this mode because there is no backend — that is expected. This profile only verifies the prod build and the Review Comments button.

## Verify

On any channel, success = the bottom-right **Review Comments** button (with a count badge) is visible and can add/export comments.
