"""App-level exception handlers: log the traceback for unhandled 500s and the
detail for 4xx HTTPException rejections, without changing the responses.

Runs against the real app (shared harness) so the handlers under test are the
ones actually registered on app.main, exercised through a real request.
"""

import logging
import unittest
from unittest import mock

from harness import ApiTestCase

_API_KEY = "test-exception-handlers-key"


class ExceptionHandlerTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def test_unhandled_error_logs_traceback_and_returns_standard_500(self) -> None:
        from app.api.routes import runs as runs_module
        from fastapi.testclient import TestClient

        # raise_server_exceptions=False so the client returns the 500 response the
        # handler produces instead of re-raising the error into the test.
        client = TestClient(self.app, headers={"X-API-Key": _API_KEY}, raise_server_exceptions=False)
        with mock.patch.object(
            runs_module.service, "list_runs", side_effect=RuntimeError("boom in list_runs")
        ), self.assertLogs("app.main", level="ERROR") as captured:
            response = client.get("/api/v1/runs")

        self.assertEqual(response.status_code, 500)
        # Response body is byte-for-byte FastAPI's default 500 (behavior unchanged).
        self.assertEqual(response.json(), {"detail": "Internal Server Error"})

        matching = [record for record in captured.records if "Unhandled exception" in record.getMessage()]
        self.assertTrue(matching, "the unhandled exception must be logged")
        record = matching[0]
        self.assertEqual(record.levelno, logging.ERROR)
        # The traceback is attached to the record, so the JSON file handler renders
        # it into app.log (the raw exception reaches the log, never the client).
        self.assertIsNotNone(record.exc_info, "the traceback must be attached for app.log")
        self.assertIsInstance(record.exc_info[1], RuntimeError)
        self.assertIn("GET", record.getMessage())
        self.assertIn("/api/v1/runs", record.getMessage())

    def test_4xx_rejection_is_logged_at_warning(self) -> None:
        with self.assertLogs("app.main", level="WARNING") as captured:
            response = self.client.get("/api/v1/validation/runs/run_00000000000000_deadbeef")

        self.assertEqual(response.status_code, 404)
        matching = [record for record in captured.records if "HTTP 404" in record.getMessage()]
        self.assertTrue(matching, "a 4xx rejection must be logged at WARNING")
        record = matching[0]
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertIn("GET", record.getMessage())


if __name__ == "__main__":
    unittest.main()
