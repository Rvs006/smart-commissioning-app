"""Prometheus metrics + dependency readiness probes for the API.

Metrics use ``prometheus-client`` (the one new runtime dependency, declared in
``backend/pyproject.toml``). The ``/metrics`` endpoint is wired in
:mod:`app.main` at the APP level (not under ``/api/v1``) and is intentionally
exempt from auth and the schema-gate, because Prometheus scrapers are
unauthenticated infrastructure — in production it should be bound to an
internal network (see the decisions in the task summary).

Readiness probing lives here too: :func:`check_database` runs ``SELECT 1`` and
:func:`check_redis` pings Redis with a short timeout. Both return a small
:class:`DependencyStatus` that never carries credentials (the redis check
reports only host[:port], never the full ``redis_url``).

HONESTY: there is no live Redis/Postgres in this environment. The Redis check
imports ``redis`` lazily and treats an unreachable broker (or a missing client
library) as ``down`` rather than raising — it is exercised in tests with a fake
client, and the real ping against a live broker requires on-site validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# A dedicated registry keeps the SCT metrics isolated from the process-global
# default registry. That avoids "Duplicated timeseries" errors when a test
# harness re-imports the app module within one interpreter.
REGISTRY = CollectorRegistry()

HTTP_REQUESTS_TOTAL = Counter(
    "sct_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "sct_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "path"),
    registry=REGISTRY,
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "sct_http_requests_in_progress",
    "HTTP requests currently being processed.",
    labelnames=("method",),
    registry=REGISTRY,
)

RUNS_BY_STATUS = Gauge(
    "sct_runs_by_status",
    "Number of runs grouped by terminal/non-terminal status.",
    labelnames=("status",),
    registry=REGISTRY,
)


def render_latest() -> tuple[bytes, str]:
    """Return the current metrics exposition body and its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def observe_request(method: str, path_template: str, status_code: int, duration_seconds: float) -> None:
    """Record one finished HTTP request against the metrics."""
    HTTP_REQUESTS_TOTAL.labels(method=method, path=path_template, status=str(status_code)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path_template).observe(duration_seconds)


def set_runs_by_status(counts: dict[str, int]) -> None:
    """Publish the runs-by-status gauge from a status -> count mapping."""
    for status, count in counts.items():
        RUNS_BY_STATUS.labels(status=str(status)).set(count)


# -- request path templating ------------------------------------------------


def route_template(request) -> str:  # noqa: ANN001 (Starlette Request)
    """Return a low-cardinality path label for the request.

    Uses the matched route's path template (e.g. ``/api/v1/runs/{run_id}``) so
    per-id paths collapse to a single time series instead of exploding metric
    cardinality. Falls back to the raw path when no route matched (404s).
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    if path_format:
        return str(path_format)
    return request.url.path


# -- dependency readiness ----------------------------------------------------


@dataclass(frozen=True)
class DependencyStatus:
    """Per-dependency readiness result with NO credentials in any field."""

    name: str
    ok: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ok" if self.ok else "error",
            "required": self.required,
            "message": self.detail,
        }


def check_database(engine) -> DependencyStatus:  # noqa: ANN001 (SQLAlchemy Engine)
    """Probe the database with ``SELECT 1``. Never raises."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as error:
        return DependencyStatus(name="database", ok=False, detail=_safe_error(error))
    return DependencyStatus(name="database", ok=True, detail="database is reachable")


def _redis_host(redis_url: str) -> str:
    """Return redis host[:port] for messages, stripping any credentials."""
    try:
        parts = urlsplit(redis_url)
    except ValueError:
        return "<unparseable>"
    host = parts.hostname or "<unknown>"
    if parts.port is not None:
        return f"{host}:{parts.port}"
    return host


def check_redis(redis_url: str, *, required: bool, timeout_s: float = 1.0, client=None) -> DependencyStatus:  # noqa: ANN001
    """Ping Redis with a short timeout. Never raises; never leaks credentials.

    ``required`` reflects whether the deployment actually needs the queue (it is
    False in inline/portable mode). ``client`` is injectable for tests; in
    production a short-timeout ``redis.Redis`` client is built from ``redis_url``.
    A missing ``redis`` library or any connection error is reported as down with
    a host-only message — the full ``redis_url`` (which may embed a password) is
    never included.
    """
    host = _redis_host(redis_url)
    try:
        if client is None:
            import redis  # lazy: keeps inline/portable deployments import-clean

            client = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=timeout_s,
                socket_timeout=timeout_s,
            )
        reachable = bool(client.ping())
    except Exception:  # noqa: BLE001 (any client/connection error => not reachable)
        return DependencyStatus(
            name="redis",
            ok=False,
            detail=f"redis at {host} is unreachable",
            required=required,
        )
    finally:
        _close_quietly(client)

    if reachable:
        return DependencyStatus(name="redis", ok=True, detail=f"redis at {host} is reachable", required=required)
    return DependencyStatus(name="redis", ok=False, detail=f"redis at {host} did not respond", required=required)


def _close_quietly(client) -> None:  # noqa: ANN001
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def _safe_error(error: Exception) -> str:
    """Truncated error text; defensive so probe bodies stay small."""
    text = str(error).strip() or error.__class__.__name__
    return text[:200]
