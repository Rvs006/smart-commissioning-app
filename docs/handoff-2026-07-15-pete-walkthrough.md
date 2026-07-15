# Handoff — Pete's 2026-07-15 v0.1.10 walkthrough punch list

**Audience:** the next Claude Code session (any account/machine). This document
is the single source of truth for the current work state. Read it fully before
touching code. Everything below was verified against `main @ 82e838c`
(the v0.1.10 changelog cut) by a 9-agent code investigation on 2026-07-15;
every claim carries file:line evidence you can re-check.

**Repo state when this was written:** `main` == `origin/main` @ `82e838c`,
working tree clean, **0 open PRs** (old remote branches are kept deliberately —
do not delete them). v0.1.10 released 2026-07-14 (~23:50) with PRs #76/#77/#78.

**Model routing (also in AGENTS.md/CLAUDE.md):** plan/investigate/architect on
**Fable (claude-fable-5)**; write code on **Opus 4.8 (claude-opus-4-8)**.

---

## 1. Context

- **Pete** — field engineer at ELECTRACOM, validating the app on his laptop and
  an MSI server. He walked through v0.1.10 end-to-end on 2026-07-15 (IP
  discovery → BACnet → MQTT discovery → UDMI workbench → reports) and produced
  the punch list below. John and Dylan will add feedback later.
- **Verdict: NOT an overhaul.** No architecture changes. ~25 items,
  ~30–35 engineer-days total if everything ships; the field-critical core is
  ~2 weeks split into two releases (v0.1.11, v0.1.12).
- **Hard date:** Pete's home lab (~60 devices, 3 servers, BBMD, real BACnet/
  MQTT field devices) comes online over the weekend; deep scan session is
  **Monday 2026-07-20**. The BACnet fix (v0.1.12 headline) should be ready then.
- **Decision made on the call:** drop the Docker distribution; the portable exe
  is the only supported path going forward (Docker can't reach host L2 for
  scans). Do NOT remove the hosted multi-user profile code (api_key/Postgres/
  Redis/Hub) — it is Docker-independent; only the Docker packaging/docs go.

---

## 2. Master root cause — the step-gating/state-reset flaw (explains 4 complaints)

The Setup/Run/Results stepper is CSS-only hiding, and route changes wipe
in-memory state:

- `.module-steps > [data-stepgroup] { display:none }` except the active step —
  `frontend/src/styles/electracom-theme.css:1299-1316`
- Route-change effect resets `step` → `"setup"`, `activeRun` → `null`,
  `reportToast` → `null` — `frontend/src/features/workflow/ModulePage.tsx:404-439`
  (`setStep("setup")` at `:427`, `setActiveRun(null)` at `:410`)

Consequences Pete observed, none of which are data loss:

1. **"Report not in the Reports tab."** The report IS created (POST
   `/api/v1/reports` → `run_service.py:66-96` persists a `report_generation`
   run, immediately `succeeded`) and IS returned by GET `/reports`
   (`reports.py:53-58`, no filters). The "Generated Reports" table carries
   `data-stepgroup="results"` (`ModulePage.tsx:2177-2178`) and the Reports page
   always lands on the hidden "setup" step. Clicking the "3 Results" step button
   reveals it. jsdom tests can't catch this class of bug (theme CSS not applied).
2. **"IP discovery loses results on navigate-away."** Page state is
   component-local `useState` reset on remount (`routes.tsx:24-29` remounts
   ModulePage per path). Server-side run persistence is fine — `GET /runs` /
   `GET /discovery/runs` list runs newest-first with `job_type` filter
   (`client.ts:889` already supports the params). Fix = rehydrate the latest
   terminal run per head on mount.
3. **"It created sample views."** Nothing was created: with `activeRun` nulled,
   `resultRows` falls back to hardcoded fixtures —
   `resultRows = discoveryView?.rows ?? udmiLiveResults?.rows ?? workspace?.rows`
   (`ModulePage.tsx:813`) with fixtures at `operatorData.ts:314-328`.
4. **"Report button should be at the end of results."** The
   generate-report picker/button exists on ALL five heads but sits in the
   run-monitor panel inside `data-stepgroup="setup run"` (`ModulePage.tsx:1425-1452`,
   section at `:1072`), so it vanishes on the Results step.

---

## 3. The three live bugs — root causes (all high confidence)

### 3a. BACnet discovery returns nothing (CONFIRMED; fix ~3d — v0.1.12 headline)

The live bacpypes3 path supports **only local-subnet broadcast Who-Is**:

- `Bacpypes3Backend.who_is` drops the directed address on purpose
  ("intentionally not passed") — `core/smart_commissioning_core/engines/bacnet_discovery.py:374-391`
- `Application.from_json` config has NO BBMD/foreign-device entries — `bacnet_discovery.py:359-371`
- The Configuration page's BBMD / Foreign Device / BBMD Address / TTL fields are
  validated and stored but **consumed nowhere**
  (`configuration_service.py:46-54,363-377`; `ConfigurationPage.tsx:146-147`)
- The uploaded `bacnet_register` is never used for discovery targeting — there
  is no BACnet analogue of `_ensure_ip_targets` (`discovery.py:168-199` is IP-only);
  the BACnet run route injects only `device."Source Interface"` (`discovery.py:323-365`, `:130-138`)
- Empty Who-Is → run ends `status=succeeded`, `device_count=0`, silently
  (`bacnet_discovery.py:643,691-706`)

Pete's device 123123 sits behind a BBMD at 10.10.10.22; his third-party browser
sees it precisely because it does foreign-device registration. Secondary
hypothesis: that browser holds UDP 47808 exclusively → bacpypes3 bind
`OSError` → blanket except → sanitized generic failure (`engines/base.py:323-326,431-432`).

**Fix:** plumb the bacnet config section (Foreign Device toggle + BBMD Address +
TTL) from `create_bacnet_discovery_run` into the backend parameters and
configure bacpypes3 FD registration (`fdBBMDAddress`/`fdSubscriptionLifetime`);
add directed **unicast Who-Is to register IPs** as fallback; replace the
sanitized bind-failure with an actionable "UDP 47808 in use — close other BACnet
tools" message. Must be field-verified on Pete's lab Monday 2026-07-20.

**Ask Pete:** run status of his failed attempt (succeeded-with-0 vs failed —
distinguishes silent-empty from 47808 conflict); whether the BACnet browser was
open during the run; his Source Interface value; whether the BBMD accepts FD
registrations.

### 3b. Register import rejections (CONFIRMED; fix ~2d — in v0.1.11)

Three findings:

1. **Reasons are recorded, then thrown away.** `ImportService.create_import`
   produces per-row `{row_number, field, code, message}` records, persisted
   (`import_service.py:759-772`); the route discards them —
   `summary, _ = service.create_import(...)` at `backend/app/api/routes/imports.py:124` —
   and the UI renders only "N accepted · M rejected" (`ModulePage.tsx:1194-1202`).
   **`GET /imports/{import_id}/errors` already exists** (`imports.py:144-149`)
   and is never called by `client.ts`. `missing_columns` is also never rendered.
2. **The likely rejector of Pete's "made-unique" fan-coil row:**
   `_validate_mqtt_asset_topic` (`import_service.py:187-211`) requires every
   Expected topic to end `/#`, `/state`, `/metadata`, `/event/pointset` or
   `/events/pointset` (empirically: `site/b1/fcu-04` rejects, `site/b1/fcu-04/#`
   accepts). GUID and Serial are **never uniqueness-checked** — the dup key is
   `(Asset ID, Expected topic)` (`import_service.py:451,717-728`) plus
   `_conflicting_asset_topic_error` (`:264-307`). Pete was fixing the wrong
   fields because no reason was shown. Also: "Expected reporting interval" must
   pass `isdigit()` so Excel's `60.0` rejects (`:105-111,150-159`); a
   semicolon-locale Excel save parses as ONE column → all 8 required columns
   "missing" → whole file rejected. Pete's end-of-line-delimiter theory was
   **disproven empirically** (CRLF/BOM/trailing blank line/trailing-comma
   padding all parse cleanly). Real parser gaps: `io.StringIO(text)` without
   `newline=""` (`import_service.py:797-807`) → CR-only saves raise `csv.Error`
   which escapes the route's `except ValueError` (`imports.py:131`) → HTTP 500;
   cp1252/UTF-16 saves → decode error.
3. **Same-filename "silent skip" is client-side.** No backend filename dedup
   exists. The file input's value is never cleared (`ModulePage.tsx:1100`,
   handler `:869-872`), so Chromium fires no change event when re-picking the
   same path → stale `File` snapshot → the previous rejection panel stays and
   old bytes get re-sent (or `ERR_UPLOAD_FILE_CHANGED`). **Pete's corrected CSV
   never reached the server until he renamed it.** Forensics: every upload that
   reached the server is kept verbatim at
   `%LOCALAPPDATA%\SmartCommissioning\imports\files\` on his machine.

### 3c. "Report missing from Reports tab" (LIKELY→confirmed by code; fix ~0.5–1.5d — in v0.1.11)

Pure presentation — see §2 item 1. Backend needs no change. Fix: default the
reports route to the results step (it has no run lifecycle) or ungate the table;
show loading state instead of "No reports yet" while `reportsQuery.isLoading`
(`ModulePage.tsx:1055-1059`, guard `:841-852`); invalidate the `reports-list`
query on `reportFromRunMutation.onSuccess`.

---

## 4. Full sized punch list (per area, code-verified)

Effort scale: trivial <2h · small ~half-day · medium 1–3d · large >3d.

### Shell / branding / configuration (~6.5d total)

| Item | Effort | Key facts |
|---|---|---|
| Version pill on hero/brand bar | small 0.5d | Version exists only at build time (`build.ps1:91-119`; CI passes `workflow_dispatch` input). Set `$env:VITE_APP_VERSION = $BuildVersion` before `npm run build` (~`build.ps1:198`); read `import.meta.env.VITE_APP_VERSION` (precedent: `VITE_REVIEW_COMMENTS`, `App.tsx:166`); render pill in brand bar (`App.tsx:~99`). Optional: echo version in `/api/v1/health`. Risk: `-SkipFrontend` reuses a dist with old baked version — add guard like the `-SkipFreeze` one (`build.ps1:212-215`). |
| ELECTRACOM logo not showing (exe) | trivial 0.25d | File ships in dist; FastAPI SPA fallback returns index.html for `/electracom-logo.png` because only `dist/assets` is mounted (`main.py:165-168`, fallback `:184-189`). Fix generically: if requested path resolves to a real file inside FRONTEND_DIST (traversal-guarded), `FileResponse` it. Covers all future `public/` files. Add a logo-200 assertion to the portable boot smoke. |
| Remove placeholder demo content | medium 1.5d | (a) "Block B Plantroom" hardcoded pill `App.tsx:115-118`; (b) "Current Stage" board is sample data (`operatorData.ts:254-279`, `DashboardPage.tsx:310-323`); (c) seeded fictional defaults incl. "Last Backup Status: Success" that never happened (`configuration_service.py:24-118`). NOTE: changing DEFAULT_CONFIGURATION does NOT update already-persisted snapshots on Pete's machines — needs migration or release note. |
| Menu naming: "BACnet" → "BACnet Discovery" | trivial 0.25d | `NAV_GROUPS` in `App.tsx:13-39`; also reconcile `pageTitles` (`App.tsx:41-53`) and `moduleData.ts` titles (three layers disagree: "IP Scanner" vs "IP Discovery" etc.). Label-only, routes unchanged (precedent `moduleData.ts:121-125`). |
| Config tab sub-sections | small 1d | Seven flat accordion sections already exist (`ConfigurationPage.tsx:26-34`). Add group headers (Connections / System / Maintenance). Recommended extra: wire the fully-built signed backup endpoint (`POST /evidence/backup`, `backup_service.py`) to a "Download backup now" button — it currently has zero UI. |
| Logging destinations | medium 2.5d | The whole Logging & Diagnostics config section is **decorative** — no code reads Remote Syslog Target/Port/Retention/Diagnostics Mode; no syslog handler exists. Actual logging = one JSON StreamHandler (`core/logging.py:151-173`). (a) local `RotatingFileHandler` to `RUNTIME_ROOT/logs/app.log` ~1d; (b) engineer-gated "Upload logs now" to a configured URL (httpx already frozen) ~1.5d. Secrets-masking + write-only sentinel for upload creds required. |
| Certs "Not configured" pill | small 0.5d | ALL section status pills are static seeded strings never recomputed (`configuration_service.py:86`, save persists whatever the client echoes). Derive certificates status server-side on load from resolvable `secret://` refs + stored expiry (reuse `_secret_path().exists()`, `_stored_certificate_expiry:612-629`). Pete IS reloading keys so it may also genuinely be unconfigured. |

### IP Discovery head (~10d total)

| Item | Effort | Key facts |
|---|---|---|
| Retain last run per head (ALL heads) | medium 2d | See §2. Seed `activeRun` from `listRuns({jobType, limit:1, status:'succeeded'})` on mount; route→jobType map; guard the reset effect. No backend change. Don't auto-jump operators to Results mid-setup. |
| Show ALL register entries incl. non-responders | medium 2d | Engine drops silent hosts: `if not open_ports: continue` (`engines/ip_scan.py:656-658`). Emit rows for every scanned host with honest "no response on scanned ports" (a TCP-connect miss is NOT proof of absence — never render a hard "fail" for it). Frontend: add Result column + wire the existing `__tone` row shading (`.row-pass/.row-fail`) into `ipRowsFromResults` (`discoveryRows.ts:99`, columns `:13`). Verdict markers already stamped in status_detail. |
| Explicit "no results found" state | trivial 0.5d | Empty-workspace block exists (`ModulePage.tsx:2356-2369`) but shows "No results yet" for a completed-empty scan. Add terminal-empty branch citing `result_summary.hosts_scanned` (`ip_scan.py:765`). |
| nmap-style general network discovery pane | large 3.5d | The bottom "target override" already does register-less CIDR/range scans through the same engine (`ModulePage.tsx:1633-1643`, `buildDiscoveryParameters:2661-2681`; service hints `ip_scan.py:89-95`; reverse-DNS + ARP MAC included). **nmap itself cannot ship** (ThreatLocker/WDAC blocks unsigned exes). Build a dedicated pane: top-N common-ports profile, hosts/ports/service-guess/hostname/MAC results view, no register verdict columns. Do not imply OS/version detection parity. Keep throttle + MAX_HOSTS/MAX_PORTS ceilings (live OT networks). |
| Consistent look-and-feel / unbind scroll boxes | medium 2d | All five heads already share ONE component (`ModulePage.tsx`, **3190 lines**) so ~80% consistent. The "bound window" = `.data-table-wrap` overflow box + `.data-table` min-width 680px (`styles.css:1046-1057`). Extract `<ResultsTable>`, `<RunMonitor>`, `<Inspector>` shared components; drop fixed min-width; results full-width with inspector below. Broad edit surface — regression risk on per-head custom sections. |

### MQTT Discovery head (~4.5d total)

| Item | Effort | Key facts |
|---|---|---|
| Template compare: green=in register, red=foreign | medium 1.5d | Discovery genuinely ignores the register (route `discovery.py:368-406` inherits only topic_filter/qos from config; engine emits one row per observed topic). Stamp verdicts at **results-read time**: load newest accepted `mqtt_register` import, build expected-topic filters via existing `_capture_topics_from_expected`/`_expected_assets_from_register` (`validation.py:139-328`), match with existing wildcard matchers (`discovery.py:552`, `discoveryRows.ts:175`), color via `__tone`. Works retroactively on old runs. Agree semantics: one `#` wildcard register row can green-light dozens of topics. |
| Long-duration timer (like UDMI 48h) | medium 2d | More exists than Pete realizes: UI capture-seconds field with 0=indefinite + working cancel already there (`ModulePage.tsx:1692-1700`, engine `mqtt_discovery.py:226-237,328-335`). Ceilings to lift: `discover_mqtt` actor `time_limit=3_600_000` (1h) vs UDMI's 49h (`worker/app/tasks.py:205` vs `:231`); add `capture_seconds <= 172_800` guard reusing `parse_capture_seconds`; unit dropdown copied from UDMI block (`ModulePage.tsx:639-648,1986-2004`). **Physics first: retain-latest-per-topic before raising max_messages** or multi-day captures eat memory. Portable exe runs engines inline in the HTTP request (`run_dispatch.py:50-57`) — day-scale captures honestly require the hosted worker profile; same caveat as UDMI's existing inline note. |
| Wire inspector to row selection | small 0.5d | All pieces exist: `selectedResultIndex` state (`:258`), inspector aside (`:2407-2584`), `JsonTree` (`:2765-2785`), Raw Payload already in rows. Make `<tr>` clickable, render payload + JsonTree for the selected row, suppress the sample-issues fallback on discovery routes. Non-JSON payloads stored as `{"_raw_present": true}` markers (`mqtt_discovery.py:399-401`) — handle gracefully. |
| Report button at end of results | trivial 0.5d | Extract the existing picker+button (`ModulePage.tsx:1425-1452`) into a small component; render a second instance inside the results stepgroup (`:2273`), same `canEngineer && activeRun && activeRunTerminal` guard. One change fixes all five heads. |

### UDMI workbench (~4.6d total)

| Item | Effort | Key facts |
|---|---|---|
| Remove top "Run UDMI Validation" control | trivial 0.25d | NOT a Docker leftover — it's the single `runActions` entry (`moduleData.ts:110-118`) that **Execute capture also findIndexes into** (`ModulePage.tsx:653-658`). Hide it from Run Controls rendering (`:1261`); do NOT delete the moduleData entry or Execute capture silently breaks (the `Math.max(0, findIndex)` fallback masks -1). |
| Snap to top when results open | trivial 0.1d | Terminal-success effect exists (`ModulePage.tsx:464-468`); add `heroRef.scrollIntoView()` when step flips to results. No scroll management exists anywhere today. |
| Dynamic inspector on row select | small 0.5d | Same wiring as MQTT inspector: row click → `setSelectedResultIndex` + sync `setExpandedAsset`/`setExpandedPayloadKey`; render observed payload via JsonTree + shared `gatedUdmiVerdict`. `stopPropagation` on the Copy button cell. |
| Show rejection reasons | small 0.5d | Frontend-only — see §3b(1). Add `getImportErrors(importId)` to client.ts; red panel listing "Row N — field: message (code)" + `missing_columns`, modeled on the existing warnings panel. Zero backend work. |
| Don't fail run on silent devices | medium 1.5d | ~70% shipped in #78 (`not_publishing` issues, `result_summary.not_publishing_devices`, honest score, report rendering). Remaining: ONE decision point `udmi_run_processor.py:84-98` — keep `failed` only for transport errors (broker_unreachable/authentication_error/broker_timeout/capture_failed family, `udmi_validation.py:1229-1241`); `live_capture_timeout` → `succeeded` with distinct stage. Update pinned tests + `docs/protocol-conformance.md:46`. Transport failures MUST stay failed (honesty rule). Ship together with RAG so silent devices read RED on a succeeded run. |
| RAG third state (amber) | small 1d | Verdicts centralized in `operatorData.ts` (`UdmiVerdictKind:182`, `udmiPayloadVerdict:189-214`, tone `:218-223`). Pete's definition needs NO issue weighting: red=offline/not publishing, amber=publishing but non-compliant, green=compliant. Add `warn` tone + `.row-amber` CSS + amber section-verdict branch. **Confirm with Pete:** strict reading demotes today's green "pass with notes" (minor-only issues) to amber. |
| Nonpub schema example set | small 0.75d | Upload machinery fully shipped (#78): `udmi_schemas.py:260-302`, strict validation incl. three roots + file: refs. Missing = a downloadable example. Add public `GET /udmi/schemas/template` zipping the vendored 1.5.2 roots + $ref closure (patterns: `imports.py:20-45` public router, `reports.py:548` in-memory zip) + README.txt + frontend download button. Watch the 2MB stored-set cap vs ~30-file canonical set — ship a trimmed minimal closure. |

### Reports (~6.5d total)

| Item | Effort | Key facts |
|---|---|---|
| Per-head report content + button placement | medium 3d | Buttons + collation already exist (see §2/§3c). Real gap: discovery reports have no head-specific content — add "Discovery inventory" sections to the builders via `DiscoveryRepository.list_devices/list_points/list_topics` (pattern: `_validation_summary`). **Byte-reproducibility**: sort all rows deterministically or the Ed25519/SHA-256 verify breaks; add reproducibility tests. |
| Reports-tab visibility bug | trivial 0.5d | See §3c. |
| ELECTRACOM headers/footers (ITP witnessing) | medium 2d | Zero branding exists. PDF is hand-rolled (deterministic PDF 1.4; only page furniture is `_append_footers`, `report_pdf.py:339-350`; **no image XObject support** — text-branded header band first, logo embedding is the risky part, phase it). DOCX is hand-rolled 3-part OOXML with empty `<w:sectPr/>` (`reports.py:544`) — add header/footer parts + rels + Content_Types overrides. XLSX: openpyxl has native header/footer. All artifact bytes change → flag to team, keep reproducibility tests green. |
| Reports page columns + remove sample rows | small 1d | Extend `ReportSummary` with `created_at` + `source_run_ids` (`reports.py:27-44`); add Generated/Source-runs columns + per-row Download (reuse `getReportDownloadPath` + `useFileDownload`); delete fabricated sample rows (`operatorData.ts:385-399`). Do together with the visibility bug (~1.5d combined). |

### Packaging / Docker removal (~2d total)

| Item | Effort | Key facts |
|---|---|---|
| Drop Docker distribution | medium 1.5d | Docs/config only — **CI has no Docker jobs at all**. Delete: 3 Dockerfiles, `frontend/nginx.conf`, `.dockerignore`, `infra/` (compose ×2, .env.example, README), `scripts/bootstrap-env.*`. Rewrite README path-2 sections + quickstart/review-guide/runbook. **Keep the hosted multi-user profile code** — it is Docker-independent. |
| Docker-era UI leftovers | small 0.5d | The real one: Learning page SETUP_PATHS 'docker' entry (`LearningPage.tsx:607-671`) + the ThreatLocker note at `:528` that recommends Docker as the escape hatch — replace with the IT allow-listing path (a SHA-256 approval flow already exists) **in the same change** or locked-down users lose their documented path. Update `LearningPage.test.tsx:29-51`, `docs/field-quickstart.md:86-87`. |

---

## 5. Release plan

| Release | Contents | Est. |
|---|---|---|
| **v0.1.11** (next) | Quick wins (logo, version pill, nav naming, snap-to-top, hide Run-UDMI card, empty-scan message, report button on results) + step-gating/state-retention fixes + import error surfacing + file-input reset + parser hardening. Stretch: reports-page columns/sample-row removal. | ~5–6d |
| **v0.1.12** (target: usable Monday 2026-07-20 lab session) | BACnet foreign-device registration + unicast Who-Is + 47808-in-use message; MQTT template-compare RAG; MQTT duration (retain-latest first); UDMI RAG amber + succeed-with-silent-devices. | ~6–7d |
| **Later** | nmap-style discovery pane; branded per-head report content + headers/footers; ModulePage component extraction (look-and-feel); logging destinations; Docker removal; placeholder/demo-content purge; config status pills derived. | ~12d |

**Open questions for Pete** (ask before/while building): BACnet run status +
browser-open question (§3a); RAG demotion of "pass with notes" (§4 UDMI);
which browser he uses (confirms the same-filename no-change-event behavior).

**Non-code context:** Raj's Claude subscription limit resets Fri/Sat; Pete was
asking John about expensing a Max subscription. Pete emailed his own notes doc
("smart commissioning app mods"). ITP = integrated testing and planning —
reports feed ITP witnessing packs, hence the branding item.

---

## 6. READY-TO-PASTE PROMPT for the v0.1.11 session

Copy everything between the lines into a fresh Claude Code session opened at
the repo root:

---

> Read `AGENTS.md` and `docs/handoff-2026-07-15-pete-walkthrough.md` in full
> before doing anything — the handoff has code-verified root causes with
> file:line evidence for everything below; do not re-investigate from scratch,
> but do re-verify each cited location before editing it.
>
> Follow the model-routing convention in AGENTS.md: do the planning/design pass
> on Fable, then write the code on Opus 4.8 (switch model or delegate
> implementation subagents to `claude-opus-4-8`).
>
> Build **v0.1.11** per handoff §5. Work on a feature branch off `main`
> (e.g. `fix/v0.1.11-walkthrough-punchlist`), conventional commits, update
> `CHANGELOG.md` with a `[0.1.11]` section. Do NOT merge or tag without my
> explicit authorization (repo convention). Scope — handoff §2, §3b, §3c, §4:
>
> 1. **Static file serving fix** (logo): generic public-file serving in the SPA
>    fallback (`backend/app/main.py:184-189`) with path-traversal guard +
>    backend unittest (`/electracom-logo.png` → image/png; `../` → 404).
> 2. **Version pill**: `VITE_APP_VERSION` piped from `build.ps1` into the
>    frontend build; pill in the brand bar; dev fallback "dev"; guard or
>    document the `-SkipFrontend` stale-version case.
> 3. **Nav naming**: "BACnet" → "BACnet Discovery" in `NAV_GROUPS`; reconcile
>    `pageTitles` and `moduleData` titles to one canonical name per head.
> 4. **Snap-to-top** when a run flips to results (all heads), jsdom test on
>    scrollIntoView.
> 5. **Hide the top "Run UDMI Validation" run-card** — hide in rendering ONLY;
>    keep the `moduleData.ts:110-118` entry (Execute capture findIndexes it).
> 6. **Empty-scan state**: terminal-empty branch with "Scan complete — no
>    responsive hosts found (N hosts probed)" using `result_summary.hosts_scanned`.
> 7. **Report controls on the Results step**: extract the existing picker+button
>    (`ModulePage.tsx:1425-1452`) into a component rendered in BOTH the run
>    monitor and the results stepgroup, same engineer/terminal-run guards.
> 8. **Step-gating + state retention** (the §2 master fix): (a) Reports route
>    defaults to / ungates the results step so the Generated Reports table is
>    visible on arrival; loading state instead of "No reports yet" while the
>    query is in flight; invalidate `reports-list` on report creation.
>    (b) Rehydrate the most recent terminal run per head on mount via
>    `listRuns({jobType, limit:1, status:'succeeded'})` with a route→jobType
>    map; guard the route-change reset effect so it doesn't wipe the rehydrated
>    run; do not auto-advance an operator who is mid-setup.
> 9. **Import UX + parser hardening** (§3b): (a) frontend — add
>    `getImportErrors(importId)` calling the existing
>    `GET /imports/{import_id}/errors`; on rejected/partial outcomes render a
>    red panel "Row N — field: message (code)" plus `missing_columns`
>    (scrollable/capped); clear the file input value after selection/import so
>    re-picking the same filename works in Chromium. (b) backend —
>    `io.StringIO(text, newline="")`; catch `csv.Error` and return a clear 400;
>    cp1252 fallback decode; detect semicolon-delimited saves and say "file is
>    not comma-delimited" instead of listing all columns missing; clearer
>    `invalid_topic` message naming the allowed suffixes.
> 10. **Stretch (only if the rest is green)**: Reports page `created_at` +
>    `source_run_ids` columns, per-row Download button, delete the fabricated
>    sample report rows (`operatorData.ts:385-399`).
>
> Constraints: Python tests are stdlib `unittest` (match CI); frontend
> `npm test -- --run`, lint, typecheck, build must pass; ruff on
> backend/worker/core; engines must never fake success (honesty rule — a
> non-responding host is "no response", not a fabricated fail); note that jsdom
> cannot see the step-gating CSS, so for the visibility fixes assert on the
> `data-step`/`data-stepgroup` attributes or component logic, and say so in the
> PR description. When done: run the full test matrix, summarize what
> changed per item, and stop for my review — I will authorize merge, then we
> cut the release with `packaging/windows_portable/build.ps1 -Version v0.1.11`
> via the CI `workflow_dispatch` as in v0.1.9/v0.1.10.

---

*Written 2026-07-15 by the analysis session (Fable). Investigation: 9 agents,
~1.2M tokens, main @ 82e838c. Companion memory (account-local, not portable):
`pete-asks-2026-07-15.md`.*
