"""Observability tests: metrics endpoint, request-id, readiness, JSON logging.

Uses the established API test harness (env set before app.main import, a shared
process-wide SQLite DB, api_key + inline execution mode) so it runs entirely
in-process against tmp SQLite with NO live Redis/Postgres/broker. Anything that
would need a real Redis is exercised with a fake client or asserted only in the
inline-mode path where Redis is genuinely not required.
"""

import io
import json
import logging
import unittest
from pathlib import Path

from harness import ApiTestCase

_API_KEY = "test-observability-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


class ObservabilityApiTests(ApiTestCase):
    """Endpoint-level coverage: /metrics, X-Request-ID, /ready.

    No default X-API-Key header: individual tests choose whether to auth so
    we can prove /metrics and /ready work WITHOUT credentials.
    """

    env = _ENV_OVERRIDES

    # -- /metrics ----------------------------------------------------------

    def test_metrics_returns_prometheus_text_without_auth(self) -> None:
        # In api_key mode authenticated routes 401 without a key; /metrics must
        # NOT, because scrapers are unauthenticated infra.
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("text/plain"),
            response.headers.get("content-type"),
        )
        # Prometheus exposition lines for our HTTP counter are present.
        self.assertIn("sct_http_requests_total", response.text)

    def test_metrics_is_not_404_by_schema_gate(self) -> None:
        # The schema gate 404s /docs|/redoc|/openapi.json in api_key mode;
        # /metrics is exempt and must answer 200.
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(self.client.get("/metrics").status_code, 200)

    def test_request_increments_request_counter(self) -> None:
        # One handled request must add exactly one to the request counter. Sum
        # across all label sets rather than probing one hardcoded path label:
        # the exact path-template label depends on Starlette's matched-route
        # internals (which vary across versions), but the invariant "a request
        # is counted once" does not.
        before_total = self._counter_total()
        self.client.get("/api/v1/health")
        after_total = self._counter_total()
        self.assertEqual(after_total, before_total + 1.0)
        # And the GET /health request is labelled sensibly (GET, 200, a path
        # that identifies health) — without coupling to the exact prefix.
        self.assertTrue(
            self._health_sample_exists(),
            "no GET/200 health sample recorded in sct_http_requests_total",
        )

    @staticmethod
    def _http_request_samples() -> list:
        from app.core.observability import REGISTRY

        return [
            sample
            for metric in REGISTRY.collect()
            if metric.name == "sct_http_requests"
            for sample in metric.samples
            if sample.name == "sct_http_requests_total"
        ]

    def _counter_total(self) -> float:
        return sum(sample.value for sample in self._http_request_samples())

    def _health_sample_exists(self) -> bool:
        return any(
            sample.labels.get("method") == "GET"
            and sample.labels.get("status") == "200"
            and "health" in (sample.labels.get("path") or "")
            and sample.value >= 1.0
            for sample in self._http_request_samples()
        )

    # -- X-Request-ID ------------------------------------------------------

    def test_request_id_is_generated_and_echoed(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertIn("X-Request-ID", response.headers)
        self.assertTrue(response.headers["X-Request-ID"])

    def test_inbound_request_id_is_preserved(self) -> None:
        provided = "inbound-correlation-1234"
        response = self.client.get("/api/v1/health", headers={"X-Request-ID": provided})
        self.assertEqual(response.headers["X-Request-ID"], provided)

    # -- /ready ------------------------------------------------------------

    def test_ready_is_ready_in_inline_mode_without_redis(self) -> None:
        # Inline mode: DB is up and Redis is NOT required, so readiness passes
        # and Redis is not even probed.
        response = self.client.get("/api/v1/ready")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertIn("database", body["checks"])
        self.assertEqual(body["checks"]["database"]["status"], "ok")
        # Redis is not probed in inline mode.
        self.assertNotIn("redis", body["checks"])

    def test_ready_body_carries_no_credentials(self) -> None:
        # Even if a password-bearing redis_url were configured, the body must
        # never echo it. We assert the configured url string is absent.
        from app.core.config import get_settings

        redis_url = get_settings().redis_url
        body_text = self.client.get("/api/v1/ready").text
        self.assertNotIn(redis_url, body_text)
        self.assertNotIn("redis://", body_text)


class ReadinessRedisModeTests(unittest.TestCase):
    """Queue-mode readiness uses the Redis probe; tested with fake clients."""

    def test_redis_required_and_reachable_is_ok(self) -> None:
        from app.core.observability import check_redis

        class _FakeOk:
            def ping(self) -> bool:
                return True

            def close(self) -> None:
                pass

        status = check_redis("redis://:secret@cache.internal:6379/0", required=True, client=_FakeOk())
        self.assertTrue(status.ok)
        self.assertTrue(status.required)
        # Host is reported but the password is never present.
        self.assertIn("cache.internal:6379", status.detail)
        self.assertNotIn("secret", status.detail)

    def test_redis_unreachable_is_down_without_leaking_url(self) -> None:
        from app.core.observability import check_redis

        class _FakeBoom:
            def ping(self) -> bool:
                raise OSError("connection refused")

            def close(self) -> None:
                pass

        status = check_redis("redis://:topsecret@cache.internal:6379/0", required=True, client=_FakeBoom())
        self.assertFalse(status.ok)
        self.assertNotIn("topsecret", status.detail)
        self.assertNotIn("redis://", status.detail)

    def test_database_check_reports_error_on_bad_engine(self) -> None:
        from app.core.observability import check_database

        class _BoomEngine:
            def connect(self):  # noqa: ANN202
                raise OSError("db down")

        status = check_database(_BoomEngine())
        self.assertFalse(status.ok)
        self.assertTrue(status.required)


class JsonLoggingTests(unittest.TestCase):
    """The JSON formatter emits valid JSON lines carrying correlation ids."""

    def test_formatter_emits_valid_json_with_request_id(self) -> None:
        from app.core.logging import (
            CorrelationIdFilter,
            JsonLogFormatter,
            reset_request_id,
            set_request_id,
        )

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        handler.addFilter(CorrelationIdFilter())

        logger = logging.getLogger("sct.test.jsonfmt")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        token = set_request_id("req-abc-123")
        try:
            logger.info("hello observability", extra={"custom_field": "value-1"})
        finally:
            reset_request_id(token)

        line = stream.getvalue().strip()
        payload = json.loads(line)  # raises if not valid JSON
        self.assertEqual(payload["message"], "hello observability")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "sct.test.jsonfmt")
        self.assertEqual(payload["request_id"], "req-abc-123")
        self.assertEqual(payload["custom_field"], "value-1")
        self.assertIn("timestamp", payload)

    def test_formatter_omits_request_id_when_unset(self) -> None:
        from app.core.logging import CorrelationIdFilter, JsonLogFormatter

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        handler.addFilter(CorrelationIdFilter())

        logger = logging.getLogger("sct.test.jsonfmt.noid")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.info("no correlation here")

        payload = json.loads(stream.getvalue().strip())
        self.assertNotIn("request_id", payload)
        self.assertNotIn("run_id", payload)

    def test_configure_logging_is_idempotent(self) -> None:
        from app.core.logging import _SCT_HANDLER_FLAG, configure_logging

        configure_logging()
        configure_logging()
        root = logging.getLogger()
        sct_handlers = [h for h in root.handlers if getattr(h, _SCT_HANDLER_FLAG, False)]
        self.assertEqual(len(sct_handlers), 1, "configure_logging must not stack handlers")


class WorkerLoggingTests(unittest.TestCase):
    """Worker actor bodies emit structured records carrying the run_id.

    No Redis/broker is touched: we import the worker logging module (stdlib
    only) and exercise its formatter + run-id binding directly, mirroring what
    an actor body does via run_id_context.
    """

    def test_worker_logger_emits_structured_record_with_run_id(self) -> None:
        import importlib.util

        worker_logging_path = (
            Path(__file__).resolve().parents[2] / "worker" / "app" / "logging.py"
        )
        spec = importlib.util.spec_from_file_location("sct_worker_logging", worker_logging_path)
        worker_logging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker_logging)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(worker_logging._JsonFormatter())
        handler.addFilter(worker_logging._RunIdFilter())

        logger = logging.getLogger("sct.test.worker")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Mirror what an actor body does: bind the run_id around the work.
        with worker_logging.run_id_context("run_20260101_abc"):
            logger.info("Starting IP discovery", extra={"actor": "discover_ip_range"})

        payload = json.loads(stream.getvalue().strip())
        self.assertEqual(payload["message"], "Starting IP discovery")
        self.assertEqual(payload["run_id"], "run_20260101_abc")
        self.assertEqual(payload["level"], "INFO")


if __name__ == "__main__":
    unittest.main()
