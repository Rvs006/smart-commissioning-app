"""Portable-exe launcher environment configuration.

FIX 9: ``configure_environment`` uses ``os.environ.setdefault`` for the
local/inline/sqlite profile, which lets a stray pre-existing env var silently
override the intended value. The launcher now routes those through
``_set_env_default``, which PRESERVES the override (the escape hatch) but PRINTS
a warning naming the variable only — never its value, since ``DATABASE_URL`` may
embed a password.

Stable data dir (2026-07-14): frozen builds keep state in
``%LOCALAPPDATA%/SmartCommissioning`` (``data_root``) instead of the
per-release exe folder, with a one-time best-effort copy-forward of the
pre-v0.1.9 exe-adjacent layout (``migrate_legacy_runtime``) — ThreatLocker
per-hash approval forces a fresh folder per release, which silently reset all
site configuration. ``configure_environment`` accepts the stable dir and also
exports ``SMART_COMMISSIONING_RUNTIME_ROOT`` so backend-derived paths
(imports, edge identity) anchor to it too.

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

    def test_explicit_runtime_root_rewires_db_secrets_and_runtime_env(self) -> None:
        root = self._make_root()
        stable_dir = tempfile.TemporaryDirectory()
        self.addCleanup(stable_dir.cleanup)
        stable = Path(stable_dir.name) / "SmartCommissioning"
        for name in _MANAGED_VARS:
            os.environ.pop(name, None)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.configure_environment(root, stable)

        self.assertEqual(
            os.environ["DATABASE_URL"],
            f"sqlite:///{(stable / 'smart_commissioning.db').as_posix()}",
        )
        self.assertEqual(
            os.environ["SMART_COMMISSIONING_SECRETS_ROOT"], str(stable / "secrets")
        )
        self.assertEqual(os.environ["SMART_COMMISSIONING_RUNTIME_ROOT"], str(stable))


class DataRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_environ = dict(os.environ)
        self.launcher = _load_launcher()
        os.environ.pop("SMART_COMMISSIONING_DATA_DIR", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def test_env_override_wins(self) -> None:
        os.environ["SMART_COMMISSIONING_DATA_DIR"] = str(Path("C:/custom/state"))
        self.assertEqual(
            self.launcher.data_root(Path("C:/bundle")), Path("C:/custom/state")
        )

    def test_dev_layout_stays_in_checkout(self) -> None:
        root = Path("C:/repo")
        self.assertEqual(self.launcher.data_root(root), root / "runtime")

    def test_frozen_uses_localappdata(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        os.environ["LOCALAPPDATA"] = temp_dir.name
        sys.frozen = True  # type: ignore[attr-defined]
        try:
            self.assertEqual(
                self.launcher.data_root(Path("C:/bundle")),
                Path(temp_dir.name) / "SmartCommissioning",
            )
        finally:
            del sys.frozen  # type: ignore[attr-defined]


class MigrateLegacyRuntimeTests(unittest.TestCase):
    """One-time copy-forward of the pre-v0.1.9 exe-adjacent state layout."""

    def setUp(self) -> None:
        self.launcher = _load_launcher()

    def _make_legacy_bundle(self) -> tuple[Path, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        root = base / "bundle"
        legacy = root / "runtime"
        (legacy / "secrets").mkdir(parents=True)
        (legacy / "logs").mkdir()
        (legacy / "smart_commissioning.db").write_bytes(b"legacy-db")
        (legacy / "secrets" / ".secret_store_key").write_bytes(b"fernet-key")
        (legacy / "logs" / "crash-1.log").write_text("old crash", encoding="utf-8")
        # Pre-v0.1.9 backend-derived state (imports, edge identity) lived in a
        # SECOND runtime dir under backend/.
        backend_runtime = root / "backend" / "runtime"
        (backend_runtime / "imports" / "files").mkdir(parents=True)
        (backend_runtime / "imports" / "files" / "upload.bin").write_bytes(b"blob")
        (backend_runtime / "edge_id").write_text("edge-1", encoding="utf-8")
        return root, base / "stable"

    def test_copies_db_secrets_imports_identity_but_not_logs(self) -> None:
        root, stable = self._make_legacy_bundle()

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.migrate_legacy_runtime(root, stable)

        self.assertEqual((stable / "smart_commissioning.db").read_bytes(), b"legacy-db")
        self.assertEqual(
            (stable / "secrets" / ".secret_store_key").read_bytes(), b"fernet-key"
        )
        self.assertEqual(
            (stable / "imports" / "files" / "upload.bin").read_bytes(), b"blob"
        )
        self.assertEqual((stable / "edge_id").read_text(encoding="utf-8"), "edge-1")
        self.assertFalse((stable / "logs").exists(), "crash logs are not state")
        self.assertIn("Migrated existing app data", buffer.getvalue())
        # The legacy folder stays behind as a rollback copy.
        self.assertTrue((root / "runtime" / "smart_commissioning.db").exists())

    def test_never_overwrites_an_existing_stable_database(self) -> None:
        root, stable = self._make_legacy_bundle()
        stable.mkdir(parents=True)
        (stable / "smart_commissioning.db").write_bytes(b"current-db")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.migrate_legacy_runtime(root, stable)

        self.assertEqual(
            (stable / "smart_commissioning.db").read_bytes(), b"current-db"
        )
        self.assertFalse((stable / "secrets").exists())
        self.assertEqual(buffer.getvalue(), "")

    def test_noop_without_a_legacy_database(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        root = base / "bundle"
        root.mkdir()

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.migrate_legacy_runtime(root, base / "stable")

        self.assertFalse((base / "stable").exists())
        self.assertEqual(buffer.getvalue(), "")

    def test_partial_migration_retries_on_next_launch(self) -> None:
        # A crash mid-migration leaves sidecar state but no database (the db
        # is copied LAST as the completion marker) — the next launch must
        # finish the job rather than strand the remaining state.
        root, stable = self._make_legacy_bundle()
        (stable / "secrets").mkdir(parents=True)
        (stable / "secrets" / ".secret_store_key").write_bytes(b"partial")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.migrate_legacy_runtime(root, stable)

        self.assertEqual((stable / "smart_commissioning.db").read_bytes(), b"legacy-db")
        self.assertEqual(
            (stable / "secrets" / ".secret_store_key").read_bytes(), b"fernet-key"
        )
        self.assertIn("Migrated existing app data", buffer.getvalue())

    def test_wal_sidecars_copied_with_the_database(self) -> None:
        root, stable = self._make_legacy_bundle()
        (root / "runtime" / "smart_commissioning.db-wal").write_bytes(b"wal")
        (root / "runtime" / "smart_commissioning.db-shm").write_bytes(b"shm")

        with contextlib.redirect_stdout(io.StringIO()):
            self.launcher.migrate_legacy_runtime(root, stable)

        self.assertEqual(
            (stable / "smart_commissioning.db-wal").read_bytes(), b"wal"
        )
        self.assertEqual(
            (stable / "smart_commissioning.db-shm").read_bytes(), b"shm"
        )

    def test_same_path_dev_layout_is_a_noop(self) -> None:
        root, _ = self._make_legacy_bundle()

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.launcher.migrate_legacy_runtime(root, root / "runtime")

        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
