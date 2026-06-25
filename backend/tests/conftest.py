"""Isolate the test database so the suite is order-independent.

Route modules bind a module-level ``service = RunService()`` to ``get_engine()``
at import, and ``get_engine`` defaults to a SQLite file under ``runtime/``. So a
module that touches the default engine before the API tests boot the app (and
migrate) -- e.g. ``test_v1_review_contracts`` instantiating ``ConfigurationService()``
-- can leave the shared ``service`` bound to an unmigrated database, and later API
tests fail with ``OperationalError: no such table: projects``. CI hides it only
because its alphabetical order runs the API tests first.

Fix: before any test module imports (pytest loads conftest ahead of collection),
point the whole process at one isolated, pre-migrated temp SQLite. The API tests
reuse it via ``SCT_TEST_DATABASE_URL``; their startup auto_migrate is then a no-op.
Per-test isolation is wrong here -- the route ``service`` binds once per process,
so every class must share one database (see the ``test_runs_api`` docstring).
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path


def _install_isolated_test_database() -> None:
    url = os.environ.get("SCT_TEST_DATABASE_URL")
    if not url:
        temp_dir = tempfile.mkdtemp(prefix="sct-tests-db-")
        atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)  # SQLite handles may linger on Windows
        url = f"sqlite:///{(Path(temp_dir) / 'smart_commissioning.db').as_posix()}"
        os.environ["SCT_TEST_DATABASE_URL"] = url
    os.environ["DATABASE_URL"] = url

    # Drop any engine/settings cached against the repo runtime/ database, then
    # create the schema via the same Alembic path app startup uses so default-
    # engine queries find the tables (the API tests' startup upgrade is a no-op).
    from app.core import config, db
    from smart_commissioning_core.db.migrate import upgrade_to_head

    config.get_settings.cache_clear()
    db.get_engine.cache_clear()
    upgrade_to_head(url)


_install_isolated_test_database()
