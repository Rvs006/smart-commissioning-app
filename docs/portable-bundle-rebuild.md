<!-- Runbook to rebuild + offline-smoke the Windows portable bundle after alembic-now-ships-in-the-wheel. The wheel build + offline smoke are verifiable in dev; the PyInstaller freeze + clean-box launch are on-site/release steps. -->

# Runbook — Rebuild & offline-smoke the Windows portable bundle

Rebuild the `Smart Commissioning App` Windows portable bundle now that Alembic
ships in the core wheel, then prove it migrates and smokes locally **with no
broker, no Postgres, no Redis, no network** (local profile: `AUTH_MODE=local`,
SQLite, jobs inline).

Grounded in: `core/pyproject.toml` (`[tool.setuptools.data-files]`),
`core/MANIFEST.in`, `core/smart_commissioning_core/db/migrate.py`
(`_resolve_alembic_paths` / `upgrade_to_head`),
`packaging/windows_portable/run_smart_commissioning_app.py`
(`configure_environment`, `_bundle_dependency_imports`, `reserve_port`),
`backend/app/main.py` (lifespan `upgrade_to_head` gated on `AUTO_MIGRATE`;
`SCT_FRONTEND_DIST` static mount), `scripts/smoke_local.ps1` /
`scripts/smoke_local.sh`, `docs/quickstart.md` profile B.

---

## 0. Why this rebuild is needed (what changed)

The prior bundle at `build/Smart_Commissioning_App_Windows_Portable/` (dated
Jun 9, pre-change) **ships no Alembic at all**: I confirmed `_internal/` has no
`smart_commissioning_core`, there is no `alembic.ini` anywhere in that bundle,
and there is no `core/` dir beside the exe. A fresh SQLite DB therefore could
not be migrated on first launch in the field.

The fix made Alembic part of the core package's install footprint via
`core/pyproject.toml` `[tool.setuptools.data-files]` (a `versions/*.py` **glob**
so new migrations ship automatically) + `core/MANIFEST.in` (sdist parity), and
`core/smart_commissioning_core/db/migrate.py` resolves `alembic.ini` from either
the source tree (`_SOURCE_TREE_ROOT = parents[2]` → `core/`) **or** the wheel
data-files location (`<sys.prefix>/share/smart_commissioning_core/`). The
rebuild must make one of those two locations exist in the bundle.

---

## 1. Prerequisites (pinned / supported build tooling)

These are the canonical, pinned versions the shipped bundle was built with and
the supported build set. Verified offline on this dev box.

| Tool | Supported / pinned version |
| --- | --- |
| OS | Windows 11 Pro / Windows Server 2022 |
| Shell | PowerShell 7+ (`pwsh`) — **not** Windows PowerShell 5.1 |
| Python | 3.12.10 |
| pip | 26.1.x |
| setuptools | >=62 (built with 82.0.1) — data-files **glob** expansion needs `setuptools>=62` |
| PyInstaller | 6.20.0 |
| Node + npm | 22 |

- Node + npm for the frontend (frontend already builds: `frontend/dist/index.html`
  and `frontend/dist/assets/` are present; build script is `tsc && vite build`).
- All runtime deps the launcher pins in `_bundle_dependency_imports()` installed
  in the build interpreter (fastapi, uvicorn, sqlalchemy, alembic, pydantic*,
  psycopg, redis, openpyxl, prometheus_client, httpx, dramatiq, multipart,
  starlette, `bacpypes3`, and `smart_commissioning_core`). Pull `bacpypes3` via
  core's `[bacnet]` extra — `pip install "./core[bacnet]" ./backend` — so
  `build.ps1`'s `--collect-all bacpypes3` can freeze the real BACnet/IP backend.
  (Without it an authorized real BACnet scan honestly RuntimeErrors in the exe
  rather than faking a result.)

---

## 2. Build the core wheel (VERIFIED OFFLINE HERE)

```sh
python -m pip wheel ./core --no-deps -w dist/wheels
```

I ran this in the dev env and confirmed the wheel
(`smart_commissioning_core-0.1.0-py3-none-any.whl`) ships **all four** migration
scripts plus the Alembic env under
`smart_commissioning_core-0.1.0.data/data/share/smart_commissioning_core/`:
`alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, and
`alembic/versions/{c9663f90f68a_initial_schema, c4a7ced176a9_engine_framework…,
c998144d98d4_edge_hub_sync_columns, d1f2a3b4c5d6_users_table_rbac}.py`, plus the
fixture `smart_commissioning_core/fixtures/udmi_full_report.json`.

> `dist/` is git-ignored, so the wheel won't dirty the tree.

### 2a. Prove the wheel migrates a fresh DB with NO source tree (VERIFIED OFFLINE)

```sh
python -m venv .venv-wheelcheck
.venv-wheelcheck/Scripts/python -m pip install --no-index --find-links dist/wheels --no-deps dist/wheels/*.whl
.venv-wheelcheck/Scripts/python -m pip install alembic==1.18.4 sqlalchemy==2.0.49 pydantic==2.12.5
.venv-wheelcheck/Scripts/python -c "from smart_commissioning_core.db.migrate import upgrade_to_head; upgrade_to_head('sqlite:///./wheelcheck.db'); print('migrated OK')"
```

I ran exactly this. With **only the wheel** on the path (no editable `core/`),
`migrate.ALEMBIC_INI_PATH` resolved to
`<venv>/share/smart_commissioning_core/alembic.ini` and `upgrade_to_head`
applied all four revisions, creating tables `projects, sites, runs, run_issues,
import_records, configuration_snapshots, discovered_devices, discovered_points,
discovered_topics, users, alembic_version` and stamping
`alembic_version = d1f2a3b4c5d6` (head). This is the same call the API makes at
startup (`backend/app/main.py` lifespan, gated on `AUTO_MIGRATE`, default true).

---

## 3. Build the frontend (on-site / build box)

```sh
cd frontend && npm ci && npm run build && cd ..
```

Produces `frontend/dist/` (already present in this checkout, so this step is a
no-op refresh here).

> The in-app engineer **Review Comments** widget ships in every build by default.
> To cut a GA bundle without it, set the build-time flag:
> `VITE_REVIEW_COMMENTS=false npm run build`. This is a Vite build-time variable
> baked into `frontend/dist/` at build — not read at runtime, so it has no entry
> in any `.env`.

---

## 4. Assemble the portable bundle (PyInstaller + Alembic-shipping)

The launcher is **not** frozen self-contained for Alembic: in
`run_smart_commissioning_app.py::configure_environment` it expects sibling source
dirs `<root>/backend`, `<root>/frontend/dist`, and (if present) `<root>/core` on
`sys.path`. Build the exe, then assemble the directory bundle. **Either** of two
Alembic-shipping options works — pick one.

> **Turnkey:** `packaging/windows_portable/build.ps1` automates Option A
> end-to-end (frontend build → PyInstaller freeze → bundle assembly with the
> cache trimming below + a sanity check that `core/alembic.ini` landed). Run
> `pwsh packaging/windows_portable/build.ps1` (or `-SkipFrontend` to reuse an
> existing `frontend/dist`). The freeze + clean-box launch are still build-box /
> on-site steps (see §6). The raw commands below document what the script does.
>
> **Requires PowerShell 7 (the `pwsh` command).** Windows PowerShell 5.1 does not
> provide `pwsh`; running `build.ps1` under 5.1 is unsupported.

**Option A (recommended — mirrors backend/ and frontend/): ship the `core/`
source tree next to the exe.** `migrate.py::_SOURCE_TREE_ROOT` then resolves
`<root>/core/alembic.ini` (verified: `parents[2]` of
`<root>/core/smart_commissioning_core/db/migrate.py` == `<root>/core`, and
`core/alembic.ini` exists). This is the same resolution path that already works
in this dev env (core is installed editable, origin = `core/`).

```sh
pyinstaller --noconfirm --name SmartCommissioningApp --console \
  packaging/windows_portable/run_smart_commissioning_app.py
# Assemble bundle dir:
mkdir -p build/Smart_Commissioning_App_Windows_Portable
cp -r dist/SmartCommissioningApp/* build/Smart_Commissioning_App_Windows_Portable/
cp -r backend  build/Smart_Commissioning_App_Windows_Portable/backend
cp -r core     build/Smart_Commissioning_App_Windows_Portable/core
mkdir -p build/Smart_Commissioning_App_Windows_Portable/frontend
cp -r frontend/dist build/Smart_Commissioning_App_Windows_Portable/frontend/dist
```

(Trim `core/__pycache__`, `core/alembic/versions/__pycache__`, and
`build/`/`dist`/test caches from the copied `core/` — `MANIFEST.in` already
prunes these for the wheel; mirror that here.)

**Option B (wheel-into-frozen): bundle the wheel's data-files into `_internal`.**
Add the wheel's `share/smart_commissioning_core/**` to the PyInstaller `datas`
so they land beside the exe, and ensure the frozen `sys.prefix` (the bundle dir)
contains `share/smart_commissioning_core/alembic.ini` — that satisfies the
data-files branch of `migrate._candidate_roots()`. This needs a spec edit
(current `build/windows_portable_tmp/spec/SmartCommissioningApp.spec` has
`datas=[]`, which is why the old build shipped no Alembic). Option A is simpler
and is what the launcher's `core_root` check already anticipates.

> `packaging/windows_portable/build.ps1` (Option A) and the launcher are the
> tracked files under `packaging/windows_portable/`; there is still **no
> committed `.spec`** (PyInstaller generates one under `build/pyinstaller/`, a
> git-ignored artifact). The script is the reproducible build; the two raw
> command blocks above remain as documentation of what it does.

---

## 5. Offline smoke against the bundle (no broker)

Start the bundle (it sets `AUTH_MODE=local`, `JOB_EXECUTION_MODE=inline`,
SQLite `DATABASE_URL` under `%LOCALAPPDATA%\SmartCommissioning\` for the
frozen exe — `SMART_COMMISSIONING_DATA_DIR` overrides it; unfrozen dev runs
keep `<repo>/runtime/` — picks a free port from 8000 via `reserve_port`, runs
`upgrade_to_head` on startup, opens a browser). To re-verify **first-launch**
behavior (e.g. a missing-Alembic regression), point
`SMART_COMMISSIONING_DATA_DIR` at an empty folder or remove
`%LOCALAPPDATA%\SmartCommissioning` first — an existing stable-dir DB is
reused and would mask it:

```
build\Smart_Commissioning_App_Windows_Portable\SmartCommissioningApp.exe
```

Note the URL it prints (e.g. `http://127.0.0.1:8000/`). Then, with **`SC_API_KEY`
unset** (local/loopback needs no key), run the smoke script:

```powershell
pwsh scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000
```
or, equivalently:
```sh
scripts/smoke_local.sh http://127.0.0.1:8000
```

### Expected smoke output: all 10 assertions PASS across 6 check categories

1. `PASS GET /api/v1/health -> 200 status=ok`
2. `PASS GET /api/v1/ready -> 200 status=ready`  (local SQLite only; no Redis/PG
   needed — a 503 here means the DB layer failed, i.e. migrations didn't run)
3. `PASS GET /metrics -> 200 Prometheus exposition text`  (matches `# HELP`/`# TYPE`/`sct_`)
   - 3b. `PASS GET /electracom-logo.png -> 200 image/png (frontend static serving)`
     — the bundle serves the built frontend, so this one always runs here. A
     FAIL naming `text/html` means the SPA fallback swallowed the asset (the
     v0.1.10 logo bug). Only a backend-only stack (no dist at `/`) skips it with
     an Info line, so the footer count is 10 for the portable bundle.
4. `PASS GET /api/v1/configuration -> 200 snapshot returned`  (demo-project/demo-site)
5. UDMI validation against the **bundled fixture, no network**:
   `PASS POST /api/v1/validation/udmi/runs -> 200 run_id=…`,
   `PASS validation run reached terminal status=succeeded` (inline → immediate),
   `PASS GET /api/v1/validation/runs/{id}/issues -> 200 issues returned`
6. **Dry-run** IP discovery (a plan, **no packets, no authorization**):
   `PASS POST /api/v1/discovery/ip/runs (dry_run) -> 200 run_id=…`,
   `PASS dry-run IP discovery returned a plan (target_count=…, no scan)`

Footer: `SMOKE PASSED  N/N checks OK` (exit 0). Any FAIL → exit 1.

> If check 2 is the only failure, the bundle started but couldn't migrate the
> SQLite DB → Alembic wasn't shipped (the exact regression this rebuild fixes).
> Verify `build\…\core\alembic.ini` (Option A) or
> `build\…\share\smart_commissioning_core\alembic.ini` (Option B) exists.

---

## 6. What is verifiable offline vs on-site only

**Verified offline in dev (no hardware/network):**
- `pip wheel ./core` builds and the wheel ships `alembic.ini` + env + **all 4**
  `versions/*.py` + the UDMI fixture (data-files glob works).
- `migrate.upgrade_to_head` applies head from a **wheel-only** install (no source
  tree) against a fresh SQLite DB — the same call the API runs at startup.
- `scripts/smoke_local.sh` (`bash -n`) and `scripts/smoke_local.ps1`
  (`[scriptblock]::Create`) parse clean; `scripts/seed_demo.py` byte-compiles.
- Frontend `dist/` exists; launcher `configure_environment` requires
  `backend/`, `frontend/dist/index.html`, optional `core/`.
- The whole smoke surface is fixture/dry-run only — it never opens a broker,
  socket scan, Postgres, or Redis (per `docs/quickstart.md` §B and the script
  headers).

**Not verifiable in this dev env (on-site / build-box only — do NOT claim tested):**
- The actual **PyInstaller freeze + bundle assembly on Windows** and a
  double-click run of the resulting `SmartCommissioningApp.exe` (I did not build
  the exe or launch a server here).
- The 10 PASS assertions coming from the **running .exe** specifically (verified the
  underlying migrate + script parsing, not an exe boot).
- Windows SmartScreen / AV behaviour on first launch (unsigned tester build, per
  the bundle's `README_FIRST.txt`).
- All **live** surfaces (active IP/BACnet scan, live MQTT publish, real
  broker/Redis/Postgres, edge→hub sync) — explicitly out of scope here; see
  `docs/phase5-onsite-validation.md` and `docs/runbook.md`.
