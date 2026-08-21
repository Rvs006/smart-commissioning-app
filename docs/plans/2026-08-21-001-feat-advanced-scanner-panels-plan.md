---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
title: "Advanced scanner panels: embed the vendored standalone scanner apps behind an SCT reverse proxy"
type: feat
date: 2026-08-21
baseline: 96979fd82b5ad1dc0a977df4b0422bf4dd129039
---

# Advanced scanner panels (IP / BACnet / MQTT raw embed)

## Goal capsule

Add an **Advanced** step to each of the three sidecar scanner modules
(`ip-scanner`, `bacnet-scanner`, `mqtt-scanner`) that embeds the vendored
standalone scanner app's OWN web UI, running inside SCT, with SCT staying on
top of it. 100% of the standalone functionality — current and future drops —
becomes available inside SCT without rebuilding each feature natively, while
SCT keeps role gating, write safety, and evidence.

The vehicle is a **reverse proxy** inside the backend: the browser iframe talks
only to SCT; SCT forwards to the loopback sidecar. The proxy is the single
choke point where SCT enforces roles, applies the write-safety stack, and
records evidence. The vendored trees stay wholesale-replaceable
(`scanners/REIMPORT.md` contract), with one new deterministic vendor-time
transform.

### Product decisions (fixed inputs, build to these)

1. All three protocols get the Advanced panel in ONE build (not one protocol
   per release).
2. Evidence is BOTH: the panel is the raw standalone tool AND SCT records
   evidence of what happens in it — scans, browses, and especially writes —
   into SCT's run/evidence store.
3. Writes (MQTT `/api/publish`, MQTT `/api/config` — a retained write to a
   device's config topic — and any future BACnet point override/relinquish
   endpoints) get the full safety stack: a per-session one-time
   acknowledgment plus a per-write confirm dialog showing the exact
   topic/point and value, and are recorded as evidence. Reads/scans/browse
   stay free-flowing: role check only, no ceremony. Reuse and align with the
   existing frictionless mode
   (`SCT_REQUIRE_SCAN_AUTHORIZATION`, `core/smart_commissioning_core/engines/safety.py:58-71`;
   portable ships `0` via `packaging/windows_portable/run_smart_commissioning_app.py:294`).
   Do NOT reintroduce the sealed two-person preview ceremony for this panel.
4. SCT stays "on top" via the reverse proxy — never by modifying the vendored
   app's behavior at runtime and never by letting the browser reach the
   sidecar port.

### Out of scope

- The native parity backlog (IP timeout/concurrency inputs, BACnet range and
  window inputs, richer native results, etc.). The Advanced panel supersedes
  most of it; anything still wanted natively is a separate plan.
- Replacing or changing the native lanes: the sidecar run routes
  (`backend/app/api/routes/scanners.py`), the MQTT live tree
  (`MqttLiveTopicTree.tsx` / `useMqttLiveSession.ts`), and the sealed
  one-message publish (`scanners_mqtt_live.py:453-611`) all stay exactly as
  they are. The Advanced panel is in ADDITION to them.
- Release machinery, packaging scripts, and the gated release apparatus.
- Any database migration (none is needed; all new evidence uses the existing
  runs store).

---

## Architecture

### The proxy surface

One new protected router, mounted at a fresh prefix that collides with
nothing (existing sidecar routes live under `/discovery`,
`backend/app/api/router.py:99-102`):

```
/api/v1/scanners/{proto}/raw/…        proto in {ip, bacnet, mqtt}
```

- `GET  /api/v1/scanners/{proto}/raw/` -> the vendored `public/index.html`
  (rewritten, see below); `/raw` without slash 307s to `/raw/` so relative
  URL resolution works.
- `GET  /raw/{asset}` -> `public/app.js`, `public/styles.css` (static relay).
- `ANY  /raw/api/{path}` -> forwarded to
  `supervisor.base_url_for(name)` + `/api/{path}`
  (`backend/app/services/sidecar_supervisor.py:127-129`), streaming request
  and response bodies. Must support: plain JSON GET/POST/DELETE, SSE **GET**
  (`/api/scan`, `/api/objects`, `/api/stream`), SSE **POST** (the BACnet
  `/api/export` streams events from a POST,
  `scanners/vendor/bacnet-scanner/server.js:488-493`), and file downloads
  with `Content-Disposition` (`/api/template`, `/api/export`,
  `/api/export-archive`, `/api/generate-register`).
- `GET /raw/sct-bridge.js` -> a small SCT-owned script (served from a Python
  string constant; no packaged data file), inert until M4.
- Control endpoints registered BEFORE the catch-all (FastAPI matches in
  registration order): `POST /raw/session`, `POST /raw/session/write-ack`,
  `POST /raw/confirm-write`.

The relay copies the proven SSE relay pattern from
`backend/app/api/routes/scanners_mqtt_live.py:396-446` (`_live_relay`):
httpx streaming client behind a module-level `_stream_client()` seam for
tests (`:92-94`), wall-clock cap (`MAX_STREAM_SECONDS`, `:79`), periodic
scope recheck, cancel-safe `asyncio.CancelledError` exit, honest terminal
control frames, never fabricating data after an upstream failure. Sidecar
unavailable -> 503 with the same vocabulary the run routes use
(`SidecarUnavailable` -> 503, `scanners.py:153-160`).

### Auth and the panel session

SCT auth is header-based (`X-API-Key` / Bearer,
`backend/app/core/auth.py:101-111`) and the vendored `app.js` sends no
headers, so iframe subresource requests cannot present a key. Two facts make
this tractable:

- In `local` mode (the portable default) every loopback request already
  resolves to a principal (`auth.py:244-247`), so the iframe works with zero
  extra machinery.
- In `api_key` mode the panel needs a browser-native credential: a cookie.

Design: `POST /api/v1/scanners/{proto}/raw/session` (engineer-gated with the
caller's normal header auth) validates project/site scope
(`require_project_site_access`), applies the read-consent gate below, then
returns a panel-session id AND sets an opaque `HttpOnly` cookie
(`SameSite=Strict`, `Path=/api/v1/scanners/`) that maps, in an in-process
table on `app.state`, to `{username, role, project_id, site_id, expiry}`.
Every proxied request authenticates via EITHER the normal header/loopback
principal OR a live panel cookie, then requires `Role.ENGINEER`
(`require_role`, `auth.py:278-297`; matches every sidecar lane). No key ever
rides a query string (the events route's explicit rule,
`backend/app/api/routes/events.py` header). In-process, unsigned, opaque
tokens are enough: sidecars themselves are process-scoped, and the panel
simply re-probes `/session` on 401.

Read consent, aligned with the frictionless mode (decision 3): session
creation applies the SAME legacy gate live sidecar scans use today
(`_require_legacy_scan_authorization`,
`backend/app/api/routes/discovery.py:348-354`) — once per panel session, not
per action. When `authorization_enforced()` is false (portable) that is a
no-op and reads are literally role-check-only; when enforced, the Advanced
tab shows the one consent checkbox before the iframe loads, recorded with
the session. No sealed preview, no approval object, ever, on this surface.

### The `/api` absolute-path problem and the chosen fix: vendor-time rewrite

All three vendored UIs call the API with absolute paths — e.g.
`fetch('/api/adapters')`
(`scanners/vendor/network-ip-scanner/public/app.js:50`),
`new EventSource('/api/scan?…')` (`…/app.js:177`),
`new EventSource('/api/stream')`
(`scanners/vendor/mqtt-discovery/public/app.js:74`), a template-literal href
`` `/api/export?asset=…` `` (`…/app.js:317`), and
`<a href="/api/template" download>` in each `public/index.html` (ip:61,
bacnet:62, mqtt:76). Inside an iframe served from
`/api/v1/scanners/ip/raw/`, those resolve to the SCT origin root and hit
SCT's own `/api/*`, not the proxy.

**Decision: rewrite at vendor time, not in the proxy.** A deterministic
script (`scripts/rewrite_vendored_scanner_embed.py`) applied to
`scanners/vendor/*/public/*.{js,html}`:

1. `'/api/` -> `'api/`, `"/api/` -> `"api/`, `` `/api/ `` -> `` `api/ ``
   (relative paths resolve against the document URL, which is why `/raw/`
   keeps its trailing slash).
2. Insert `<script src="sct-bridge.js" defer></script>` into each
   `index.html` (inert until M4; a 404 for it when the app runs standalone
   outside SCT is harmless).
3. **Re-run `node build-bundle.js` for `network-ip-scanner` and
   `bacnet-scanner`**: their committed `dist/bundle.js` embeds `public/*` as
   base64 (`build-bundle.js` walks `public/`), and the supervisor PREFERS
   the bundle when present (`sidecar_supervisor.py:77-82`) — rewriting only
   the loose files would silently serve the old UI. `mqtt-discovery`'s
   `dist/` is gitignored and rebuilt by the portable build
   (`scanners/REIMPORT.md:80-82`), so its source rewrite is sufficient.

Why vendor-time and not a proxy-side response rewriter:

- The transform is reviewable in the vendor diff and PINNED by a contract
  test that reads the vendored files from disk (the exact golden-fixture
  style of `backend/tests/test_ip_scanner_contract.py`), so a future drop
  that reintroduces absolute paths fails CI naming the seam — the
  REIMPORT.md philosophy. A runtime rewriter fails silently on a new quoting
  pattern.
- The proxy stays a dumb byte pipe: no buffering or content transformation
  on static, SSE, or zip-download responses.
- Precedent: REIMPORT.md's SCRUB step (`REIMPORT.md:36-67`) already
  transforms the tree before vendoring. This adds one more mechanical,
  scripted step of the same kind. (The "never hand-edit vendor/" rule is
  about hand-merging upstream code; a scripted, tested, re-applied transform
  is the opposite of that.)

Cost accepted: the step MUST be re-applied on every future re-import.
Mitigations: it is one script invocation added to the REIMPORT.md checklist,
and the contract test makes forgetting it a red CI, not a silent break. Also
add a standing-agreement request (`REIMPORT.md:121-129`) that upstream move
to relative `api/` paths at source, which turns the rewrite into a no-op.

### The evidence tap (decision 2: "both")

The proxy records evidence as **terminal run rows** in the existing runs
store — no new tables, no migration:

- `scanner_raw_action` — evidence-worthy reads: scan / objects-browse /
  compare / connect / disconnect / subscribe / register import-clear /
  exports. Created after the sidecar responds; status mirrors the outcome.
  For SSE actions the relay records at stream end: endpoint, query scope
  (e.g. the scan's start/end range — the sidecar reads them from the query,
  `scanners/vendor/network-ip-scanner/server.js:371-377`), duration, frame
  count, and whether the client cancelled. For downloads the relay hashes
  bytes in transit and records sha256 + size + filename — same
  hash-binding ethos as reports.
- `scanner_raw_write` — guarded writes (M4), carrying operator, protocol,
  endpoint, exact topic (for `/api/config` the sidecar's response contains
  the RESOLVED topic — `mqtt-discovery/server.js:489` returns `{ok, topic}`
  — record both requested and resolved), payload up to the 64 KiB publish
  cap (the `_PUBLISH_PAYLOAD_MAX_BYTES` precedent,
  `scanners_mqtt_live.py:454-455`) plus sha256/byte-count, qos, retain, the
  session-ack reference, and the confirm-token id.

Not recorded (noise, no evidence value): `health`, `status`, `stream`
snapshots, `search`, `focus`, `adapters`, `template`, plain `GET register`.
A single classification map (one dict per protocol in a pure-helper module)
decides lane and record-flag per METHOD+path; it is the contract-tested
seam (below). Redaction before persisting: the `/api/connect` body carries
broker `username`/`password` (`mqtt-discovery/server.js:356`) — mask values
under credential-shaped keys, same list as
`backend/app/services/log_service.py` (`password`/`token`/`secret`/…).

New `job_type` literals touch `backend/app/schemas/jobs.py` and the frontend
`JobType` union in `frontend/src/api/client.ts`; run history renders them
like any run. They are strings in the runs table — no migration.

Honesty boundary, stated in code comments and docs: the proxy records what
crosses the wire. Client-side-only interactions inside the vendored UI
(sorting, expanding a tree node, reading a payload already streamed) leave
no wire trace and are NOT claimed as evidence.

### The write guard (decision 3)

Enforcement lives in the proxy; UX lives in SCT; the vendored app is not
modified beyond the injected bridge script tag.

- Classification fail-closed rule: `GET`/`HEAD` -> read lane; any other
  method -> **write lane unless explicitly allowlisted as a read-action**
  (the known POST reads: `compare`, `register`, `search`, `focus`,
  `subscribe`, `connect`, `disconnect`, and BACnet's POST `export`).
  Consequence: when a future drop adds a BACnet override/relinquish
  endpoint, it is automatically write-guarded on day one; the worst
  misclassification is ceremony on a new read, never freedom on a new
  write.
- Device-write list today: MQTT `POST /api/publish`
  (`mqtt-discovery/server.js:470-476`) and `POST /api/config`
  (`server.js:479-490`; note its defaults — retain=true, qos=1 — the
  confirm dialog must show them).
- Flow: `sct-bridge.js` wraps `window.fetch`; on a write-lane request it
  `postMessage`s the parent with `{method, path, body}`. The
  `AdvancedScannerPanel` shows (first time per session) the one-time write
  acknowledgment, then the per-write confirm dialog rendering the exact
  topic/point and value from the intercepted body. On confirm, SCT calls
  `POST /raw/confirm-write`, which mints a single-use token bound to
  `sha256(method | path | body)` with a short TTL; the bridge re-sends the
  original request with `X-SCT-Write-Confirm: <token>`; the proxy verifies
  session write-ack + token + hash before forwarding, then records the
  `scanner_raw_write` run. Hash binding means the dialog cannot show one
  payload while different bytes go to the wire — the sealed-publish idea's
  one good trick, without its two-person ceremony.
- Fail closed: no bridge, broken bridge, missing ack, missing/stale/reused
  token, or hash mismatch -> 403 with a plain-language detail the vendored
  UI surfaces as its normal error toast. From M2 (before the guard exists)
  writes are 403'd outright with an honest "write actions arrive in a later
  milestone" detail — the panel never silently forwards a write.

### Single-tenant arbitration

The MQTT sidecar holds ONE broker connection, already arbitrated between the
capture run and the native live session under one lease lock
(`scanners_mqtt_live.py:175-195`, `scanners.py:295-306`). The raw panel's
`/api/connect` joins that arbitration by REUSING the lease: the proxy
acquires `mqtt_live_session.service` on proxied connect (owner = panel
operator) and releases on disconnect/session expiry — the native UI's
"session held by…" surfaces then tell the truth about the raw panel too.
For IP/BACnet, proxied `/api/scan` gets a cheap 409 when a native sidecar
run of the same protocol is queued/running (`RunService.list_runs`,
`run_service.py:1425`), mirroring the existing MQTT check. The reverse
interleaving (native run starting mid-raw-scan) is accepted residual risk —
the BACnet client is invoke-id multiplexed and protocol-safe
(`scanners.py:376-379`), and the failure mode is a confusing scan result,
not damage.

### Sidecar token (considered, deferred to upstream)

Today the sidecar has no auth; isolation is loopback + random port known
only to the backend. A shared-secret header (supervisor generates per boot,
passes via env, sidecar enforces) would close the "any co-resident local
process can drive the sidecar" gap — but requires vendored `server.js`
cooperation, i.e. an upstream change, not a local edit (REIMPORT.md
never-hand-edit rule; also `auth.py:244-247` already trusts co-resident
loopback processes as ADMIN on the SCT API in local mode, so the token only
buys real ground in `api_key` deployments). M5 adds it to the standing
agreements as a requested upstream feature with supervisor-side support
ready behind "enforce if the health probe advertises token support". Not a
blocker for any milestone.

---

## Milestones

Ordered by risk: the rewrite + SSE-through-proxy is the existential unknown,
so it ships first on one protocol.

### M1 — Reverse proxy core + Advanced tab for IP (reads only)

**Scope.** Prove the embed end to end on the simplest protocol (no writes,
no broker state, still exercises SSE scan, JSON POST compare, register
CRUD, and a file download):

- `scripts/rewrite_vendored_scanner_embed.py` + run it on all three vendor
  trees (rewrite + bridge tag + rebundle ip/bacnet) — one commit, SCRUB-style.
- `backend/app/api/routes/scanner_raw.py`: session endpoint (cookie +
  read-consent alignment), static relay, API relay for `proto=ip` only,
  SSE relay, download relay, 503/502 honesty. Mount at
  `prefix="/scanners"` in `backend/app/api/router.py` (protected router,
  after `:99-102`).
- `backend/app/services/scanner_raw_policy.py`: proto->sidecar-name map,
  path classification (read/write/blocked + record flag), redaction
  helpers, hash helpers — pure functions.
- Frontend: `frontend/src/features/workflow/AdvancedScannerPanel.tsx`
  (iframe + session probe + honest unavailable/sidecar-down/consent
  states); extend `ModuleStep` (`ModulePage.tsx:298`), `StepNav`
  (`ModulePage.tsx:8251-8286` — Advanced renders as a trailing tab outside
  the numbered done-sequence), and gate on the sidecar routes only
  (predicate as at `ModulePage.tsx:619-621`; NOT the `-sct` engine routes).
- Verify no CSP/`X-Frame-Options` on the backend blocks same-origin
  framing; the Vite dev proxy already forwards `/api` (SSE included, per
  the existing fetch-SSE lanes).

**Likely files.** `backend/app/api/routes/scanner_raw.py` (new),
`backend/app/services/scanner_raw_policy.py` (new),
`backend/app/api/router.py`, `scripts/rewrite_vendored_scanner_embed.py`
(new), `scanners/vendor/*/public/*` + two `dist/bundle.js` (generated),
`frontend/src/features/workflow/AdvancedScannerPanel.tsx` (new),
`frontend/src/features/workflow/ModulePage.tsx`.

**Tested seam.** Repo convention: sidecar-driving routes get no TestClient
run coverage (`backend/tests/test_bacnet_object_browse_route.py:1-13`), but
the proxy's sidecar seam is mockable exactly like the live-session suite
(`backend/tests/test_mqtt_live_session_api.py:1-8`, httpx.MockTransport
behind `_stream_client`): so (a) pure-helper unit tests for
classification/redaction/hashing (`test_scanner_raw_policy.py`), (b) an
API suite with mocked transport for auth/cookie issuance, consent gating,
forwarding, SSE relay frames, download hashing, and 503s
(`test_scanner_raw_api.py`), (c) the embed contract test
(`test_scanner_raw_embed_contract.py`) reading vendored `public/*` AND
base64-decoding the committed bundles' embedded assets to assert no
quote-adjacent `/api/` remains and the bridge tag exists, (d)
`AdvancedScannerPanel.test.tsx` for tab gating and state rendering.

**Exit.** The full standalone IP UI runs inside the SCT Advanced tab —
adapters load, a scan streams live over SSE, compare works, the template
downloads — in local mode and in `api_key` mode (cookie), with engineer
role enforced and everything green in CI.

### M2 — All three protocols, writes hard-blocked

**Scope.** Make the proxy table-driven across `{ip, bacnet, mqtt}`:
BACnet's POST-SSE `/api/export` and `/api/objects` stream; MQTT's
`/api/stream` long-SSE, zip `export-archive`, and connect/disconnect flow
through — with proxied MQTT connect acquiring/releasing the existing live
lease and both directions of the run-vs-panel 409 arbitration; IP/BACnet
raw scans 409 against a running native run of the same protocol. All
write-lane requests (publish/config/unknown non-GET) return 403 with an
honest "arrives in a later milestone" detail.

**Likely files.** `scanner_raw.py`, `scanner_raw_policy.py`,
`backend/app/services/mqtt_live_session.py` (consume only; new holder
metadata if a label is needed), `AdvancedScannerPanel.tsx`,
`ModulePage.tsx`.

**Tested seam.** Policy-map completeness (every literal endpoint parsed
from the three vendored `server.js` route switches — `network-ip-scanner`
:336-431, `bacnet-scanner` :362-544, `mqtt-discovery` :435-528 — must be
classified; a NEW upstream endpoint fails the test and names the seam);
lease acquire/release around mocked connect/disconnect; 409 arbitration
both ways; POST-SSE relay; write-block 403s.

**Exit.** All three Advanced panels are fully functional read-side; no
write can cross the proxy; native lanes still pass their existing suites
untouched.

### M3 — Evidence tap

**Scope.** Record `scanner_raw_action` runs per the classification map's
record flags: JSON actions post-response; SSE actions at stream end
(duration, frames, cancelled flag, query scope); downloads with in-transit
sha256 + size; `/api/connect` bodies redacted. Add the `job_type` literals
(`backend/app/schemas/jobs.py`, `frontend/src/api/client.ts`) and confirm
run history renders them sanely. Runs carry the panel session's
project/site so scoping and retention behave like every other run.

**Likely files.** `scanner_raw.py`, `scanner_raw_policy.py`,
`backend/app/services/run_service.py` (consume: `create_job_run` :452,
`update_run_status` :1755, `update_result_summary` :1901),
`backend/app/schemas/jobs.py`, `frontend/src/api/client.ts`,
`frontend/src/features/workflow/RunHistoryPage.tsx` (labels only, if
needed).

**Tested seam.** Policy: which actions record and what the summaries
contain (pure); API suite: a mocked scan stream produces exactly one
terminal `scanner_raw_action` run with honest fields, a cancelled stream
records `cancelled`, a redacted connect body never persists a password,
a download run carries the hash of the exact bytes relayed.

**Exit.** An operator's raw-panel scans/browses/exports appear in SCT run
history as terminal runs with honest summaries; nothing records for
look-only panel use; no password material persists anywhere.

### M4 — Write guard (publish / config / future overrides)

**Scope.** The full stack from the architecture section: `sct-bridge.js`
content (fetch wrapper + postMessage protocol), `POST /raw/session/write-ack`,
`POST /raw/confirm-write` (single-use hash-bound token, short TTL),
proxy-side enforcement order (engineer role -> session write-ack -> valid
token -> hash match -> forward -> record `scanner_raw_write`), and the SCT-side
dialogs in `AdvancedScannerPanel.tsx`: the one-time acknowledgment and the
per-write confirm showing exact topic/point + value (+ the config lane's
retain=true/qos=1 defaults). MQTT publish and config become usable; the
unknown-non-GET fail-closed default stays.

**Likely files.** `scanner_raw.py`, `scanner_raw_policy.py`, a
`scanner_raw_bridge.py` string-constant module (new),
`AdvancedScannerPanel.tsx` + test, `ModulePage.tsx` (none or minimal).

**Tested seam.** Token lifecycle pure tests (mint/verify/expire/single-use/
hash-mismatch); API suite: write without ack -> 403, with ack but no token ->
403, tampered body vs token hash -> 403, happy path forwards exactly once
and records evidence with resolved topic + payload hash; bridge protocol
covered by frontend tests (postMessage in/out, dialog content equals
intercepted body). The bridge JS itself stays too small to need its own
harness — the proxy is the enforcement, and it is fully tested.

**Exit.** An operator can publish and write config from the raw MQTT panel
only through ack + exact-value confirm, each write lands as a
`scanner_raw_write` run, and a synthetic "future override" endpoint (an
unclassified POST in a test fixture) is automatically guarded.

### M5 — Hardening, re-import contract, docs

**Scope.**
- `scanners/REIMPORT.md`: add the "EMBED REWRITE" checklist step (run the
  script, rebundle, run the embed contract test) and two standing-agreement
  asks: relative `api/` paths at source; optional shared-token support
  (supervisor side ready, enforced only when the health probe advertises
  it).
- Panel-session expiry sweep + explicit close on logout; cookie attributes
  reviewed; relay limits (max concurrent panel streams, reusing the
  live-session stream-attach idea).
- `CHANGELOG.md` entry; `docs/review-guide.md` / `AGENTS.md`+`CLAUDE.md`
  handoff touch-up (version bookkeeping convention only when a release
  happens); a short operator note that the Advanced panel is the raw tool
  with its own look (the two-look UX is accepted, not a bug).
- Full-suite pass and `scripts/check_public_hygiene.py` clean.

**Likely files.** `scanners/REIMPORT.md`, `scanner_raw.py`,
`sidecar_supervisor.py` (env pass-through only, if the token lands
upstream), `CHANGELOG.md`, `docs/review-guide.md`.

**Tested seam.** The embed + endpoint-classification contract tests are the
long-term gate; expiry sweep unit test; hygiene script in CI as today.

**Exit.** A future upstream drop either passes cleanly through the
documented re-import (rewrite step included) or fails CI with a message
naming the exact seam that moved.

---

## Risks

1. **Rewrite fragility + stale committed bundles (highest).** The rewrite
   is a textual transform of upstream code; a future drop using a new
   pattern (`fetch(base + '/api/…')`, URL constructor) slips past it — and
   `network-ip-scanner` / `bacnet-scanner` serve their committed
   `dist/bundle.js` in preference to `public/`
   (`sidecar_supervisor.py:77-82`), so a rewrite without a rebundle LOOKS
   done and isn't. Mitigation: the contract test asserts on BOTH the loose
   files and the decoded bundle assets, and the standing agreement pushes
   the fix upstream (relative paths at source).
2. **SSE through two proxies.** Sidecar -> backend relay -> (dev: Vite proxy)
   -> iframe. Buffering, idle timeouts, and cancel propagation are the
   classic failure points; the BACnet POST-that-streams is the odd one.
   Mitigation: copy `_live_relay`'s cancel-safety and cap wholesale; M1
   proves the worst case (long IP scan) before anything else builds on it.
3. **Evidence fidelity is wire-level only.** The proxy cannot see
   client-side interactions, and SSE summaries are metadata, not full
   captures. If the field expectation drifts toward "replayable session
   recording", this design under-delivers. Mitigation: state the boundary
   in the panel UI and docs; record request scope + response
   hashes/counts, which is the same standard the native lanes meet.
4. **Frictionless-vs-evidence tension.** Reads are deliberately
   ceremony-free (portable), yet still generate run rows; a chatty operator
   session adds noise to run history. Mitigation: record only the
   classified evidence-worthy actions; revisit the record-flag list after
   first field use.
5. **Single-tenant sidecars.** One broker connection (MQTT), one in-memory
   register per sidecar; the raw panel and native lanes share them.
   Mitigation: lease reuse + 409 arbitration (M2); residual interleavings
   are documented as honest-confusion, not damage.
6. **Two-look UX inside the tab** — accepted by product decision. The panel
   frame labels it as the raw standalone tool; no re-theming of vendored
   CSS.
7. **`api_key`-mode cookie surface.** A new browser-credential path needs
   care: `HttpOnly`, `SameSite=Strict`, path-scoped, opaque, in-process,
   engineer-gated issuance, and no key material in URLs. It grants access
   only to the proxy prefix.

## Constraints and conventions

- **Public repo hygiene**: no site names, personnel, real addresses, device
  ids, broker hosts, or MACs anywhere in this work — including this plan,
  test fixtures (use documentation ranges), and commit messages.
  `scripts/check_public_hygiene.py` gates CI.
- **Conventional Commits**; log notable changes in `CHANGELOG.md`. One
  vendor transform = its own `chore(scanners):` commit, per REIMPORT.md.
- **Smallest correct change, reuse first**: the relay copies
  `scanners_mqtt_live.py` patterns; consent reuses
  `_require_legacy_scan_authorization`; evidence reuses the runs store;
  arbitration reuses the live lease. No new dependencies; httpx and stdlib
  only.
- **CI** (`.github/workflows/ci.yml`): ruff + core/backend `unittest` +
  frontend lint/typecheck/test/build run on PRs and pushes to `main` —
  land each milestone via PR so the full matrix gates it. Keep alphabetical
  test collection order working.
- **Do not touch** the release/packaging apparatus; the portable build's
  existing bundle regeneration already covers `mqtt-discovery`.
- **Model routing**: this plan was authored on Fable
  (`claude-fable-5`); implement on Opus 4.8 (`claude-opus-4-8`) per the
  repo convention.
