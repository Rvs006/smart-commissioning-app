#!/usr/bin/env python3
"""Exercise the workflow-built Windows portable exe against a quiet MQTT broker."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

_TERMINAL = {"succeeded", "failed", "cancelled"}
_PASSWORD_MARKER = "portable-release-password-must-not-leak"


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("MQTT client closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_packet(connection: socket.socket) -> tuple[int, bytes]:
    packet_type = _recv_exact(connection, 1)[0]
    multiplier = 1
    remaining = 0
    for _ in range(4):
        digit = _recv_exact(connection, 1)[0]
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return packet_type, _recv_exact(connection, remaining)
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


def _remaining_length(size: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = size % 128
        size //= 128
        if size:
            digit |= 0x80
        encoded.append(digit)
        if not size:
            return bytes(encoded)


def _send_packet(connection: socket.socket, packet_type: int, payload: bytes = b"") -> None:
    connection.sendall(bytes([packet_type]) + _remaining_length(len(payload)) + payload)


def _subscription_count(payload: bytes) -> int:
    position = 2  # packet identifier
    count = 0
    while position < len(payload):
        if position + 2 > len(payload):
            raise ValueError("truncated MQTT topic length")
        topic_length = int.from_bytes(payload[position : position + 2], "big")
        position += 2 + topic_length
        if position >= len(payload):
            raise ValueError("truncated MQTT subscription")
        position += 1  # requested QoS
        count += 1
    return count


class QuietMqttBroker:
    """Minimal MQTT 3.1.1 broker: acknowledge, then publish nothing."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.2)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._subscription_sessions = 0
        self._errors: list[str] = []
        self._clients: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._serve, name="quiet-mqtt-broker", daemon=True)

    def __enter__(self) -> QuietMqttBroker:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=3)
        for thread in self._clients:
            thread.join(timeout=3)
        if self._thread.is_alive() or any(thread.is_alive() for thread in self._clients):
            raise RuntimeError("quiet MQTT broker thread leaked")
        if self._errors:
            raise RuntimeError(f"quiet MQTT broker failed: {self._errors[0]}")

    def wait_for_subscription_sessions(self, count: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._subscription_sessions < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"quiet MQTT broker saw fewer than {count} subscription sessions")
                self._condition.wait(remaining)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    self._errors.append("listener stopped unexpectedly")
                return
            thread = threading.Thread(
                target=self._serve_client,
                args=(connection,),
                name="quiet-mqtt-client",
                daemon=True,
            )
            self._clients.append(thread)
            thread.start()

    def _serve_client(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(2)
            packet_type, _payload = _read_packet(connection)
            if packet_type != 0x10:
                raise ValueError("first MQTT packet was not CONNECT")
            _send_packet(connection, 0x20, b"\x00\x00")
            packet_type, payload = _read_packet(connection)
            if packet_type != 0x82:
                raise ValueError("second MQTT packet was not SUBSCRIBE")
            topic_count = _subscription_count(payload)
            _send_packet(connection, 0x90, payload[:2] + (b"\x00" * topic_count))
            with self._condition:
                self._subscription_sessions += 1
                self._condition.notify_all()
            connection.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    packet_type, _payload = _read_packet(connection)
                except TimeoutError:
                    continue
                except ConnectionError:
                    return
                if packet_type == 0xC0:
                    _send_packet(connection, 0xD0)
                elif packet_type == 0xE0:
                    return
        except OSError:
            if not self._stop.is_set():
                self._errors.append("client socket stopped unexpectedly")
        except (ConnectionError, ValueError) as error:
            if not self._stop.is_set():
                self._errors.append(str(error))
        finally:
            connection.close()


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    body, _headers = _request(base_url, path, method=method, payload=payload)
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} did not return a JSON object")
    return value


def _assert_app_root(base_url: str) -> None:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GET / returned HTTP {error.code}") from error
    if status != 200 or b'<div id="root"></div>' not in body:
        raise RuntimeError("GET / did not return HTTP 200 with the portable app root")


def _wait_run(base_url: str, run_id: str, timeout: float = 45.0) -> tuple[dict[str, Any], list[str]]:
    deadline = time.monotonic() + timeout
    stages: list[str] = []
    while time.monotonic() < deadline:
        run = _json_request(base_url, f"api/v1/validation/runs/{run_id}")
        stage = str(run.get("stage") or "")
        if not stages or stages[-1] != stage:
            stages.append(stage)
        if run.get("status") in _TERMINAL:
            return run, stages
        time.sleep(0.2)
    raise TimeoutError(f"run {run_id} did not reach a terminal status")


def _wait_stage(base_url: str, run_id: str, expected: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = _json_request(base_url, f"api/v1/validation/runs/{run_id}")
        if run.get("stage") == expected:
            return run
        if run.get("status") in _TERMINAL:
            raise RuntimeError(f"run {run_id} became {run.get('status')} at {run.get('stage')}")
        time.sleep(0.1)
    raise TimeoutError(f"run {run_id} did not enter {expected}")


def _sqlite_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"portable SQLite run has no {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"portable SQLite {field} is not an ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_sqlite_datetime(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _sqlite_datetime(value, field=field)


def _lease_snapshot(database_path: Path, run_id: str, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if database_path.is_file():
            try:
                uri = f"{database_path.resolve().as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=0.5)
                try:
                    connection.execute("PRAGMA busy_timeout = 500")
                    row = connection.execute(
                        """
                        SELECT status, claimed_at, heartbeat_at, lease_expires_at,
                               terminal_at, cancel_requested
                        FROM runs WHERE id = ?
                        """,
                        (run_id,),
                    ).fetchone()
                finally:
                    connection.close()
                if row is not None:
                    return {
                        "status": str(row[0]),
                        "claimed_at": _sqlite_datetime(row[1], field="claimed_at"),
                        "heartbeat_at": _sqlite_datetime(row[2], field="heartbeat_at"),
                        "lease_expires_at": _optional_sqlite_datetime(
                            row[3], field="lease_expires_at"
                        ),
                        "terminal_at": _optional_sqlite_datetime(row[4], field="terminal_at"),
                        "cancel_requested": bool(row[5]),
                    }
            except (OSError, sqlite3.Error, RuntimeError) as error:
                last_error = error
        time.sleep(0.05)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(f"could not read live lease state for run {run_id}{detail}")


def _assert_cancel_request_persisted(
    database_path: Path,
    run_id: str,
    response: dict[str, Any],
) -> None:
    if str(response.get("run_id") or "") != run_id:
        raise RuntimeError("Stop response did not identify the requested run")
    if str(response.get("status") or "") not in {"running", "cancelled"}:
        raise RuntimeError("Stop response did not retain a cancellable run status")
    if _lease_snapshot(database_path, run_id)["cancel_requested"] is not True:
        raise RuntimeError("Stop request did not persist cancel_requested")


def _observe_heartbeat_renewals(
    database_path: Path,
    run_id: str,
    *,
    expected_lease_seconds: float,
    expected_heartbeat_seconds: float,
    cadence_allowance_seconds: float,
) -> tuple[dict[str, object], datetime, dict[str, object], datetime]:
    first = _lease_snapshot(database_path, run_id)
    first_observed_at = datetime.now(UTC)
    if first["status"] != "running":
        raise RuntimeError("portable run was not running when lease proof started")
    initial_heartbeat = first["heartbeat_at"]
    original_expiry = first["lease_expires_at"]
    assert isinstance(initial_heartbeat, datetime)
    assert isinstance(original_expiry, datetime)
    tolerance = max(0.25, expected_heartbeat_seconds * 0.25)
    initial_window = (original_expiry - initial_heartbeat).total_seconds()
    initial_age = (first_observed_at - initial_heartbeat).total_seconds()
    if abs(initial_window - expected_lease_seconds) > tolerance:
        raise RuntimeError(
            "portable executable did not apply the requested lease window: "
            f"observed {initial_window:.3f}s, expected {expected_lease_seconds:.3f}s"
        )
    if initial_age < -tolerance or initial_age > cadence_allowance_seconds:
        raise RuntimeError("initial SQLite heartbeat exceeded the cadence allowance")
    if original_expiry <= first_observed_at:
        raise RuntimeError("initial SQLite lease was already expired")

    snapshots = [(first, first_observed_at)]
    heartbeat_values = {initial_heartbeat}
    last_heartbeat = initial_heartbeat
    deadline = time.monotonic() + max(8.0, (expected_heartbeat_seconds * 4.0) + 2.0)
    while len(heartbeat_values) < 3 and time.monotonic() < deadline:
        time.sleep(min(0.2, expected_heartbeat_seconds / 4.0))
        snapshot = _lease_snapshot(database_path, run_id, timeout=1.0)
        snapshot_observed_at = datetime.now(UTC)
        if snapshot["status"] != "running":
            raise RuntimeError("portable run stopped before heartbeat renewal proof completed")
        heartbeat_at = snapshot["heartbeat_at"]
        lease_expires_at = snapshot["lease_expires_at"]
        assert isinstance(heartbeat_at, datetime)
        assert isinstance(lease_expires_at, datetime)
        heartbeat_age = (snapshot_observed_at - heartbeat_at).total_seconds()
        lease_window = (lease_expires_at - heartbeat_at).total_seconds()
        if abs(lease_window - expected_lease_seconds) > tolerance:
            raise RuntimeError(
                "portable executable did not apply the requested lease window: "
                f"observed {lease_window:.3f}s, expected {expected_lease_seconds:.3f}s"
            )
        if heartbeat_at < last_heartbeat:
            raise RuntimeError("portable SQLite heartbeat moved backwards")
        if heartbeat_age < -tolerance or heartbeat_age > cadence_allowance_seconds:
            raise RuntimeError("initial SQLite heartbeat exceeded the cadence allowance")
        if lease_expires_at <= snapshot_observed_at:
            raise RuntimeError("initial SQLite lease expired before renewal proof completed")
        if heartbeat_at not in heartbeat_values:
            heartbeat_gap = (heartbeat_at - last_heartbeat).total_seconds()
            if heartbeat_gap > cadence_allowance_seconds:
                raise RuntimeError("initial SQLite heartbeat renewal had a cadence gap")
            heartbeat_values.add(heartbeat_at)
            snapshots.append((snapshot, snapshot_observed_at))
            last_heartbeat = heartbeat_at

    if len(heartbeat_values) < 3:
        raise RuntimeError("portable SQLite did not record two heartbeat renewals")
    renewed_expiries = [snapshot["lease_expires_at"] for snapshot, _at in snapshots[1:]]
    if not all(isinstance(value, datetime) and value > original_expiry for value in renewed_expiries):
        raise RuntimeError("portable heartbeats did not advance lease_expires_at")

    last_snapshot, last_observed_at = snapshots[-1]
    return (
        {
            "expected_lease_seconds": expected_lease_seconds,
            "expected_heartbeat_seconds": expected_heartbeat_seconds,
            "renewal_count": len(heartbeat_values) - 1,
            "initial_heartbeat_at": initial_heartbeat.isoformat(),
            "original_lease_expires_at": original_expiry.isoformat(),
            "last_heartbeat_at": last_snapshot["heartbeat_at"].isoformat(),
            "last_lease_expires_at": last_snapshot["lease_expires_at"].isoformat(),
        },
        original_expiry,
        last_snapshot,
        last_observed_at,
    )


def _monitor_running_heartbeats_until_terminal(
    base_url: str,
    database_path: Path,
    run_id: str,
    *,
    expected_lease_seconds: float,
    expected_heartbeat_seconds: float,
    cadence_allowance_seconds: float,
    original_lease_expiry: datetime,
    initial_running_snapshot: dict[str, object],
    initial_observed_at: datetime,
    boundary_grace_seconds: float = 6.0,
    timeout: float = 45.0,
) -> tuple[dict[str, Any], list[str], dict[str, object]]:
    """Continuously prove live heartbeats, retaining the last pre-terminal value."""
    if initial_running_snapshot["status"] != "running":
        raise RuntimeError("heartbeat monitor did not start from a running SQLite row")
    last_running_heartbeat = initial_running_snapshot["heartbeat_at"]
    last_running_expiry = initial_running_snapshot["lease_expires_at"]
    assert isinstance(last_running_heartbeat, datetime)
    assert isinstance(last_running_expiry, datetime)
    monitor_start_heartbeat = last_running_heartbeat
    boundary_target = original_lease_expiry + timedelta(seconds=boundary_grace_seconds)

    tolerance = max(0.25, expected_heartbeat_seconds * 0.25)
    sample_count = 0
    renewal_count = 0
    max_age = 0.0
    max_gap = 0.0
    stages: list[str] = []
    boundary_observation: dict[str, object] | None = None
    deadline = time.monotonic() + timeout
    poll_seconds = min(0.2, max(0.01, expected_heartbeat_seconds / 4.0))

    def validate_running(snapshot: dict[str, object], observed_at: datetime) -> None:
        nonlocal last_running_expiry
        nonlocal last_running_heartbeat
        nonlocal max_age
        nonlocal max_gap
        nonlocal renewal_count
        nonlocal sample_count

        heartbeat_at = snapshot["heartbeat_at"]
        lease_expires_at = snapshot["lease_expires_at"]
        assert isinstance(heartbeat_at, datetime)
        assert isinstance(lease_expires_at, datetime)
        heartbeat_age = (observed_at - heartbeat_at).total_seconds()
        lease_window = (lease_expires_at - heartbeat_at).total_seconds()
        if heartbeat_at < last_running_heartbeat:
            raise RuntimeError("portable SQLite heartbeat moved backwards")
        if heartbeat_age < -tolerance or heartbeat_age > cadence_allowance_seconds:
            raise RuntimeError("running SQLite heartbeat exceeded the cadence allowance")
        if lease_expires_at <= observed_at:
            raise RuntimeError("running SQLite lease expired during the live capture")
        if abs(lease_window - expected_lease_seconds) > tolerance:
            raise RuntimeError(
                "portable executable changed the requested lease window during capture: "
                f"observed {lease_window:.3f}s, expected {expected_lease_seconds:.3f}s"
            )
        if heartbeat_at > last_running_heartbeat:
            heartbeat_gap = (heartbeat_at - last_running_heartbeat).total_seconds()
            if heartbeat_gap > cadence_allowance_seconds:
                raise RuntimeError("portable SQLite heartbeat renewal had a cadence gap")
            renewal_count += 1
            max_gap = max(max_gap, heartbeat_gap)
            last_running_heartbeat = heartbeat_at
            last_running_expiry = lease_expires_at
        sample_count += 1
        max_age = max(max_age, max(0.0, heartbeat_age))

    validate_running(initial_running_snapshot, initial_observed_at)
    terminal_snapshot: dict[str, object] | None = None
    completed: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        run = _json_request(base_url, f"api/v1/validation/runs/{run_id}")
        stage = str(run.get("stage") or "")
        if not stages or stages[-1] != stage:
            stages.append(stage)
        snapshot = _lease_snapshot(database_path, run_id, timeout=1.0)
        observed_at = datetime.now(UTC)
        sqlite_status = str(snapshot["status"])
        if sqlite_status == "running":
            validate_running(snapshot, observed_at)
            if observed_at >= boundary_target and boundary_observation is None:
                boundary_heartbeat = snapshot["heartbeat_at"]
                boundary_expiry = snapshot["lease_expires_at"]
                assert isinstance(boundary_heartbeat, datetime)
                assert isinstance(boundary_expiry, datetime)
                if (
                    run.get("status") != "running"
                    or "lease" in stage.casefold()
                    or boundary_heartbeat <= monitor_start_heartbeat
                ):
                    raise RuntimeError(
                        "live run did not cross its original lease boundary with a renewal"
                    )
                boundary_observation = {
                    "boundary_observed_at": observed_at.isoformat(),
                    "boundary_heartbeat_at": boundary_heartbeat.isoformat(),
                    "boundary_lease_expires_at": boundary_expiry.isoformat(),
                    "boundary_heartbeat_age_seconds": (
                        observed_at - boundary_heartbeat
                    ).total_seconds(),
                }
        elif sqlite_status in _TERMINAL:
            terminal_at = snapshot["terminal_at"]
            assert isinstance(terminal_at, datetime)
            terminal_gap = (terminal_at - last_running_heartbeat).total_seconds()
            if not 0 <= terminal_gap <= cadence_allowance_seconds:
                raise RuntimeError(
                    "terminal time exceeded the cadence allowance from the last "
                    "pre-terminal heartbeat"
                )
            if boundary_observation is None:
                raise RuntimeError(
                    "portable run reached terminal before the original lease boundary proof"
                )
            terminal_snapshot = snapshot
            if run.get("status") in _TERMINAL:
                completed = run
                break
        else:
            raise RuntimeError(f"portable SQLite run entered unexpected status {sqlite_status}")
        time.sleep(poll_seconds)

    if terminal_snapshot is None or completed is None:
        raise TimeoutError(f"run {run_id} did not reach a terminal status")
    if completed.get("status") != terminal_snapshot["status"]:
        raise RuntimeError("run API and SQLite disagreed on terminal status")
    terminal_at = terminal_snapshot["terminal_at"]
    assert isinstance(terminal_at, datetime)
    terminal_gap = (terminal_at - last_running_heartbeat).total_seconds()
    assert boundary_observation is not None
    return (
        completed,
        stages,
        {
            "continuous_sample_count": sample_count,
            "continuous_renewal_count": renewal_count,
            "max_running_heartbeat_age_seconds": max_age,
            "max_running_heartbeat_gap_seconds": max_gap,
            "last_running_heartbeat_at": last_running_heartbeat.isoformat(),
            "last_running_lease_expires_at": last_running_expiry.isoformat(),
            "terminal_at": terminal_at.isoformat(),
            "terminal_from_last_running_heartbeat_seconds": terminal_gap,
            "cadence_allowance_seconds": cadence_allowance_seconds,
            **boundary_observation,
        },
    )


def _udmi_request(port: int, capture_seconds: float) -> dict[str, object]:
    return {
        "project_id": "portable-release-project",
        "site_id": "portable-release-site",
        "job_type": "udmi_validation",
        "parameters": {
            "requested_from": "windows_portable_release_smoke",
            "use_live_broker": True,
            "broker_host": "127.0.0.1",
            "broker_port": port,
            "use_tls": False,
            "username": "portable-release-user",
            "password": _PASSWORD_MARKER,
            "capture_seconds": capture_seconds,
            "state_topic": "portable/release/device/state",
            "expected_schedule": {
                "asset_id": "PORTABLE-RELEASE-1",
                "udmi_version": "1.5.2",
                "units": {},
            },
        },
    }


def _assert_no_severe_log_records(text: str, *, source: str, structured: bool) -> None:
    severe_levels = {"ERROR", "CRITICAL", "FATAL"}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if structured:
                raise RuntimeError(f"{source}:{line_number} is not a JSON log record") from None
            if re.search(r"(^|\s)(ERROR|CRITICAL|FATAL)(:|\s)", line):
                raise RuntimeError(
                    f"{source}:{line_number} contains a severe log line"
                ) from None
            continue
        if isinstance(record, dict) and str(record.get("level") or "").upper() in severe_levels:
            raise RuntimeError(f"{source}:{line_number} contains a severe JSON log record")

    for marker in (
        "Traceback (most recent call last)",
        "Exception in thread",
        "Unhandled exception",
        "Fatal Python error",
    ):
        if marker.casefold() in text.casefold():
            raise RuntimeError(f"{source} contains unhandled-error marker: {marker}")


def _assert_log_cleanup(
    data_dir: Path,
    run_ids: list[str],
    *,
    stdout_log: Path,
    stderr_log: Path,
) -> dict[str, int]:
    log_dir = data_dir / "logs"
    deadline = time.monotonic() + 5
    log_text = ""
    app_log_paths: list[Path] = []
    while time.monotonic() < deadline:
        app_log_paths = [
            path for path in sorted(log_dir.glob("app.log*")) if path.is_file()
        ]
        log_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in app_log_paths
        )
        if all(f"inline heartbeat stopped for run_id={run_id}" in log_text for run_id in run_ids):
            break
        time.sleep(0.1)
    for run_id in run_ids:
        started = log_text.count(f"inline heartbeat started for run_id={run_id}")
        stopped = log_text.count(f"inline heartbeat stopped for run_id={run_id}")
        if started != 1 or stopped != 1:
            raise RuntimeError(
                f"heartbeat log lifecycle for {run_id} was started={started}, stopped={stopped}"
            )
    forbidden = (
        _PASSWORD_MARKER,
        "Traceback (most recent call last)",
        "inline heartbeat did not stop promptly",
        "inline heartbeat refresh failed",
        "database is locked",
        "execution owner lease expired",
    )
    for marker in forbidden:
        if marker.casefold() in log_text.casefold():
            raise RuntimeError(f"portable logs contain forbidden marker: {marker}")
    crash_logs = [path for path in log_dir.glob("crash-*.log*") if path.stat().st_size]
    if crash_logs:
        raise RuntimeError(f"portable run wrote a crash log: {crash_logs[0].name}")
    _assert_no_severe_log_records(log_text, source="app.log", structured=True)
    redirected: dict[str, str] = {}
    for label, path in (("stdout", stdout_log), ("stderr", stderr_log)):
        if not path.is_file():
            raise RuntimeError(f"redirected {label} log is missing: {path}")
        redirected[label] = path.read_text(encoding="utf-8", errors="replace")
        _assert_no_severe_log_records(
            redirected[label],
            source=f"redirected {label}",
            structured=False,
        )
        for marker in forbidden:
            if marker.casefold() in redirected[label].casefold():
                raise RuntimeError(
                    f"redirected {label} contains forbidden marker: {marker}"
                )
    return {
        "heartbeat_started": len(run_ids),
        "heartbeat_stopped": len(run_ids),
        "app_log_files_scanned": len(app_log_paths),
        "stdout_bytes_scanned": len(redirected["stdout"].encode("utf-8")),
        "stderr_bytes_scanned": len(redirected["stderr"].encode("utf-8")),
        "severe_records": 0,
    }


def smoke(args: argparse.Namespace) -> dict[str, object]:
    if " " not in str(args.exe_path.resolve()) or " " not in str(args.data_dir.resolve()):
        raise RuntimeError("exe and data paths must both contain spaces")
    if not args.exe_path.is_file():
        raise FileNotFoundError(args.exe_path)
    database_path = args.data_dir / "smart_commissioning.db"

    _assert_app_root(args.base_url)
    health = _json_request(args.base_url, "api/v1/health")
    if health.get("status") != "ok":
        raise RuntimeError("health endpoint did not report ok")
    readiness = _json_request(args.base_url, "api/v1/ready")
    database = readiness.get("checks", {}).get("database", {})
    if readiness.get("status") != "ready" or database.get("status") != "ok":
        raise RuntimeError("readiness did not report ready with database=ok")

    # Span two full lease windows plus a recovery margin. This keeps the run live
    # long enough for repeated database observations and for maintenance to reap
    # a heartbeat loop that dies after a few initial renewals.
    capture_seconds = (args.lease_seconds * 2.0) + 10.0
    with QuietMqttBroker() as broker:
        accepted_at = time.monotonic()
        accepted = _json_request(
            args.base_url,
            "api/v1/validation/udmi/runs",
            method="POST",
            payload=_udmi_request(broker.port, capture_seconds),
        )
        accepted_returned_at = time.monotonic()
        accepted_elapsed = accepted_returned_at - accepted_at
        source_run_id = str(accepted.get("run_id") or "")
        if not source_run_id or accepted_elapsed >= 5:
            raise RuntimeError("asynchronous inline run did not return a run id promptly")
        broker.wait_for_subscription_sessions(1)
        _wait_stage(args.base_url, source_run_id, "capturing_live_mqtt")

        cadence_allowance = max(
            args.heartbeat_seconds * 2.5,
            args.heartbeat_seconds + 1.0,
        )
        (
            heartbeat_observation,
            original_lease_expiry,
            monitor_start_snapshot,
            monitor_started_at,
        ) = _observe_heartbeat_renewals(
            database_path,
            source_run_id,
            expected_lease_seconds=args.lease_seconds,
            expected_heartbeat_seconds=args.heartbeat_seconds,
            cadence_allowance_seconds=cadence_allowance,
        )
        completed, completed_stages, continuous_observation = (
            _monitor_running_heartbeats_until_terminal(
                args.base_url,
                database_path,
                source_run_id,
                expected_lease_seconds=args.lease_seconds,
                expected_heartbeat_seconds=args.heartbeat_seconds,
                cadence_allowance_seconds=cadence_allowance,
                original_lease_expiry=original_lease_expiry,
                initial_running_snapshot=monitor_start_snapshot,
                initial_observed_at=monitor_started_at,
            )
        )
        heartbeat_observation.update(continuous_observation)
        if completed.get("status") != "succeeded":
            raise RuntimeError(f"quiet inline run ended as {completed.get('status')}")
        if completed.get("stage") != "udmi_validation_complete_with_silent_devices":
            raise RuntimeError(f"quiet inline run ended at unexpected stage {completed.get('stage')}")
        if any("lease" in stage.casefold() for stage in completed_stages):
            raise RuntimeError("quiet inline run history contained a lease-expired stage")
        terminal_lease = _lease_snapshot(database_path, source_run_id)
        terminal_at = terminal_lease["terminal_at"]
        assert isinstance(terminal_at, datetime)
        if (
            terminal_lease["status"] != "succeeded"
            or terminal_at.isoformat() != heartbeat_observation["terminal_at"]
        ):
            raise RuntimeError("terminal SQLite state changed after heartbeat monitoring")
        serialized_run = json.dumps(completed, sort_keys=True)
        if _PASSWORD_MARKER in serialized_run or completed.get("parameters", {}).get("password") != "********":
            raise RuntimeError("run API did not redact the synthetic broker password")

        cancelled = _json_request(
            args.base_url,
            "api/v1/validation/udmi/runs",
            method="POST",
            payload=_udmi_request(broker.port, 60.0),
        )
        cancelled_run_id = str(cancelled.get("run_id") or "")
        broker.wait_for_subscription_sessions(2)
        _wait_stage(args.base_url, cancelled_run_id, "capturing_live_mqtt")
        cancel_response = _json_request(
            args.base_url,
            f"api/v1/runs/{cancelled_run_id}/cancel",
            method="POST",
        )
        _assert_cancel_request_persisted(database_path, cancelled_run_id, cancel_response)
        cancelled_terminal, cancelled_stages = _wait_run(args.base_url, cancelled_run_id)
        if cancelled_terminal.get("status") != "cancelled":
            raise RuntimeError("Stop request did not produce cancelled terminal status")
        if any("lease" in stage.casefold() for stage in cancelled_stages):
            raise RuntimeError("cancelled inline run history contained a lease-expired stage")

    canonical = _json_request(
        args.base_url,
        "api/v1/validation/udmi/runs",
        method="POST",
        payload={
            "project_id": "portable-release-project",
            "site_id": "portable-release-site",
            "job_type": "udmi_validation",
            "parameters": {
                "requested_from": "windows_portable_release_smoke",
                "expected_schedule": {
                    "asset_id": "PORTABLE-RELEASE-1",
                    "udmi_version": "1.5.2",
                    "units": {},
                },
                "state_payload": {
                    "version": "1.5.2",
                    "timestamp": "2026-07-26T00:00:00Z",
                    "system": {
                        "serial_no": "PORTABLE-RELEASE-1",
                        "last_config": "2026-07-26T00:00:00Z",
                        "hardware": {"make": "Release", "model": "Portable"},
                        "software": {},
                        "operation": {"operational": True},
                        "not_in_udmi_schema": True,
                    },
                },
            },
        },
    )
    canonical_id = str(canonical.get("run_id") or "")
    canonical_terminal, _canonical_stages = _wait_run(args.base_url, canonical_id)
    if canonical_terminal.get("status") != "succeeded":
        raise RuntimeError("canonical UDMI validation smoke did not finish")
    issues = _json_request(args.base_url, f"api/v1/validation/runs/{canonical_id}/issues")
    if not any(
        "canonical UDMI 1.5.2 state schema" in str(issue.get("description") or "")
        for issue in issues.get("issues", [])
        if isinstance(issue, dict)
    ):
        raise RuntimeError("canonical nested-schema issue was not recorded")

    report = _json_request(
        args.base_url,
        "api/v1/reports",
        method="POST",
        payload={
            "project_id": "portable-release-project",
            "site_id": "portable-release-site",
            "report_type": "udmi_validation",
            "output_format": "zip",
            "source_run_ids": [source_run_id],
            "report_title": "Portable Release Validation",
        },
    )
    report_id = str(report.get("report_id") or "")
    if not report_id or report_id == source_run_id:
        raise RuntimeError("report run id did not preserve distinct source-run provenance")
    if report.get("source_run_ids") != [source_run_id] or report.get("status") != "succeeded":
        raise RuntimeError("report summary did not preserve the validation source-run id")
    report_summary = _json_request(args.base_url, f"api/v1/reports/{report_id}")
    if report_summary.get("source_run_ids") != [source_run_id]:
        raise RuntimeError("stored report lost its validation source-run id")
    report_bytes_1, _headers_1 = _request(args.base_url, f"api/v1/reports/{report_id}/download")
    report_bytes_2, _headers_2 = _request(args.base_url, f"api/v1/reports/{report_id}/download")
    if not report_bytes_1 or report_bytes_1 != report_bytes_2:
        raise RuntimeError("repeated report downloads were not byte-identical")
    with ZipFile(BytesIO(report_bytes_1)) as archive:
        validation_summary = json.loads(archive.read("validation_summary.json"))
    source_rows = validation_summary.get("source_runs", [])
    if not any(
        isinstance(row, dict) and str(row.get("run_id") or "") == source_run_id
        for row in source_rows
    ):
        raise RuntimeError("downloaded report bytes do not identify the validation source run")

    if not database_path.is_file():
        raise RuntimeError("portable SQLite state was not written under the configured data directory")
    heartbeat_counts = _assert_log_cleanup(
        args.data_dir,
        [source_run_id, cancelled_run_id, canonical_id],
        stdout_log=args.stdout_log,
        stderr_log=args.stderr_log,
    )

    result = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": {
            "health": "passed",
            "app_root": "passed",
            "readiness_database": "passed",
            "asynchronous_acceptance": "passed",
            "long_quiet_heartbeat": "passed",
            "sqlite_lease_configuration": "passed",
            "multiple_sqlite_heartbeat_renewals": "passed",
            "stop_cancellation": "passed",
            "canonical_udmi": "passed",
            "report_source_run_provenance": "passed",
            "repeated_report_byte_equality": "passed",
            "path_with_spaces": "passed",
            "log_and_heartbeat_thread_cleanup": "passed",
            "redirected_stdout_stderr_error_scan": "passed",
        },
        "lease_seconds": args.lease_seconds,
        "heartbeat_seconds": args.heartbeat_seconds,
        "capture_seconds": capture_seconds,
        "source_run_id": source_run_id,
        "cancelled_run_id": cancelled_run_id,
        "canonical_run_id": canonical_id,
        "report_run_id": report_id,
        "report_sha256": hashlib.sha256(report_bytes_1).hexdigest(),
        "report_size": len(report_bytes_1),
        "heartbeat_log_counts": heartbeat_counts,
        "sqlite_heartbeat_observation": heartbeat_observation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--exe-path", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--lease-seconds", type=float, required=True)
    parser.add_argument("--heartbeat-seconds", type=float, required=True)
    parser.add_argument("--stdout-log", type=Path, required=True)
    parser.add_argument("--stderr-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.lease_seconds < 1 or args.heartbeat_seconds <= 0:
        parser.error("lease and heartbeat seconds must be positive")
    if args.heartbeat_seconds >= args.lease_seconds:
        parser.error("heartbeat seconds must be below lease seconds")
    result = smoke(args)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
