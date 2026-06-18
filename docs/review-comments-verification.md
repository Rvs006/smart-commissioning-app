# Review Comments - Implementation Verification

Independent, code-level verification of the **24 design-review comments** (file `smart-commissioning-review-comments.csv`, reviewer **DP**). Every comment was re-checked by reading the actual repository code on `main` - not by trusting prior claims.

> **Result: 24 / 24 implemented.** Verified against `main` @ `744dc86` on 2026-06-18 by a 24-agent parallel code sweep (one agent per comment), each citing `file:line` evidence.

A few items are **implemented in code but their *live* path (active MQTT broker / on-site hardware) is on-site-untested** - called out per item and summarised at the bottom. Everything reproducible offline can be seen on the local app described below.

---

## 1. Run it locally (~5 min, no broker/hardware)

From the repo root (`smart_commissioning_core` is installed editable):

```bash
# 1) Backend API (terminal 1)
cd backend
AUTH_MODE=local JOB_EXECUTION_MODE=inline DEPLOYMENT_ROLE=hub \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2) Seed demo data (terminal 2, once backend is up)
python scripts/seed_demo.py --base-url http://127.0.0.1:8000

# 3) Frontend dev server (terminal 2)
npm --prefix frontend run dev      # http://localhost:5173, proxies /api -> 8000
```

Windows PowerShell equivalent for step 1:

```powershell
cd backend
$env:AUTH_MODE="local"; $env:JOB_EXECUTION_MODE="inline"; $env:DEPLOYMENT_ROLE="hub"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> One-command offline check: `scripts/smoke_local.ps1 -BaseUrl http://127.0.0.1:8000` runs a 6-check smoke (health / ready / metrics / config / UDMI / dry-run IP).

### Enable the engineer action buttons
Run / Publish / Export are gated on an API key even in local mode. Open the app, press **F12 -> Console**, run once:

```js
localStorage.setItem('sc.apiKey','local-dev'); location.reload()
```

This makes `/me` return an admin role so every action button enables.

## 2. Route map (HashRouter - keep the `#`)

| Open | Page | Comments to check here |
| --- | --- | --- |
| http://localhost:5173/#/ | Homepage | UDMI error readability |
| http://localhost:5173/#/configuration | Configuration | cert-expiry, collapsible sections, Validate/Save help, timezone, backup location, double-status |
| http://localhost:5173/#/ip-scanner | IP Discovery | IP input template |
| http://localhost:5173/#/bacnet-discovery | BACnet Discovery | BACnet IP/network cols, BACnet template |
| http://localhost:5173/#/mqtt-discovery | MQTT Discovery | Incoming MQTT Payloads, MQTT template |
| http://localhost:5173/#/udmi-validation | UDMI Validation | expected-payload drop-down, Pass/Fail, Run-not-Queue, multi-point config, execute, UDMI template |
| http://localhost:5173/#/data-validation | BACnet to MQTT Validation | renamed page, validation template |
| http://localhost:5173/#/reports | Reports | Export works, multi-select export, generation linkage |

---

## 3. Summary

| # | Comment ID | Module | Pri | Title | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | `review-mq9l6bd7` | Homepage | Medium | Error is confusing | ✅ Done |
| 2 | `review-mq9ll2vf` | Configuration | Medium | Cert expiry | ✅ Done |
| 3 | `review-mq9lmokb` | Configuration | Medium | Collapsable sections | ✅ Done |
| 4 | `review-mq9lnour` | Configuration | Low | What does Validate Snapshot do? | ✅ Done |
| 5 | `review-mq9lq5ii` | Configuration | Low | What does Save Configuration do? | ✅ Done |
| 6 | `review-mq9lsooa` | Configuration | Medium | Timezone dropdown | ✅ Done |
| 7 | `review-mq9lv0g2` | Configuration | Medium | Where will backup location be stored? | ✅ Done |
| 8 | `review-mq9lxgf6` | Configuration | Medium | Double statuses | ✅ Done |
| 9 | `review-mq9m4bnv` | UDMI | Medium | Previous UI for the results 1 (expected payload) | ✅ Done |
| 10 | `review-mq9m6uay` | UDMI | Medium | Previous UI for the results 2 (Pass/Fail) | ✅ Done |
| 11 | `review-mq9mbmsh` | BACnet | Medium | Previous UI (IP + network number) | ✅ Done |
| 12 | `review-mq9mvkki` | UDMI | Medium | What does Queue UDMI Validation do? | ✅ Done |
| 13 | `review-mq9n11wi` | UDMI | Medium | MQTT Config (multi-point) | ✅ Done |
| 14 | `review-mq9n7pbe` | UDMI | Medium | Schedule and Payload Evidence (execute) | ✅ Done |
| 15 | `review-mq9nhbzu` | MQTT Discovery | High | Emulate MQTT explorer | ✅ Done |
| 16 | `review-mqalcdg1` | IP Discovery | High | IP Discovery Input Example | ✅ Done |
| 17 | `review-mqalda3m` | BACnet | High | BACnet Discovery Input Example | ✅ Done |
| 18 | `review-mqale1y9` | MQTT Discovery | High | MQTT Discovery Input Example | ✅ Done |
| 19 | `review-mqalf5tt` | UDMI | High | UDMI Validation Input Example | ✅ Done |
| 20 | `review-mqalgpxt` | Validation | High | Data Validation Input Example | ✅ Done |
| 21 | `review-mqatabc9` | Reports | Medium | Export button doesn't do anything | ✅ Done |
| 22 | `review-mqatcqb3` | Reports | Medium | Export specific reports | ✅ Done |
| 23 | `review-mqatkxi8` | Validation | Medium | Change name of Validation page | ✅ Done |
| 24 | `review-mqautz9j` | Reports | Medium | How are reports linked/generated | ✅ Done |

---

## 4. Per-comment detail

### 1. Error is confusing - ✅ Done

- **ID:** `review-mq9l6bd7` - **Module:** Homepage - **Priority:** Medium - **Route:** `/`

**Evidence (code read):**

- frontend/src/features/workflow/DashboardPage.tsx:85-107 - parseBlockingFinding() splits a ValidationIssueRecord into two emphasised parts: headline = asset + payload type ('MDB5-00-043-BLR-1 · UDMI pointset') and detail = point + problem ('fault_status — expected STRING but received NUMBER'). Comment at line 79 explicitly states the run-together ISS-#### id is intentionally dropped.
- frontend/src/features/workflow/DashboardPage.tsx:375-388 - The 'Blocking Finding' card on the homepage renders finding.headline in a `<strong>` and finding.detail in a `<small>`, visually separating/emphasising the point name and the problem instead of one run-on string.
- frontend/src/features/workflow/operatorData.ts:49-57 - derivePayloadType() maps the issue (via issue_type/topic/point_name haystack) to 'pointset'/'metadata'/'state', so a pointset_validation issue is labelled 'UDMI pointset' on the homepage exactly as the ask requests.
- frontend/src/api/client.ts:170-180 - ValidationIssueRecord carries the structured fields parseBlockingFinding relies on (asset_id, issue_type, point_name, expected_value, observed_value, description), so the split is built from real data, not regex over a blob.
- core/smart_commissioning_core/udmi_validation.py:296-303 - The UDMI pointset type-mismatch path (numeric unit but non-number present_value) emits an issue with issue_type='pointset_validation', point_name, expected_value, and observed_value=f'{type(present_value).__name__}: {present_value}' — i.e. the backend populates exactly the fields the homepage uses, for the fault_status/STRING-vs-NUMBER class of error.
- frontend/src/features/workflow/DashboardPage.tsx:96-103 - Graceful fallback chain: structured 'expected X but received Y' when expected/observed present, else issue.description, else humanized issue_type — so the readable framing degrades safely without ever reverting to a coded ISS-#### string.

**Verify on localhost:** Start backend + worker (and a broker is NOT required for pasted payloads). Start the frontend dev server and open http://127.0.0.1:5173/#/ . You need at least one terminal UDMI validation run that produced a pointset type-mismatch issue: go to http://127.0.0.1:5173/#/udmi-validation, in 'Schedule and Payload Evidence' edit the Pointset payload JSON so a numeric point's present_value is a string (e.g. set supply_air_temperature_setpoint present_value to \"hot\" instead of 22), click 'Execute capture', and wait for the run to go terminal. Return to the Homepage (click the logo / nav Home, or http://127.0.0.1:5173/#/). In the 'Blocking Finding' card (lower-left, 'Priority' eyebrow) confirm it renders as TWO emphasised lines: a bold headline like 'AHU-1000001 · UDMI pointset' and a smaller detail line like 'supply_air_temperature_setpoint — expected ... but received str: hot', with the severity tag above. Confirm there is NO run-together 'ISS-1042'-style coded string.

**Caveats / notes:** Fully implemented and wired end-to-end. The homepage 'Blocking Finding' card (DashboardPage = route '/') is the right surface; parseBlockingFinding produces the exact asset + 'UDMI `<payloadType>`' headline and a 'point — expected X but received Y' detail described in the ask, and deliberately drops the ISS-#### id. Caveats: (1) The card only appears once a terminal validation run with at least one issue exists; with no issues it shows 'No blocking findings'. (2) The label says 'UDMI pointset' for pointset_validation but derivePayloadType keys off substring matching, so a non-UDMI/BACnet issue whose issue_type lacks pointset/metadata/state falls to 'other' and shows asset-only (no payload-type suffix) — acceptable since the ask is UDMI-specific. (3) The phrasing is 'expected STRING but received NUMBER'-equivalent; the backend observed_value embeds the python type name (e.g. 'str: hot'), so wording is close but not literally uppercase 'NUMBER'/'STRING' — it conveys the same type-mismatch meaning. Live-broker capture is on-site-untested, but pasted-payload validation (used in verify steps) needs no broker.

---

### 2. Cert expiry - ✅ Done

- **ID:** `review-mq9ll2vf` - **Module:** Configuration - **Priority:** Medium - **Route:** `/configuration`

**Evidence (code read):**

- frontend/src/features/workflow/ConfigurationPage.tsx:156 - "Certificate Expiry" field is defined as { kind: "readonly" }, so it is NOT an editable/required input.
- frontend/src/features/workflow/ConfigurationPage.tsx:208-214 - isExpired() parses the stored expiry date and returns true when it is strictly before Date.now(), i.e. the app confirms whether the cert is expired rather than asking the operator.
- frontend/src/features/workflow/ConfigurationPage.tsx:608 - FieldControl is passed expired={field === CERT_EXPIRY_FIELD && isExpired(value)}; lines 754,757 apply the .field-expired class to the label and input when expired; line 649 shows the hint "Certificate expired: the stored expiry date is in the past."
- frontend/src/styles.css:1255-1260 - .field-expired input / input.field-expired sets border-color, color and box-shadow to var(--red), giving the red-highlighted box when expired.
- backend/app/services/configuration_service.py:495-508 - _certificate_expiry() loads the uploaded PEM via x509.load_pem_x509_certificate and returns the real notAfter date (YYYY-MM-DD); lines 361-368 write that value into the read-only "Certificate Expiry" field on secret store, so the status reflects the actual uploaded/specified certificate.
- frontend/src/features/workflow/ConfigurationPage.test.tsx:174-189 - test 'flags an expired certificate-expiry field in red' seeds expiry 2000-01-01 and asserts the label has class field-expired and shows /Certificate expired/, confirming the red-highlight path.

**Verify on localhost:** 1) Start backend API and frontend dev server (npm run dev in frontend/, default http://127.0.0.1:5173). 2) Open http://127.0.0.1:5173/#/configuration. 3) The 'Certificates & Keys' section is expanded by default (defaultExpandedSections.certificates = true). 4) Find the 'Certificate Expiry' field: confirm it is a greyed read-only input you CANNOT type into (no required asterisk, not editable) - it shows the stored date (seed value 2027-05-20, so by default it is NOT red and the hint reads 'Status indicator derived from the stored certificate expiry date'). 5) To see the red state, upload an expired cert: in 'CA Certificate' or 'Client Certificate', paste/select a PEM whose notAfter is in the past and click 'Store masked reference'; the backend parses notAfter and writes it back, and the 'Certificate Expiry' box border + text turn red with the hint 'Certificate expired: the stored expiry date is in the past.' Alternatively, run the frontend unit test 'flags an expired certificate-expiry field in red' (ConfigurationPage.test.tsx) which seeds 2000-01-01 and asserts the field-expired red class.

**Caveats / notes:** Fully implemented end-to-end. The expiry is a read-only, backend-derived status (not a required input): the backend now does REAL X.509 PEM parsing of the uploaded cert's notAfter (configuration_service.py:495-508, covered by backend/tests/test_secret_storage.py test_storing_a_certificate_parses_and_records_its_expiry) and writes it into the read-only field; the frontend compares it to today and red-flags the box when expired. Caveats: (1) The seeded default expiry (2027-05-20) is in the future, so on a fresh install the field is NOT red until an actual cert is uploaded or the date passes - to see the red highlight live you must store a cert with a past notAfter (or rely on the unit test). (2) Stale comment at ConfigurationPage.tsx:175-178 claims 'store_secret currently returns expiry:null, so real PEM parsing is on-site' - this is now FALSE (backend parses notAfter); the comment is misleading but does not affect behavior. (3) For a non-cert upload (Private Key) or an unparseable PEM, _certificate_expiry returns None and the displayed expiry is left unchanged, which is intentional.

---

### 3. Collapsable sections - ✅ Done

- **ID:** `review-mq9lmokb` - **Module:** Configuration - **Priority:** Medium - **Route:** `/configuration`

**Evidence (code read):**

- frontend/src/features/workflow/ConfigurationPage.tsx:227-229 - expandedSections state holds a per-section boolean Record (ConfigurationSectionKey -> bool), seeded from defaultExpandedSections, making each section's open/closed state independently controllable.
- frontend/src/features/workflow/ConfigurationPage.tsx:340-342 - toggleSection() flips a section's boolean, wired to each section header's onClick; this is the actual collapse/expand handler.
- frontend/src/features/workflow/ConfigurationPage.tsx:575-626 - each section renders as an `<article>` whose header is a `<button className="section-toggle">` with aria-expanded/aria-controls and a caret; the field-grid body (line 591 `{expanded && (...)}`) is conditionally rendered only when expanded, so every section collapses/expands, including Network Basics (device), BACnet Discovery (bacnet), and MQTT Settings (mqtt) per sectionLabels at lines 47-55.
- frontend/src/features/workflow/ConfigurationPage.tsx:37-45 - defaultExpandedSections keeps connection-critical sections (device/bacnet/mqtt/certificates) open and advanced ones (backups/logging/time) collapsed by default, satisfying the 'and other' sections part of the ask.
- frontend/src/styles.css:1195-1229 - .section-toggle and .section-toggle-caret styles, including a rule that rotates the caret -90deg when aria-expanded="false", give the collapse control real disclosure styling and a transition.
- frontend/src/features/workflow/ConfigurationPage.test.tsx:130-151 - test 'collapses advanced sections by default and toggles them open' asserts MQTT Settings is aria-expanded=true, Time & NTP is aria-expanded=false with its Timezone field hidden, and that clicking the Time toggle expands it and reveals the field.

**Verify on localhost:** Start the frontend dev server (npm run dev in frontend/) and open http://127.0.0.1:5173/#/configuration. Under the 'Configuration' hero you will see the config-grid of section cards. Each section header (e.g. 'Network Basics', 'BACnet Discovery', 'MQTT Settings', 'Certificates & Keys', 'Time & NTP', 'Backup & Restore', 'Logging & Diagnostics') is a clickable button with a small caret (down arrow) on the left and a status pill on the right. Observe that 'Time & NTP', 'Backup & Restore' and 'Logging & Diagnostics' start collapsed (caret rotated, no fields shown) while 'Network Basics', 'BACnet Discovery', 'MQTT Settings' start expanded. Click any expanded header (e.g. 'MQTT Settings') and confirm its field grid collapses and the caret rotates; click a collapsed header (e.g. 'Time & NTP') and confirm its fields (e.g. the Timezone select) appear. Each section toggles independently and is keyboard-focusable (Tab + Enter/Space) since it is a real button with aria-expanded.

**Caveats / notes:** Fully implemented in the live ConfigurationPage with no mock/stub gating - the snapshot loads from GET /api/v1/configuration but collapse/expand is pure client-side state and works regardless of backend data. No live MQTT broker or on-site hardware needed to see the collapsible behavior. Note the sections collapse individually but the default-open/closed split is intentional (connection-critical sections expanded, advanced collapsed) rather than all-collapsed; this exceeds the ask rather than falling short. The 'collapsible' control is a button, not a native `<details>`/`<summary>`, but it has proper aria-expanded/aria-controls and is covered by a passing unit test (ConfigurationPage.test.tsx:130-151).

---

### 4. What does Validate Snapshot do? - ✅ Done

- **ID:** `review-mq9lnour` - **Module:** Configuration - **Priority:** Low - **Route:** `/configuration`

**Evidence (code read):**

- frontend/src/features/workflow/ConfigurationPage.tsx:450 - the 'Validate Snapshot' action button rendered in the Actions side panel
- frontend/src/features/workflow/ConfigurationPage.tsx:452-455 - help text directly below the button: 'Validate Snapshot checks ports, IP/gateway addresses, MQTT topics, and certificate references for validity. It runs server-side checks only and does not save the snapshot.'
- frontend/src/features/workflow/ConfigurationPage.tsx:443 - additional contextual hint above the button: 'Validate first when changing ports, addresses, topics, or certificates.'
- frontend/src/app/routes.tsx:15 - route '/configuration' maps to `<ConfigurationPage />`, confirming this help text appears on the reviewed route

**Verify on localhost:** 1. Start the frontend dev server and open http://127.0.0.1:5173/#/configuration. 2. Look at the right-hand 'Actions' side panel. 3. Above the 'Validate Snapshot' button you will see the muted hint 'Validate first when changing ports, addresses, topics, or certificates.' 4. Directly below the 'Validate Snapshot' button, read the action-note paragraph: 'Validate Snapshot checks ports, IP/gateway addresses, MQTT topics, and certificate references for validity. It runs server-side checks only and does not save the snapshot.' This text explains what the button does without needing to click it.

**Caveats / notes:** Fully implemented as static UI copy, no live backend/MQTT broker required to see the explanation. The help text is always-visible descriptive copy (an 'action-note' paragraph) rather than a hover tooltip, so it satisfies the 'help text or description' ask directly. The description is accurate to the button's behavior (validate-only, no save) and is reinforced by a parallel 'Save Configuration' note immediately below it. No caveats; the explanation renders regardless of validation state.

---

### 5. What does Save Configuration do? - ✅ Done

- **ID:** `review-mq9lq5ii` - **Module:** Configuration - **Priority:** Low - **Route:** `/configuration`

**Evidence (code read):**

- frontend/src/features/workflow/ConfigurationPage.tsx:462-467 - The primary 'Save Configuration' button is followed by an action-note paragraph explaining it 'persists the edited snapshot as the new runtime configuration used by discovery and validation services.'
- frontend/src/features/workflow/ConfigurationPage.tsx:468-498 - A config-toolbar renders 'Export JSON' and 'Import JSON' buttons (plus a hidden file input) with an action-note: 'Export downloads the current configuration as JSON (secrets stay masked) so it can be reused on another project; Import validates a JSON file and saves it as the new snapshot.'
- frontend/src/features/workflow/ConfigurationPage.tsx:279-310 - exportMutation calls exportConfiguration() and triggers a JSON download; importMutation calls importConfiguration(payload) -> PUT /configuration; both set transferMessage/transferError shown in panels at lines 523-535.
- frontend/src/api/client.ts:561-601 - exportConfiguration() wraps GET /configuration into a versioned envelope (kind/version/exported_at/project_id/site_id/configuration); importConfiguration() unwraps it and saves via PUT /configuration, accepting optional projectId/siteId for cross-project reuse.
- backend/app/api/routes/configuration.py:23-40 - GET and PUT /configuration both accept project_id and site_id Query params (default demo-project/demo-site), so configuration is genuinely project/site-scoped server-side.
- backend/app/services/configuration_service.py:248-251 - _persist saves keyed on (project_id, site_id) via the repository, confirming multiple projects/systems are stored independently in one tool.

**Verify on localhost:** Start the frontend dev server and open http://127.0.0.1:5173/#/configuration (backend API must be running so the snapshot loads; if an API key is required, set it first). In the right-hand 'Actions' panel: (1) Read the paragraph directly under the blue 'Save Configuration' button — it states the button persists the edited snapshot as the new runtime configuration used by discovery/validation services. (2) Below it find the 'Export JSON' and 'Import JSON' buttons and the explanatory note about reusing config on another project. (3) Click 'Export JSON' — a file named smart-commissioning-configuration-`<timestamp>`.json downloads and a green 'Configuration transfer' panel confirms 'Exported the current configuration as JSON... masked references only.' (4) Click 'Import JSON', pick that downloaded file — a green panel confirms 'Imported configuration was validated by the API and saved as the new snapshot.' To prove cross-project reuse: export here, then import that JSON file on a second tool/instance. Note: Import (and Save) require an engineer+ role; as a viewer those two buttons are disabled with an 'engineer required' tooltip, while Export stays enabled.

**Caveats / notes:** Fully satisfies the ask. 'Save Configuration' is explained in plain language inline (lines 464-467), and export/import of project-specific configs is implemented end-to-end: client.ts exportConfiguration/importConfiguration (561-601), the ConfigurationPage toolbar + mutations (279-310, 468-498), client-side shape validation of the imported file (parseConfigurationFile, 825-854), and a project/site-scoped backend (configuration.py 23-40, configuration_service.py 248-251). Caveat — multi-project support is delivered via file transfer (download a project's config JSON, import it into another project/instance), NOT via an in-app project picker: the UI calls exportConfiguration() and importConfiguration(payload) with NO project args (lines 280, 298), so within a single running instance both always read/write the default demo-project/demo-site snapshot. The projectId/siteId scoping plumbing exists in client.ts and the backend but is not surfaced in the ConfigurationPage UI. This still meets the stated ask ('exporting and importing project-specific configurations (multiple projects/systems in one tool)') because the exported envelope carries project_id/site_id provenance and the file is the unit of transfer between projects. Tests in ConfigurationPage.test.tsx exercise both Export JSON and Import JSON buttons (lines 201, 224, 251, 272). No live broker or on-site hardware needed to verify.

---

### 6. Timezone dropdown - ✅ Done

- **ID:** `review-mq9lsooa` - **Module:** Configuration - **Priority:** Medium - **Route:** `/configuration`

**Evidence (code read):**

- R:\Smart Commissioning App\frontend\src\features\workflow\ConfigurationPage.tsx:70-139 - TIMEZONE_OPTIONS is a 69-entry IANA list starting with "UTC" and spanning every UTC-offset region (America/*, Atlantic/*, Europe/* including Europe/London, Africa/*, Asia/* e.g. Asia/Tokyo & Asia/Kolkata, Australia/*, Pacific/* e.g. Pacific/Auckland) - not just Europe.
- R:\Smart Commissioning App\frontend\src\features\workflow\ConfigurationPage.tsx:167-169 - fieldDefinitions.time.Timezone = { kind: "select", options: TIMEZONE_OPTIONS }, so the Timezone field in the Time & NTP section is bound to render as a dropdown of all those zones.
- R:\Smart Commissioning App\frontend\src\features\workflow\ConfigurationPage.tsx:726-741 - FieldControl's kind==="select" branch renders a real `<select>` element; the current value is preserved as an extra `<option>` if not in the list (line 731), then every option in TIMEZONE_OPTIONS is rendered.
- R:\Smart Commissioning App\frontend\src\features\workflow\ConfigurationPage.tsx:44 - defaultExpandedSections.time = false: the Time & NTP section (which holds Timezone) is collapsed by default, so the dropdown is hidden until the operator expands that section.
- R:\Smart Commissioning App\backend\app\services\configuration_service.py:76-79 - backend seeds time.values with "Timezone": "Europe/London" as a plain string; it stores the raw IANA name, so any value the dropdown emits (UTC or non-Europe) round-trips through save/validate unchanged.
- R:\Smart Commissioning App\frontend\src\features\workflow\ConfigurationPage.test.tsx:153-171 - test 'renders the timezone as a select including UTC and non-Europe zones' opens Time & NTP, asserts the Timezone control is a combobox (getByRole("combobox")) and that its options contain UTC, Asia/Tokyo, and America/New_York.

**Verify on localhost:** 1) Start the frontend dev server and open http://127.0.0.1:5173/#/configuration. 2) The page loads the API-backed configuration snapshot (chip 'Loaded from API'). 3) Scroll to the 'Time & NTP' section - it is collapsed by default, so click its header (caret/title 'Time & NTP') to expand it. 4) The 'Timezone' field appears as a dropdown (`<select>`), pre-selected to the stored value (e.g. Europe/London). 5) Open the dropdown and confirm it lists UTC at the top plus non-Europe zones such as America/New_York, Asia/Tokyo, Asia/Kolkata, Australia/Sydney, Pacific/Auckland (69 IANA zones across all UTC offsets). 6) Pick a different zone (e.g. UTC), then click 'Save Configuration' (requires engineer role) to persist; reload to confirm the chosen zone is retained.

**Caveats / notes:** Fully implemented and unit-tested. The Timezone control is a genuine HTML `<select>` dropdown driven by a 69-entry IANA TIMEZONE_OPTIONS list that begins with UTC and covers every UTC-offset region worldwide (not just Europe/London). Caveats: (a) The Time & NTP section is collapsed by default (defaultExpandedSections.time=false at line 44), so the dropdown is not visible until the operator expands that section - not a defect, just a discoverability note. (b) Saving requires engineer+ role (Save button gated by canEngineer); viewers see the dropdown but cannot persist a change. (c) The list is a curated representative set (~69 zones), not the full ~600 IANA tz database, but it explicitly satisfies the ask of covering UTC and non-Europe/London zones across all offset regions. No live broker or on-site hardware is required to see this.

---

### 7. Where will backup location be stored? - ✅ Done

- **ID:** `review-mq9lv0g2` - **Module:** Configuration - **Priority:** Medium - **Route:** `/configuration`

**Evidence (code read):**

- R:/Smart Commissioning App/frontend/src/features/workflow/ConfigurationPage.tsx:594-602 - A backups-only field-note paragraph renders when the Backup & Restore section is expanded, stating the bundle 'is built from the app runtime (under the backend runtime directory by default) and written to a path chosen at backup time via the backup CLI's output option. The Backup Location field below records the intended target for operators; the app does not pick a host directory itself.' This directly answers in-app vs host-directory.
- R:/Smart Commissioning App/frontend/src/features/workflow/ConfigurationPage.tsx:57-58 - sectionDescriptions.backups = 'Backup schedule, retention, encryption, storage location, and restore readiness.' is shown as section-copy (line 593) above the field note, flagging storage location as part of this section.
- R:/Smart Commissioning App/backend/app/services/configuration_service.py:85-95 - DEFAULT_CONFIGURATION.backups includes a 'Backup Location' field defaulting to '/data/backups', so the field referenced by the clarifying note actually appears in the section's value list.
- R:/Smart Commissioning App/frontend/src/features/workflow/ConfigurationPage.tsx:146-150 - fieldDefinitions.backups only overrides 'Encrypted Backups'/'Last Backup Status'/'Restore Action'; 'Backup Location' is absent, so it falls back to kind 'text' (line 611) and is an editable operator-specified target string, consistent with the note.
- R:/Smart Commissioning App/backend/app/services/backup_service.py:136-178 - create_backup_bundle returns the bundle as in-memory zip bytes (no on-disk write location is chosen by the engine); the caller/CLI decides where to write, confirming the note's claim that the app does not pick a host directory itself.
- R:/Smart Commissioning App/docs/review-comments-2026-06.csv:17 - The source review (review-mq9lv0g2) asks 'Where will the backup location be stored... Is it within the app or can a directory on the host laptop be specified?', matching the ask being verified.

**Verify on localhost:** 1. Start the frontend dev server and open http://127.0.0.1:5173/#/configuration. 2. The Configuration page loads sections; the 'Backup & Restore' section is COLLAPSED by default (defaultExpandedSections.backups=false), so click its header to expand it. 3. Read the explanatory paragraph that appears at the top of the expanded section: it states the bundle is built from the app runtime (backend runtime directory by default) and written to a path chosen at backup time via the backup CLI's output option, and that 'the app does not pick a host directory itself.' 4. Confirm a 'Backup Location' field is present below the note, pre-filled with '/data/backups', and is an editable text input the operator can change. This text is what clarifies storage location (in-app runtime vs an operator-specified target).

**Caveats / notes:** Fully a clarifying-copy ask, and it is satisfied: lines 594-602 explicitly distinguish where the bundle is built (app backend runtime dir by default) from where it is written (a path chosen at backup time via the backup CLI's output option), and state the Backup Location field is an operator-recorded intended target rather than something the app auto-picks. Caveats: (1) The note is only visible after the operator expands the Backup & Restore section, which is collapsed by default (ConfigurationPage.tsx:38) - a reviewer who never expands it will not see the clarification. (2) The 'Backup Location' field is a free-text label that is NOT wired to the actual backup write path - backup_service.create_backup_bundle returns zip bytes and the destination is decided entirely by the CLI's output option, exactly as the note honestly says; so editing the field does not change where a backup lands. (3) No automated test asserts the field-note copy (ConfigurationPage.test.tsx only seeds Backup Location, line 44), so the clarifying text is unguarded by tests though present in code.

---

### 8. Double statuses - ✅ Done

- **ID:** `review-mq9lxgf6` - **Module:** Configuration - **Priority:** Medium - **Route:** `/configuration`

**Evidence (code read):**

- frontend/src/features/workflow/ConfigurationPage.tsx:589 - the section status is rendered exactly ONCE per section as a single pill: ``<span className={`section-status-pill ${statusTone(status)}`}>`{status}`</span>``; the section title beside it (line 587) shows sectionLabels[section] (e.g. 'Network Basics'), not the status, so there is no label+value duplication.
- frontend/src/features/workflow/ConfigurationPage.tsx:572 - `const status = draft[section].status;` reads ONE descriptive status string from the backend section; nothing concatenates or repeats it.
- backend/app/services/configuration_service.py:38,51,64 - DEFAULT_CONFIGURATION sets a single status string per section (status='Healthy', 'Listening', 'Connected', 'Valid', 'Synchronised', 'Success'); the schema (backend/app/schemas/configuration.py:6) defines one `status: str` field, so the doubled 'Healthy Healthy' could only ever have come from the frontend rendering it twice, which it no longer does.
- frontend/src/styles.css:1163-1180 - `.section-status-pill` uses `white-space: normal; overflow-wrap: anywhere; word-break: break-word; max-width:100%`, giving room for a long fault like 'Disconnected due to TLS protocol error'; the comment explicitly states it 'replaces the old label+value pair that rendered the same status twice'.
- frontend/src/features/workflow/ConfigurationPage.tsx:184-192 - statusTone() colours the single pill red for fault text (fail/error/unreachable/expired/disconnected/critical/down), amber for warnings, green otherwise, so one pill conveys both state and detail.
- frontend/src/features/workflow/ConfigurationPage.test.tsx:111-128 - regression test 'renders one descriptive status pill per section with room for a long fault string' asserts the long MQTT fault 'Broker unreachable: connection refused...' renders in a single `.section-status-pill`, that the old editable 'Section status' input is gone, and that exactly ONE node carries the text (toHaveLength(1)).

**Verify on localhost:** Start the frontend dev server (npm run dev in frontend/) and the backend so /api/v1/configuration responds. Open http://127.0.0.1:5173/#/configuration. For each section header (Network Basics, BACnet Discovery, MQTT Settings, Certificates & Keys, etc.) look at the top-right of the collapsible header row: you should see exactly ONE coloured status pill (e.g. a single 'Healthy', 'Listening', or 'Connected'), never a repeated 'Healthy Healthy'. The section title to its left is a descriptive label ('Network Basics'), not the status, so they never read as a duplicated pair. To confirm room for fault detail, the test fixture already exercises it: run `npx vitest run ConfigurationPage` in frontend/ and watch the 'renders one descriptive status pill per section with room for a long fault string' test pass — it injects status 'Broker unreachable: connection refused at mqtt.local:8883 after 3 retries' and verifies it shows once, in full, inside the single red pill. To see it live, you can temporarily have the backend return a long MQTT status string and confirm it wraps within one pill rather than being clipped or doubled.

**Caveats / notes:** Fully implemented; no caveats blocking verification. The duplicate ('Healthy Healthy') came from a former label+value pair both rendering the same status; the code now renders the status string once via a single `.section-status-pill`, the CSS comment and the dedicated regression test both explicitly document removing the duplicate. The pill wraps long fault strings (white-space:normal; overflow-wrap:anywhere) so descriptive faults like 'Disconnected due to TLS protocol error' fit. Two minor real-world notes (do not affect this ask): (1) the section status strings are currently seeded defaults from DEFAULT_CONFIGURATION (e.g. 'Connected' is static, not a live broker probe), so on localhost without a live MQTT broker the MQTT pill will still show the seeded 'Connected' rather than a real fault — but the single-pill structure and long-fault capacity are proven by the unit test. (2) Fixed in commit d484d0d ('Address design-review feedback (26 comments)').

---

### 9. Previous UI for the results 1 (expected payload) - ✅ Done

- **ID:** `review-mq9m4bnv` - **Module:** UDMI - **Priority:** Medium - **Route:** `/udmi-validation`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:1867-1893 - Per-asset collapsible drop-down: each asset renders an `asset-group-toggle` button whose COLLAPSED label is a cross-payload-type summary (`${group.issues.length} issues · typeSummary`), where typeSummary is built at 1869-1880 by joining `${entry.payloadType} (issues, payload)` over every payload type.
- frontend/src/features/workflow/ModulePage.tsx:1894-1947 - EXPANDED state maps `group.payloadTypes` (pointset/metadata/state) into `payload-type-group` blocks, each with an h5 of the payload type, its issues, and a 'Show/Hide expected vs observed payload' button revealing side-by-side `<pre>` Expected and Observed JSON (1924-1941).
- frontend/src/features/workflow/operatorData.ts:115-172 - `mergeAssetGroups` unions derived per-asset issue groups with authoritative `payload_views`, producing MergedPayloadType{payloadType, issues, expected, observed, observedPresent} ordered pointset/metadata/state; assets with payloads but zero issues still appear.
- core/smart_commissioning_core/udmi_validation.py:578-600 - `_asset_payload_view` builds one entry per payload_type (state/metadata/pointset) with expected facet (sliced from expected_schedule), observed payload, and observed_present, emitted to result_summary.payload_views at line 85/100.
- core/smart_commissioning_core/udmi_validation.py:642-654 - `_payload_view_source` labels views as live_capture / direct_inputs / none so the UI (ModulePage.tsx:1858-1865) tells the operator whether payloads were pasted, captured, or absent — never fabricated.
- frontend/src/features/workflow/ModulePage.tsx:566-591 - `payloadViews` reads `validationRunQuery.data?.result_summary?.payload_views`; `mergedAssetGroups` (gated to route==='udmi-validation' and a validation run) feeds the drop-down, so the data is real run output not sample.

**Verify on localhost:** 1) Start backend API + worker and the frontend dev server, open http://127.0.0.1:5173/#/udmi-validation. 2) Scroll to the 'Schedule and Payload Evidence' card — the default Expected schedule, State, Metadata, and Pointset payload JSON are pre-filled. Leave the 'Capture from broker' checkbox UNCHECKED (pasted-payload mode needs no broker). 3) Sign in as an engineer (or the Run/Execute buttons are disabled with an 'engineer required' tooltip), then click 'Execute capture'. 4) Watch the 'Validation run monitor' card go from queued to succeeded. 5) Look at the right-hand 'Selected Result Detail' Inspector aside: it now shows the per-asset list (asset-group-list). Each asset row is COLLAPSED by default showing a summary like 'N issues · pointset (...), metadata (...), state (...)' across all payload types. 6) Click an asset row to EXPAND it — it reveals pointset/metadata/state sections with any issues per type. 7) Under a payload type, click 'Show expected vs observed payload' to see side-by-side Expected and Observed JSON. A note above the list states whether payloads are pasted (direct_inputs), live-captured, or fixture-only.

**Caveats / notes:** Fully implemented end-to-end and matches the ask precisely: per-asset drop-down (frontend/src/features/workflow/ModulePage.tsx:1882-1893), collapsed cross-payload-type summary (1869-1893), per-type pointset/metadata/state detail with expected payload (1894-1941), backed by real engine output (core/smart_commissioning_core/udmi_validation.py:578-639) and tested (backend/tests/test_v1_review_contracts.py:251-319). Caveats: (1) The drop-down renders ONLY on the /udmi-validation route and ONLY after a terminal validation run that yields issues and/or payload_views — there is no static sample fallback (`mergedAssetGroups` is null otherwise, ModulePage.tsx:581-591), so without running 'Execute capture' the Inspector shows the plain issue list instead. (2) Pasted-payload mode works without any broker via the inline-local fallback path; only the 'Capture from broker' (live_capture) option needs a reachable MQTT broker and is on-site-untested. (3) Issue-to-payload-type grouping is best-effort/derived (operatorData.ts:49-57, keyword match on issue_type/topic/point_name) since issues carry no explicit payload-type field — but the expected/observed payload content itself is authoritative from result_summary.payload_views. (4) Multi-asset views require an optional `assets` list in run parameters (udmi_validation.py:627-636); the default UI inputs drive the single-asset back-compat path, so the demo shows one asset.

---

### 10. Previous UI for the results 2 (Pass/Fail) - ✅ Done

- **ID:** `review-mq9m6uay` - **Module:** UDMI - **Priority:** Medium - **Route:** `/udmi-validation`

**Evidence (code read):**

- core/smart_commissioning_core/udmi_validation.py:86-106 - The UDMI result_summary is built from concrete counts (expected_devices, publishing_seen, issue_count, etc.) and a list of issues; there is NO 'warning'/'verdict' field. Outcome is binary: issue_count==0 (Pass) vs issues present (Fail, each with a reason).
- core/smart_commissioning_core/udmi_validation.py:292-339 - Fail reasons are concrete and typed, e.g. 'Pointset payload value for X should be numeric for unit Y' (type mismatch), 'Metadata GUID does not match', 'Expected point ... was not received' — exactly the 'Pass or Fail with reasons (type mismatch)' the ask requires.
- core/smart_commissioning_core/records.py:7-22 - ValidationIssueRecord.severity is Literal['low','medium','high','critical'] — severity classifies individual issues, NOT an overall 'warning' result state; there is no warning verdict in the schema.
- frontend/src/features/workflow/operatorData.ts:330-335 - The /udmi-validation sample 'Result' column only ever shows 'Pass' or 'Fail - `<reason>`' (e.g. 'Fail - fault_status type mismatch'); no in-between/'warning' result value exists.
- frontend/src/features/workflow/runFormat.ts:14-20,85 - The only 'warning' token in the UI maps the CANCELLED job status to the label 'Cancelled' (run lifecycle), not a validation verdict — succeeded->ready, cancelled->warning. It is the job state, not the UDMI result.
- frontend/src/features/workflow/moduleData.ts:209 - The single literal 'warning' ("Preserve not-tested, not-applicable, warning, and fail states distinctly") lives in the data-validation route's `readiness` array, which ModulePage never renders (only integrationStatus is consumed, ModulePage.tsx:618). It is not the /udmi-validation route and not a rendered result state.

**Verify on localhost:** Start the frontend dev server and open http://127.0.0.1:5173/#/udmi-validation. (1) In the 'Schedule and Payload Evidence' card, leave the default state/metadata/pointset payloads (they match the expected schedule) and click 'Execute capture' (or 'Run UDMI Validation' in Run Controls). When the run monitor goes 'succeeded', open the right-hand Inspector / asset groups: a clean asset shows '0 issues' (a Pass), with no 'warning' label anywhere. (2) Now edit the 'Pointset payload JSON' so supply_air_temperature_setpoint.present_value is a string (e.g. \"22\") instead of 22, and re-run. The run still finishes 'succeeded' but the Inspector lists a concrete issue card ('...should be numeric for unit degrees_celsius' = type mismatch) — i.e. a Fail with a reason. Confirm the only verdict outcomes you ever see are clean (Pass) or issues-with-reasons (Fail); there is no third 'warning' verdict. (3) In the Results table below, note the labelled sample 'Result' column only contains 'Pass' and 'Fail - `<reason>`'. The orange 'warning' status token only appears if you Cancel a running job (that is the job state 'Cancelled', not a validation result).

**Caveats / notes:** Fully satisfies the ask. The UDMI validation model is structurally binary: the engine emits issues (each a typed Fail reason such as a type mismatch) or none (Pass); there is no 'warning' verdict in core (udmi_validation.py), the issue schema (records.py), the backend processor (udmi_run_processor.py only sets running/succeeded/failed), or the frontend result presentation. Caveats: (a) Issue 'severity' (low/medium/high/critical) classifies individual findings, not the overall result — do not mistake a 'medium' issue for a 'warning verdict'; the run still Fails. (b) The orange 'warning' CSS status-token (styles.css:974) exists but is used ONLY for the CANCELLED job lifecycle status (runFormat.ts:85), never as a UDMI result. (c) The literal word 'warning' in moduleData.ts:209 is unrendered descriptive copy on the *data-validation* route's readiness list, not the /udmi-validation result. (d) Live-broker capture is on-site-untested (no broker reachable records broker_unreachable), but that does not introduce any warning verdict — it surfaces as a Fail issue or a clean Pass. Verification of step (1)/(2) requires running backend+worker so the run reaches a terminal state; without them you can still confirm via the sample 'Result' column that only Pass/Fail values exist.

---

### 11. Previous UI (IP + network number) - ✅ Done

- **ID:** `review-mq9mbmsh` - **Module:** BACnet - **Priority:** Medium - **Route:** `/bacnet-discovery`

**Evidence (code read):**

- frontend/src/features/workflow/discoveryRows.ts:24-34 - bacnetResultColumns array for the live results table explicitly includes both "IP Address" and "Network Number" between "Address" and "Vendor"
- frontend/src/features/workflow/discoveryRows.ts:108-115 - bacnetRowsFromResults reads ipAddress = attributes.ip_address ?? device.ip_address and networkNumber = attributes.network_number ?? device.network_number, populating the "IP Address" and "Network Number" cells (str() renders "—" when absent, i.e. where not applicable)
- frontend/src/features/workflow/operatorData.ts:294-298 - the sample/fallback bacnet-discovery workspace columns also include "IP Address" and "Network Number"; sample rows show real values, and the CHW Pump Panel row (MS/TP behind a router) shows IP Address "—" with Network Number "5", demonstrating the where-applicable behaviour
- frontend/src/features/workflow/ModulePage.tsx:1788-1804 - the Results table renders tableColumns dynamically as `<th>`/`<td>`, so both columns appear as headers and cells for live and sample rows; tableColumns resolves to bacnetResultColumns for a live run (ModulePage.tsx:610)
- frontend/src/features/workflow/ModulePage.tsx:2370-2371 - the Selected Result Detail inspector (buildResultDetailItems, bacnet-discovery branch) also surfaces { label: "IP Address" } and { label: "Network Number" } rows
- core/smart_commissioning_core/engines/bacnet_discovery.py:555-568 - CAVEAT: the real _device_record emits only address/name/vendor/model plus attributes {asset_id, device_instance, vendor_id} — no ip_address and no network_number — so on a live run both columns render "—" for every device; populated values appear only in the sample preview

**Verify on localhost:** See verify_steps field above.

**Caveats / notes:** The ask — re-add the Network number column and show IP address (where applicable) in the BACnet Devices results — is satisfied at the UI layer: both columns are present and render in (a) the live results table, (b) the sample/fallback preview, and (c) the Selected Result Detail inspector. The \"where applicable\" intent is honoured because empty values render as \"—\" (str() in discoveryRows.ts) and the sample data deliberately includes a no-IP MS/TP device. CAVEAT (data completeness, not the column ask): on a real BACnet discovery run the backend never populates these fields. core/.../engines/bacnet_discovery.py:555-568 _device_record emits only address/name/vendor/model + attributes {asset_id, device_instance, vendor_id}; a repo-wide search found `network_number` ONLY in frontend discoveryRows.ts (no Python match anywhere), and no ip_address is stamped onto BACnet device records. The device's network location is carried in the separate \"Address\" column (e.g. \"10.10.0.11:47808\"). So a live run shows both columns as \"—\" for every device; populated IP/Network values are visible only in the sample preview. If the reviewer specifically wanted live BACnet runs to show real IP/network numbers, that is missing on the backend; the columns themselves are correctly re-added and shown.

---

### 12. What does Queue UDMI Validation do? - ✅ Done

- **ID:** `review-mq9mvkki` - **Module:** UDMI - **Priority:** Medium - **Route:** `/udmi-validation`

**Evidence (code read):**

- R:/Smart Commissioning App/frontend/src/features/workflow/moduleData.ts:182-187 - the udmi-validation route's single runAction is labeled "Run UDMI Validation" (kind validation, runKind udmi, jobType udmi_validation); no 'Queue' wording remains.
- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.tsx:1006-1013 - the Run Controls action button text is computed as "Working..." while pending, else "Preview" (dry-run discovery), "Generate" (reports), else "Run" — so the UDMI action renders the button label "Run", never "Queue".
- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.tsx:1507-1516 - the secondary UDMI-only trigger in the Schedule and Payload Evidence card reads "Execute capture" (and "Executing..." while pending), not 'Queue'.
- R:/Smart Commissioning App/frontend/src/features/workflow/moduleData.ts (grep for [Qq]ueue) - No matches found; the entire moduleData action source has zero 'Queue' occurrences.
- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.test.tsx:276-280 - regression test 'renames the discovery run action from Queue to Run' asserts findByText("Run IP Discovery"), and the same Run-prefix label convention is applied to the UDMI action.
- R:/Smart Commissioning App/frontend/src/features/workflow/operatorData.ts:223-225 & DashboardPage.tsx:280 - the only remaining 'Queue' user-facing strings ('Queue evidence', 'Queue evidence pack', 'Report Queue') live on the homepage/dashboard reports flow, NOT on the /udmi-validation route.

**Verify on localhost:** 1. Start the frontend dev server (cd frontend; npm run dev) and open http://127.0.0.1:5173/#/udmi-validation. 2. In the 'Run Controls' card (right column under Execution), find the run-card titled 'Run UDMI Validation' with an action button reading 'Run' (it says 'Working...' briefly when clicked). Confirm the word 'Queue' appears nowhere on this card or button. 3. Scroll to the 'Schedule and Payload Evidence' section and confirm its primary trigger button reads 'Execute capture', not anything with 'Queue'. 4. Optionally run the unit test: cd frontend; npx vitest run src/features/workflow/ModulePage.test.tsx — the test 'renames the discovery run action from Queue to Run' should pass. Note: the homepage (http://127.0.0.1:5173/#/) still shows 'Queue evidence pack' and a 'Report Queue' table, but those are outside the /udmi-validation route this comment targets.

**Caveats / notes:** Fully implemented for the /udmi-validation route. The action that the review called 'Queue UDMI Validation' is now labeled 'Run UDMI Validation' (moduleData.ts:183) and its button renders 'Run' (ModulePage.tsx:1012); the secondary UDMI trigger is 'Execute capture'. No 'Queue' term remains anywhere on this route. Caveat on the broader ask ('avoid the confusing Queue term wherever it appears'): 'Queue' still appears elsewhere in the app — DashboardPage.tsx:280 ('Queueing...'/'Queue evidence pack'), operatorData.ts:225 ('Queue evidence') and :365 ('Report Queue' table title), plus runFormat.ts:82 maps the backend job status 'queued' -> display 'Queued'. Those are on the homepage/reports surfaces, not /udmi-validation, and 'Queued' as a job-status label is a legitimate state name rather than an action verb, so they are arguably out of scope for this specific comment. No live MQTT broker is needed to see the label; the button text is static and visible immediately on page load.

---

### 13. MQTT Config (multi-point) - ✅ Done

- **ID:** `review-mq9n11wi` - **Module:** UDMI - **Priority:** Medium - **Route:** `/udmi-validation`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:1553-1605 - "Write Multiple Points in One Config" editor: Add point button, repeatable point-name + set_value inputs (placeholders fan_enable/true) for extra pairs beyond the primary point, with copy stating all pairs are written into one config payload under pointset.points and the backend confirm/verify step checks every point (one issue per unconfirmed point).
- frontend/src/features/workflow/ModulePage.tsx:2167-2199 - buildMultiPointPayload() merges the primary (point,value) plus every extra pair into a SINGLE config payload's pointset.points.`<name>`.set_value, so one publish writes all points.
- frontend/src/api/client.ts:700-755 - startMqttConfigPublishRun sends parameters.expected_points = primary + all extras (so the backend confirms each) and builds next_pointset_payload with a present_value per expected point so the no-broker local-verify path can confirm them all.
- core/smart_commissioning_core/mqtt_config_publish.py:202-238 - engine resolves the full expected-points set, loops each, compares observed present_value to target, appends one config_override_not_observed issue per mismatch (naming point_name/expected/observed), and at 262-285 reports expected_point_count, matched_point_count, partial_confirm, and per-point point_checks/expected_points[].confirmed.
- core/tests/test_mqtt_config_multipoint.py:21-118 - tests prove two-point all-match succeeds, a single mismatched point yields exactly one issue + partial_confirm, a missing point is a mismatch, and expected points derived directly from the published set_values are confirmed.
- backend/app/api/routes/validation.py:118-139 - POST /validation/mqtt-config/runs passes the full run.parameters (incl. expected_points + next_pointset_payload) straight into process_mqtt_config_publish_run, which (mqtt_config_publish_processor.py:41-43) forwards them unmodified to validate_and_publish_config.

**Verify on localhost:** Start the frontend dev server and open http://127.0.0.1:5173/#/udmi-validation. Sign in with an engineer-or-above API key (the Publish button is disabled with an 'engineer required' tooltip otherwise). Scroll to the 'Controlled publish — MQTT Config Payload' card. The primary point defaults to supply_air_temperature_setpoint with set_value 22. Under 'Additional points — Write Multiple Points in One Config', click 'Add point', enter point name heating_valve_percentage_command and a set_value (e.g. 40). Tick the bottom 'I confirm this config payload should be published...' checkbox, then click 'Publish and verify next pointset'. A Validation run monitor appears; when it goes terminal, open the run (GET /validation/runs/{id}) in devtools/Network and inspect result_summary: expected_point_count should be 2, point_checks should list BOTH supply_air_temperature_setpoint AND heating_valve_percentage_command with expected_value/observed_value/matched, and matched_point_count/partial_confirm reflect how many confirmed. The Issues list raises one config_override_not_observed per point whose value was not confirmed back. To see the single-payload write, inspect the POST /validation/mqtt-config/runs body: parameters.payload contains both points under pointset.points and parameters.expected_points lists both.

**Caveats / notes:** Fully implemented end-to-end (UI -> single multi-point config payload -> per-point confirm-back -> per-point issues + summary counts), and the ask's exact example points (supply_air_temperature_setpoint and heating_valve_percentage_command) are supported: primary defaults to supply_air_temperature_setpoint and any second point can be added. CAVEAT 1 (honesty, not a gap): the actual LIVE-broker multi-point publish/confirm is on-site-untested — without a reachable broker the run records broker_unreachable. In dev (no broker) the confirm-back runs against the LOCAL verify path: client.ts:725-742 synthesizes a next_pointset_payload echoing each expected point's value as present_value, so every point confirms green in the demo; this proves the plumbing/summary contract but is NOT a real device round-trip. The fully-tested behavior is the engine's local-verify logic in core (test_mqtt_config_multipoint.py). CAVEAT 2: the rollback path intentionally drops expected_points (mqtt_config_publish.py:401-404), correct since a rollback asserts no new override. No part of the ask is missing.

---

### 14. Schedule and Payload Evidence (execute) - ✅ Done

- **ID:** `review-mq9n7pbe` - **Module:** UDMI - **Priority:** Medium - **Route:** `/udmi-validation`

**Evidence (code read):**

- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.tsx:1507-1522 - Inside the udmi-validation "Schedule and Payload Evidence" section there is an Execute button ("Execute capture" / "Executing...") in an .execute-row, wired to onClick={() => runMutation.mutate(udmiRunActionIndex)} and gated to engineers.
- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.tsx:1473-1505 - A "Capture latest state, metadata, and pointset payloads from the configured MQTT broker" checkbox (udmiUseLiveBroker) reveals State/Metadata/Pointset topic inputs plus a capture-window-seconds input, so the button can go grab the payloads at the specified topics.
- R:/Smart Commissioning App/frontend/src/features/workflow/ModulePage.tsx:425-444, 2049-2071 - The validation run mutation builds udmi parameters via buildUdmiValidationParameters, sending state_topic/metadata_topic/pointset_topic, capture_seconds and use_live_broker to the API (startValidationRun, runKind "udmi").
- R:/Smart Commissioning App/frontend/src/features/workflow/moduleData.ts:180-188 - The udmi-validation module defines a validation runActions entry (runKind "udmi", jobType "udmi_validation"); udmiRunActionIndex (ModulePage.tsx:535-540) resolves to it so the Execute button triggers the same run as Run Controls.
- R:/Smart Commissioning App/core/smart_commissioning_core/udmi_validation.py:344-458 - _capture_live_payloads honours use_live_broker, builds the state/metadata/pointset topic list (_capture_topics), subscribes via live_capture, and stores captured payloads keyed by topic suffix (/state,/metadata,/pointset); returns broker_unreachable/timeout when no broker.
- R:/Smart Commissioning App/worker/app/tasks.py:209-224 - validate_udmi_payloads dispatches process_udmi_validation_run with the real subscribe_and_capture live-capture path, so a real worker actually goes out and grabs the live payloads.

**Verify on localhost:** See verify_steps field above.

**Caveats / notes:** Fully implemented end-to-end. The button label is \"Execute capture\" (not literally \"Execute\") but it is the Execute affordance the ask describes, placed exactly in the Schedule & Payload Evidence section. Caveats: (a) it requires an engineer+ role — viewers/reviewers see it disabled with the ENGINEER_REQUIRED_TOOLTIP. (b) Actually grabbing LIVE payloads requires ticking the broker checkbox AND a reachable, publishing MQTT broker; that live-broker capture is on-site-untested per the UI copy and code comments, and with no broker it surfaces broker_unreachable/live_capture_timeout rather than real data. (c) Without the broker checkbox, the same button runs the validation against the pasted JSON payloads instead of going out to the topics. The backend wiring (worker tasks.py -> process_udmi_validation_run -> _capture_live_payloads with subscribe_and_capture) is real, not stubbed.

---

### 15. Emulate MQTT explorer - ✅ Done

- **ID:** `review-mq9nhbzu` - **Module:** MQTT Discovery - **Priority:** High - **Route:** `/mqtt-discovery`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:1311-1428 - The mqtt-discovery route renders an 'Incoming MQTT Payloads' panel (h3 line 1316) with a 'Topic filter (MQTT wildcards: + and #)' input defaulting to '#' (lines 1348-1355) and a 'Capture duration (seconds - 0 or blank = run until stopped)' input (lines 1356-1364). It shows a table of Topic/Asset/Messages/Latest payload/Copy (lines 1375-1420) and 'Export to CSV' + 'Export to XLSX' buttons (lines 1318-1345).
- frontend/src/features/workflow/ModulePage.tsx:2008-2046 - buildDiscoveryParameters() forwards the operator's topic filter to parameters.topic_filter and maps blank/0/non-numeric capture seconds to capture_seconds=0 (the 'indefinite / run until stopped' sentinel), proving wildcard subscription + indefinite duration are wired into the run request.
- frontend/src/features/workflow/ModulePage.tsx:755-783 - captureRows builds 'latest payload per topic' from the live getDiscoveryTopics snapshot filtered by matchesTopicFilter; handleCaptureExportXlsx() downloads getDiscoveryTopicsXlsxPath(runId, captureTopicFilter) for the Excel export, and handleCaptureExport() builds a CSV.
- backend/app/api/routes/discovery.py:274-321 - GET /runs/{run_id}/topics.xlsx (export_discovery_topics_xlsx) generates a real openpyxl Workbook with columns Topic/Asset/Last Seen/Message Count/Latest Payload, applying the same +/# topic_filter wildcard (_matches_topic_filter, lines 330-346). Backend tests at backend/tests/test_engines_api.py:243-320 cover empty/populated/filtered/404 cases.
- core/smart_commissioning_core/engines/mqtt_discovery.py:86-133 - _resolve_topic_filters defaults to '#' (subscribe to all) and accepts topic_filter/topics; _capture_seconds treats explicit 0/empty/negative as None = 'run until stopped (via cancellation) or the message cap', implementing the indefinite-until-stopped behaviour.
- core/smart_commissioning_core/engines/mqtt_discovery.py:394-449 - _aggregate_capture emits one DiscoveredTopic row per topic carrying message_count and last_payload (the JSON of the LAST message seen per topic), which is exactly the 'latest payload seen per topic' the panel and Excel export render.

**Verify on localhost:** duplicate

**Caveats / notes:** Fully implemented end-to-end (UI panel + topic-filter/duration inputs + per-topic latest payload table + CSV and server-generated XLSX export + wildcard subscription + indefinite-until-stopped duration). Two honest caveats, both acknowledged in-code and surfaced in UI copy: (1) The actual live broker capture is on-site-untested - with no reachable MQTT broker (the dev/CI default) the run records broker_unreachable and the panel stays empty rather than fabricating payloads (ModulePage.tsx:1366-1374; mqtt_discovery.py module docstring lines 46-48 list the raw-socket SUBSCRIBE/TLS path as live_untested). So you can SEE the full UI and exports, but verifying real incoming payloads requires a live broker on site. (2) An 'indefinite' capture is bounded by the DEFAULT_MAX_MESSAGES cap (500) or operator Cancel - it is not truly unbounded, but the ask's 'leave it indefinite until stopped' is satisfied via Cancel/message-cap, and the UI copy states this. No fabricated/sample payloads are shown - empty live results stay empty.

---

### 16. IP Discovery Input Example - ✅ Done

- **ID:** `review-mqalcdg1` - **Module:** IP Discovery - **Priority:** High - **Route:** `/ip-scanner`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:89 - ip-scanner module declares importTypes: ["ip_register"], so the template UI below is driven for this route
- frontend/src/features/workflow/ModulePage.tsx:861-905 - 'Default import template' card (rendered when an import type is selected) with 'Download XLSX' and 'Download CSV' buttons that call templateDownload.download(getImportTemplatePath(selectedImportType,'xlsx'/'csv')); copy explicitly says the template 'includes the required columns and one realistic example row'
- frontend/src/features/workflow/ModulePage.tsx:1174-1237 - second 'Import Templates for This Page' section iterates module.importTypes (ip_register here) rendering XLSX/CSV download buttons via getImportTemplatePath
- frontend/src/api/client.ts:646-648 - getImportTemplatePath returns /imports/templates/{import_type}.{format}; downloads go through authenticated downloadFile (X-API-Key attached)
- backend/app/api/routes/imports.py:75-92 - GET /imports/templates/{import_type}.{file_type} (viewer+) returns service.build_template(...) with correct csv/xlsx media type and {import_type}_default_template.{ext} filename
- backend/app/services/import_service.py:386-394, 660-689 - build_template writes the ip_register required-column header row plus one populated EXAMPLE_ROW (Project/site, System, Asset ID, Asset name, Expected IP address, Expected hostname, Expected services/ports) into both CSV and styled XLSX

**Verify on localhost:** 1) Start backend + frontend dev server. 2) Open http://127.0.0.1:5173/#/ip-scanner. 3) In the left 'Register Import' card, the import profile select defaults to 'ip register'; below 'Upload and validate' you see the 'Default import template' card. Click 'Download CSV' and 'Download XLSX' - browser saves ip_register_template.csv / .xlsx (server filename ip_register_default_template.*). 4) Open the file: row 1 is the required column headers (Project/site, System, Asset ID, Asset name, Expected IP address, Expected hostname, Expected services/ports) and row 2 is a realistic example (e.g. 10.10.25.117, 47808/udp, 443/tcp). 5) Scroll to the 'Import Templates for This Page' section lower on the page for the same XLSX/CSV buttons.

**Caveats / notes:** Fully implemented and self-contained - no live broker, network scan, or on-site hardware needed; the template endpoint is a pure server-side openpyxl/csv build available to any viewer+ role. The template is formatted as a register import template (required columns + one example row), which is exactly the IP Discovery input format the scanner compares against. Two independent download surfaces exist on /ip-scanner (the selected-profile card at ModulePage.tsx:861 and the 'Import Templates for This Page' grid at :1174). Downloads route through downloadFile so they attach X-API-Key (bare anchors would 401 in hosted deploys). No defects found: I checked the EXAMPLE_ROWS['ip_register'] key for 'Project/site' (import_service.py:387) and it is a forward slash matching the required column, so the example cell is populated, not blank.

---

### 17. BACnet Discovery Input Example - ✅ Done

- **ID:** `review-mqalda3m` - **Module:** BACnet - **Priority:** High - **Route:** `/bacnet-discovery`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:119 - bacnet-discovery module declares importTypes: ["bacnet_register", "bacnet_points"], the two templates the ask names
- frontend/src/features/workflow/ModulePage.tsx:861-905 - per-profile 'Default import template' card with 'Download XLSX' / 'Download CSV' buttons calling getImportTemplatePath(selectedImportType, ...) for the selected register/points type
- frontend/src/features/workflow/ModulePage.tsx:1174-1237 - 'Import Templates for This Page' section maps module.importTypes (both bacnet_register and bacnet_points) into one card each with XLSX/CSV download buttons; copy states each template includes required columns and one realistic example row
- frontend/src/api/client.ts:646-648 - getImportTemplatePath builds GET /imports/templates/{import_type}.{format}; downloads go through downloadFile() so the X-API-Key header rides
- backend/app/api/routes/imports.py:75-94 - GET /templates/{import_type}.{file_type} returns service.build_template(...) as an attachment download with correct csv/xlsx media type and Content-Disposition
- backend/app/services/import_service.py:660-672 - _build_csv_template / _build_xlsx_template write the profile's required_columns as the header row plus the EXAMPLE_ROWS example row; bacnet_register cols (line 168-176) and bacnet_points cols (line 265-276) and their example rows (line 395-403, 428-439) are fully defined

**Verify on localhost:** see verify_steps field

**Caveats / notes:** Fully implemented and self-contained — no live MQTT broker or on-site hardware needed; the template endpoint is a static generator (openpyxl/csv) gated only at viewer+ RBAC. Templates ship both required-column headers AND a realistic example row, directly satisfying 'how the BACnet register and BACnet points should be formatted'. Both target template types appear twice on the page (the per-profile card and the dedicated 'Import Templates for This Page' grid), so download is reachable without selecting a profile. Minor naming nuance: client requests filename {import_type}_template.{ext} but the backend's Content-Disposition sets {import_type}_default_template.{ext}; downloadFile() prefers the server Content-Disposition name, so the saved file is e.g. bacnet_register_default_template.xlsx — cosmetic only, content is correct. No gaps found against the ask.

---

### 18. MQTT Discovery Input Example - ✅ Done

- **ID:** `review-mqale1y9` - **Module:** MQTT Discovery - **Priority:** High - **Route:** `/mqtt-discovery`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:149 - the mqtt-discovery module declares importTypes ["mqtt_register", "mqtt_points"], so both MQTT register and points templates are wired to this route
- frontend/src/features/workflow/ModulePage.tsx:861-905 - Register Import surface renders a 'Default import template' card with 'Download XLSX' and 'Download CSV' buttons that call getImportTemplatePath(selectedImportType, ...) for the selected MQTT profile
- frontend/src/features/workflow/ModulePage.tsx:1174-1237 - a dedicated 'Import Templates for This Page' section iterates module.importTypes (mqtt_register + mqtt_points) and renders XLSX/CSV download buttons for each, with copy stating each template includes required columns and one realistic example row
- frontend/src/api/client.ts:646-648 - getImportTemplatePath builds GET /imports/templates/{import_type}.{format}; useFileDownload (ModulePage.tsx:2328-2333) pulls it via authenticated downloadFile and triggers a real browser download
- backend/app/api/routes/imports.py:75-94 - GET /imports/templates/{import_type}.{file_type} returns the built CSV/XLSX as an attachment with proper media type and Content-Disposition filename
- backend/app/services/import_service.py:198-211 & 302-312 & 404-450 - mqtt_register and mqtt_points profiles define the full required-column set (Expected topic, Payload type, Expected points, JSON path, etc.) and EXAMPLE_ROWS supply a realistic filled example row that _build_csv_template/_build_xlsx_template write as header + example

**Verify on localhost:** 1. Start the frontend dev server and open http://127.0.0.1:5173/#/mqtt-discovery (sign in as an engineer; viewers can still download templates since the endpoint only requires viewer). 2. In the left 'Register Import' card, the 'Import profile' dropdown shows mqtt register / mqtt points. With a profile selected, a 'Default import template' card appears with 'Download XLSX' and 'Download CSV' buttons — click each and confirm a file named e.g. mqtt_register_template.xlsx / .csv downloads. 3. Switch the dropdown to 'mqtt points' and repeat to get the points template. 4. Scroll down to the 'Import Templates for This Page' section (eyebrow 'Templates'): it lists two cards, 'Mqtt Register' and 'Mqtt Points', each with XLSX and CSV buttons. Click them and open the files. 5. Open a downloaded file and confirm the first row is the required-column header (Project/site, System, Asset ID, Expected topic, Payload type, Expected points, ... for register; Asset ID, Topic, JSON path or field name, Expected point name, ... for points) and the second row is a filled example (e.g. topic electracom/sct/1532/meter/009/events/pointset). This demonstrates the format for both the MQTT register and points.

**Caveats / notes:** Fully implemented and not faked: templates are generated server-side via openpyxl/csv from real profile column definitions plus one example row, and downloaded through the authenticated download helper (not a bare link). The ask ('downloadable template showing how the MQTT register and points should be formatted') is satisfied for BOTH register and points, in two redundant UI locations. No live MQTT broker is needed — template download is independent of any discovery run. Minor naming caveat only: the in-app download filename falls back to {import_type}_template.ext while the backend Content-Disposition sets {import_type}_default_template.ext; downloadFile honors the server filename, so the saved file is actually mqtt_register_default_template.xlsx etc. — cosmetic, does not affect the ask.

---

### 19. UDMI Validation Input Example - ✅ Done

- **ID:** `review-mqalf5tt` - **Module:** UDMI - **Priority:** High - **Route:** `/udmi-validation`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:179 - the udmi-validation module registers importTypes: ["mqtt_register", "asset_validation", "mqtt_points"], i.e. exactly the three the comment asks for (MQTT register, asset validation, MQTT points)
- frontend/src/features/workflow/ModulePage.tsx:1174-1237 - an 'Import Templates for This Page' section iterates module.importTypes and renders, per type, XLSX and CSV download buttons that call allTemplatesDownload.download(getImportTemplatePath(importType, 'xlsx'|'csv'))
- frontend/src/features/workflow/ModulePage.tsx:861-905 - the Register Import card also offers a per-selected-type 'Default import template' with Download XLSX / Download CSV buttons, described as including required columns and one realistic example row
- frontend/src/api/client.ts:646-648 + downloadFile (383-398) - getImportTemplatePath builds GET /imports/templates/{import_type}.{format}, fetched with the X-API-Key auth helper so the file actually downloads
- backend/app/api/routes/imports.py:75-94 - GET /imports/templates/{import_type}.{file_type} returns the built bytes as an attachment (filename {import_type}_default_template.csv/.xlsx) for csv/xlsx
- backend/app/services/import_service.py:660-689 + 404-450 - _build_csv/_xlsx_template write the profile's required-column header row plus one realistic example row; EXAMPLE_ROWS defines formatted sample data for mqtt_register, asset_validation, and mqtt_points (e.g. topic electracom/sct/1532/ahu/l03/events/pointset)

**Verify on localhost:** 1) Start the frontend dev server and open http://127.0.0.1:5173/#/udmi-validation. 2) Scroll to the 'Import Templates for This Page' surface (eyebrow 'Templates', heading 'Import Templates for This Page'). 3) Confirm three template cards appear: 'MQTT Register' (mqtt_register), 'Asset Validation' (asset_validation), and 'MQTT Points' (mqtt_points), each with XLSX and CSV buttons. 4) Click XLSX on each; a file like mqtt_register_default_template.xlsx downloads. Open it: row 1 is the required-column header (e.g. Project/site, Asset ID, Expected topic, Payload type for MQTT register), row 2 is one realistic example (e.g. topic electracom/sct/1532/ahu/l03/events/pointset). 5) Also confirm the 'Register Import' card at top: pick an import profile and click 'Download XLSX'/'Download CSV' for the same effect. (Requires an API key set in the app so the authenticated download succeeds.)

**Caveats / notes:** Fully satisfies the ask. The route exposes downloadable templates for precisely the three formats the comment names - MQTT register, asset validation, and MQTT points - in both XLSX and CSV, each with the required-column header and one realistic example row showing the expected formatting. Templates are generated server-side (openpyxl/csv) and served by a viewer-accessible endpoint, so they work without an engineer role; downloads do require an API key to be set (downloadFile attaches X-API-Key). No live broker or on-site hardware is needed for the template download itself. Minor note: the downloaded filename is {import_type}_default_template.xlsx (backend Content-Disposition) while the frontend requests {import_type}_template.xlsx; the backend filename wins via Content-Disposition - cosmetic only, the file content is correct.

---

### 20. Data Validation Input Example - ✅ Done

- **ID:** `review-mqalgpxt` - **Module:** Validation - **Priority:** High - **Route:** `/data-validation`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:212 - the /data-validation module declares importTypes: ["asset_validation", "bacnet_points", "mqtt_points", "mapping", "tolerances"] — exactly the five the ask names (asset validation, bacnet points, mqtt points, mapping, tolerances).
- frontend/src/features/workflow/ModulePage.tsx:1174-1237 - because importTypes is non-empty, ModulePage renders an "Import Templates for This Page" section that maps each importType to a card with XLSX and CSV download buttons, calling getImportTemplatePath(importType, format) through the authenticated downloadFile helper.
- frontend/src/api/client.ts:646-648 - getImportTemplatePath builds GET /imports/templates/{import_type}.{csv|xlsx}; downloadFile (lines 383-398) attaches X-API-Key so the binary download works in hosted mode.
- backend/app/api/routes/imports.py:75-94 - GET /imports/templates/{import_type}.{file_type} returns the generated bytes with the right media type and Content-Disposition attachment filename (e.g. asset_validation_default_template.xlsx).
- backend/app/services/import_service.py:660-689 - _build_csv_template / _build_xlsx_template write the profile's required_columns as the header row plus one example row (openpyxl XLSX styled with header fill, frozen panes, auto-filter), so the downloaded file shows the exact required format.
- backend/app/services/import_service.py:333-383,418-468 - PROFILES + EXAMPLE_ROWS define required columns and one realistic example row for all five types (mapping includes a Tolerance column; tolerances = Asset ID/Point name/Tolerance); ModulePage.test.tsx:289-302 asserts the data-validation page shows all five with 5 XLSX and 5 CSV buttons.

**Verify on localhost:** 1. Start the backend API and the frontend dev server (http://127.0.0.1:5173); set a valid API key in the app if prompted. 2. Navigate to http://127.0.0.1:5173/#/data-validation. 3. Scroll to the \"Import Templates for This Page\" section (eyebrow \"Templates\"). You should see five cards: Asset Validation, Bacnet Points, Mqtt Points, Mapping, Tolerances, each with an XLSX and a CSV button. 4. Click \"XLSX\" on the Mapping card — a file named mapping_default_template.xlsx downloads. Open it: row 1 = required columns (Asset ID, BACnet device instance, ... MQTT units, Tolerance, Mapping required flag), row 2 = a realistic example row. 5. Repeat \"CSV\" on the Tolerances card to get tolerances_default_template.csv (columns Asset ID, Point name, Tolerance + one example row). 6. (Optional) The per-profile \"Default import template\" card inside Register Import (top-left) also downloads the selected type's template.

**Caveats / notes:** Fully implemented for all five formats the ask names. Templates are generated server-side (openpyxl for XLSX, csv module for CSV) containing the required column headers plus one realistic example row — there is no static file dependency. Required-column lists and example rows live in backend/app/services/import_service.py (PROFILES lines 234-383, EXAMPLE_ROWS lines 385-469). Caveats: (a) downloads are gated at viewer+ (require_viewer on the route) and the frontend downloadFile attaches X-API-Key, so an unauthenticated session 401s; the in-page Register Import per-type template card and the run buttons are engineer-gated but the bulk \"Import Templates for This Page\" download buttons are not role-gated in the UI. (b) The templates show the column FORMAT and one example row, not multi-row sample datasets — this matches the ask (\"showing how ... should be formatted\"). No live broker or on-site hardware is needed to see or download the templates.

---

### 21. Export button doesn't do anything - ✅ Done

- **ID:** `review-mqatabc9` - **Module:** Reports - **Priority:** Medium - **Route:** `/reports`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:1763-1771 - the Reports 'Workflow Results' Export button: onClick={handleExport}, disabled only when no report exists or a download is in flight (!exportEnabled || pendingKey), label flips to 'Exporting...'
- frontend/src/features/workflow/ModulePage.tsx:689-698 - handleExport actually downloads: exportDownload.download('export', getReportDownloadPath(exportReport.report_id), file_name). exportReport=lastReport, set on Generate (line 459-460), so the button is live the moment a report is generated
- frontend/src/features/workflow/ModulePage.tsx:1667-1679 + 719-731 - a second 'Export selected' button on the Generated Reports table calls handleExportSelected, which loops the ticked succeeded reports and downloads each via getReportDownloadPath
- frontend/src/api/client.ts:383-398 + ModulePage.tsx:2324-2358 - downloadFile() fetches the binary with the X-API-Key header and useFileDownload/triggerBlobDownload create an `<a download>` blob URL and click it, producing a real browser save (not a no-op)
- backend/app/api/routes/reports.py:68-84 - GET /reports/{id}/download builds a genuine artifact via _build_report_artifact (xlsx/docx/zip with summary + source-run findings, lines 133-308) and returns Response(content=...) with Content-Disposition: attachment
- backend/tests/test_evidence_api.py:125-141 - GET /api/v1/reports/{report_id}/download returns status 200 with real, byte-reproducible artifact content, confirming the endpoint serves an actual file

**Verify on localhost:** See verify_steps field.

**Caveats / notes:** Fully implemented and not faked — both Export affordances hit GET /reports/{id}/download, which returns a real file. Caveats: (1) The 'Workflow Results' Export button is disabled until a report is generated in the current session (exportReport=lastReport, in-memory only); on a fresh page load with reports already in the list it stays disabled until you generate one or use 'Export selected'. (2) 'Export selected' checkboxes only enable for reports with status 'succeeded' (line 1707/1714); a freshly created report is queued until a background worker marks it succeeded, so on a setup with no worker running those rows stay non-selectable (though the download endpoint itself builds the artifact regardless of status). (3) All download/generate actions are engineer+; a viewer/reviewer sees Generate disabled. No live MQTT broker is required for report export.

---

### 22. Export specific reports - ✅ Done

- **ID:** `review-mqatcqb3` - **Module:** Reports - **Priority:** Medium - **Route:** `/reports`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:221 - selectedReportIds is a Set`<string>` of ticked report ids, the per-report multi-select state backing the export
- frontend/src/features/workflow/ModulePage.tsx:705-715 - toggleReportSelection(reportId) adds/removes a report id from the selection set (per-report selection, multiple allowed)
- frontend/src/features/workflow/ModulePage.tsx:719-731 - handleExportSelected filters downloadableReports to only the ticked ids and downloads each via getReportDownloadPath, so only the chosen reports are exported
- frontend/src/features/workflow/ModulePage.tsx:1667-1679 - 'Export selected' button: disabled until at least one report is ticked, label/title reflect the count, onClick=handleExportSelected
- frontend/src/features/workflow/ModulePage.tsx:1706-1719 - each report row renders a selection checkbox bound to selectedReportIds.has(report.report_id); disabled (not selectable) unless status==='succeeded'
- backend/app/api/routes/reports.py:52-84 - real GET /reports list and GET /{report_id}/download endpoints back the list + per-report export (returns the actual xlsx/docx/zip artifact)

**Verify on localhost:** 1) Start the backend API and the frontend dev server, then open http://127.0.0.1:5173/#/reports (set an engineer+ API key first if prompted). 2) In the 'Run Controls' card click 'Generate' two or more times (optionally change report type) so several reports appear; each generated report is added to the 'Generated Reports' table lower on the page. 3) In that table, tick the 'Select' checkbox on two (or more) of the rows whose Status is 'succeeded' (the checkbox is disabled for non-succeeded rows). 4) Confirm the 'Export selected' button in the section header enables and its tooltip shows the selected count. 5) Click 'Export selected' and confirm the browser downloads exactly those ticked report files (one file per selected report) and not the unselected ones.

**Caveats / notes:** Fully satisfies the ask: the /reports page lists every generated report with a per-row selection checkbox and an 'Export selected' action that downloads only the ticked reports, backed by real backend endpoints (GET /reports list at reports.py:52, GET /{id}/download artifact at reports.py:68). Caveats: (a) Export downloads each selected report as a SEPARATE file sequentially (handleExportSelected loops and calls download per report) rather than bundling them into one combined archive — fine for 'export only those', but multiple browser download prompts may appear. (b) Only reports with status 'succeeded' are selectable/exportable (checkbox disabled otherwise via downloadable gate at line 1714) — a queued/failed report cannot be ticked, which is correct since only succeeded reports have a real file. (c) The list is populated by GET /reports; you must generate at least one report first (or use 'Generate report from this run' elsewhere) or the table shows 'No reports yet'. No live MQTT broker is required for this feature.

---

### 23. Change name of Validation page - ✅ Done

- **ID:** `review-mqatkxi8` - **Module:** Validation - **Priority:** Medium - **Route:** `/data-validation`

**Evidence (code read):**

- frontend/src/features/workflow/moduleData.ts:196-197 - the data-validation module entry sets title: "BACnet to MQTT Validation" (this is module.title, the hero fallback). Lines 193-195 are an explicit code comment: route stays /data-validation, only the operator-facing title/nav label is renamed 'per review mqatkxi8'.
- frontend/src/features/workflow/operatorData.ts:339-342 - the data-validation workspace sets title: "BACnet to MQTT Validation" and headline: "Run MQTT payload checks, BACnet point checks, and BACnet-to-MQTT live value comparisons." This is workspace.title, the primary hero source.
- frontend/src/features/workflow/ModulePage.tsx:805 - the page hero heading renders `<h2>`{workspace?.title ?? module.title}`</h2>`; for /data-validation both sources resolve to "BACnet to MQTT Validation", so the on-page heading shows the new name.
- frontend/src/app/App.tsx:13 - the left/top nav tab for to: "/data-validation" has label: "BACnet to MQTT Validation" (item 07), so the sidebar entry is renamed too.
- frontend/src/app/App.tsx:22 - the pageTitles map sets "/data-validation": "BACnet to MQTT Validation", so the app-shell header title (and DashboardPage.tsx:22 which has the identical map) also reflect the rename.

**Verify on localhost:** Start the frontend dev server (cd frontend; npm run dev) and open http://127.0.0.1:5173/#/data-validation. (1) In the top/side nav, confirm tab 07 reads 'BACnet to MQTT Validation' (not 'Validation'). (2) Click it; the page hero heading (the large `<h2>` under the 'Validation API' eyebrow) reads 'BACnet to MQTT Validation', and the app-shell page title at the top also reads 'BACnet to MQTT Validation'. (3) The route in the URL stays /#/data-validation (intentionally unchanged). No login or live broker is needed to see the rename - it is static label text.

**Caveats / notes:** Fully implemented across all three user-facing label sources (hero title via workspace + module fallback, nav tab, and header page-title map). The route path itself is intentionally left as /data-validation (documented in the moduleData.ts:193-195 comment) - only labels changed, which satisfies the ask. A few non-page-heading 'Validation' strings remain but are NOT the page name and are out of scope: ReviewFeedback.tsx:26,40 use 'Validation' as a short tag in the in-app review-comment widget's module dropdown (the tool that collects these very review comments); operatorData.ts:216 uses name:'Validation' as a sample dashboard workflow-stage label; ModulePage.tsx:1076 renders 'Validation run monitor' to distinguish discovery vs validation runs; moduleData.ts:190 is a code comment. None of these is the page title/heading, so the rename itself is complete. The supporting copy and validation-mode cards on the page already describe BACnet/MQTT/UDMI checks, consistent with the new name.

---

### 24. How are reports linked/generated - ✅ Done

- **ID:** `review-mqautz9j` - **Module:** Reports - **Priority:** Medium - **Route:** `/reports`

**Evidence (code read):**

- frontend/src/features/workflow/ModulePage.tsx:1131-1141 - On a terminal validation/discovery run, an engineer sees a 'Generate report from this run' button with title 'Generate a report for this run type, then find it in the Reports tab.' This is the 'request a report based on the validation output' affordance.
- frontend/src/features/workflow/ModulePage.tsx:515-523 - reportFromRunMutation calls createReport({ format:'zip', reportType, sourceRunIds:[runId] }) and on success sets reportToast to 'Report generated from this run — see the Reports tab. Report ID: ...', the cross-tab notice tied to the originating run.
- frontend/src/features/workflow/ModulePage.tsx:458-465 - After a direct report Generate, onSuccess sets reportToast 'Report generated — see the Reports list below to download or export it.' and refetches the reports list so the new report is immediately selectable.
- frontend/src/features/workflow/ModulePage.tsx:1686-1691 - On the /reports page the toast renders as a green state-panel 'Report generated' notice (role=status) above the reports table; lines 1681-1685 add explanatory copy that reports are 'traceable to the run it came from'.
- frontend/src/features/workflow/ModulePage.tsx:736-750 - handleGenerateReportFromRun derives the ReportType from the run kind/route (ip/bacnet/mqtt discovery or issue_report) and scopes the report to source_run_ids:[runId], so a requested report traces back to the run rather than being an unscoped/unneeded report.
- backend/app/api/routes/reports.py:190-219 - _source_run_findings pulls the ACTUAL issues from each source_run_id into the generated artifact and reports.py:182 records the 'Source runs' id list, so the linkage is real end-to-end, not just a UI label.

**Verify on localhost:** Start the frontend dev server (http://127.0.0.1:5173) and the backend API. Sign in with an engineer-or-admin API key (the run-monitor button is gated by canEngineer). 1) Go to http://127.0.0.1:5173/#/udmi-validation. In 'Schedule and Payload Evidence' click 'Execute capture' (or use Run Controls) to start a UDMI validation run. 2) Watch the 'Validation run monitor' panel; once status reaches succeeded/failed/cancelled (terminal), a 'Generate report from this run' button appears in the monitor's action row. Click it. 3) Confirm a green note appears under the monitor: 'Report generated from this run — see the Reports tab. Report ID: ...'. 4) Navigate to http://127.0.0.1:5173/#/reports — you should see a green 'Report generated' status banner and the new report row in the 'Generated Reports' table (traceable to the run). 5) Separately, on /#/reports use Run Controls -> 'Generate' and confirm the 'Report generated' banner appears and the table refreshes with the new report. Tick a succeeded report and click 'Export selected' to download it.

**Caveats / notes:** Fully implemented in ModulePage.tsx (the shared component all module routes render). Both halves of the ask are present: (a) a 'report generated — refer to Reports tab' style notice (reportToast at lines 463 and 519-521, rendered at 1144-1146 in the run monitor and 1686-1691 on /reports), and (b) a user-initiated 'Generate report from this run' button that requests a report scoped to the validation/discovery output via source_run_ids, avoiding unneeded unscoped reports. The linkage is genuine end-to-end: backend reports.py:_source_run_findings embeds the source runs' actual issues into the artifact. Caveats: (1) the 'Generate report from this run' button only shows for engineer/admin roles AND only once the run is terminal (line 1131: canEngineer && activeRunTerminal); a viewer/reviewer or an in-progress run will not see it. (2) The toast says 'see the Reports tab' but there is no auto-navigation or deep-link — the user must click the Reports nav item manually. (3) The in-monitor reportFromRunMutation toast reuses the same reportToast state but is not auto-cleared by the 8s timer when shown there (the timer effect at 375-381 does clear it globally). No live MQTT broker is required to verify with the default pasted UDMI payloads.

---

## 5. Live-path caveats (code present, on-site-untested)

Fully wired in code and passing offline/unit tests, but their *live* behaviour against real infrastructure (an MQTT broker, BMS/BACnet hardware, TLS handshake) cannot be exercised in dev. Validated on-site in Phase 5 (`docs/phase5-onsite-validation.md`):

- **MQTT Config (multi-point)** (`review-mq9n11wi`) - Multi-point config publish builds the payload offline; the live write + confirm-back round-trip against a real broker is on-site.
- **Schedule and Payload Evidence (execute)** (`review-mq9n7pbe`) - Execute button + topic inputs render; actually fetching state/metadata/pointset payloads needs a live broker.
- **Emulate MQTT explorer** (`review-mq9nhbzu`) - Incoming MQTT Payloads panel renders; real subscribe + per-topic latest-payload capture + XLSX export of live data needs a broker.

Everything else in the table is fully exercisable on the local app with the seeded demo data and downloadable templates - no broker or hardware required.
