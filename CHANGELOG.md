# Changelog

All notable changes to the Smart Commissioning App are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **The tolerances template no longer rejects its own example row.** The
  `Tolerance` column was validated as an integer, so the `0.5` shipped in the
  downloadable template failed import with `invalid_numeric` — downloading the
  template and uploading it unchanged was an error. Tolerance cells are now
  checked with the comparison engines' own `parse_tolerance`, so the import
  gate accepts exactly what the engines later read (`0.5`, `5%`, `abs:0.5`,
  `percent:5`) and rejects the rest as `invalid_tolerance`. Integer fields
  (BACnet device/object instance, reporting interval) are unchanged.

## [0.1.10] - 2026-07-14

### Added


- **Red/green verdicts across the UDMI validation results** (field asks,
  2026-07-14). Result rows shade green (pass, including pass-with-notes) or
  red (fail) so passes need zero reading time; the per-asset payload sections
  carry the same tint plus an explicit verdict — "PASS — UDMI Compliant" /
  "FAIL — please see details below" — so scrolling draws the eye to the red.
  The per-row "View" panel now shows the actual issue text inline when there
  are one or two issues, or "N issues — see the issue details below the
  table." when more. One shared verdict helper feeds all three surfaces so
  they can never disagree. Amber/RAG weighting was deliberately deferred.

- **Non-published UDMI schema sets.** Projects that conform to no published
  UDMI version declare a version label starting with `nonpub` (e.g.
  `nonpub.1`) in the register and payloads; the validator then checks payloads
  against an operator-uploaded schema set with that label (canonical Draft 7
  only — the focused checks encode published-1.5.2 assumptions). Upload,
  list, and delete sets on the UDMI page (engineer-gated;
  `POST/GET/DELETE /api/v1/udmi/schemas` with root/ref/schema validation at
  upload). A declared nonpub version with no uploaded set is a high-severity
  issue naming exactly what to upload — never a silent pass. Re-uploads take
  effect without a restart.

- **End-to-end validation report** (field ask via Jon, 2026-07-14). Reports
  generated from a validation run now carry three sections in every format:
  "Summary" (per-run expected/publishing/silent/blocking counts + compliance
  %, with a device-weighted overall line), "Failure detail" (per-point
  findings with expected/observed values and suggested actions), and "Silent
  systems" (devices that published nothing within the capture window —
  neither validated nor failed). New **PDF export** joins Word/Excel/zip via
  a dependency-free deterministic PDF writer, keeping the byte-reproducible
  integrity verification; the run monitor gains a report-format picker
  (PDF default).

- **Hour-scale capture windows.** The UDMI run-time control takes a
  seconds/minutes/hours unit (wire format stays seconds), the queued worker's
  capture time limit rises from 1 hour to 48 hours (metadata commonly reports
  daily), and windows beyond 48 h are refused up front instead of dying
  mid-run. Validation run summaries now record the silent-device IDs, not
  just their count.

- **IP register imports now warn about UDP port entries instead of silently
  ignoring them.** The IP scan verifies TCP ports only, so entries like
  `47808/udp` in "Expected services/ports" or "Ports that should not be
  enabled" were accepted but never actually checked. Those rows are still
  accepted; the import response now carries informational warnings (rendered
  as an amber, non-blocking panel in the upload feedback), and the 47808/udp
  message points at the BACnet discovery run — the engine that really
  verifies BACnet/IP.


- **IP scan flags hostname mismatches against the register's "Expected
  hostname".** When reverse DNS is enabled and returns a name for a responsive
  host that the IP register also carries a hostname for, the scan compares the
  two (case-insensitively, on the short name — the reverse-DNS domain suffix is
  stripped) and appends `HOSTNAME MISMATCH: expected <x>, got <y>` to the
  host's detailed status, with a `hosts_with_hostname_mismatch` count in the
  run summary. Warning-only: a blank on either side (no PTR record, site DNS
  not configured, reverse DNS disabled, register row without a hostname) never
  counts as a mismatch, since commissioning networks often run without DNS.

### Fixed


- **The hero "payload conformance" score is now fed by validation outcomes.**
  It was a publishing-liveness ratio — a device that published anything
  counted as fully conforming, and in pasted-payload mode the score was a
  constant 100% — so it happily showed "100%" beside a blocking issue. The
  backend now stamps `payload_conformance_percent` (devices that published
  AND carry no blocking-severity issue; clamped below 100 whenever any
  blocking issue exists) plus `blocking_issue_count`, and the hero prefers
  them (pre-upgrade runs fall back to the old ratio, labelled honestly).


- **IP scan actually probes the register's declared ports and verdicts
  expected-port coverage both ways.** The register's "Expected services/ports"
  and "Ports that should not be enabled" columns previously only fed the
  flagging maps — with a blank port field the scan probed just the 4 defaults
  (80, 443, 1883, 502), so a host expected on 445/135/139/5985/7070 reported
  `responsive: 443` with no findings while nmap showed all six expected ports
  open (field report, 2026-07-14). Each host's probe list is now the base list
  (operator spec or defaults) union that host's register-declared expected and
  forbidden ports; hosts not in the register keep exactly the base list. New
  verdicts in the detailed status: `MISSING EXPECTED PORTS: <ports>` when an
  expected port does not answer (with a `hosts_with_missing_expected` run
  summary count) and an explicit `EXPECTED PORTS OK: <n>/<n> open` pass when
  every expected port is open and nothing forbidden/unexpected fired — a clean
  host is a recorded decision, not silence. The per-host union respects the
  ports-per-sweep ceiling; register ports the cap drops are reported as
  `PROBE LIST CAPPED: register ports not probed: <ports>` (and never verdicted
  missing), not silently truncated. Each host's record now also carries its
  register `expected_ports` / `forbidden_ports` and the scanned port count.

## [0.1.9] - 2026-07-14

### Fixed

- **Source Interface dropdown lists virtual adapters again (ranked last)
  instead of hiding them.** On Hyper-V vSwitch / NIC-team hosts (e.g. a
  supervisor server) the machine's only routable IPv4 rides an adapter Windows
  flags *Virtual*; the hard virtual-adapter filter introduced with NIC UX v2
  (and unmasked when the net-facts timeout fix made classification actually
  succeed inside the exe) left the dropdown Auto-only with no way to bind the
  real egress NIC (field report, 2026-07-14). Virtual adapters now appear at
  the bottom of the list with an explicit "pick only if this adapter carries
  the site network" tag; the wired-first auto-default and the multi-adapter
  Auto hint still ignore them.

- **Portable exe settings survive upgrading to a new release.** All app state
  (configuration, MQTT credentials, encrypted certificates and their key,
  imports, run history, edge identity, crash logs) now lives in
  `%LOCALAPPDATA%\SmartCommissioning` instead of `runtime\` beside the exe —
  per-hash allowlisting (ThreatLocker) forces every release into a fresh
  folder, which silently reset the whole site configuration on each upgrade
  ("re-enter broker credentials, certs and NIC every time we open it", field
  report 2026-07-14). On first launch the new exe migrates state forward from
  an older release's exe-adjacent `runtime\` folders when it finds them (the
  originals stay behind as a rollback copy), and it never overwrites an
  existing stable-dir database. `SMART_COMMISSIONING_DATA_DIR` overrides the
  location; the unfrozen dev layout keeps `<repo>/runtime`. The Windows
  portable CI boot smoke now asserts state lands in the stable dir and nothing
  leaks back beside the exe.

## [0.1.2 – 0.1.8] - 2026-07-10 to 2026-07-13

Releases v0.1.2 through v0.1.8 were tagged without cutting individual
changelog sections; the entries below shipped across those releases and are
grouped here as released work.

### Fixed

- **UDMI misplaced-field diagnostics and location.room support.** When an
  identity value (site, room, serial, GUID, …) is absent at its canonical UDMI
  path but present elsewhere in the payload — e.g. a publisher nesting a second
  `system` object inside `system` — the issue now names where the value was
  found and the exact expected path instead of claiming it is missing, and a
  dedicated finding calls out the double-nested `system` with the one-move fix.
  The register Room column now matches `system.location.section` **or**
  `system.location.room` (both canonical UDMI; a device carrying both fields
  passes when either equals the register value), and the expected template
  embeds the room value under `location.room` when it only fits that field's
  laxer pattern instead of degrading to a placeholder. A metadata pointset
  nested at the wrong level (e.g. under `system`) is reported once with its
  actual path, and the register point/unit comparison runs against the nested
  copy — real content differences (missing points, device-side typos, wrong
  units) surface instead of one false "not defined" per register point.

### Added

- **UDMI run monitor shows the actual capture window.** After a UDMI validation
  run, the run monitor (and the live-results banner) reports the capture window
  the run really used — "120 s (bounded)", "until all topics reported
  (indefinite)", or the capped no-cancel fallback — so an operator can tell why
  a capture stopped when it did.

### Fixed

- **MQTT register import rejects conflicting Asset ID reuse.** A register row
  that reuses an already-registered asset identity (Asset ID, or Asset name
  when the ID is blank) for a different device's topic root is now rejected at
  upload (first row wins) with a per-row error naming both topic roots —
  previously the upload reported every row accepted and one device later
  vanished from the grouped validation results (on-site 2026-07-13). Same-ID
  rows sharing a topic root (one row per payload type) import unchanged, and
  the run-time collision guard still covers imports accepted before this rule.

- **UDMI workbench on-site follow-ups (2026-07-13).** Expected-template
  timestamps (`timestamp`, state `last_config`) now show the template build time
  instead of the 1970 epoch sentinel that read as a broken clock. Register
  identity values that can never fit canonical UDMI patterns (numeric Asset IDs,
  free-text Rooms/Sites, bare GUIDs) no longer invalidate the whole expected
  metadata template: the template embeds a schema-valid placeholder and a
  low-severity note names the register column, value, and required pattern.
  Per-asset "did not publish" issues now say which topics were subscribed and
  what actually arrived (unrecognised topic path, non-JSON payload, or nothing),
  and the result summary records the capture window that was actually used.
  Register rows for the same asset (one per payload type) now merge into a
  single validation entry instead of duplicating every issue per row, and rows
  rejected at import are reported as a run issue — a dropped register row can no
  longer silently remove a device from the results. Rows that reuse one Asset ID
  for different device topics (a register copy-paste error that makes one device
  vanish and doubles its neighbour's issue list) stay separate and the run
  reports the collision with both topic roots.

- **UDMI expected payload templates.** Expected-versus-observed evidence now
  renders complete state, metadata, and pointset UDMI shapes. Known register
  values are embedded at their UDMI paths; schema-valid sentinel values identify
  device-supplied fields rather than copying broker observations into an
  expectation. Invalid register constraints are now reported explicitly.

- **Portable build identification.** `README_FIRST.txt` and the Windows EXE
  Properties → Details tab now show the build version, executable name, and
  product description.

- **UDMI live-result readability and identity checks.** Expected payload evidence
  now uses the same UDMI field paths as captured payloads (`version`,
  `system.hardware.make`, and `system.hardware.model`) instead of internal
  matcher names. Expected captured identity values that are absent now fail
  explicitly, and the result detail includes a local expandable JSON tree for
  inspecting a captured payload without sending it to an external service.

## [0.1.1] - 2026-07-10

### Fixed

- **Live MQTT/UDMI commissioning capture.** Register wildcard filters are retained
  alongside derived state, metadata, and pointset topics; capture subscriptions are
  batched so retained publishes cannot interrupt setup. Results now retain subscribed
  filters, captured topics, retained/timestamp evidence, and sanitized broker details
  for field diagnosis.

This is the pre-1.0 development line. Entries below summarize the program by
theme and are derived from the actual git history (`git log --oneline`), from
the MVP scaffold baseline through the phase 0–4b production-hardening work.

### Added

- **Security: patched Python runtime dependencies** — pinned Starlette 1.3.1,
  cryptography 48.0.1, pydantic-settings 2.14.2, and python-multipart 0.0.31 to
  clear the current published advisory ranges. Starlette now includes the
  Windows `StaticFiles` UNC-path protection and `FormParser` field/part-size
  enforcement missing from 1.0.0. The frozen Windows smoke also proves the
  canonical UDMI schema files are present and exercises nested schema
  validation through the running executable.

- **UDMI workbench: run until every register topic reports (or a set run time)** —
  the workbench Setup stage's capture field is now **Run time (seconds)**: blank
  runs the live capture until a payload has been seen for **every** required
  topic group from the imported register (distinct topics, wildcard-aware), or
  the operator presses **Cancel run**. Worker captures are capped at 1h and 500
  distinct concrete-topic slots (duplicates reuse a slot); inline blank captures
  are capped at 240s. A positive number bounds the run to that many seconds.
  Multi-asset runs now use **one shared
  broker subscription** across all assets' topics, encoded as one MQTT
  SUBSCRIBE packet before the broker can deliver retained payloads (messages route back to each
  asset's state/metadata/pointset evidence) instead of sequential per-asset
  windows, so quiet assets are no longer starved behind chatty ones. Cancel is
  wired end-to-end for UDMI validation (Cancel run → cooperative flag → capture
  stops within ~1s → run finishes as `cancelled` with its real partial results).
  Honesty: a non-cancelled capture that ends with required topics still silent is
  terminal `failed` and retains `live_capture_timeout` plus a `not_publishing`
  issue naming the missing topics; `live_payloads_captured` is only claimed when
  every required topic group reported. A broker drop after messages arrive keeps
  those partial payloads but records a coarse broker failure and terminal
  `failed`. A topic only satisfies completion after its payload decodes to a
  JSON object; malformed/scalar messages remain evidence but produce a critical
  issue instead of a false complete capture. Bounded fallbacks remain visible through `capture_mode` /
  `indefinite_bounded_inline` rather than hanging unkillably.
- **UDMI workbench: canonical schema-version validation** — the register
  template's **Expected schema version** now flows into the UDMI validator and is
  compared against each captured/pasted payload's declared top-level `version`
  (mismatch → immediate critical issue; missing version flagged). On a match the
  payload structure is checked offline against the complete recursive Draft 7
  schema closure for state, metadata, and events/pointset vendored verbatim from
  the official UDMI `1.5.2` tag. This validates nested objects, required fields,
  formats, enums, patterns, limits, and additional-property rules as well as the
  existing focused operator checks.
  Expected register units must now **match** the metadata payload units (with
  `kwh`→`kilowatt_hours`-style alias normalisation, and an explicit `no_units`
  declaration treated as a real observed value) instead of only being
  "a known unit"; an expected unit missing from metadata is now an error, and
  imports reject point/unit lists that cannot pair one-to-one. Expected points
  are checked in the **metadata** pointset as
  well as the live pointset events, and a register wildcard topic additionally
  captures the legacy `…/event/pointset` convention. The workbench gained a
  **register-driven live mode** (both register and broker capture auto-enabled
  after an accepted `mqtt_register` import, while remaining operator-editable):
  Run sends no pasted schedule/payloads and the backend fans out one
  expected asset per register row — a single row keeps its capture topics, and
  a blank **Payload type** means the row requires the whole asset trio even when
  only one explicit sibling topic was supplied. The register's **Expected
  reporting interval** now flags stale pointset timestamps (including retained
  MQTT evidence, whose RETAIN flag is preserved), and invalid bare wildcards or
  unknown payload types are rejected during import. Failed MQTT/UDMI live runs
  carry a sanitized operator-facing `error_message`, and
  a register-driven run with no register import is refused (400) instead of
  silently validating the packaged sample fixture. The workbench Results table
  now renders **real per-asset, per-payload rows** from the run's payload views
  and issues (labelled with whether payloads were captured or pasted) instead
  of permanently showing the "Sample preview" rows.
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

- **Run History page** — an Operate view listing every recorded run in one
  sortable, filterable table with **absolute** Started/Finished timestamps, run
  type, status, and a derived **duration**, plus **Export CSV** of the visible
  rows. A read-only view over the existing `GET /runs` data (no backend change,
  no new dependency); non-terminal runs show Finished/Duration as `—` rather
  than a fabricated finish. Replaces reaching for the raw SQLite file to review
  past runs.

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

- **Docs install sweep (2026-07)** — install/setup docs now lead with the
  `scripts/bootstrap-env.*` scripts as the canonical way to create `infra/.env`
  (manual `CHANGE_ME` editing stays as a fallback), and the NIC-selection
  proposal was updated for the wired-first Source Interface default (empty
  seed = never chosen; the Auto sentinel stored only on an explicit pick).

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

- **Live MQTT outcome and configuration honesty.** Saved MQTT **Use TLS** values
  must be Enabled or Disabled, and malformed per-run `use_tls` overrides are
  rejected instead of silently disabling TLS. Non-cancelled broker, discovery,
  and incomplete live-UDMI outcomes now finish `failed`; mid-capture broker drops
  retain real partial evidence, and operator cancellation remains `cancelled`.
- **Medium/low audit backlog: honesty and safety fixes (2026-07-09).** A batch of
  smaller audit findings, all now honest or fail-closed: MQTT **fails closed when
  a configured CA file is missing** (no silent fallback to system trust); the
  Configuration page warns when connecting to a **TLS broker by IP** (the cert SAN
  cannot match an address) and when **Use TLS / Port look mismatched**; the IP
  scanner gained an **ad-hoc target field** (CIDR / range / addresses) so a sweep
  no longer requires an imported register, and its results now populate the
  **Asset** (from the register) and **Last Seen** columns; scan authorization
  records the **real authenticated user** instead of a hardcoded
  `frontend-operator`; the **Certificate Expiry** pill shows the **soonest**
  expiry across CA + client (so an expired cert cannot hide behind a later one)
  and imported **dangling cert references are dropped** so the UI no longer shows a
  missing cert as "in use"; and the portable exe **warns (by name, never value)**
  when an ambient environment variable overrides its local / inline / SQLite
  profile. Two items are documented as **by design** (not changed): MQTT config
  publish is **QoS0 / non-retained**, and loopback `local` mode grants **keyless
  admin** (single-user edge; RBAC is a boundary only under `api_key` with per-user
  keys).
- **Usability audit: five misleading-output fixes (2026-07-09).** A multi-agent
  audit found five places where a working-looking screen hid a non-result; all
  now honest: (1) the IP scanner is TCP-only, so its UI no longer ships BACnet's
  UDP 47808 as a default port / protocol option (a TCP probe cannot detect
  BACnet — use BACnet Discovery); (2) a live BACnet scan with no Source Interface
  configured now fails with an actionable, operator-visible reason (on the run's
  error_message) telling the engineer to pick and save a Source Interface, instead
  of an opaque "engine execution failed" — and it no longer stamps a false "Live
  bacpypes3 scan" provenance label on a run where no socket was ever bound; (3) a
  failed/timed-out UDMI live capture no longer relabels the pasted default
  payloads as "live-captured"; (4) a fresh install no longer seeds placeholder
  certificate references that render as a real, in-use, valid certificate — cert
  fields start empty and are optional in validation; (5) an evidence pack
  generated with no selected source runs is labelled "None selected" instead of
  claiming to cover "All completed runs".
- **MQTT password Show/Hide reveal is now repeatable, and a secure/non-secure
  connection selector was added (field review, 2026-07-09).** The Configuration
  page's MQTT (and Key) password fields now render a Show/Hide toggle that flips
  the input between masked and plaintext as many times as the operator wants
  (the earlier reveal only worked once). The MQTT Settings section also gains an
  explicit **Use TLS** (Enabled/Disabled) control so a secure (8883/TLS) vs
  non-secure (1883/plain) broker connection can be chosen directly rather than
  inferred only from the port. `build_mqtt_connection_settings` honours the
  persisted `Use TLS` selection (a valid explicit `use_tls` run parameter still
  wins; malformed overrides fail closed); configs saved before the control keep
  the port-based default (8883 = TLS).
- **Real BACnet discovery in the portable exe (field bug, on-site 2026-07-09).**
  The exe returned *simulated* devices ("Acme Controls"/"Globex BMS") for every
  BACnet scan because (a) `bacpypes3` was not bundled, (b) the route never
  requested the real backend, and (c) the UI never showed which backend ran.
  Now: an **authorized, non-dry-run** BACnet scan defaults to the real
  `bacpypes3` backend (`bacpypes3` is bundled with `--collect-all` + a frozen
  hidden-import + a `_internal\bacpypes3` boot-smoke assert); if the real stack
  or the NIC bind is unavailable the run records a **real failed status** — it
  never silently returns simulated data. Dry-run stays a simulated *plan*;
  explicit simulated or unknown backends on a live run are rejected with 400. The
  discovery results view now shows a prominent **"SIMULATED — demo data"**
  banner vs a **"Live bacpypes3 scan"** confirmation, driven by
  `result_summary.backend`. NOTE: the real bacpypes3 path is validated by frozen
  import + boot only — real on-wire discovery remains an on-site step.
- **IP discovery now populates MAC address and hostname, and the "View" button
  works.** The TCP-connect sweep never read MAC (blank) and reverse DNS was off
  and unexposed (blank hostname), and the per-row **View** button was a dead
  handler. Now: after a host is confirmed live, a best-effort **ARP-cache MAC
  lookup** (`/proc/net/arp` on Linux, `arp -a` on Windows, time-bounded,
  no-window) fills `mac_address`; **reverse DNS is defaulted on** for real
  (non-dry) runs so `hostname` fills — both degrade to blank on a miss and are
  never fabricated (MAC only exists for same-L2 hosts; hostname only with a PTR
  record). The results table shows MAC + Hostname columns and **View** opens a
  per-host detail panel.
- **NIC adapter classification / gateway / DNS restored in the portable exe.**
  The Windows net-facts helper (`Get-NetAdapter` / `Get-NetRoute` /
  `Get-DnsClientServerAddress`) was capped at a 5s timeout, but those CIM/WMI
  queries take ~9.5s inside the frozen exe's no-window subprocess, so the call
  *always* timed out and every adapter silently degraded to `unknown` type with
  no gateway/DNS and no virtual-adapter filtering — the entire 2026-07-03 NIC
  UX (wired-first default, "Wi-Fi not recommended" tag, gateway/DNS panel) was
  dead in the shipped exe while dev/tests passed (they mock psutil and never run
  the real PowerShell). Raised the timeout to 20s, kept the facts-cache TTL
  `>=` the timeout, and — critically — the helper now **logs** a warning on
  timeout / non-zero exit instead of swallowing it to `None`, so a future field
  failure is diagnosable. Guarded by unit tests on the timeout floor and the
  log-on-failure path.
- **Portable exe now bundles `psutil`, restoring the NIC picker.** The frozen
  launcher never imported `psutil`, and backend/app import-guards it (degrading
  to an Auto-only Source Interface list instead of erroring), so the packaged
  exe silently shipped without NIC enumeration — the health-only boot smoke
  could not see it. The launcher now freezes `psutil` explicitly and the
  `windows-portable` CI boot smoke additionally asserts `psutil` exists inside
  the frozen bundle (a `_internal\psutil` presence check — the endpoint itself
  cannot detect the regression because the import-guard makes an empty list a
  valid 200 either way).
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

- **Repo-wide over-engineering cleanup (~1,750 lines net, behavior-neutral).**
  An audit ranked and adversarially verified the cuts before applying them;
  every gate (ruff, backend/core/worker `unittest`, frontend
  test/lint/typecheck/build) stays green. Highlights: a shared
  `backend/tests/harness.py` collapses the DB-harness boilerplate duplicated
  across 11 test files; four field pre-flight scripts fold into
  `smoke_local.{sh,ps1}` behind a `--preflight`/`-Preflight` flag
  (`phase5_preflight.*` deleted); dead modules and endpoints go
  (`discovery_observations`, `smoke_udmi_api`, the `/blueprint` route and its
  client, the pre-DB `import_runtime_state` migration, the worker's dead
  `main.py`/`smoke_udmi_adapter.py`/`generate_report` actor); verbatim
  duplicate helpers are de-duplicated across core engines and repositories;
  hand-rolled code is replaced by stdlib (`dict.fromkeys` dedup,
  `dataclasses.asdict`); import profiles stop listing their columns twice; and
  seven never-rendered `ModuleDefinition` fields plus other dead frontend
  exports are dropped.
- **`pydantic-settings` dropped from the worker.** The worker read two env vars
  (`REDIS_URL`, `DATABASE_URL`) through a settings model; that is now plain
  `os.getenv`, removing the dependency from the worker image.
- **CI runs halved per PR commit.** `ci.yml` and `windows-compat.yml` triggered
  on unfiltered `push` *and* `pull_request`, so same-repo PR branches ran every
  workflow twice; `push` is now filtered to `main`. `windows-compat.yml` also no
  longer re-runs the ruff/eslint/tsc gates that `ci.yml` already owns.
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
[0.1.1]: https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.1
