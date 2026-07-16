# Observability

Structured logs, the Prometheus metrics surface, recommended alerts/SLOs, and
where the portable crash log lands. Accurate to `backend/app/core/logging.py`,
`backend/app/core/observability.py`, and `backend/app/main.py`.

## 1. Structured logs

The API and worker emit **single-line JSON** via `JsonLogFormatter`
(`backend/app/core/logging.py`). The formatter uses only the standard library,
so the FastAPI backend and the Dramatiq worker share it without an extra logging
dependency. `configure_logging(LOG_LEVEL)` is installed in the API lifespan
startup (idempotent — re-running replaces the handler rather than stacking).

### Fields

Always present:

| Field | Meaning |
| --- | --- |
| `timestamp` | UTC ISO 8601, from the record's creation time. |
| `level` | Log level name (`INFO`, `WARNING`, ...). |
| `logger` | Logger name (module path). |
| `message` | Rendered log message. |

Present when set (correlation, via `contextvars` + `CorrelationIdFilter`):

| Field | Meaning |
| --- | --- |
| `request_id` | Per-request id. Accepts an inbound `X-Request-ID` header or mints a uuid4; echoed back on the response. Bound for the whole request. |
| `run_id` | Bound by the worker (`run_id_context`) while processing a run, so every log line during a job carries the run id. |

Conditional:

- `exc_info` / `stack_info` — formatted traceback / stack when present.
- Any keys passed via `logger.info(..., extra={...})` are merged into the JSON
  (reserved `LogRecord` attributes and private `_`-prefixed keys are skipped).

The correlation values are stored in `contextvars`, so they are isolated per
asyncio task / per worker message and never leak across concurrent requests.

### Example record

```json
{"timestamp":"2026-06-12T09:14:02.481922+00:00","level":"INFO","logger":"app.services.run_dispatch","message":"BACnet discovery job queued for worker execution.","request_id":"a1b2c3d4e5f6...","run_id":"run_01HXYZ..."}
```

### Where logs go

- **Hosted (compose):** container stdout. Tail with
  `docker compose -f infra/docker-compose.yml logs -f api` (or `worker`,
  `frontend`, `postgres`, `redis`) and forward to your aggregator. Because every
  line is JSON with `request_id`/`run_id`, a single request or job can be traced
  across the api and worker by filtering on the id.
- **Edge/portable:** stdout of the portable process / launcher, **and** a local
  rotating JSON log file (below).

### Local rotating log file

Alongside the console handler, the API installs a **rotating file handler**
(`configure_file_logging`, `backend/app/core/logging.py`) that writes the same
single-line JSON to **`<RUNTIME_ROOT>/logs/app.log`**. It rotates by **size**
(5 MiB per file, 10 rollovers kept — a ~50 MiB hard cap so a chatty capture can
never fill a field laptop's disk); `app.log.1` … `app.log.10` are the rollovers.
The handler opens the file lazily (`delay=True`), so importing the app never
creates an empty `app.log` and Windows holds no open handle while logging is
quiet.

**Level precedence** (resolved by `effective_log_level`,
`backend/app/services/log_service.py`, applied at startup and after a
Configuration save):

1. **Diagnostics Mode = Enabled → `DEBUG`.**
2. else the **`LOG_LEVEL`** environment variable, if set (keeps existing
   deployments and CI byte-identical when the new fields are untouched).
3. else the configured **Log Level** word (`Info` → `INFO`).
4. else `INFO`.

**Retention** is *time-based pruning of rotated/crash files only*, run once at
startup: `purge_old_logs` deletes `*.log*` files older than the configured
**Log Retention** days (default 30). Live rotation stays size-based — the days
value does not shorten `app.log` itself.

> Windows file-locking caveat: `RotatingFileHandler` rollover renames the file,
> which fails if another process holds it open. Do **not** hold `app.log` open
> in Notepad/an editor while the app runs; `logging` degrades to stderr on a
> rollover error rather than crashing, but the rotation is silently lost.

**Scope:** this file destination is for the **API** process (single-process
edge/portable target). The hosted **worker** stays **console-only** — each
container has its own log pipeline, and a `RotatingFileHandler` shared across
processes would be a Windows-locking / rename race. Do not point the worker at a
shared file.

### Retrieving logs — `/logs/bundle` and `/logs/upload`

Two **engineer-gated** endpoints (`backend/app/api/routes/logs.py`) let an
operator collect logs without remote-desktop access:

- **`GET /api/v1/logs/bundle`** — downloads every `*.log*` file under the local
  logs directory as a single zip (masked; see below). Empty logs dir → 404.
- **`POST /api/v1/logs/upload`** — builds the same masked bundle and POSTs it
  (multipart `file` field) to the configured **Log Upload URL**, with the
  **Log Upload Token** sent only as an `Authorization: Bearer` header. The
  response reports the **honest outcome**: `uploaded` (2xx), `rejected` (≥400,
  with the status), or `no_response` (the endpoint did not answer) — never a
  fabricated success, never a retry, and all three return HTTP 200 (the
  `outcome` field is the result). A missing/invalid URL is a 400.

**Masking guarantee (stated honestly):** the bundler redacts values under known
credential-shaped keys (`password`/`token`/`secret`/`api_key`/`authorization`/
`private_key`) in both JSON and `key=value` forms. It is **not** a DLP scanner
and cannot catch a secret embedded in free text by a future log call. The
primary control is **containment**: the bundle only ever contains files from the
local logs directory — the secrets store, the database, and uploaded import
files are never included. The upload token never appears in the URL, the
response, or any log line.

### Portable crash log

For the portable edge build, an unhandled crash (the kind that would otherwise
vanish when the console window closes) is captured to a crash log **under the
app data directory** — alongside the SQLite database and secrets. The portable
launcher in `packaging/windows_portable/` (`install_crash_logging`) writes
timestamped files under `%LOCALAPPDATA%\SmartCommissioning\logs\` for the
frozen exe (`<SMART_COMMISSIONING_DATA_DIR>\logs\` when that override is set;
`<repo>/runtime/logs/` for unfrozen dev runs):
`crash-<timestamp>.log` for uncaught Python exceptions and
`faulthandler-<timestamp>.log` for interpreter-level faults. When triaging a
portable crash, collect those files together with the runtime bundle (see
`docs/backup-restore.md`). Note: after an in-place upgrade, an exe-adjacent
`runtime\logs\` folder is a pre-upgrade rollback copy — it holds only old
logs, so do not triage current crashes from it. They are the edge equivalent
of `docker logs` for a process with no attached console.

## 2. Prometheus metrics surface

Metrics use `prometheus-client` against a **dedicated registry** (isolated from
the process-global default registry so re-importing the app in tests does not
raise duplicate-timeseries errors). The `/metrics` endpoint is wired in
`app.main` **at the app level (not under `/api/v1`)** and is intentionally
exempt from auth and the schema gate — scrapers are unauthenticated infra, so in
production **bind it to an internal network and do not expose it publicly**.

Exposed series (all prefixed `sct_`):

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `sct_http_requests_total` | Counter | `method`, `path`, `status` | Total HTTP requests handled. `path` is the **route template** (e.g. `/api/v1/runs/{run_id}`), not the raw path, to keep cardinality bounded. |
| `sct_http_request_duration_seconds` | Histogram | `method`, `path` | Request latency. |
| `sct_http_requests_in_progress` | Gauge | `method` | Requests currently being processed. |
| `sct_runs_by_status` | Gauge | `status` | Number of runs grouped by status, refreshed cheaply at scrape time from the run store. |

The request metrics are recorded by middleware in `app.main`; `/metrics` itself
is excluded from measurement. `sct_runs_by_status` is repopulated on each scrape
from a `GROUP BY status` over the runs table; a DB hiccup during a scrape is
swallowed (the scrape never 500s) and logged at debug.

### Example scrape config

```yaml
scrape_configs:
  - job_name: smart-commissioning-api
    metrics_path: /metrics
    scheme: http
    scrape_interval: 15s
    static_configs:
      # The api's loopback debug port, or your internal service address.
      # Do NOT scrape across an untrusted network — /metrics is unauthenticated.
      - targets: ["127.0.0.1:8000"]
```

In compose, scrape the api service on its internal network (e.g.
`api:8000`) from a Prometheus that shares that network, rather than the public
frontend port.

## 3. Readiness as a signal

`/api/v1/ready` (`backend/app/api/routes/health.py`) is the dependency-aware
signal:

- Always probes the **database** (`SELECT 1`).
- Probes **Redis only when `JOB_EXECUTION_MODE != inline`** (required for the
  queue). In inline/portable mode Redis is reported `required: false` and an
  unreachable broker does **not** make the service not-ready.
- Returns **503** only if a *required* dependency is down. The body carries
  per-dependency status and **never** includes credentials (the Redis check
  reports host[:port] only).

Use `/ready` as the container/orchestrator readiness probe and `/health` as the
liveness probe.

## 4. Recommended alerts and SLOs

Suggested starting points; tune to the deployment. These reference only the
metrics and signals that actually exist above.

### SLOs

- **Availability:** `/api/v1/health` reachable ≥ 99.5% over 30 days (liveness).
- **Readiness:** `/api/v1/ready` returns 200 ≥ 99% over 30 days (database +,
  hosted, Redis up).
- **API latency:** 95th percentile of `sct_http_request_duration_seconds` for
  the main read paths under ~1s (discovery/validation submissions are async, so
  the submit call is fast; the work happens in jobs).
- **Run success rate:** terminal `failed` runs as a fraction of finished runs
  stays below an agreed threshold (e.g. < 5%), excluding the deliberate
  authorization-gate `failed` outcomes.

### Alerts

| Alert | Condition (sketch) | Why |
| --- | --- | --- |
| API down | `/health` scrape/probe failing for > 2m | Process down or unreachable. |
| Not ready | `/ready` 503 for > 5m | A required dependency (DB, or Redis in queue mode) is down. |
| Error-rate spike | `rate(sct_http_requests_total{status=~"5.."}[5m])` elevated | Server-side failures. |
| Latency regression | p95 of `sct_http_request_duration_seconds` above SLO for > 10m | Slow dependency / saturation. |
| Queue backlog (hosted) | `sct_runs_by_status{status="running"}` and/or `queued` rising with no terminal transitions over N minutes | Worker stuck/down or Redis unreachable; jobs not draining. |
| Queue age (hosted) | Oldest non-terminal run age exceeds a threshold | Jobs queued but not being picked up — derive from run timestamps; pair with the backlog alert. |
| Run failure rate | Ratio of `failed` to finished runs over a window exceeds threshold | Systematic job failures. |
| In-progress pileup | `sct_http_requests_in_progress` sustained high | Requests not completing (deadlock/slow downstream). |

> "Queue depth/age" alerts are derived from `sct_runs_by_status` plus run
> timestamps from the runs list — there is no separate broker-depth metric
> exported today. If you need true Redis/Dramatiq queue depth, scrape it from
> Redis directly or add a queue-depth gauge in a future change; that broker-side
> metric is **not** currently emitted (it would require a live Redis to be
> meaningful — see the honesty note below).

## 5. Honesty note

There is no live Redis/Postgres/broker/hardware in the development or CI
environment. The readiness and Redis checks (`check_database`, `check_redis`)
are exercised in tests with a **fake client / temporary SQLite**, and they treat
an unreachable broker (or a missing `redis` client library) as `down` rather
than raising. The real ping against a live broker, and any broker-depth metric,
require **on-site / live-infra validation** and are not asserted to "work" here.
The metric *definitions*, the request middleware, and the structured logger are
fully exercised in-process; the dependency probes are exercised against fakes.
