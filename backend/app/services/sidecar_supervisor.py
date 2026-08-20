"""Supervise the vendored local scanner sidecars (loopback-only helper processes).

The IP scanner sidecar is the vendored ``scanners/vendor/network-ip-scanner``
Node app. It serves a small HTTP API (register import, SSE scan, compare) on a
loopback port. This supervisor owns that process's whole lifecycle for the
backend: it reserves a free loopback port, spawns the app bound to it with
``PORT`` + ``NO_OPEN=1`` (so it never opens a browser or listens off-host),
polls ``GET /api/health`` until it answers, and terminates the child on
shutdown so nothing is orphaned.

HONESTY / FAIL-SOFT: a sidecar that will not start must never crash app
startup. ``start_all`` logs the failure and leaves that sidecar marked
unavailable; ``port_for`` then raises :class:`SidecarUnavailable`, which the
scanners route turns into a 503. The adapter engine likewise records a real
failed run when it cannot reach the sidecar — it never fabricates results.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Frozen sidecar names (the scanners route + adapters resolve the port by name).
IP_SCANNER = "network-ip-scanner"
BACNET_SCANNER = "bacnet-scanner"
MQTT_SCANNER = "mqtt-discovery"

# Repo root: backend/app/services/sidecar_supervisor.py -> parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _sidecar_dir(name: str, env_var: str) -> Path:
    """Vendored app directory for a sidecar, overridable by env var."""
    return Path(os.getenv(env_var, str(_REPO_ROOT / "scanners" / "vendor" / name))).expanduser()


# The managed sidecars start_all spawns, as (name, vendored-app-dir). Each is a
# Node app serving a loopback HTTP API with a GET /api/health probe.
_MANAGED_SIDECARS: tuple[tuple[str, Path], ...] = (
    (IP_SCANNER, _sidecar_dir(IP_SCANNER, "SMART_COMMISSIONING_IP_SCANNER_DIR")),
    (BACNET_SCANNER, _sidecar_dir(BACNET_SCANNER, "SMART_COMMISSIONING_BACNET_SCANNER_DIR")),
    (MQTT_SCANNER, _sidecar_dir(MQTT_SCANNER, "SMART_COMMISSIONING_MQTT_SCANNER_DIR")),
)

# How long to wait for the child to answer /api/health before giving up.
_HEALTH_TIMEOUT_S = float(os.getenv("SMART_COMMISSIONING_SIDECAR_HEALTH_TIMEOUT_S", "15"))
_HEALTH_POLL_INTERVAL_S = 0.25


class SidecarUnavailable(RuntimeError):
    """Raised by :meth:`SidecarSupervisor.port_for` for a sidecar that is not up."""


def reserve_port() -> int:
    """Reserve a free loopback TCP port by binding :0 and reading it back.

    ponytail: classic bind-:0 TOCTOU — the socket closes here and the child
    binds a moment later, so another process could grab the port in between.
    Acceptable for a single local sidecar; retry once on the child's EADDRINUSE
    if this ever bites.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _entry_args(sidecar_dir: Path) -> list[str]:
    """Node entry: portable ``dist/bundle.js`` if built, else dev ``server.js``."""
    bundle = sidecar_dir / "dist" / "bundle.js"
    if bundle.is_file():
        return ["dist/bundle.js"]
    return ["server.js"]


class _Sidecar:
    """One spawned child + its reserved port; None process means unavailable."""

    __slots__ = ("name", "port", "process")

    def __init__(self, name: str, port: int, process: subprocess.Popen | None) -> None:
        self.name = name
        self.port = port
        self.process = process


class SidecarSupervisor:
    """Own the lifecycle of the vendored loopback scanner sidecar processes."""

    def __init__(self) -> None:
        self._sidecars: dict[str, _Sidecar] = {}

    async def start_all(self) -> None:
        """Start every managed sidecar. Never raises — failures mark unavailable."""
        for name, sidecar_dir in _MANAGED_SIDECARS:
            self._start_sidecar(name, sidecar_dir)

    async def stop_all(self) -> None:
        """Terminate every spawned child so nothing is orphaned."""
        for sidecar in self._sidecars.values():
            _terminate(sidecar)
        self._sidecars.clear()

    def port_for(self, name: str) -> int:
        """Return the loopback port a running sidecar is bound to.

        Raises :class:`SidecarUnavailable` when the sidecar failed to start or
        was never registered, so callers translate it to a 503 rather than
        pretending a scan can run.
        """
        sidecar = self._sidecars.get(name)
        if sidecar is None or sidecar.process is None:
            raise SidecarUnavailable(f"sidecar '{name}' is not available")
        if sidecar.process.poll() is not None:
            raise SidecarUnavailable(f"sidecar '{name}' has exited")
        return sidecar.port

    def base_url_for(self, name: str) -> str:
        """Loopback base URL (``http://127.0.0.1:<port>``) for a running sidecar."""
        return f"http://127.0.0.1:{self.port_for(name)}"

    # -- internals ---------------------------------------------------------

    def _start_sidecar(self, name: str, sidecar_dir: Path) -> None:
        try:
            node = shutil.which("node")
            if node is None:
                logger.warning("Sidecar '%s' unavailable: 'node' is not on PATH.", name)
                self._sidecars[name] = _Sidecar(name, 0, None)
                return
            if not sidecar_dir.is_dir():
                logger.warning("Sidecar '%s' unavailable: %s is missing.", name, sidecar_dir)
                self._sidecars[name] = _Sidecar(name, 0, None)
                return

            port = reserve_port()
            env = dict(os.environ)
            env["PORT"] = str(port)
            env["NO_OPEN"] = "1"  # never auto-open a browser (loopback headless)
            # New process group on Windows so we can signal the tree on stop.
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
            process = subprocess.Popen(  # noqa: S603 (fixed args, vendored app)
                [node, *_entry_args(sidecar_dir)],
                cwd=str(sidecar_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            if not _await_health(port, process):
                logger.warning("Sidecar '%s' did not become healthy on port %d; marking unavailable.", name, port)
                _terminate(_Sidecar(name, port, process))
                self._sidecars[name] = _Sidecar(name, 0, None)
                return
            self._sidecars[name] = _Sidecar(name, port, process)
            logger.info("Sidecar '%s' started on 127.0.0.1:%d.", name, port)
        except Exception:  # noqa: BLE001 (a sidecar must never crash app startup)
            logger.exception("Failed to start sidecar '%s'; marking unavailable.", name)
            self._sidecars[name] = _Sidecar(name, 0, None)


def _await_health(port: int, process: subprocess.Popen) -> bool:
    """Poll ``GET /api/health`` until it answers ``ok`` or the deadline passes."""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False  # child died during boot
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:  # noqa: S310 (fixed loopback URL)
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    return False


def _terminate(sidecar: _Sidecar) -> None:
    """Best-effort stop of one child and its tree; never raises."""
    process = sidecar.process
    if process is None or process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill /T reaps any grandchildren the node app may have spawned.
            subprocess.run(  # noqa: S603,S607
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
        process.wait(timeout=6.0)
    except Exception:  # noqa: BLE001 (shutdown cleanup is best-effort)
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            logger.debug("Could not kill sidecar '%s'.", sidecar.name, exc_info=True)
