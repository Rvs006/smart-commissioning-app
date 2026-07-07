# Changelog

All notable changes to the Smart Commissioning App are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

This is the pre-1.0 development line. Entries below summarize the program by
theme and are derived from the actual git history (`git log --oneline`), from
the MVP scaffold baseline through the phase 0–4b production-hardening work.

### Added

- **Production scaffold (MVP baseline)** — multi-service layout: React + TypeScript
  + Vite frontend, FastAPI backend, Dramatiq worker, `infra/` Docker Compose
  stack, and `docs/`.
- **Shared core package** — extracted `smart_commissioning_core`, a shared
  package for UDMI validation and MQTT logic consumed by both the backend and
  the worker.
- **Persistence** — moved runs, configuration, and imports to SQLAlchemy
  persistence (with Alembic migrations).
- **Real discovery and validation engines** — implemented real discovery and
  validation engines with scan-safety controls (replacing mocked flows).
- **Frontend wired to real data** — connected the frontend to real discovery,
  run, and validation data.
- **Observability, integrity, and DR** — structured logging, the Prometheus
  metrics surface, evidence integrity (SHA-256 + Ed25519 signing),
  backup/restore/retention, and Server-Sent Events (SSE).
- **Edge → hub synchronization** — signed, immutable edge-to-hub run + evidence
  record synchronization (per-edge Ed25519 identity, watermark-based ingest).
- **CI/CD and tooling** — lint, typecheck, and test tooling plus a GitHub
  Actions CI workflow with `python`, `frontend`, and `sbom` jobs.
- **SBOM** — additive SBOM + license-inventory job and generated inventory
  (`docs/SBOM.generated.md`).
- **On-site validation checklist** — Phase 5 checklist (`docs/phase5-onsite-validation.md`)
  enumerating the live-network / real-infrastructure steps that must pass before
  production rollout.
- **Electracom UI theme + in-app Brief & Learning** — restyled the operator
  console to the Electracom "Smart Point" look & feel (warm palette, teal accent,
  brand logo) with a **light/dark theme toggle**, and added two standalone
  onboarding surfaces: a **Product Brief** (`/#/brief` — Basics, Key Features,
  Section Reference, and a role-based Guided Tour) and a **Learning** path
  (`/#/learning` — role-based walkthroughs). Content is scoped to this app's own
  modules — theme and format only, no feature copy.
- **Step-based module layout** — each module page is now split into a
  **Setup / Run / Results** segmented flow so the operator works one screen at a
  time instead of scrolling every panel at once. The step auto-advances (Run when
  a run is queued, Results on success) and manual step clicks always override.
- **Workflow-stage navigation** — the module tabs are now grouped under the stage
  they belong to (**Configure / Discover / Validate / Report / Operate**) instead
  of a flat row of equal tabs, so the nav mirrors the order of the job.
- **Reviewer guide** — `docs/review-guide.md`: a single page for an engineer
  picking up the app to review — how to run it (frontend-only or full-stack
  Docker), what to look at, and what is in scope for this round. Linked from the
  README header and the documentation table.
- **Header chip hover tooltips** — the header chips now explain themselves on
  hover (active site, "API workspace", the access-role chip, the Brief/Learning
  links, and the brand logo).
- **Configuration field hover hints** — every Configuration field shows a short
  hover tooltip describing what it is (MQTT broker/port/QoS/keep-alive, BACnet,
  certificates, NTP, backup, logging), keeping the "no inline info-icons"
  decision (hover only).
- **Source-interface (NIC) selection** — implemented source-interface (NIC)
  selection: a device **"Source Interface"** configuration field (default
  **"Auto (OS default route)"**), a viewer-gated `GET /api/v1/system/interfaces`
  enumeration endpoint (psutil-backed, import-guarded), and per-engine socket
  source-binding for IP sweep / MQTT / BACnet; "Auto" preserves today's
  OS-default-route behaviour. Real multi-NIC egress verification remains an
  on-site step.
- **NIC UX v2 (field feedback, 2026-07-03)** — the interfaces endpoint now
  classifies each adapter (`ethernet` / `wifi` / `usb_ethernet` / `unknown`;
  virtual adapters — Hyper-V, WSL, VPN, VM — are filtered out like loopback)
  and returns `subnet_mask`, `gateway`, and `dns_servers` per adapter (a
  deliberate product reversal of the earlier gateway/DNS omission; MAC/driver
  strings stay omitted). On Windows the extra facts come from one cached,
  locale-safe PowerShell `ConvertTo-Json` call (absolute System32 path, 5 s
  timeout, UTF-8, degrades to `unknown`/nulls — never 500s). The dropdown
  orders wired Ethernet first and tags Wi-Fi "not recommended for
  commissioning traffic"; a read-only **"Selected adapter — this laptop"**
  panel shows IP / subnet / gateway / primary+secondary DNS with explicit
  "Windows manages these settings" copy (the app never writes NIC config);
  an advisory hint suggests the wired adapter when Auto is selected on a
  multi-adapter laptop; and discovery runs now fail fast with an actionable
  400 when the configured source interface is missing or down (dry runs
  exempt) instead of scanning out the wrong NIC.
- **API-key re-issue + honest key lifecycle** — admin-only
  `POST /api/v1/users/{id}/key` regenerates a user's API key (old key stops
  working, plaintext shown once), with a **Re-issue key** button on the Users
  page — a lost key is no longer a dead end. The session badge now
  distinguishes a *rejected* key (401/403 → "Key not recognised" + Clear key)
  from an *unreachable server* (network/5xx → non-destructive "Server
  unreachable" state), so a backend restart or Wi-Fi blip can no longer trick
  an engineer into clearing a healthy key — the root cause of the field
  report that keys "expire after one use". Issued-key copy now says the key
  is *displayed* once but never expires.
- **Install-first onboarding** — README restructured around
  "Get it running (pick one path)" (Windows portable app vs Docker Desktop)
  with copy-paste steps, a prerequisites table, and a 3-step first run;
  `docs/quickstart.md` aligned; and the in-app **Learning** page gained an
  **Installation & Setup** guide covering both install paths and first run
  (set API key, pick Source Interface, dry-run first scan).

- **Windows portable build+boot CI** — new `windows-portable.yml` workflow:
  on changes to bundle inputs (`packaging/`, `backend/`, `core/`, `frontend/`)
  a windows-2022 runner builds the portable bundle via `build.ps1`, uploads it
  as a downloadable artifact (14-day retention), then boots the produced exe
  and requires `/api/v1/health` to answer 200 before teardown — closing the
  gap where source-level CI let two real portable-build bugs (the PS 5.1
  `-Include`/`-LiteralPath` deletion hazard and the missing
  `--collect-all cryptography`) reach field engineers undetected.

- **One-command hosted-secrets bootstrap (field feedback, 2026-07-03)** —
  `scripts/bootstrap-env.ps1` (PowerShell 7) and `scripts/bootstrap-env.sh`
  (POSIX sh) generate `infra/.env` from `infra/.env.example`, filling every
  `CHANGE_ME` placeholder with a cryptographically random 32-byte hex secret
  and printing the generated `API_KEY` plus the compose command to run next.
  Both refuse to overwrite an existing `infra/.env` (exit nonzero) so a
  deployed config is never destroyed, and the sh script writes the file with
  owner-only permissions (`umask 077`) since it holds live credentials. Replaces the error-prone "edit every
  `CHANGE_ME` in Notepad" step in the hosted quickstart; the manual path
  remains as a one-line fallback.

### Changed

- **Source Interface — wired-first default (field feedback, 2026-07-03)**
  *(reverses the earlier "Auto stays the default, advisory hint only" decision
  after engineers' scans on Auto egressed via Wi-Fi)* — a configuration whose
  Source Interface was never chosen (value absent or empty) now defaults to the
  first **up** wired adapter (Ethernet before USB-Ethernet, per the
  already-sorted enumeration), visibly pre-selected in the dropdown and saved
  like a manual pick. To make "never chosen" detectable, the backend now seeds
  and backfills an empty Source Interface (empty already validates and behaves
  as Auto) instead of the literal Auto sentinel, which is stored only when
  picked in the dropdown — so fresh databases and legacy snapshots get the
  wired default, while an explicitly saved "Auto (OS default route)" (or any
  other saved value) is never overridden. With no wired adapter up the field
  falls back to Auto exactly as before, and the multi-adapter hint still
  nudges explicit-Auto users toward the wired NIC.

- **Source Interface — richer NIC confirmation** *(interim step, superseded in
  the same release by **NIC UX v2** under Added)* — first made the Source
  Interface control an adapter dropdown with read-only **IP / Subnet Mask /
  Gateway** confirmation fields, sourcing `gateway` from a guarded
  `Get-CimInstance` routing-table lookup. NIC UX v2 replaced that lookup with a
  single cached `Get-NetAdapter`/`Get-NetRoute`/`Get-DnsClientServerAddress`
  facts call and extended the contract with `adapter_type` and `dns_servers`.
  "Auto (OS default route)" stays the default and the free-text fallback for
  non-enumerated hosts is preserved throughout.
- Refactored the standalone UDMI payload validator into the shared core package
  with an app-level API, a shared issue model, and persistent run history.
- Aligned the frontend CI Node version to the lockfile's npm and raised the test
  timeout to stabilize the frontend job.
- Restyled the whole operator console via a design-token override (warm cream +
  teal Electracom palette) with a dark mode, and ran a dark-mode legibility pass
  (review-comments launcher, badge, and card elevation on the dark page).
- Pinned the portable-build toolchain (PowerShell 7, Python 3.12.10,
  pip 26.1.x, setuptools >=62, PyInstaller 6.20.0, Node 22) in the README and
  build docs, with explicit notes that the portable build requires
  PowerShell 7 (`pwsh`), not Windows PowerShell 5.1.

### Security

- Added authentication, secret encryption at rest (Fernet-based secret store
  holding `secret://` references in the database, never secret bytes), and infra
  hardening.
- Added evidence integrity via SHA-256 hashing and detached Ed25519 signatures,
  reused by both the evidence-pack and edge → hub sync paths.

### Fixed

- **Inactive users' API keys no longer grant local-mode admin.** In `local`
  auth mode, a key matching a deactivated (or corrupt-role) user row used to
  fall through to the keyless-loopback admin path, contradicting the
  documented contract that an inactive key is rejected; it now 401s. 401
  details are also uniform per client location, so a rejected key no longer
  reveals whether a user row exists.
- **MQTT wildcard capture now accepts real publish topics.** The raw MQTT
  transport now matches subscribed filters such as `#` and `prefix/#` against
  concrete broker publish topics, so MQTT discovery / live UDMI capture no
  longer drops messages just because the subscription filter is broader than the
  received topic. Covered by a transport regression test; live broker validation
  is still part of Phase 5.
- **IP discovery now scans the imported register.** "Run IP Discovery" failed
  with an opaque "engine failed" because the frontend never supplied a scan
  target (`cidr`/range) and the engine had no other source of hosts. The IP route
  now falls back to the **Expected IP address** column of the most recent accepted
  IP register import for the project/site (the engine gained an explicit
  `addresses` target), so "upload register → run discovery" sweeps exactly the
  registered hosts. When no register and no range exist, the API returns an
  actionable **400** instead of the sanitized engine failure.
- **Reports are downloadable immediately.** Report generation has no worker
  actor — the artifact is built on demand at download — yet the report run was
  left at the default `queued` status forever, so the UI (which only exports
  `succeeded` reports) showed "Queued" and never let you download. Report runs
  are now marked `succeeded` on creation.
- **Expected hostname is optional on the IP register.** Hostnames are rarely used
  on commissioning networks, so a blank `Expected hostname` no longer rejects a
  row (it was previously required). The column is still offered in the template
  and preserved when present; the import panel now lists it under **Optional
  columns**.
- Fixed a secret-corruption bug, a dead error panel, and a fixture path-traversal
  issue.
- Regenerated the frontend lockfile with **npm 10** for cross-npm compatibility,
  and regenerated it to include the full esbuild dependency tree.
- Replaced placeholder/marketing figures on the Product Brief "at a glance"
  stats (`∞`, `100%`, "console for every site") with honest, verifiable facts
  (deployment profiles, access roles, evidence signature schemes), and fixed the
  stat value/label rendering inline (now stacked) so they no longer overlap.
- Failed/cancelled runs stay on the module **Run** step (where the monitor shows
  the error) instead of auto-advancing to an empty Results view.
- Tidied the header bar so the brand and the controls cluster stay on a single
  row at common widths (they previously wrapped to two ragged lines once the
  Brief/Learning/Dark chips were added); narrow widths still wrap cleanly.
- Removed the large empty gaps between Configuration cards: the two-column grid
  sized each row to its tallest card, so the very tall Certificates card left
  space under its shorter neighbours. The cards now pack in a balanced
  multi-column (masonry) flow.
- Certificates & Keys is compact by default — each secret (CA cert, client cert,
  private key) shows its masked value with a **Replace…** action; the paste box
  and file picker only appear when replacing.
- **Windows portable build repaired under PowerShell 5.1 and now bundles
  cryptography.** `Remove-PythonCaches` relied on `Get-ChildItem -Include` with
  `-LiteralPath`, which Windows PowerShell 5.1 silently drops — so it matched
  every file and deleted non-cache files from the bundled `backend/` and `core/`
  trees; it now post-filters by extension. The default PyInstaller args add
  `--collect-all cryptography` so a bare build produces a working exe (fixes
  "No module named cryptography.fernet"), and `build.ps1` / `smoke_local.ps1`
  now carry `#Requires -Version 7.0` to fail fast under 5.1.
- **Keyless loopback admin is recognised in local mode.** The frontend now
  always fetches `GET /me` and resolves a keyless 401/403 to a null principal,
  driving the session principal from `/me` in both local and hosted modes. This
  enables the Certificates & Keys Replace/Save actions on the portable/local
  profile without a manual `localStorage` key; hosted `api_key` mode is
  unchanged (a bad key still surfaces "Key not recognised").

### Removed

- Removed the dead UI prototypes and the zip-inspector dev tool (still available
  in git history at the baseline commit `3471050`).

### Not yet validated

The following paths were implemented and unit-tested but developed **without
access to the corresponding real infrastructure**, and require on-site
validation before production rollout:

- **Live-network discovery/scanning** — active IP sweep and BACnet Who-Is
  against a real BMS/OT network (only ever run in dry-run / offline fixtures).
- **Live MQTT broker** — real broker connectivity, TLS, and UDMI message capture
  against site devices. Wildcard topic-filter matching is unit-tested, but the
  actual broker/device run still requires Phase 5 validation.
- **Postgres** — the hosted profile's PostgreSQL system of record under load.
- **Docker image build** — building and running the `infra/` Compose stack on a
  real Docker daemon.
- **Edge → hub sync over the wire** — synchronization against a remote staging
  hub.

See [docs/phase5-onsite-validation.md](docs/phase5-onsite-validation.md) for the
full checklist.

[Unreleased]: https://github.com/Rvs006/smart-commissioning-app/commits/main
