"""Portable-exe launcher environment configuration.

FIX 9: ``configure_environment`` uses ``os.environ.setdefault`` for the
local/inline/sqlite profile, which lets a stray pre-existing env var silently
override the intended value. The launcher now routes those through
``_set_env_default``, which PRESERVES the override (the escape hatch) but PRINTS
a warning naming the variable only — never its value, since ``DATABASE_URL`` may
embed a password.

The launcher lives outside the backend package, so it is loaded here by path via
``importlib.util.spec_from_file_location`` (its only module-level imports are
stdlib, so this is cheap and side-effect free).
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

LAUNCHER_PATH = (
    Path(__file__).resolve().parents[2]
    / "packaging"
    / "windows_portable"
    / "run_smart_commissioning_app.py"
)

# The five variables routed through the visible-override helper.
_MANAGED_VARS = (
    "DATABASE_URL",
    "ENVIRONMENT",
    "AUTH_MODE",
    "JOB_EXECUTION_MODE",
    "ALLOW_INLINE_WORKER_FALLBACK",
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "portable_launcher_under_test", LAUNCHER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ConfigureEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_environ = dict(os.environ)
        self._saved_sys_path = list(sys.path)
        self.launcher = _load_launcher()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)
        sys.path[:] = self._saved_sys_path

    def _make_root(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        (root / "backend").mkdir()
        dist = root / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        return root

    def test_preexisting_database_url_retained_with_hidden_value_warning(self) -> None:
        root = self._make_root()
        # Clean slate for the other managed vars so only DATABASE_URL warns.
        for name in _MANAGED_VARS:
            os.environ.pop(name, None)
        os.environ["DATABASE_URL"] = "postgresql://stray"

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.configure_environment(root)

        # The stray override is preserved (setdefault escape hatch).
        self.assertEqual(os.environ["DATABASE_URL"], "postgresql://stray")

        output = buffer.getvalue()
        # The warning names the variable...
        self.assertIn("DATABASE_URL", output)
        # ...but never leaks the value (which may embed a password).
        self.assertNotIn("stray", output)
        self.assertNotIn("postgresql://stray", output)

    def test_sqlite_default_applied_when_unset_without_warning(self) -> None:
        root = self._make_root()
        for name in _MANAGED_VARS:
            os.environ.pop(name, None)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.configure_environment(root)

        expected = (
            f"sqlite:///{(root / 'runtime' / 'smart_commissioning.db').as_posix()}"
        )
        self.assertEqual(os.environ["DATABASE_URL"], expected)
        # Nothing overridden, so nothing is printed.
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
