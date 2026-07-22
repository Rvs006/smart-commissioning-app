# Handoff — Smart Commissioning App v0.1.13 (the rest of field engineer's punch list)

> **✅ SHIPPED 2026-07-16.** v0.1.13 merged, CI-verified (including the backend
> Python this banner's original status said was unrun), released with a
> boot-smoked portable bundle, and followed same-day by v0.1.14 (placeholder
> residue + a Windows CI flake). The pickup steps in §6 are spent. What stays
> live here: **§4 (deferred items)** and **§5 (open decisions for field engineer)** —
> plus §7's honesty caveats, which hold until the 2026-07-20 lab session.

**Written:** 2026-07-16. **Audience:** whoever picks this up next — any account, fresh session.
You do **not** need the conversation that produced this. Everything you need is below.

**One-line status (superseded — see banner):** v0.1.13 is code-complete on a branch, frontend checks are green, but **no
backend Python has run yet** — it is CI-unverified until a PR runs it. Pick it up **after the
v0.1.12 BACnet exe is built**.

---

## 1. What v0.1.13 is

field engineer (the field engineer) did a walkthrough of the app on 2026-07-15 and left **24 notes**. Those
notes were split across three releases:

- **v0.1.11** — already built and released. Roughly 11 of the smaller items (menu naming, version
  pill, logo fix, snap-to-top, re-import same filename, run-controls-at-bottom, reports visible in
  the Reports tab, import-rejection reasons, and more).
- **v0.1.12** — the **BACnet** fix only (empty scan with no reason → find devices behind a BBMD via
  foreign-device registration, and explain every empty/failed scan). Its exe is being built now.
- **v0.1.13 — this release.** Everything from field engineer's list that was **still not done** after v0.1.11
  and v0.1.12. Twelve items implemented, two deliberately deferred.

An audit ran first, checking every one of field engineer's asks against the actual code on `main` + the
v0.1.12 branch, so v0.1.13 only rebuilds what genuinely was not there. The twelve items below are
the ones the audit found still outstanding.

---

## 2. Status — read this before you trust anything

- **Branch:** `fix/v0.1.13-remaining-punchlist`, **stacked on top of** `fix/v0.1.12-bacnet-foreign-device`
  (NOT on `main`). It currently contains the v0.1.12 BACnet work underneath it.
- **Frontend gates: GREEN.** The orchestrator ran lint, typecheck, the full vitest suite
  (**297 tests**), and `vite build` — all pass.
- **Backend (Python): NOT executed.** Every touched `.py` file was checked for syntax/lint with a
  ruff-in-WASM pass (clean), but **no Python actually ran**. This machine is under ThreatLocker,
  which blocks the local Python interpreter from even reading repo `.py` files, and there is no
  BACnet or MQTT broker hardware in CI. So **all backend behaviour — report generation, config
  service, logging endpoints, MQTT engine, IP engine, UDMI processor — is unverified until a PR runs
  it in CI.** Treat every backend claim in section 3 as "written and syntax-clean," not "proven."
- **Commits on the branch:** **4 feature commits + 1 docs commit.** Check the branch log for the
  exact hashes before you open the PR.

---

## 3. The 12 items implemented (each mapped to field engineer's exact words)

### Reports

1. **Electracom headers/footers on reports** — field engineer: *"Ensure reports are generated within
   Electracom headers and footers… it will form part of ITP and witnessing documentation."*
   Added an ELECTRACOM **text** wordmark header + footer band to all three page-formatted report
   types (PDF, DOCX, XLSX), with the doc title, page numbers, and run id. This is **phase 1 (text
   only)** — embedding the actual logo image is deferred to a later phase (the hand-rolled PDF
   writer has no image support yet).

2. **Per-silo report content** — field engineer: *"Reporting needs to be available per test silos."* The
   per-silo *button* already shipped in v0.1.11; what was missing was head-specific *content*. Added
   a "Discovery inventory" block to all four report builders: discovered IP hosts, discovered BACnet
   devices/points + expected-but-silent devices, and discovered MQTT topics. Reports are
   byte-reproducible; empty heads render an honest empty note rather than being omitted.

### UDMI

3. **Don't fail the whole run on one silent device** — field engineer: *"cant bin the whole validation if one
   or more devices are not publishing. It should proceed to the results showing red as offline."*
   A completed capture window with a silent/partial device now ends **succeeded** (a distinct stage,
   "complete with silent devices") instead of failing the whole run. Real transport failures
   (broker unreachable, TLS, auth, timeout) still fail — deliberately. Because the run now reaches
   "succeeded," the existing auto-advance + snap-to-top starts working for these runs with no UI
   change.

4. **RAG on the results page** — field engineer: *"Green – online, publishing and UDMI compliant / Amber –
   online, publishing but not UDMI compliant / RED – offline and unable to check payloads."*
   Added the missing **amber** state and re-mapped the colours to exactly field engineer's scheme: offline/not
   publishing → **red**, publishing-but-non-compliant → **amber**, compliant → **green**. Honesty
   guard: a device that actually returned a payload can never be painted "offline." Pairs with item 3
   at release time (silent devices must read red on a succeeded run).

5. **Nonpub schema template** — field engineer: *"generate nonpub schema template to be added to the
   validation page."* Added a public `GET /udmi/schemas/template` endpoint returning a byte-stable
   zip of the vendored UDMI 1.5.2 schema set (+ README + LICENSE) and a **Download** button in the
   Non-Published UDMI Schema Sets section. The template is real upstream data, not fabricated.

### MQTT

6. **Compare broker scan against the template** — field engineer: *"currently… seems to ignore the uploaded
   template… keep the whole broker discovery and compare the results against the template. Turning
   rows green if it matches… red if it's a foreign / unmatched device."* Added a **Register Match**
   column: green = topic matches an imported register row, red = observed on the broker but not in
   the register. Verdicts are computed at read-time (retroactive, no re-run). Honest gating: only a
   succeeded non-dry run with a register imported is compared; no register → no row colours (never
   an all-red table).

7. **UDMI-style long timer** — field engineer: *"needs the same timer we have in UDMI to allow us to run the
   discovery over a period of hours, days, weeks."* Added a duration-unit dropdown (like UDMI),
   raised the worker ceiling to ~49h and the API guard to 48h, and made long captures
   memory-safe by retaining only the latest payload per topic (counts stay honest). Honest caveat
   surfaced in the UI: in the portable/inline exe an indefinite capture is bounded, and a hosted
   worker is needed for true multi-day runs.

8. **MQTT inspector (was dead)** — field engineer: *"inspector at bottom… currently does nothing… should dig
   into the payload… can we see if its retained, when it was published, QoS level."* Rows are now
   selectable and the inspector shows the real payload (with a JSON tree), plus **Retained**,
   **Delivery QoS**, and **Received at**. Honesty caveats baked into the wording: "received at" is
   our clock (MQTT 3.1.1 has no wire publish timestamp), retained=true means a broker replay, and
   delivery QoS is capped by our own subscription QoS — see the QoS decision in section 5.

### IP

9. **Show non-responders** — field engineer: *"IP scan silently drops unresponsive IP Addresses… needs to show
   all IP entries in same display format as the UDMI tests."* The engine now emits a row for **every
   scanned host**, including silent ones ("no response on scanned ports"), and the table gains a
   **Result** column with pass/amber/fail tones. Honesty guard: a TCP-connect miss is never labelled
   "offline"/red — a register-expected silent host is amber ("expected but did not answer,
   inconclusive"), an unregistered silent host is neutral.

### Config

10. **Certs pill wrong after upload** — field engineer: *"Certs and keys… hero showing not configured after
    uploading keys."* The certificates status pill is now **derived server-side from what's actually
    stored on disk** instead of a static seeded "Not configured" string: green when material is
    stored (with expiry), red when expired, amber when a file is missing and needs re-upload.

11. **Logging local-or-URL** — field engineer: *"ability to log locally to machine, or to upload to URL."*
    Added a real local rotating file log (`RUNTIME_ROOT/logs/app.log`) plus an engineer-gated
    "Upload logs now" to a configured URL, with secrets masked in the uploaded bundle and a
    write-only credential field. The old fictional syslog fields were removed from the config screen.
    See the log-upload wire-contract decision in section 5.

12. **Purge stale placeholder / Docker-era content** — field engineer: *"Check documents available at top of
    page are inline with the changes made."* Removed the fake "Block B Plantroom" site pill, the
    Docker install path and its false "repository is private" claim from the Learning page, the
    sample "Current Stage" dashboard board, and fabricated config defaults (e.g. "Last Backup Status:
    Success" → "Never run"). A migration stops existing installs from still showing the old fake
    values.

---

## 4. The 2 DEFERRED items (and why) — plus the one that shipped

**Deferred to v0.1.14:**

- **General nmap-style discovery pane** — field engineer: *"last section doesn't bring much… better this is
  turned into a general nmap discovery section."* Deferred because the **substance of the feature is
  a curated port list that only field engineer can supply** (top-20 / top-100), and building the pane before
  that answer risks shipping exactly the kind of disappointing section it's meant to replace. It also
  rewrites the same `ip_scan.py` host loop that item 9 (show non-responders) just rewrote — doing
  that twice back-to-back while the dated BACnet release sits stacked underneath is avoidable churn.
  (Note: nmap itself can never ship on this machine — ThreatLocker blocks unsigned exes — so this
  will always be the app's own engine with common-port profiles, not real nmap.)

- **Look-and-feel component extraction (Part B)** — field engineer: *"Look and feel of each testing silo to be
  the same… use the UDMI page."* The heavy part — extracting shared ResultsTable/Inspector components
  out of the ~3600-line ModulePage — was deferred because it **moves zero pixels** (pure code motion)
  and landing a ~430-line refactor under six other feature commits in the same file maximises rebase
  pain for no visible gain.

**But the part field engineer actually feels DID ship:** the **scroll-unbind** (look-and-feel Part A). The
tables no longer trap content inside a fixed-width scroll box — that was field engineer's concrete complaint,
and it's a CSS-only change that landed in this release.

---

## 5. Open decisions for field engineer (each shipped with a sensible default — one line to change)

Everything below already works with a default; these are field engineer's calls, not blockers.

- **RAG "pass with notes" → amber.** Default shipped: a minor-only "pass with notes" device shows
  **amber**, and a publishing device with critical issues is **amber** (red is reserved for
  offline/not-publishing). If field engineer wants minor-only to stay green, it's a one-line flip in
  `udmiVerdictTone`.
- **MQTT subscribe QoS.** Default shipped: subscription QoS stays **0**, so the new "Delivery QoS"
  column will almost always read 0. Raising it to QoS 2 would make the field meaningful but changes
  broker traffic per message — deliberately **not** changed silently; field engineer decides.
- **MQTT wildcard semantics.** Default shipped: a register row like `demo-site/b1/#` greens **every**
  topic under that prefix (including a rogue publishing inside the namespace), with the matched
  wildcard shown per row. Alternative is an amber "covered by wildcard, not individually listed."
  Also open: are `$SYS/#` broker-internal topics red "not in register" (default) or excluded?
- **Report sign-offs.** Default shipped: A4 paper, "ELECTRACOM" text wordmark + run-id footer, no
  doc/revision-number field. field engineer confirms whether that's ITP-sufficient (adding fields later
  changes the report bytes). Also: do the potentially-thousands-of-rows BACnet points go in
  PDF/DOCX or xlsx/zip-only.
- **Log-upload wire contract.** Default shipped: a **multipart POST (field `file`) + Bearer token**
  — an implemented guess. If field engineer's web endpoint expects something else, it's a one-function change.
  Also flag: removing the old "Remote Syslog Target"/"Syslog Port" fields deletes any collector
  address someone had typed in (a changelog note is the only preservation).

---

## 6. Pickup steps (in order)

1. **Wait for v0.1.12 to merge to `main`** (its exe is being built now). Don't start until it's in.
2. **Rebase this branch onto `main`.** It currently stacks on `fix/v0.1.12-bacnet-foreign-device`;
   once v0.1.12 is in `main`, rebase `fix/v0.1.13-remaining-punchlist` onto `main` so its history is
   just the 4 feature + 1 docs commits on top of released code.
3. **Open a PR.** This is the first time the backend Python actually runs. **Expect CI to surface
   things** — the whole backend is unverified (section 2). Fix forward on the branch.
4. **Only after CI is green**, decide the release + exe build for v0.1.13.
5. When you next touch the older `docs/handoff-2026-07-15-field-walkthrough.md`, correct a few stale
   claims the audit found: the IP "target override already does register-less scans" line is wrong
   (all register resolvers still run); "results full-width with inspector below" was already shipped;
   the MQTT-inspector estimate was an engine change, not just UI wiring; the per-silo report
   *button* already shipped in v0.1.11.

---

## 7. Honesty caveats that MUST reach the operator (field engineer)

These are behaviours that are correct but surprising. Put them in the runbook / release notes.

- **Saved config snapshots don't auto-adopt new default VALUES.** If field engineer has a saved configuration,
  changing a default in code does **not** rewrite his stored value. Fabricated *statuses*
  self-heal (the migration fixes them), but any real *value* field engineer needs (e.g. the BACnet Foreign
  Device toggle from v0.1.12) must be set by hand on his machine. Same principle applies wherever
  defaults changed this release.
- **Historical FAILED UDMI runs stay FAILED.** The "succeed with silent devices" fix (item 3) only
  affects **new** runs. Runs that already failed under the old rule remain failed — field engineer must re-run
  to get the new succeeded-with-silent behaviour and the RAG colours.
- **Re-download old reports after upgrading.** Report branding + inventory (items 1 and 2) changed
  the report **bytes**. Any report generated before this upgrade won't match the new format and
  won't have the Electracom furniture. Reports field engineer wants in the ITP pack should be re-generated on
  the new build.

---

*End of handoff. Source material (all outside the repo): the 26-item audit, the 12-item v0.1.13
implementation results, the v0.1.12 BACnet results, `scratchpad/v0113/conflict-map.json`, and the
per-plan JSON files under `scratchpad/v0113/`.*
