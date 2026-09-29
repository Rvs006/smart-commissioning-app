"""Pure-read GET routes must not reserve SQLite's writer slot.

Every write session starts with ``BEGIN IMMEDIATE`` (see
smart_commissioning_core/db/engine.py). A GET that reads through one queues
behind any running writer and, after the 5 s busy timeout, fails with
``sqlite3.OperationalError: database is locked``. These routes read through the
query-only session instead (deferred ``BEGIN``), which WAL never blocks.
"""

import unittest
import uuid
from unittest import mock

from harness import ApiTestCase

_API_KEY = "test-read-only-sessions-key"


class ReadOnlyGetRouteTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    _GETS = (
        ("/api/v1/runs", {"project_id": "demo-project", "site_id": "demo-site"}),
        (
            "/api/v1/imports/latest",
            {"import_type": "mqtt_register", "project_id": "demo-project", "site_id": "demo-site"},
        ),
        ("/api/v1/udmi/schemas", {}),
    )

    def _write_locks_taken(self, path: str, params: dict, headers: dict | None = None) -> list[str]:
        from app.core.db import get_engine
        from sqlalchemy import event

        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(" ".join(statement.split()).upper())

        engine = get_engine()
        event.listen(engine, "before_cursor_execute", record)
        try:
            response = self.client.get(path, params=params, headers=headers)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        self.assertIn(response.status_code, (200, 404), response.text)
        return [s for s in statements if s.startswith("BEGIN IMMEDIATE")]

    def test_list_routes_read_without_the_write_lock(self) -> None:
        for path, params in self._GETS:
            with self.subTest(path=path):
                self.assertEqual(self._write_locks_taken(path, params), [])

    def test_named_user_scope_checks_read_without_the_write_lock(self) -> None:
        # A named user's scope grants are resolved on every request, so a
        # write-session grant lookup would lock every scoped GET, not just one.
        from smart_commissioning_core.db.repositories import UserRepository

        created = self.client.post(
            "/api/v1/users",
            json={"username": f"read-only-viewer-{uuid.uuid4().hex[:8]}", "role": "viewer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        headers = {"X-API-Key": created.json()["api_key"]}
        # last_used_at is a genuine write; this test is about the reads around it.
        with mock.patch.object(UserRepository, "touch_last_used"):
            for path, params in self._GETS:
                with self.subTest(path=path):
                    self.assertEqual(self._write_locks_taken(path, params, headers), [])


if __name__ == "__main__":
    unittest.main()
