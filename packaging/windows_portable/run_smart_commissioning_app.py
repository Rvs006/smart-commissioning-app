from __future__ import annotations

import datetime as _dt
import faulthandler
import importlib
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path


APP_NAME = "Smart Commissioning App"
DEFAULT_PORT = 8000

# Keep a reference to the always-open faulthandler log so the OS does not close
# it; a hard crash (segfault, C-level fault) is dumped here by faulthandler.
_FAULTHANDLER_FILE = None


def install_crash_logging(runtime_root: Path) -> Path | None:
    """Write uncaught exceptions (and low-level faults) to a timestamped file.

    Field failures in the portable .exe are otherwise invisible — the console
    window closes and nothing is captured. This installs a ``sys.excepthook``
    that appends a full traceback to ``<runtime_root>/logs/crash-*.log`` and (if
    available) enables ``faulthandler`` so even interpreter-level crashes leave a
    dump. Local file only: there is NO network upload. Fully guarded so a logging
    failure can never prevent the app from starting; returns the log directory
    on success or ``None`` if crash logging could not be installed.
    """
    global _FAULTHANDLER_FILE
    try:
        log_dir = runtime_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        crash_path = log_dir / f"crash-{stamp}.log"

        def _excepthook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
            try:
                with crash_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"=== {APP_NAME} crash {_dt.datetime.now().isoformat()} ===\n")
                    traceback.print_exception(exc_type, exc_value, exc_tb, file=handle)
                    handle.write("\n")
            except Exception:  # noqa: BLE001 (crash logging must never raise)
                pass
            # Preserve default behaviour: still print to stderr.
            sys.__excepthook__(exc_type, exc_value, exc_tb)

        sys.excepthook = _excepthook

        try:
            fault_path = log_dir / f"faulthandler-{stamp}.log"
            _FAULTHANDLER_FILE = fault_path.open("a", encoding="utf-8")
            faulthandler.enable(file=_FAULTHANDLER_FILE)
        except Exception:  # noqa: BLE001 (faulthandler is best-effort)
            _FAULTHANDLER_FILE = None

        return log_dir
    except Exception:  # noqa: BLE001 (never block startup on crash-log setup)
        return None


def _bundle_dependency_imports() -> None:
    """Keep PyInstaller aware of runtime dependencies imported by backend/app."""
    import alembic  # noqa: F401
    # bacpypes3 (real BACnet/IP discovery backend, core's [bacnet] extra) is
    # imported lazily via a string import in Bacpypes3Backend._ensure_app, so
    # PyInstaller cannot trace it from the launcher. Naming it here (belt-and-
    # braces alongside --collect-all bacpypes3 in build.ps1) keeps it in the
    # freeze. Optional dep, same pattern as psutil below: if it is ever absent an
    # authorized real scan honestly RuntimeErrors instead of faking a result.
    import bacpypes3  # noqa: F401
    import dramatiq  # noqa: F401
    import fastapi  # noqa: F401
    import fastapi.middleware.cors  # noqa: F401
    import fastapi.responses  # noqa: F401
    import fastapi.staticfiles  # noqa: F401
    import httpx  # noqa: F401
    import multipart  # noqa: F401
    import openpyxl  # noqa: F401
    import prometheus_client  # noqa: F401
    # psutil is import-guarded in backend/app (degrades to an Auto-only NIC
    # list when absent), so a missing module here breaks the Source Interface
    # picker silently instead of crashing boot — keep it frozen explicitly.
    import psutil  # noqa: F401
    import psycopg  # noqa: F401
    import pydantic  # noqa: F401
    import pydantic_core  # noqa: F401
    import pydantic_settings  # noqa: F401
    import redis  # noqa: F401
    import smart_commissioning_core  # noqa: F401
    import sqlalchemy  # noqa: F401
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_root(root: Path) -> Path:
    """Machine-stable state directory, decoupled from the per-release exe folder.

    ThreatLocker approves the portable exe per file hash, so every release
    lands in a fresh folder. State anchored next to the exe (the pre-v0.1.9
    layout) was silently reset on every upgrade — the field-reported
    "re-enter broker credentials, certs and NIC on every open". Frozen builds
    therefore keep state in ``%LOCALAPPDATA%\\SmartCommissioning`` (per-user,
    survives upgrades); ``SMART_COMMISSIONING_DATA_DIR`` overrides it for
    roaming/USB profiles. The unfrozen dev layout keeps ``<repo>/runtime`` so
    developer state stays inside the checkout.
    """
    override = os.environ.get("SMART_COMMISSIONING_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "SmartCommissioning"
    return root / "runtime"


def migrate_legacy_runtime(root: Path, runtime_root: Path) -> None:
    """One-time copy-forward of pre-v0.1.9 exe-adjacent state.

    Earlier portable builds kept the database and secrets in
    ``<exe-folder>/runtime`` and import files / edge identity in
    ``<exe-folder>/backend/runtime``. When this launcher finds such state next
    to the exe and the stable data dir has no database yet, it copies the
    state forward (db, secrets incl. the Fernet key, imports, edge identity;
    logs stay behind) so an in-place upgrade keeps the site configuration.
    Never overwrites an existing stable-dir database and never deletes the
    legacy folders — they remain as a rollback copy. The database is copied
    LAST because its presence is also the completed-migration marker (the
    guard above): a failure or interrupt mid-copy leaves no stable-dir
    database, so the next launch retries the whole migration instead of
    stranding the not-yet-copied state. Fully best-effort: any failure is
    reported and startup continues (a retry happens on the next launch).
    """
    db_name = "smart_commissioning.db"
    legacy_roots = [root / "runtime", root / "backend" / "runtime"]
    legacy_db = legacy_roots[0] / db_name
    try:
        if not legacy_db.exists():
            return
        if (runtime_root / db_name).exists():
            return
        if legacy_roots[0].resolve() == runtime_root.resolve():
            return
        runtime_root.mkdir(parents=True, exist_ok=True)
        for legacy in legacy_roots:
            if legacy.is_dir():
                shutil.copytree(
                    legacy,
                    runtime_root,
                    # db* also skips -wal/-shm sidecars; they are copied below,
                    # just before the db itself.
                    ignore=shutil.ignore_patterns("logs", f"{db_name}*"),
                    dirs_exist_ok=True,
                )
        for sidecar_name in (f"{db_name}-wal", f"{db_name}-shm"):
            sidecar = legacy_roots[0] / sidecar_name
            if sidecar.exists():
                shutil.copy2(sidecar, runtime_root / sidecar_name)
        shutil.copy2(legacy_db, runtime_root / db_name)
        print(f"Migrated existing app data from {legacy_roots[0]} to {runtime_root}.")
    except Exception as error:  # noqa: BLE001 (migration must never block startup)
        print(f"WARNING: could not migrate previous app data from {legacy_roots[0]}: {error}")


def reserve_port(start: int = DEFAULT_PORT, attempts: int = 50) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available local port found from {start} to {start + attempts - 1}.")


def _set_env_default(name: str, value: str) -> None:
    """``os.environ.setdefault`` that surfaces a pre-existing override.

    The portable profile wants a specific local/inline/sqlite value for each of
    these variables. ``setdefault`` preserves any pre-existing value as a
    deliberate escape hatch, but a stray/leftover env var (e.g. a ``DATABASE_URL``
    pointing at a remote Postgres) would otherwise swap in a different profile
    *silently*. Warn when that happens — naming the VARIABLE only, never its value,
    since ``DATABASE_URL`` may embed a password — then defer to ``setdefault`` so
    the override still stands.
    """
    existing = os.environ.get(name)
    if existing is not None and existing != value:
        print(
            f"WARNING: {name} is already set in the environment; keeping that "
            f"value and NOT applying the portable default (value hidden)."
        )
    os.environ.setdefault(name, value)


def configure_environment(root: Path, runtime_root: Path | None = None) -> None:
    backend_root = root / "backend"
    core_root = root / "core"
    frontend_dist = root / "frontend" / "dist"
    if runtime_root is None:
        # Unfrozen dev layout / legacy callers: state stays inside the checkout.
        runtime_root = root / "runtime"

    if not backend_root.exists():
        raise FileNotFoundError(f"Backend folder missing: {backend_root}")
    if not (frontend_dist / "index.html").exists():
        raise FileNotFoundError(f"Built frontend missing: {frontend_dist / 'index.html'}")

    sys.path.insert(0, str(backend_root))
    if core_root.exists():
        # Resolve smart_commissioning_core from the bundled source tree when it
        # is not already importable (for example in the unfrozen dev layout).
        sys.path.insert(0, str(core_root))
    os.environ["SCT_FRONTEND_DIST"] = str(frontend_dist)
    os.environ["SMART_COMMISSIONING_SECRETS_ROOT"] = str(runtime_root / "secrets")
    # Anchor the backend-derived paths (imports, edge identity, default sqlite
    # location) to the same stable dir; without this app.core.runtime derives
    # them from the per-release backend folder and they are lost on upgrade.
    os.environ["SMART_COMMISSIONING_RUNTIME_ROOT"] = str(runtime_root)
    # Run/import/configuration records live in this SQLite file; the API
    # applies migrations on startup (AUTO_MIGRATE defaults to true).
    _set_env_default(
        "DATABASE_URL",
        f"sqlite:///{(runtime_root / 'smart_commissioning.db').as_posix()}",
    )
    _set_env_default("ENVIRONMENT", "portable_windows")
    # Single-user edge profile bound to 127.0.0.1 only, so skip API-key auth;
    # the hosted compose profile (infra/) sets AUTH_MODE=api_key instead.
    _set_env_default("AUTH_MODE", "local")
    _set_env_default("JOB_EXECUTION_MODE", "inline")
    _set_env_default("ALLOW_INLINE_WORKER_FALLBACK", "true")


def open_browser_later(url: str) -> None:
    def open_url() -> None:
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_url, daemon=True).start()


def main() -> int:
    root = app_root()
    runtime_root = data_root(root)

    # Install local crash logging before anything else so even a failure during
    # environment setup / app import is captured to a file (the portable exe has
    # no attached console to read otherwise).
    install_crash_logging(runtime_root)
    migrate_legacy_runtime(root, runtime_root)

    try:
        configure_environment(root, runtime_root)
        app_module = importlib.import_module("app.main")
        uvicorn = importlib.import_module("uvicorn")
    except Exception as error:
        print(f"{APP_NAME} could not start.")
        print(str(error))
        input("Press Enter to close...")
        return 1

    port = reserve_port()
    url = f"http://127.0.0.1:{port}/"

    print(f"{APP_NAME} is starting.")
    print(f"App URL: {url}")
    print(f"App data (settings, certs, run history): {runtime_root}")
    print("Keep this window open while testing. Press Ctrl+C to stop the app.")
    open_browser_later(url)

    uvicorn.run(app_module.app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
