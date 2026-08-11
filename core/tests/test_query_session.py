import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    query_session_factory,
    session_factory,
)
from smart_commissioning_core.db.models import Project
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import create_mock_engine, event, func, select, text
from sqlalchemy.exc import OperationalError


class _FaultInjectingCursor:
    def __init__(self, owner: "_FaultInjectingConnection", cursor: object) -> None:
        self._owner = owner
        self._cursor = cursor

    def execute(self, statement: str, *args: object, **kwargs: object) -> object:
        if (
            self._owner.fail_query_only_cleanup
            and statement.strip().upper() == "PRAGMA QUERY_ONLY=OFF"
        ):
            self._owner.fail_query_only_cleanup = False
            raise sqlite3.OperationalError("injected query_only cleanup failure")
        return self._cursor.execute(statement, *args, **kwargs)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)


class _FaultInjectingConnection:
    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.fail_query_only_cleanup = False
        self.closed = False

    def cursor(self, *args: object, **kwargs: object) -> _FaultInjectingCursor:
        return _FaultInjectingCursor(
            self,
            self._connection.cursor(*args, **kwargs),
        )

    def close(self) -> None:
        self.closed = True
        self._connection.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class QuerySessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine_from_url("sqlite://")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE query_fixture (value INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO query_fixture (value) VALUES (41)"))

    def _capture_statements(self) -> tuple[list[str], object]:
        statements: list[str] = []

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement.strip().upper())

        event.listen(self.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", capture)
        return statements, capture

    def test_query_factory_uses_deferred_begin_and_shares_sqlite_memory_database(
        self,
    ) -> None:
        statements, _capture = self._capture_statements()
        with session_factory(self.engine)() as session:
            self.assertEqual(session.scalar(text("SELECT value FROM query_fixture")), 41)
        mutation_statements = list(statements)

        statements.clear()
        with query_session_factory(self.engine)() as session:
            self.assertEqual(session.scalar(text("SELECT value FROM query_fixture")), 41)
        query_statements = list(statements)

        self.assertIn("BEGIN IMMEDIATE", mutation_statements)
        self.assertIn("PRAGMA QUERY_ONLY=ON", query_statements)
        self.assertIn("BEGIN", query_statements)
        self.assertNotIn("BEGIN IMMEDIATE", query_statements)

    def test_query_sessions_reject_raw_and_orm_writes(self) -> None:
        Base.metadata.create_all(self.engine)
        factory = query_session_factory(self.engine)

        with factory() as session:
            with self.assertRaisesRegex(OperationalError, "readonly"):
                session.execute(text("INSERT INTO query_fixture (value) VALUES (42)"))
            session.rollback()

        with factory() as session:
            session.add(Project(id="read-only", name="Read only"))
            with self.assertRaisesRegex(OperationalError, "readonly"):
                session.flush()
            session.rollback()

        with session_factory(self.engine)() as session:
            self.assertEqual(session.scalar(text("SELECT COUNT(*) FROM query_fixture")), 1)
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(Project)
                    .where(Project.id == "read-only")
                ),
                0,
            )

    def test_query_only_is_reset_after_close_commit_rollback_and_error(self) -> None:
        factory = query_session_factory(self.engine)

        def close_path() -> None:
            session = factory()
            session.scalar(text("SELECT value FROM query_fixture"))
            session.close()

        def commit_path() -> None:
            with factory() as session:
                session.scalar(text("SELECT value FROM query_fixture"))
                session.commit()

        def rollback_path() -> None:
            with factory() as session:
                session.scalar(text("SELECT value FROM query_fixture"))
                session.rollback()

        def error_path() -> None:
            with factory() as session:
                with self.assertRaises(OperationalError):
                    session.execute(text("SELECT * FROM table_that_does_not_exist"))

        def write_error_path() -> None:
            with factory() as session:
                with self.assertRaisesRegex(OperationalError, "readonly"):
                    session.execute(
                        text("INSERT INTO query_fixture (value) VALUES (99)")
                    )

        for label, path in {
            "close": close_path,
            "commit": commit_path,
            "rollback": rollback_path,
            "statement error": error_path,
            "write error": write_error_path,
        }.items():
            with self.subTest(label):
                path()
                with session_factory(self.engine).begin() as writer:
                    self.assertEqual(writer.scalar(text("PRAGMA query_only")), 0)
                    writer.execute(text("INSERT INTO query_fixture (value) VALUES (42)"))

    def test_held_query_reader_does_not_reserve_the_sqlite_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = (Path(temp_dir) / "concurrency.db").as_posix()
            engine = create_engine_from_url(f"sqlite:///{database}")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE concurrency_fixture "
                            "(value INTEGER NOT NULL)"
                        )
                    )
                    connection.execute(
                        text("INSERT INTO concurrency_fixture (value) VALUES (1)")
                    )

                reader = query_session_factory(engine)()
                self.assertEqual(
                    reader.scalar(text("SELECT value FROM concurrency_fixture")),
                    1,
                )

                finished = threading.Event()
                errors: list[BaseException] = []

                def write_while_reader_is_held() -> None:
                    try:
                        with session_factory(engine).begin() as writer:
                            writer.execute(
                                text(
                                    "INSERT INTO concurrency_fixture (value) VALUES (2)"
                                )
                            )
                    except BaseException as exc:  # pragma: no cover - asserted below
                        errors.append(exc)
                    finally:
                        finished.set()

                writer_thread = threading.Thread(target=write_while_reader_is_held)
                writer_thread.start()
                completed_while_reader_was_held = finished.wait(3)
                reader.close()
                writer_thread.join(5)

                self.assertTrue(
                    completed_while_reader_was_held,
                    "writer stayed blocked behind a deferred read transaction",
                )
                self.assertFalse(errors, errors)
                self.assertFalse(writer_thread.is_alive())
                with query_session_factory(engine)() as session:
                    self.assertEqual(
                        session.scalar(text("SELECT COUNT(*) FROM concurrency_fixture")),
                        2,
                    )
            finally:
                engine.dispose()

    def test_failed_query_only_cleanup_discards_the_connection(self) -> None:
        connections: list[_FaultInjectingConnection] = []

        def creator() -> _FaultInjectingConnection:
            connection = _FaultInjectingConnection()
            connections.append(connection)
            return connection

        def create_fault_injecting_engine(
            url: str, *args: object, **kwargs: object
        ) -> object:
            kwargs.pop("connect_args", None)
            return sqlalchemy_create_engine(url, *args, creator=creator, **kwargs)

        with patch(
            "smart_commissioning_core.db.engine.create_engine",
            side_effect=create_fault_injecting_engine,
        ):
            engine = create_engine_from_url("sqlite://")
        try:
            session = query_session_factory(engine)()
            self.assertEqual(session.scalar(text("SELECT 1")), 1)
            failed_connection = connections[0]
            failed_connection.fail_query_only_cleanup = True
            session.close()

            with engine.connect() as connection:
                self.assertEqual(connection.scalar(text("SELECT 2")), 2)
            self.assertTrue(failed_connection.closed)
            self.assertEqual(len(connections), 2)
        finally:
            engine.dispose()

    def test_non_sqlite_query_factory_is_read_oriented_without_rebinding(self) -> None:
        engine = create_mock_engine(
            "postgresql+psycopg://",
            lambda *_args, **_kwargs: None,
        )

        factory = query_session_factory(engine)  # type: ignore[arg-type]

        self.assertIs(factory.kw["bind"], engine)
        self.assertFalse(factory.kw["autoflush"])
        self.assertFalse(factory.kw["expire_on_commit"])


if __name__ == "__main__":
    unittest.main()
