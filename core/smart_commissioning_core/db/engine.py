"""Engine and session helpers usable with SQLite (local/dev) and Postgres."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def default_sqlite_url(runtime_root: Path) -> str:
    """Return the default SQLite URL for a runtime directory.

    e.g. default_sqlite_url(Path("backend/runtime"))
         -> "sqlite:///backend/runtime/smart_commissioning.db"
    """
    database_path = (Path(runtime_root) / "smart_commissioning.db").as_posix()
    return f"sqlite:///{database_path}"


def create_engine_from_url(url: str) -> Engine:
    """Create an engine with per-backend defaults.

    SQLite: check_same_thread=False (FastAPI/threaded workers) plus WAL journal
    mode and enforced foreign keys on every connection. Postgres (and other
    server databases): conservative pool sizing with pre-ping and recycling.
    """
    backend = make_url(url).get_backend_name()
    if backend == "sqlite":
        engine = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 5}
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
            # Disable pysqlite's legacy transaction handling: it defers BEGIN
            # until the first DML statement, which leaves SELECTs in autocommit
            # and silently breaks read-modify-write transactions (FOR UPDATE is
            # a no-op on SQLite). SQLAlchemy then controls transaction scope via
            # the "begin" event below.
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn) -> None:  # noqa: ANN001
            # BEGIN IMMEDIATE takes the write lock at transaction start, so
            # concurrent read-modify-write transactions (e.g. result_summary
            # merges from backend and worker processes) serialize instead of
            # losing updates.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine

    return create_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to the engine.

    expire_on_commit=False so returned dict serializations built from ORM rows
    remain usable after the transaction closes.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
