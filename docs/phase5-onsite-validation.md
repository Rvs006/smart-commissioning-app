# Phase 5 — On-Site Validation Checklist

Everything in Phases 0–4b is implemented and unit-tested, but every **live-network /
real-infrastructure path** was developed without access to building hardware, a
broker, Redis/Postgres, a remote hub, or a Docker daemon. This checklist is the
work that can only be done against real infrastructure. Run it on the first real
building (and a staging hub) before declaring the tool production-ready for
company-wide use.

Legend: ☐ = to verify · **STOP** = a failure here blocks production rollout.

> Safety first: active scanning (IP sweep, BACnet Who-Is) and live MQTT config
> publishing can disturb a live BMS/OT network. Do every "live" step first in
> **dry-run**, then on a **non-production / lab segment**, and only then on the
> real building with site authorization and a change window. See
> [security-posture.md](security-posture.md) and [runbook.md](runbook.md).

---

## 0. Pre-flight (off-site, before travelling)

- ☐ **On the technician laptop, on arrival (before any live action): run the pre-site preflight** — `scripts/smoke_local.sh --preflight http://127.0.0.1:8000` (bash) or `pwsh scripts/smoke_local.ps1 -Preflight -BaseUrl http://127.0.0.1:8000` (Windows). It does only SAFE checks (health/ready/metrics/config, cert-ref shape, **dry-run** IP + MQTT discovery, and a TCP-only broker reachability probe — no scan, no publish, no secret material printed) and exits non-zero on any failure. Also see the live-surface map in [phase5-live-surface-inventory.md](phase5-live-surface-inventory.md).
- ☐ `docker compose -f infra/docker-compose.yml build api worker frontend` succeeds on a machine with the Docker daemon running. **STOP** if images don't build — never validated here.
- ☐ `docker compose -f infra/docker-compose.yml config` renders with real `.env`; `${VAR:?}` guards fail fast when a required secret is missing.
- ☐ CI is green on the branch (push to the company remote first — see the PR). Confirm the `python`, `frontend`, and `sbom` jobs all run.
- ☐ Rebuild the Windows portable bundle from the **current** source (`packaging/windows_portable/`); confirm it ships `backend/`, `core/`, `frontend/dist/index.html`, **and the Alembic env** (`alembic.ini` + `alembic/versions/*.py`) so first launch migrates the bundled SQLite DB to head `d1f2a3b4c5d6` with no network. Alembic now ships via `core/pyproject.toml` `[tool.setuptools.data-files]` (a `versions/*.py` glob) — see the full rebuild + offline-smoke steps in [portable-bundle-rebuild.md](portable-bundle-rebuild.md) (the wheel build + wheel-only migrate are verified; the PyInstaller freeze is the on-site/release step). Code-sign the `.exe` to avoid SmartScreen/AV friction.
- ☐ Generate a fresh API key and Redis/Postgres passwords — run `sh scripts/bootstrap-env.sh` (Windows: `pwsh scripts/bootstrap-env.ps1`) to create `infra/.env` with crypto-random secrets (or fill each by hand with `openssl rand -hex 32`); never use the `.env.example` placeholders.

---

## 1. Hosted deployment bring-up (staging server)

- ☐ Bring up the compose stack with `AUTH_MODE=api_key`, real `API_KEY`, `CORS_ORIGINS`, `DATABASE_URL` (Postgres), `REDIS_URL` (with password).
- ☐ `GET /api/v1/health` → 200 (liveness, no deps).
- ☐ `GET /api/v1/ready` → ready, and the body reports **DB up** and **Redis up** per-dependency. **STOP** if `/ready` is ready while Redis is actually down — the real-Redis probe was never exercised here.
- ☐ Stop Redis → `/ready` reports `not_ready` for the broker (queue mode). Restart → recovers.
- ☐ Confirm `/ready` body contains **no** `redis://…@` credentials.
- ☐ `GET /metrics` returns Prometheus text **without** auth, contains `sct_http_requests_*` and `sct_runs_by_status`, and is **not** reachable from outside the internal network (bind/firewall it).
- ☐ `/docs`, `/redoc`, `/openapi.json` return **404** in `api_key` mode (schema not disclosed to unauthenticated clients).
- ☐ Alembic migrations applied automatically on first start (`AUTO_MIGRATE`), schema at head **`d1f2a3b4c5d6`** (`users_table_rbac`; the chain ends `…c998144d98d4 → d1f2a3b4c5d6`). Confirm `alembic current` (or the startup log) reports `d1f2a3b4c5d6 (head)` — an older bundle that stops at `c998144d98d4` is missing the RBAC `users` table and admin auth/last-admin guard will not work.
- ☐ Structured JSON logs carry `request_id`; a request's `X-Request-ID` is echoed and propagated. Worker logs share the same JSON shape and carry `run_id`.

## 1a. Postgres hub specifics

- ☐ The full app works on **Postgres** (everything here was proven on SQLite). Watch for: timestamp tz handling, JSON column behavior, the `BEGIN IMMEDIATE`/`SELECT FOR UPDATE` concurrency path (SQLite-specific code is bypassed on Postgres — confirm `update_result_summary` merges don't lose updates under concurrent worker+API writes). **STOP** on any lost-update under concurrency.

---

## 2. Worker / queue end-to-end (real Redis + Dramatiq)

- ☐ With `JOB_EXECUTION_MODE=queue` (or `auto`), enqueue a UDMI validation run → a **real worker process** consumes it from Redis and writes results. **STOP** if the worker never picks it up.
- ☐ Worker actor failures: kill a job mid-flight → confirm retry/backoff behaves and no run is stuck silently (the placeholder-actor "stuck forever" bug from the original audit must not recur).
- ☐ Redis restart with AOF: queued jobs survive a broker restart (appendonly on).
- ☐ Confirm the worker resolves MQTT broker settings from stored config on the worker path (`register_worker_mqtt_configuration_provider`, called at worker import in `worker/app/tasks.py`). Run params still take precedence.
- ☐ **Worker mutual-TLS via shared secrets volume** (now wired, conditional): mount the backend's `SMART_COMMISSIONING_SECRETS_ROOT` (and its `.secret_store_key`) into the worker and point the worker env at it. Confirm a `secret://` CA/client-cert/private-key ref is decrypted by the worker (`_resolve_secret` in `worker/app/mqtt_config_provider.py`) and the live TLS handshake uses real material — not silently empty. **STOP** if the worker advertises `secret://` refs it cannot resolve.
- ☐ Negative case: with the secrets volume **absent**, the worker registers no decrypting resolver, `secret://` refs resolve to nothing, and cert material must come from run parameters (plain paths the worker can read). This is the documented fallback, not a failure. (Resolver/materialization is unit-tested; the live broker handshake from the worker is on-site-untested — see §5.)

---

## 3. IP discovery (live site network) — SAFETY-CRITICAL

- ☐ **Dry-run first**: start an IP scan with `parameters.dry_run=true` → returns a target plan and opens **zero** sockets. Verify with a packet capture if possible.
- ☐ Authorization gate: a real (non-dry-run) scan **without** the authorization parameter → **403**. Only proceeds with `parameters.authorized=true` (or the `scan_authorization` audit form). Confirm the UI's "I am authorized to scan this network" checkbox is required.
- ☐ Throttle holds on a real network: `scan_max_concurrency` / `scan_rate_limit_per_sec` actually bound in-flight connections and packet rate. **STOP** if a scan saturates or destabilizes the field network.
- ☐ Request params can only **narrow** the throttle, never exceed the operator policy (clamp verified in code; confirm on real traffic).
- ☐ Cancel: a long scan stops promptly after `POST /runs/{id}/cancel` (cooperative — stops at the next batch boundary).
- ☐ Results: responsive hosts + open ports persist and render in the UI from real data (not sample rows).
- ☐ Reverse-DNS against the site resolver does not stall the scan.

## 4. BACnet discovery (real controllers) — SAFETY-CRITICAL & UNVALIDATED

- ☐ Install the optional stack: `pip install 'smart-commissioning-core[bacnet]'` (bacpypes3). Selecting the real backend without it raises a clear error (verified).
- ☐ **The entire `Bacpypes3Backend` was never run against hardware.** Every uncertain call is marked `# UNVERIFIED:` in `core/smart_commissioning_core/engines/bacnet_discovery.py`. Validate each against a **lab BACnet device first**:
  - ☐ Who-Is / I-Am round-trip returns real device instances. **STOP** & fix the adapter if the API shape differs from the documented bacpypes3 calls.
  - ☐ ReadProperty object-list and present-value reads work; chunking/segmentation handled.
  - ☐ Application construction/teardown leaks no sockets across runs.
- ☐ Dry-run performs **no** Who-Is broadcast.
- ☐ Authorization gate enforced (Who-Is broadcasts can disrupt fragile field buses).
- ☐ Only after lab validation: run on the real building in a change window.

## 5. MQTT discovery + UDMI (real broker)

- ☐ Connect to the real broker over the hand-rolled MQTT 3.1.1 client: **TLS**, username/password, and (if used) client-cert auth. The TLS/auth path was never exercised live. **STOP** on handshake/auth failures and fix before trusting results.
- ☐ Wildcard subscribe captures real topics/payloads within the bounded window; verify both `#` and a scoped `prefix/#` filter capture concrete broker publish topics, and topic/message counts render from real data.
- ☐ UDMI validation against **live** state/pointset capture (not just the bundled fixture): silent-device detection, units, schedule checks behave on real payloads.
- ☐ Broker credentials never appear in run `result_summary`, issue text, or logs (sanitization verified in tests — confirm on real broker errors).
- ☐ **`secret://` cert resolution at connect time (live)**: with config holding `secret://` CA/client-cert/private-key refs, confirm the handshake materializes them — CA in-memory (`load_verify_locations(cadata=…)`), client cert + key to transient 0600 temp files removed after the context is built (`MqttClient.__enter__`/`__exit__`). **STOP** on handshake/auth failure.
- ☐ **Indefinite "run until stopped" capture (worker-only)**: an MQTT discovery run with `capture_seconds=0` on the **worker** (`JOB_EXECUTION_MODE=queue`/Dramatiq) runs until Cancel or the `max_messages` cap (`result_summary.capture_mode == "indefinite"`, `indefinite_bounded_inline == false`; polls + re-checks cancel in 1s slices). The SAME run on the **inline/in-request** path (portable edge) is bounded to the default window and flags `indefinite_bounded_inline == true` so it cannot tie up the request worker.
- ☐ **Cancel/stop control**: while a long/indefinite capture runs, `POST /api/v1/runs/{run_id}/cancel` (engineer+; UI "Cancel run") stops it promptly mid-window and the run flips to `cancelled`. A viewer does not see the control and gets 403 if it calls cancel.

## 6. MQTT config publish + rollback (live gateways) — SAFETY-CRITICAL

- ☐ Validate-only path first (no publish) on real gateways.
- ☐ Live publish requires the publish-confirmation gate **and** scan authorization (the engine core self-enforces — verified). Confirm a publish cannot fire without both.
- ☐ **Multipoint confirm-back**: publish a config payload setting MULTIPLE points (`pointset.points.<name>.set_value`, or an explicit `expected_points` list). After the device's next pointset, confirm `result_summary.point_checks[]` reports per-point expected/observed/matched, with `expected_point_count`/`matched_point_count` correct and `partial_confirm` true when some-but-not-all match (status still `failed` on any mismatch, one `config_override_not_observed` issue per missed point). The legacy single-point summary fields must still populate for a single-point publish (back-compat). Rollback drops all forward expectations (`_suppress_expected_point_derivation`).
- ☐ Capture the gateway's **retained prior config** before publishing (live retained-value read was never exercised — confirm it actually captures).
- ☐ `POST /validation/mqtt-config/runs/{id}/rollback` republishes the captured prior value and restores the gateway. **STOP** if rollback does not cleanly restore — do not publish to production gateways until rollback is proven on a lab gateway.
- ☐ Change-window + approval process around any live publish to a real building.

---

## 7. Evidence integrity & reports

- ☐ Generate a report from real run records; download it. Generate in **each** format (`docx`, `xlsx`, `zip`) and confirm the XLSX opens in Excel/LibreOffice and is byte-reproducible (openpyxl/zip timestamps pinned).
- ☐ `GET /api/v1/evidence/reports/{id}/verify` → `hash_matches=true`, `signature_valid=true`, `key_matches_current=true`.
- ☐ Tamper a stored run record → verify reports the mismatch (don't trust by inspection — actually try it on staging).
- ☐ Reports are reproducible byte-for-byte from stored runs (deterministic generation).

## 8. Backup / restore / retention drill

- ☐ `python -m app.scripts.backup` produces a signed bundle (consistent SQLite snapshot + secrets + imports). For the Postgres hub, run the documented `pg_dump` procedure instead.
- ☐ Restore into a clean runtime: manifest signature + member hashes verified **before** any write; refuses to overwrite without `--force`; a tampered bundle is rejected; zip-slip member names are rejected.
- ☐ **Disaster drill**: simulate a lost engineer laptop — restore an edge from its last backup and confirm no evidence loss (define your RPO/RTO from [backup-restore.md](backup-restore.md)).
- ☐ Retention: `python -m app.scripts.retention` dry-run lists candidates and deletes **nothing**; apply requires explicit confirmation; runs linked to evidence packs are **never** deleted.

## 9. Edge → hub sync (real network + air-gapped)

- ☐ Enroll an edge: export its `edge_id` + public-key fingerprint, add to the hub's trusted-edges allowlist.
- ☐ **Online**: `python -m app.scripts.sync --hub-url …` pushes un-synced terminal runs over TLS with the edge API key; the hub ingests; runs are marked synced **only** after a confirmed accept. Verify via the hub's `GET /runs` filtered by `edge_id`/project/site. (Real network push never exercised — only TestClient.)
- ☐ **Air-gapped**: `--output runs.scbundle`, carry the file out, `python -m app.scripts.ingest runs.scbundle` at the hub. Confirm runs land and the edge is only marked synced after.
- ☐ Trust on the real hub: a bundle from an unenrolled edge → rejected, nothing written. A tampered bundle → rejected. A same-id run with changed content → `rejected_immutable`, hub copy unchanged.
- ☐ Hub multi-project view: runs from multiple edges aggregate and attribute correctly.
- ☐ Postgres-hub ingest under concurrency (two edges pushing the same new run id at once) does not double-insert.

---

## 10. Security & access (real deployment)

- ☐ Reverse proxy terminates **TLS** in front of api + frontend; only 443 is public. API/Postgres/Redis/metrics are **not** publicly reachable.
- ☐ `api_key` mode enforced; rotate the key per [runbook.md](runbook.md) §7 and confirm old key stops working.
- ☐ Secrets at rest: uploaded cert/key material is Fernet-encrypted on disk (0600); the secret-store key and the signing key are present, 0600, and **backed up** (lose them and evidence becomes unverifiable / secrets unreadable).
- ☐ `secret://` certificate references for **MQTT TLS** are materialized correctly at connection time (resolver wired in `backend/app/services/__init__.py` and `worker/app/mqtt_config_provider.py`; CA loaded in-memory via `load_verify_locations(cadata=…)`, client cert/key via transient 0600 temp files cleaned up after the handshake). The wiring is implemented and unit-tested; verify only the **live handshake** against the real broker (cross-ref §5). Confirm no `mqtt-tls-*.pem` temp files leak in `TMPDIR` across runs.
- ☐ **RBAC last-admin self-lockout guard**: in `api_key` mode with real user rows, deactivating (`POST /users/{id}/deactivate`) or demoting (`POST /users/{id}/role`) the **last active admin user row** returns **409** and changes nothing (`LastAdminError` / `UserRepository.count_active_admins`). Recovery note: the guard counts only `users`-table rows — the shared-key / `AUTH_MODE=local` bootstrap admin is NOT counted and is the **lockout-recovery path** if all admin rows are lost. Document that recovery in the runbook so a site cannot lock itself out.
- ☐ Per-project/site scoping: two engineers on different sites do not clobber each other's configuration (the original global-config bug must be gone).

## 11. Frontend (against the real backend)

- ☐ Login/key entry works; 401s surface the auth message, not a blank screen.
- ☐ Discovery/validation tables show **real** data after runs; anything still labelled "sample preview" is clearly marked and not mistaken for live results.
- ☐ SSE live progress works through the reverse proxy (nginx buffering off for `text/event-stream`); on SSE failure it falls back to 1.5s polling with no regression.
- ☐ Authenticated downloads (reports/templates, and the MQTT capture **"Export to XLSX"/"Export to CSV"** controls) work in `api_key` mode (they go through `fetch`+blob, not bare links).
- ☐ Cancel and config-rollback controls work end-to-end.

---

## Go / No-Go

Production-ready for a site when: §0–2 pass, the **safety-critical** drills (§3 throttle/authorize/cancel, §4 lab-validated BACnet, §6 lab-validated rollback) pass, §7–9 evidence+backup+sync round-trip on real infra, and §10 TLS/secrets/scoping hold.

**Do not** run active scans, live config publishes (incl. multipoint), or indefinite MQTT captures on a production building until the dry-run, authorization, throttle, cancel, and rollback behaviors are each confirmed on a lab/non-production segment first. Run the §0 preflight on arrival.
