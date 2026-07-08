# Smart Commissioning App

> Commissioning and verification platform for smart-building Master Systems Integrators (MSIs) —
> configure a site, discover its devices, validate UDMI/BACnet/MQTT data against what was
> specified, and export signed commissioning evidence.

![CI](https://github.com/Rvs006/smart-commissioning-app/actions/workflows/ci.yml/badge.svg)
![status](https://img.shields.io/badge/status-pre--production%20(pending%20on--site%20validation)-orange)
![version](https://img.shields.io/badge/version-0.1.0-blue)
![python](https://img.shields.io/badge/python-3.12-blue)
![node](https://img.shields.io/badge/node-22-green)

## Get it running (pick one path)

| You are… | Use | Time | You need |
| --- | --- | --- | --- |
| A field engineer on a Windows laptop | **Path 1 — Portable app** (no install) | ~2 min | Nothing installed; just the zip |
| Setting up a shared team server, or want the identical-everywhere stack | **Path 2 — Docker Desktop** | ~10 min | Docker Desktop; ~32 GB RAM recommended; repo access |
| Looking for the cloud/hosted deployment | Not an engineer task — see [docs/team-pilot-deployment.md](docs/team-pilot-deployment.md) | — | — |

### Path 1 — Windows portable app (nothing to install)

1. Download `SmartCommissioningApp_Windows_Portable.zip` from the
   **[latest release](https://github.com/Rvs006/smart-commissioning-app/releases/latest)**.
2. Right-click the zip → **Extract All** to a normal folder (Desktop is fine).
3. Double-click `SmartCommissioningApp.exe`. Keep the black console window open.
4. Your browser opens automatically — usually <http://127.0.0.1:8000/>. If port 8000 is busy the
   launcher picks the next free port and prints the correct URL in the console; use that URL.
5. That's it — no API key, no sign-in. The app trusts your own laptop (loopback), so
   Run / Publish / Export and the certificate/key **Replace** buttons are enabled automatically.

> ⚠️ **Windows SmartScreen may warn** — this is an internal unsigned build. Choose
> **More info → Run anyway**, only if you got the zip from the project owner or the releases page
> above. On a locked-down company laptop with application allow-listing (e.g. ThreatLocker), ask
> IT to approve the exe first.

> **Stop the app:** press `Ctrl+C` in the console window, or just close the window.

### Path 2 — Docker Desktop (recommended for team consistency)

**Prerequisites:** Docker Desktop installed and running; ~32 GB RAM recommended for the full
stack; the repository cloned. The repository is **private** — your engineer needs to be added as
a collaborator (GitHub → repo → **Settings → Collaborators**) before they can clone it.

```bash
git clone https://github.com/Rvs006/smart-commissioning-app.git
cd smart-commissioning-app
```

One command brings up frontend + API + worker + Postgres + Redis. `API_KEY` is required in this
profile — the bootstrap script writes `infra/.env` from `infra/.env.example` with fresh random
secrets and prints the `API_KEY` to paste into **Set API key** (it refuses to overwrite an
existing `infra/.env`). **Use the block for your shell** (`bootstrap-env.ps1` needs
PowerShell 7 / `pwsh`):

```powershell
# Windows (PowerShell 7 / pwsh)
./scripts/bootstrap-env.ps1
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
```

```bash
# Linux / macOS
./scripts/bootstrap-env.sh
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
```

Then sign in — hosted deployments require an API key to view real data and take any action
(without one, pages show **"Authentication required — set an API key"** and Run / Upload /
Generate / Export stay disabled; that is expected):

1. Open **<http://127.0.0.1:8080>** (nginx serves the UI and proxies `/api` → the API).
2. Click **"Set API key"** at the **top-right** of the header.
3. Paste the `API_KEY` value the bootstrap script printed and click **Save**. The page reloads
   and shows your role — Reports, runs, results, uploads, and network scans now work.

**Better than sharing the admin key — give each engineer their own:** sign in with the `API_KEY`
(it acts as **admin**), open the **Users** tab, create a user with the right role (e.g.
`engineer`), and hand them the **one-time key** shown. They set it the same way. See
[docs/team-pilot-deployment.md](docs/team-pilot-deployment.md).

**What works without any key:** the blank import **templates** (Download XLSX/CSV) and the
**import-profile list** are public format helpers — column headers plus one example row, no
project data — so an engineer can prepare a register before they have a key. See
[infra/README.md](infra/README.md) for the full hosted runbook.

### First run (both paths) — 3 steps

1. **Hosted only:** click **Set API key** (top-right) and paste your key — the portable app skips
   this step entirely.
2. Open **Configuration** and confirm **Source Interface** — when never chosen, the tool
   pre-selects the first up wired adapter (Ethernet/USB-Ethernet). On a multi-NIC laptop (Wi-Fi
   for internet + wired Ethernet to the BMS network) confirm the wired adapter is selected so
   scans egress on the building network.
3. In **IP Scanner**, upload the project's IP register under **Register Import** (blank XLSX/CSV
   templates are downloadable in the same panel — scan targets come from the register's
   "Expected IP address" column), then tick **Dry run** and start the scan — it produces a plan
   of scan targets, sends no packets, and needs no authorization.

The same guide lives in the app: **Learning → Installation & Setup** (`/#/learning`).

### Prerequisites at a glance

| Path | OS | Installed software | RAM | Network |
| --- | --- | --- | --- | --- |
| Portable app | Windows 11 Pro / Server 2022 | Nothing | Any | Loopback only (`127.0.0.1`) |
| Docker Desktop | Windows / macOS / Linux with Docker Desktop | Docker Desktop (+ PowerShell 7 on Windows for the bootstrap script) | ~32 GB recommended | Ports 8080/8000 on loopback |

---

## What it does

Branded **ELECTRACOM "Smart Commissioning Tool"**, this is the web platform our engineers use to
commission smart buildings: bring up a project's network/BACnet/MQTT/certificate settings, import
the expected register, scan the live building network, confirm every device is publishing
UDMI-compliant data that matches the design, and hand the client a tamper-evident evidence pack.

Commissioning a smart building means proving that hundreds of field devices were installed,
addressed, and configured to match the design — and producing evidence of it. This tool turns that
manual, error-prone checklist into a repeatable five-stage workflow:

| Stage | What happens | Pages |
| --- | --- | --- |
| **1. Configure** | Capture the site's network, BACnet, MQTT broker (with TLS certs), time/NTP and backup settings. Export/import per-project config so the same engineer can jump between projects. | Configuration |
| **2. Import** | Upload the *expected* register (CSV/XLSX) — the devices and points the design says should exist. Every module ships a downloadable template. Registers are flexible: asset ID *or* name, optional Notes/payload-type, `prefix/#` topic wildcards, multiple points per row, and UDMI metadata columns (make/model/GUID/serial/firmware/site/room). | each module |
| **3. Discover** | Scan the live network: IP sweep, BACnet device/object discovery, and MQTT topic capture (MQTT-Explorer-style wildcard subscription with latest-payload export). The IP sweep honours operator port ranges and flags **forbidden** and **unexpected-open** ports; the MQTT subscribe inherits the configured Root Topic and QoS, and `#` / `prefix/#` filters match the concrete publish topics returned by the broker. | IP Scanner, BACnet Discovery, MQTT Discovery |
| **4. Validate** | Check observed data against the design: UDMI payload validation (pointset / metadata / state, expected-vs-observed) and BACnet ↔ MQTT comparison within tolerances. UDMI validates **every asset in the register in one run** and matches make/model/GUID/serial/firmware/site/room. Result is a clear **Pass / Fail** with reasons. | UDMI Validation, BACnet to MQTT Validation |
| **5. Report** | Export a commissioning evidence pack (XLSX / DOCX / ZIP) scoped to the runs you choose, carrying the actual findings and stamped with an integrity signature. | Reports |

A controlled **MQTT config publish** path (multi-point write + read-back confirm, with rollback)
lets an engineer correct a device's setpoints and prove the change took effect.

### In-app onboarding — Brief & Learning

The console is branded in the **Electracom** theme with a **light/dark toggle** in the header, and
ships two standalone onboarding surfaces (linked from the header, or reachable directly):

- **Product Brief** — `/#/brief` — what the tool is and how it works, in four tabs: Basics, Key
  Features, Section Reference, and a role-based **Guided Tour**.
- **Learning** — `/#/learning` — an **Installation & Setup** guide (both install paths plus
  first-run steps) and pick-your-role walkthroughs of the exact modules each role (Commissioning
  Engineer, BMS Designer, Project Manager, Integration Engineer) touches on site.

The module tabs are grouped by workflow stage — **Configure → Discover → Validate → Report →
Operate** — so the navigation mirrors the order of the job rather than presenting a flat row of equal
tabs. Each module page is then organised as a **Setup → Run → Results** step flow (a segmented
control at the top of the page), so an operator works one screen at a time rather than scrolling
every control at once. The step advances automatically as a run is queued and completes.

---

## For developers

Everything below is for contributors and reviewers — a field engineer running the app does not
need any of it.

### Architecture

One codebase, three deployment profiles. The core scan/validation logic lives in a shared Python
package (`smart_commissioning_core`) used identically by the API and the background worker, so an
in-request "inline" run and a queued worker run behave the same.

```mermaid
flowchart LR
    U[Operator browser] --> FE[Frontend<br/>React + Vite / nginx]
    FE -->|/api| API[Backend API<br/>FastAPI]
    API --> CORE[Core engines<br/>IP · BACnet · MQTT · UDMI]
    API --> Q[[Redis queue]]
    Q --> W[Worker<br/>Dramatiq]
    W --> CORE
    CORE -->|scan / publish| NET{{Building network<br/>BACnet · MQTT · IP}}
    API --> DB[(SQLite or Postgres<br/>SQLAlchemy + Alembic)]
    W --> DB
    DB -. signed edge to hub sync .-> HUB[(Central Hub<br/>Postgres · RBAC · aggregation)]
```

**Deployment profiles** (`DEPLOYMENT_ROLE`):

- **`standalone` / portable** — a single Windows `.exe`, SQLite, jobs run inline, bound to
  `127.0.0.1`, no broker/DB/Redis required. For one engineer on one laptop, including air-gapped OT
  networks. (A commissioning tool *must* run on the site network — central-only can't reach
  air-gapped sites.)
- **`edge`** — the on-site instance that does the live scanning, then pushes signed run bundles to a
  hub (online, or offline via a carried `.scbundle` file).
- **`hub`** — a central, multi-project instance (Postgres, company SSO/RBAC) that ingests edge runs
  fail-closed (trusted-edge allowlist, signature + per-run hash verification, immutable upsert).

### Tech stack

| Layer | Tech |
| --- | --- |
| Frontend | React 18, TypeScript, Vite, TanStack Query/Router |
| API | FastAPI (Python 3.12), Pydantic v2 |
| Worker | Dramatiq on Redis |
| Core | `smart_commissioning_core` — engines, UDMI/MQTT logic, persistence |
| Persistence | SQLAlchemy 2 + Alembic — SQLite (local) / PostgreSQL (hosted) |
| Packaging | PyInstaller Windows portable bundle; Docker Compose hosted stack |

### Run from source (developer profile)

Single-user loopback profile: SQLite, jobs inline, auth bypassed for `127.0.0.1`. Requires
**Python 3.12** and **Node 22**. The full copy-paste steps (editable installs, uvicorn, demo seed,
Vite dev server, smoke one-liner) live in
**[docs/quickstart.md §C](docs/quickstart.md#c-developer-profile--run-from-source)**.

### Build the Windows portable bundle

Build a self-contained directory bundle (exe + backend + core + frontend) with
`packaging/windows_portable/build.ps1`; double-click `SmartCommissioningApp.exe`. See
[docs/portable-bundle-rebuild.md](docs/portable-bundle-rebuild.md).

> **Building requires PowerShell 7 (the `pwsh` command).** Stock Windows
> PowerShell 5.1 does not provide `pwsh`, and running `build.ps1` under 5.1 is
> unsupported.

#### Build prerequisites (pinned tooling)

These are the versions the shipped bundle was built with and the supported build
set (see [docs/portable-bundle-rebuild.md](docs/portable-bundle-rebuild.md)):

| Tool | Supported / pinned version |
| --- | --- |
| OS | Windows 11 Pro / Windows Server 2022 |
| Shell | PowerShell 7+ (`pwsh`) — **not** Windows PowerShell 5.1 |
| Python | 3.12.10 |
| pip | 26.1.x |
| setuptools | >=62 (built with 82.0.1) |
| PyInstaller | 6.20.0 |
| Node | 22 |

### Repository layout

```text
frontend/    React + TypeScript operator UI
backend/     FastAPI HTTP API (smart-commissioning-api)
worker/      Dramatiq background jobs (smart-commissioning-worker)
core/        smart_commissioning_core — engines, UDMI/MQTT logic, DB models, Alembic migrations
infra/       Docker Compose stack (frontend, api, worker, Postgres, Redis)
packaging/   Windows portable bundle (launcher + build.ps1)
scripts/     seed_demo, smoke tests, Phase 5 preflight, edge→hub sync CLI
docs/        architecture, runbook, security posture, on-site validation
device_udmi_payload_validation/   standalone reference UDMI validator
Smart Commissioning Tool Specification.pdf
```

### Documentation

| Document | Covers |
| --- | --- |
| [docs/what-is-this.md](docs/what-is-this.md) | Plain-English onboarding: what the app is, what it does, how to explain it |
| [docs/field-quickstart.md](docs/field-quickstart.md) | Printable one-page card for an engineer at the panel: run the exe, pick the NIC, dry-run then scan |
| [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) | Agent/contributor quick-reference: setup, the `unittest` test commands CI runs, lint, local run, conventions, gotchas (identical files) |
| [docs/review-guide.md](docs/review-guide.md) | How to review this build: run it, what to look at, scope for this round |
| [docs/windows-compatibility.md](docs/windows-compatibility.md) | Windows 11 Pro / Server 2022 support paths, CI coverage, local smoke command |
| [docs/quickstart.md](docs/quickstart.md) | Validate a running stack in 5 minutes (smoke test) + developer run-from-source |
| [docs/production-architecture.md](docs/production-architecture.md) | System model mapping the specification to the production build |
| [docs/runbook.md](docs/runbook.md) | Deploy, operate, and recover (hosted compose + edge/portable profiles) |
| [docs/security-posture.md](docs/security-posture.md) | Threat model, auth, secret handling, scan-safety, IEC 62443 alignment |
| [docs/sync-architecture.md](docs/sync-architecture.md) | Signed edge → hub run + evidence synchronization |
| [docs/observability.md](docs/observability.md) | Structured logs, Prometheus metrics, alerts/SLOs, crash log |
| [docs/backup-restore.md](docs/backup-restore.md) | Backup/restore + retention, RPO/RTO guidance per profile |
| [docs/protocol-conformance.md](docs/protocol-conformance.md) | UDMI/MQTT/BACnet support: tested vs. simulated vs. live-untested |
| [docs/review-comments-verification.md](docs/review-comments-verification.md) | The 24 design-review comments mapped to code + localhost verify steps |
| [docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md) | On-site validation checklist for live-network/infra paths |
| [docs/team-pilot-deployment.md](docs/team-pilot-deployment.md) | Safe controlled-pilot boundary + hosted setup for the team |
| [docs/SBOM.md](docs/SBOM.md) | Python dependency + license inventory |
| [docs/proposals/nic-interface-selection.md](docs/proposals/nic-interface-selection.md) | Design doc for source-interface (NIC) selection for active scans (implemented) |

### Security & safety

- **Auth** — `local` (loopback-only, portable default) or `api_key` mode; per-user **RBAC**
  (`viewer < reviewer < engineer < admin`) gates every route, with a race-safe last-admin guard.
- **Secrets at rest** — broker passwords and TLS keys are Fernet-encrypted (`0600`), masked on read,
  and redacted from API responses; never returned to clients.
- **Scan safety** — live scans are **dry-run by default**, require an explicit authorization flag,
  are rate-throttled, and support cooperative cancellation. No packets leave without consent.
- **Evidence integrity** — reports and backups are hashed (SHA-256) and signed (detached Ed25519);
  restores verify before writing (with a zip-slip guard).
- **Honest status** — live-infrastructure paths that have not been run against real hardware are
  marked as such, never faked. See the honesty rule in [CONTRIBUTING.md](CONTRIBUTING.md).

### Project status & roadmap

> **Repo status - last verified Friday, 2026-07-03:** local Python/frontend gates
> passed and the CI `python`/`frontend` jobs are green. The Windows portable build
> was repaired (a PowerShell 5.1 file-cleanup bug, default `cryptography` bundling,
> and an explicit PowerShell 7 requirement), and engineer actions now auto-enable
> on the local/portable loopback profile. Live IP/BACnet/MQTT hardware validation
> remains Phase 5 work before production rollout.

**Code-complete and hardened, pending on-site sign-off.** Phases 0–4b are built and merged:
persistence, auth + secret encryption, real discovery/validation engines with scan-safety,
observability / evidence-integrity / backup-restore, and signed edge → hub sync. CI is green on the
blocking `python` and `frontend` jobs.

The **only gate to production is Phase 5 on-site validation** — the live paths (active scanning
against real BMS/OT hardware, a real MQTT broker, Postgres/Redis at scale, a remote hub) were
developed without that infrastructure and must be validated on site. A controlled team **pilot**
(config / import / fixture-validation / dry-run / reports) is safe today; see
[docs/team-pilot-deployment.md](docs/team-pilot-deployment.md) and
[docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md).

**Implemented:** source-interface (NIC) selection — an operator can choose which
network interface active scans egress from, via a **"Source Interface"** field on
the Configuration page backed by `GET /api/v1/system/interfaces`, for multi-NIC
commissioning laptops. Real multi-NIC egress is still verified on site. Design
doc: [docs/proposals/nic-interface-selection.md](docs/proposals/nic-interface-selection.md).

### Reviewing the V1 design feedback?

[docs/review-comments-verification.md](docs/review-comments-verification.md) maps all **24 design
review comments → ✅ implemented**, each with `file:line` evidence and the exact localhost route to
see it in the running app.

### Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the exact commands CI runs (tests, lint, type
checks), the npm-lockfile lesson, and branch/PR conventions. Change history is in
[CHANGELOG.md](CHANGELOG.md); third-party notices in [NOTICE](NOTICE).
