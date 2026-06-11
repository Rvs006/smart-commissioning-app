from __future__ import annotations

import importlib
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


APP_NAME = "Smart Commissioning App"
DEFAULT_PORT = 8000


def _bundle_dependency_imports() -> None:
    """Keep PyInstaller aware of runtime dependencies imported by backend/app."""
    import alembic  # noqa: F401
    import dramatiq  # noqa: F401
    import fastapi  # noqa: F401
    import fastapi.middleware.cors  # noqa: F401
    import fastapi.responses  # noqa: F401
    import fastapi.staticfiles  # noqa: F401
    import httpx  # noqa: F401
    import multipart  # noqa: F401
    import openpyxl  # noqa: F401
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


def reserve_port(start: int = DEFAULT_PORT, attempts: int = 50) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available local port found from {start} to {start + attempts - 1}.")


def configure_environment(root: Path) -> None:
    backend_root = root / "backend"
    core_root = root / "core"
    frontend_dist = root / "frontend" / "dist"
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
    os.environ["SMART_COMMISSIONING_RUNS_ROOT"] = str(runtime_root / "runs")
    os.environ["SMART_COMMISSIONING_SECRETS_ROOT"] = str(runtime_root / "secrets")
    # Run/import/configuration records live in this SQLite file; the API
    # applies migrations on startup (AUTO_MIGRATE defaults to true).
    os.environ.setdefault(
        "DATABASE_URL",
        f"sqlite:///{(runtime_root / 'smart_commissioning.db').as_posix()}",
    )
    os.environ.setdefault("ENVIRONMENT", "portable_windows")
    os.environ.setdefault("JOB_EXECUTION_MODE", "inline")
    os.environ.setdefault("ALLOW_INLINE_WORKER_FALLBACK", "true")


def open_browser_later(url: str) -> None:
    def open_url() -> None:
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_url, daemon=True).start()


def main() -> int:
    root = app_root()

    try:
        configure_environment(root)
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
    print("Keep this window open while testing. Press Ctrl+C to stop the app.")
    open_browser_later(url)

    uvicorn.run(app_module.app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
