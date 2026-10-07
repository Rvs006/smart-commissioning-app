"""Pure-read GET routes must not reserve SQLite's writer slot.

Every write session starts with ``BEGIN IMMEDIATE`` (see
smart_commissioning_core/db/engine.py). A GET that reads through one queues
behind any running writer and, after the 5 s busy timeout, fails with
``sqlite3.OperationalError: database is locked``. These routes read through the
query-only session instead (deferred ``BEGIN``), which WAL never blocks.
"""

import threading
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
            # The listener is engine-wide. The lifespan's lease-recovery thread
            # legitimately writes when earlier suites left expired leases in the
            # shared test DB; that is not the request under test.
            if threading.current_thread().name == "run-lifecycle-maintenance":
                return
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

    def test_validation_run_detail_and_issues_read_without_the_write_lock(self) -> None:
        # The UDMI page fetches the run and its issues together on open. Both
        # load the whole result_summary, which is large after a long capture.
        from app.core.db import get_engine
        from smart_commissioning_core.db.db_run_store import DbRunStore

        run = DbRunStore(get_engine()).create_run(
            project_id="demo-project", site_id="demo-site", job_type="udmi_validation"
        )
        for suffix in ("", "/issues"):
            path = f"/api/v1/validation/runs/{run['run_id']}{suffix}"
            with self.subTest(path=path):
                self.assertEqual(self._write_locks_taken(path, {}), [])

    def test_named_user_scope_checks_read_without_the_write_lock(self) -> None:
        # A named user's scope grants are resolved on every request, so a
        # write-session grant lookup would lock every scoped GET, not just one.
        # The last_used_at stamp is a real write, so reads throttle it: only the
        # first GET in the interval takes the writer.
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"read-only-viewer-{uuid.uuid4().hex[:8]}", "role": "viewer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        headers = {"X-API-Key": created.json()["api_key"]}
        self.assertEqual(len(self._write_locks_taken("/api/v1/me", {}, headers)), 1)
        for path, params in self._GETS:
            with self.subTest(path=path):
                self.assertEqual(self._write_locks_taken(path, params, headers), [])

    def test_named_user_mutation_still_stamps_last_used(self) -> None:
        from smart_commissioning_core.db.repositories import UserRepository

        created = self.client.post(
            "/api/v1/users",
            json={"username": f"read-only-engineer-{uuid.uuid4().hex[:8]}", "role": "engineer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        headers = {"X-API-Key": created.json()["api_key"]}
        self.client.get("/api/v1/me", headers=headers)
        with mock.patch.object(UserRepository, "touch_last_used") as touch:
            self.client.post("/api/v1/runs/run_missing/cancel", headers=headers)
        touch.assert_called_once()


class QuerySessionFactoryTests(unittest.TestCase):
    def test_building_a_repository_without_an_engine_does_not_touch_it(self) -> None:
        # Mirrors session_factory: DB-less paths (run context build) construct
        # repositories with engine=None and must not fail until they query.
        from smart_commissioning_core.db.repositories import ImportRepository

        ImportRepository(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
