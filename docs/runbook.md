# Operations Runbook

Deploy, operate, and recover the Smart Commissioning App. This runbook is
specific to the two deployment profiles this repository actually ships:

- **Hosted profile** — Docker Compose (`infra/docker-compose.yml`): nginx
  frontend, FastAPI API, Dramatiq worker, Postgres, password-protected Redis.
  Use when several users share a server.
- **Edge / portable profile** — the portable Windows executable built from
  `packaging/windows_portable/`: binds `127.0.0.1` only, uses SQLite, runs jobs
  inline, defaults to `AUTH_MODE=local` (no API key). Use on a technician
  laptop near the site network.

See `infra/README.md` for the compose quickstart this runbook builds on, and
`docs/production-architecture.md` for the system model.

## 1. Deploy

### Hosted (Docker Compose)

```sh
# from the repo root — fills every CHANGE_ME with a crypto-random secret,
# prints the API_KEY, refuses to overwrite an existing infra/.env
sh scripts/bootstrap-env.sh        # Windows: pwsh scripts/bootstrap-env.ps1
docker compose -f infra/docker-compose.yml --env-file infra/.env up -d --build
```

Manual fallback: `cp infra/.env.example infra/.env` and fill every `CHANGE_ME`
with `openssl rand -hex 32` (see the env table below).

Startup is ordered by healthchecks: Postgres and Redis must report healthy
before the API starts; the **API applies Alembic migrations on startup** (it
owns the schema); the worker waits for the API so the schema exists before it
picks up jobs. The worker never runs migrations.

Open the app at `http://127.0.0.1:8080` (or your `FRONTEND_PORT`). Every
published port binds to loopback only — to expose the app beyond the host, put
a **TLS-terminating reverse proxy** in front of the frontend port. The app does
not terminate TLS itself.

### Edge (portable Windows executable)

The portable build under `packaging/windows_portable/` runs uvicorn bound to
`127.0.0.1`, uses a local SQLite database, executes jobs inline (no
Redis/worker), and defaults to `AUTH_MODE=local`. No `.env` is required for a
single-operator laptop; the SQLite file and secret material live under
`%LOCALAPPDATA%\SmartCommissioning` (overridable via
`SMART_COMMISSIONING_DATA_DIR`; dev checkouts keep `<repo>/runtime`), so they
survive upgrading to a new release folder. Migrations auto-run on first start.

## 2. Required environment

The hosted profile reads these (compose fails fast via `${VAR:?}` when a
required one is missing). The edge profile uses the built-in defaults from
`backend/app/core/config.py` and needs none of them.

| Variable | Required (hosted) | Purpose | Default |
| --- | --- | --- | --- |
| `POSTGRES_DB` | yes | Database name shared by api + worker. | `smart_commissioning` (example) |
| `POSTGRES_USER` | yes | Database role. | `smart_commissioning` (example) |
| `POSTGRES_PASSWORD` | yes | Postgres password. `openssl rand -hex 32`. | — |
| `REDIS_PASSWORD` | yes | Redis `requirepass`. `openssl rand -hex 32`. | — |
| `API_KEY` | yes (api_key mode) | Shared key clients send when `AUTH_MODE=api_key`. `openssl rand -hex 32`. | — |
| `AUTH_MODE` | — | `api_key` (compose default) or `local` (edge default). | `local` (code default); compose sets `api_key` |
| `CORS_ORIGINS` | — | Comma-separated browser origins for direct cross-origin API access. Same-origin traffic via the nginx `/api` proxy needs no CORS. | `http://localhost:5173,http://127.0.0.1:5173` (code); compose sets `http://127.0.0.1:8080,...` |
| `DATABASE_URL` | assembled | `postgresql+psycopg://...`. **Assembled inside compose** from `POSTGRES_*`; set explicitly only outside compose. | SQLite under the app data dir (edge; `%LOCALAPPDATA%\SmartCommissioning` when frozen) |
| `REDIS_URL` | assembled | `redis://:<pw>@redis:6379/0`. **Assembled inside compose** from `REDIS_PASSWORD`. | `redis://localhost:6379/0` (code) |
| `AUTO_MIGRATE` | — | API applies Alembic migrations on startup. Set `false` only if migrating out of band. | `true` |
| `JOB_EXECUTION_MODE` | — | `auto` / `queue` / `inline`. `inline` skips Redis. | `auto` |
| `FRONTEND_PORT` / `API_PORT` | — | Loopback host ports. | `8080` / `8000` |
| `LOG_LEVEL` | — | Root log level for the JSON logger. | `INFO` |

`DATABASE_URL` and `REDIS_URL` are **not** defined separately in `.env`; they
are composed inside `docker-compose.yml` from the `POSTGRES_*` / `REDIS_PASSWORD`
values so each secret lives in exactly one place.

Auth model (enforced by `backend/app/core/auth.py`):

- **`api_key`** — every `/api/v1` request (except health) must present the key
  via `X-API-Key` or `Authorization: Bearer <key>`. With no key configured the
  API **fails closed** (rejects everything).
- **`local`** — only loopback clients are accepted; if an `API_KEY` is *also*
  set, a valid key is accepted from any address.

## 3. First start

1. Bring the stack up (section 1). The API's lifespan handler installs the JSON
   log formatter, warns if `AUTH_MODE=api_key` but `API_KEY` is unset, and — when
   `AUTO_MIGRATE=true` — ensures the runtime directories exist and runs
   `upgrade_to_head` (Alembic) against `DATABASE_URL`.
2. Verify health (section 4).

## 4. Health, readiness, and metrics checks

```sh
# Liveness (cheap, no dependency I/O — answers whenever the process is up)
curl http://127.0.0.1:8080/api/v1/health

# Readiness (probes the dependencies this deployment actually needs)
curl http://127.0.0.1:8080/api/v1/ready

# Container healthcheck status per service
docker compose -f infra/docker-compose.yml ps

# Prometheus metrics (app level, NOT under /api/v1; unauthenticated)
curl http://127.0.0.1:8080/metrics    # via nginx, or :8000 direct
```

- `/api/v1/health` is unauthenticated and exempt from auth (probes need no
  credentials). Returns `status: ok` with service/environment/timestamp.
- `/api/v1/ready` returns **503** until the database is reachable. It always
  probes the database (`SELECT 1`); it probes **Redis only when**
  `JOB_EXECUTION_MODE != inline` (so the portable/inline profile is not marked
  not-ready over a Redis it never uses). Redis appears in the body as
  `required: false` in inline mode. Neither probe leaks credentials — the Redis
  check reports host[:port] only.
- `/metrics` is exposed at the app level, exempt from auth and the schema gate
  (scrapers are unauthenticated infra). **Bind it to an internal network / do
  not expose it publicly.** See `docs/observability.md` for the metric surface.

Redis has no published host port; ping it from inside the stack:

```sh
docker compose -f infra/docker-compose.yml exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" ping'
```

## 5. Logs and log format

The API and worker emit **structured single-line JSON** via
`backend/app/core/logging.py` (`JsonLogFormatter`), stdlib only. Every record
carries `timestamp` (UTC ISO 8601), `level`, `logger`, `message`, and — when
set — the correlation ids `request_id` and `run_id`. Extra fields passed via
`logger.info(..., extra={...})` are merged into the JSON.

- **Hosted:** logs go to each container's stdout — collect with
  `docker compose -f infra/docker-compose.yml logs -f api` (or `worker`,
  `frontend`, `postgres`, `redis`) and ship them to your log aggregator.
- **Edge/portable:** logs go to the console / the portable launcher's captured
  output. The portable crash log is written under
  `%LOCALAPPDATA%\SmartCommissioning\logs\` (see `docs/observability.md` for
  the exact crash-log location).
- The request-id middleware accepts an inbound `X-Request-ID` (or mints a
  uuid4), binds it for the whole request, and echoes it on the response — so a
  client-supplied id correlates frontend, API, and worker log lines.

See `docs/observability.md` for the full field list, the metrics surface, the
example scrape config, and recommended alerts/SLOs.

## 6. Common failures and fixes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `docker compose` exits immediately citing `POSTGRES_PASSWORD is required` | A `CHANGE_ME` placeholder left in `.env`, or `.env` missing. | Fill every value in `infra/.env` (compose fails fast on purpose). |
| All authenticated `/api/v1` calls return **401** | `AUTH_MODE=api_key` but `API_KEY` unset (fail-closed), or client not sending the key. | Set `API_KEY`; recreate the api (section 7). Client sends `X-API-Key`. The startup log warns when api_key mode has no key. |
| `/api/v1/ready` returns **503** | Database unreachable, or migrations not yet applied. | Check `docker compose ps` for postgres health; check the api log for the migration step; confirm `DATABASE_URL`. |
| `/api/v1/ready` 503 mentions Redis (hosted) | Redis down or wrong `REDIS_PASSWORD`. | Recreate redis (section 7); confirm the password matches across redis/api/worker. |
| Jobs accepted but never progress (hosted) | Worker down, or Redis unreachable from the worker. | Check the worker log; confirm it shares `DATABASE_URL`/`REDIS_URL`. The API may report "run started inline because Redis/Dramatiq was unavailable" when the queue is down. |
| 404 on `/docs` or `/openapi.json` | Expected in `api_key` mode — schema endpoints are gated off when not loopback-only. | Use `local` mode for interactive docs, or read the API surface from `docs/production-architecture.md`. |
| Active scan returns **403** "Active network scan requires authorization" | The scan run lacked the authorization contract. | Provide `parameters.authorized = true` or the audit form `parameters.scan_authorization = {authorized: true, authorized_by: "<who>"}`. A `dry_run = true` preview needs no auth. See `docs/security-posture.md`. |
| BACnet real backend errors about `bacpypes3` | The `bacpypes3` backend was selected without the optional extra installed. | `pip install 'smart-commissioning-core[bacnet]'`, or use the default simulated backend. The real path is UNVALIDATED — see `docs/protocol-conformance.md`. |
| MQTT/UDMI live capture reports `broker_unreachable` / `live_capture_unavailable` | No broker egress from the API/worker process (expected here). | Run live UDMI/MQTT from a service with broker access, or supply captured payloads directly. The engine records the honest status rather than faking success. |

## 7. Secret and key rotation

All rotations below are recipe-specific to this stack. None destroy run/import
data.

- **API key** (`API_KEY`): generate a new value (`openssl rand -hex 32`),
  update `infra/.env`, recreate the api so it re-reads the environment:
  ```sh
  docker compose -f infra/docker-compose.yml up -d api
  ```
  Clients (the frontend stores the key in the browser) must re-enter the new key.
  To rotate **all** compose secrets at once, move `infra/.env` aside and re-run
  `scripts/bootstrap-env.sh` / `.ps1` (it refuses to overwrite an existing
  `.env`) — the Postgres in-place caveat below still applies.
- **Redis password** (`REDIS_PASSWORD`): update `.env`, recreate redis, api, and
  worker together (all three embed the password):
  ```sh
  docker compose -f infra/docker-compose.yml up -d redis api worker
  ```
  In-flight queued jobs survive (Redis persists to `redis_data`, appendonly on).
- **Postgres password** (`POSTGRES_PASSWORD`): the image only applies the
  password on first init, so change it in place first, then update `.env` and
  recreate dependents:
  ```sh
  docker compose -f infra/docker-compose.yml exec postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "ALTER USER $POSTGRES_USER WITH PASSWORD '<new>';"
  # edit .env, then:
  docker compose -f infra/docker-compose.yml up -d api worker
  ```
- **Secret-store key** (`.secret_store_key` under the secrets root): this is the
  **Fernet key** that encrypts uploaded certificate/private-key material at rest
  (`backend/app/services/configuration_service.py`). It is generated on first
  use and lives in `SMART_COMMISSIONING_SECRETS_ROOT` (default
  `backend/runtime/secrets/`). **Rotation is manual and consequential:** material
  encrypted under the old key cannot be read with a new key. To rotate, decrypt
  the stored secrets with the old key (use the configuration service's
  `mask_secrets=False` reveal path), generate a new key, then re-store each
  secret so it is re-encrypted. Back up the secrets root before rotating (see
  `docs/backup-restore.md`).
- **Signing key** (evidence-pack signature/hash key): used by the
  evidence/backup/verify path being added in a parallel phase. Rotate by issuing
  a new key in the secret store and re-signing or re-verifying as that phase's
  CLI documents; previously-signed evidence stays verifiable against the key it
  was signed with. Treat it like the secret-store key: back up, rotate, never
  reuse across environments.
- **MQTT / BACnet client material** (CA cert, client cert, private key): stored
  as `secret://` references, encrypted at rest. Re-upload via the configuration
  secrets endpoint to rotate; the response returns only masked metadata.

After any rotation, re-run the readiness check (section 4) and confirm the
relevant subsystem still authenticates.

## 8. Upgrade and migration procedure

The backend **owns the schema** and applies Alembic migrations on API startup
(`smart_commissioning_core.db.migrate.upgrade_to_head`). Standard upgrade:

```sh
# Hosted
docker compose -f infra/docker-compose.yml pull          # or rebuild: --build
# BACK UP FIRST (docs/backup-restore.md): pg_dump (hosted) or copy the SQLite file (edge)
docker compose -f infra/docker-compose.yml up -d --build api
# The api runs upgrade_to_head on startup; watch the log for the migration step.
docker compose -f infra/docker-compose.yml up -d worker frontend
```

Notes:

- Always take a backup before upgrading (section in `docs/backup-restore.md`).
  Alembic migrations are forward-only here; rollback is "restore the pre-upgrade
  backup", not a down-migration.
- Bring the **api up first** so the schema is current before the worker resumes
  consuming jobs (the worker assumes the schema exists; it never migrates).
- To apply migrations out of band (e.g. a maintenance window), set
  `AUTO_MIGRATE=false`, run the migration step yourself, then start the api.
- Edge/portable upgrades: replace the executable; it migrates the local SQLite
  database on first start. Back up the SQLite file and secrets root first.

## 9. Incident triage

A first-15-minutes checklist:

1. **Is the process up?** `curl /api/v1/health`. If it fails, the API is down —
   `docker compose ps` / `logs api`.
2. **Is it ready?** `curl /api/v1/ready`. A 503 names the failing dependency in
   the body (`database` / `redis`) without leaking credentials. Drill into that
   dependency's container (`logs postgres` / `logs redis`).
3. **Correlate by request id.** Grab the `X-Request-ID` from the failing
   response (or have the user supply it), then filter the JSON logs for that
   `request_id` across api and worker. For a stuck job, filter by `run_id`.
4. **Check the queue (hosted).** Rising `sct_runs_by_status{status="running"}`
   with no terminal transitions, or no worker log activity, points at a worker /
   Redis problem. Confirm the worker is up and Redis reachable.
5. **Check metrics/alerts.** `/metrics` exposes request rate, latency, in-progress
   count, and runs-by-status. See `docs/observability.md` for the SLOs and the
   alerts that should already be firing.
6. **Auth incidents.** A spike of 401/403: confirm `AUTH_MODE`/`API_KEY`
   expectations; a 403 on a scan is the authorization gate working as designed
   (`docs/security-posture.md`), not a bug.
7. **Cancel a runaway run.** `POST /api/v1/runs/{run_id}/cancel` sets the
   cooperative cancel flag; engines poll it and stop early, flipping the run to
   `cancelled`. A finished run is unaffected.
8. **Contain, then recover.** For data corruption or a bad upgrade, follow
   `docs/backup-restore.md` (restore + verify). For a security event (suspected
   key compromise), rotate the affected secret (section 7) and review the audit
   trail of configuration changes / runs / exports.

## 10. Scaling notes

- **Worker throughput (hosted):** scale the worker horizontally —
  `docker compose -f infra/docker-compose.yml up -d --scale worker=N`. Workers
  share `DATABASE_URL`/`REDIS_URL` and consume from the same Dramatiq queue, so
  more workers process more concurrent discovery/validation jobs. The API stays
  single-purpose (HTTP + migrations).
- **Scan gentleness, not raw speed:** discovery engines run under a conservative
  throttle (`scan_max_concurrency=16`, `scan_rate_limit_per_sec=10`,
  `scan_connect_timeout_s=5` in `config.py`) so a scan pointed at a live building
  network cannot overwhelm controllers or a broker. Per-run parameters may narrow
  these but should not exceed site policy. Do **not** raise these globally to go
  faster against an OT network.
- **Database:** Postgres is the shared system of record for hosted deployments.
  Size it for run/import/configuration row volume; reports are generated
  in-memory at download time (no large report blobs in the DB).
- **Edge profile does not scale out** by design: one operator, one laptop,
  inline jobs, SQLite. For multi-user throughput, use the hosted profile.
- **Front door:** put the TLS-terminating reverse proxy / load balancer in front
  of the frontend port; the app binds loopback only.

## 11. Edge/Hub sync

On-site **edge** instances push immutable, signed run+evidence bundles to a
central **hub** that aggregates results across projects/sites. The full model,
trust/enrollment, immutability rules, and step-by-step transports are in
`docs/sync-architecture.md`; this section is the operator's quick path. The
mechanism is in `core/smart_commissioning_core/sync.py` and
`sync_identity.py`; the round-trip is proven in-process across two SQLite DBs in
`core/tests/test_sync.py` (a real network push and a Postgres hub are
`live_untested` — see that doc's §7).

> The edge sync CLI (`python -m app.scripts.sync`), the hub ingest endpoint
> (`POST /api/v1/hub/runs/ingest`), the offline ingest CLI, and the trusted-edges
> allowlist config are being added in a parallel phase — referenced generically
> here. Their behavior is fixed by the core and described in
> `docs/sync-architecture.md`.

**Enroll an edge (one-time):** export the edge's `edge_id` + public-key
fingerprint (or PEM); pin it in the hub's **trusted-edges allowlist** out of
band, confirming the fingerprint. Until the entry exists every bundle from that
edge is rejected (`rejected_untrusted`) and **nothing is written**. The private
signing key never leaves the edge.

**Run a sync:**

- **Online** — build the un-synced (watermark) set on the edge, push the bundle
  bytes to the hub with the edge API key, read the returned ingest summary, then
  mark the runs synced on the edge **only after a confirmed-accepted push**.
- **Offline (air-gapped)** — build, export a `.scbundle` file, carry it out,
  ingest at the hub with the offline ingest CLI, confirm the hub accepted it,
  then mark the runs synced on the edge.

Only **terminal** runs (`succeeded`/`failed`/`cancelled`) sync; in-flight runs
are never bundled.

**Verify what landed (hub):** read the runs API filtered by `project_id` /
`site_id` / `edge_id` (the hub stamps `edge_id` on every ingested run); reconcile
against the push's `inserted_run_ids`.

**Rejected bundle:** read the ingest summary's `rejected_reason` / counters.

| Counter | Cause | Action |
| --- | --- | --- |
| `rejected_untrusted` | Edge not in the allowlist, or its key's fingerprint ≠ the pinned value. | Enroll a genuinely new edge; for an unexpected fingerprint change verify out of band before re-pinning (possible key-compromise — `docs/security-posture.md` §7). |
| `rejected_bad_signature` | Signature did not verify against the trusted key. | Rebuild + re-push on the edge; if it persists, re-verify enrollment. |
| `rejected_bad_hash` | A bundle member was altered in transit (whole bundle rejected, nothing written). | Discard; rebuild a fresh bundle and re-transfer (suspect the media for offline carry). |
| `rejected_immutable` (run-level) | Same `run_id` already on the hub with **different** content. | Immutability guard working, not a bug — the hub never overwrites. A re-run should get a new id. Hub copy is unchanged. |

A whole-bundle rejection writes nothing — fix the cause, rebuild on the edge,
re-transfer.

**Watermark:** `synced_at IS NULL` means "this edge has not pushed this run yet".
Mark synced only after a confirmed-accepted push so the run is retried otherwise;
it is per-instance and idempotent. The hub leaves `synced_at` NULL and preserves
the edge's `created_at`/`updated_at`.
