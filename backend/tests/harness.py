"""Shared scaffolding for the API test modules.

unittest discovery (``discover -s tests``) puts this directory on sys.path, so
test modules import it as a plain top-level module::

    from harness import ApiTestCase
"""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path


def shared_test_database_url() -> str:
    """Process-wide temporary SQLite database shared by all API test modules.

    Route modules instantiate their services -- and therefore the SQLAlchemy
    engine -- at the first app.main import, so every test class in the test
    run must point at the same database file. The directory is removed at
    interpreter exit (best effort: lingering SQLite handles can block deletion
    on Windows, hence ignore_errors).
    """
    existing = os.environ.get("SCT_TEST_DATABASE_URL")
    if existing:
        return existing
    temp_dir = tempfile.mkdtemp(prefix="sct-test-db-")
    atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
    url = f"sqlite:///{(Path(temp_dir) / 'smart_commissioning.db').as_posix()}"
    os.environ["SCT_TEST_DATABASE_URL"] = url
    return url


class ApiTestCase(unittest.TestCase):
    """Shared scaffolding: env overrides + cache reset + lifespan-entered client.

    setUpClass applies the env overrides (incl. the shared DATABASE_URL) and
    clears the settings/engine caches BEFORE app.main is imported, then enters
    the TestClient as a context manager so the startup lifespan applies the
    Alembic migrations. tearDownClass restores everything.
    """

    # Applied to every ApiTestCase, overridable per subclass via ``env``. Inline
    # runs execute on a background thread in production (ITEM-4) so the run
    # monitor renders while a run is live, but the API tests below POST a run then
    # assert its terminal status/results synchronously — so the suite forces the
    # synchronous path. A subclass that needs the async behaviour sets
    # env = {"INLINE_RUN_ASYNC": "1", ...} and the merge below lets it win.
    _BASE_ENV: dict[str, str | None] = {"INLINE_RUN_ASYNC": "0"}
    # Subclasses override; a None value means "ensure the variable is unset".
    env: dict[str, str | None] = {}
    # Default headers for every request (e.g. {"X-API-Key": ...}); None = none.
    client_headers: dict[str, str] | None = None
    # Simulated peer address (Starlette's TestClient default).
    client_addr: tuple[str, int] = ("testclient", 50000)

    @classmethod
    def setUpClass(cls) -> None:
        overrides: dict[str, str | None] = {
            "DATABASE_URL": shared_test_database_url(),
            **cls._BASE_ENV,
            **cls.env,
        }
        cls._previous_env = {}
        for key, value in overrides.items():
            cls._previous_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        # Reset cached settings/engine so the app picks up the temporary database.
        from app.core import config as config_module
        from app.core import db as db_module

        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        cls.before_client()

        from app.main import app
        from fastapi.testclient import TestClient

        cls.app = app
        cls._client_context = TestClient(app, headers=cls.client_headers, client=cls.client_addr)
        cls.client = cls._client_context.__enter__()

    @classmethod
    def before_client(cls) -> None:
        """Hook: runs after the env/cache setup, before the client is created."""

    @classmethod
    def tearDownClass(cls) -> None:
        from app.core import config as config_module
        from app.core import db as db_module

        cls._client_context.__exit__(None, None, None)
        db_module.get_engine().dispose()
        for key, value in cls._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()
