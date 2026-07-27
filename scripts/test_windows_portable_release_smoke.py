#!/usr/bin/env python3
"""Focused tests for portable release lease and log acceptance helpers."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from smoke_windows_portable_release import (
    _PASSWORD_MARKER,
    _assert_cancel_request_persisted,
    _assert_log_cleanup,
    _assert_no_severe_log_records,
    _lease_snapshot,
    _monitor_running_heartbeats_until_terminal,
    _observe_heartbeat_renewals,
)


def _database(path: Path, *, lease_seconds: float) -> None:
    now = datetime.now(UTC)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                status TEXT,
                claimed_at TEXT,
                heartbeat_at TEXT,
                lease_expires_at TEXT,
                terminal_at TEXT,
                cancel_requested INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "running",
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                None,
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _execute(database: Path, statement: str, parameters: tuple[str, ...]) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


class PortableReleaseSmokeTests(unittest.TestCase):
    def test_cancel_proof_uses_private_sqlite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=1)
            response = {"run_id": "run-1", "status": "running"}
            self.assertNotIn("cancel_requested", response)
            _execute(
                database,
                "UPDATE runs SET cancel_requested = ? WHERE id = 'run-1'",
                ("1",),
            )
            _assert_cancel_request_persisted(database, "run-1", response)
            _execute(
                database,
                "UPDATE runs SET cancel_requested = ? WHERE id = 'run-1'",
                ("0",),
            )
            with self.assertRaisesRegex(RuntimeError, "persist cancel_requested"):
                _assert_cancel_request_persisted(database, "run-1", response)

    def test_observes_two_sqlite_heartbeat_renewals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=1)

            def renew() -> None:
                for _ in range(2):
                    time.sleep(0.08)
                    heartbeat = datetime.now(UTC)
                    _execute(
                        database,
                        """
                        UPDATE runs
                        SET heartbeat_at = ?, lease_expires_at = ?
                        WHERE id = 'run-1'
                        """,
                        (
                            heartbeat.isoformat(),
                            (heartbeat + timedelta(seconds=1)).isoformat(),
                        ),
                    )

            thread = threading.Thread(target=renew)
            thread.start()
            observation, _expiry, _snapshot, _observed_at = _observe_heartbeat_renewals(
                database,
                "run-1",
                expected_lease_seconds=1,
                expected_heartbeat_seconds=0.05,
                cadence_allowance_seconds=0.25,
            )
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(observation["renewal_count"], 2)

    def test_rejects_executable_ignoring_requested_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=60)
            with self.assertRaisesRegex(RuntimeError, "requested lease window"):
                _observe_heartbeat_renewals(
                    database,
                    "run-1",
                    expected_lease_seconds=15,
                    expected_heartbeat_seconds=2,
                    cadence_allowance_seconds=5,
                )

    def test_initial_probe_rejects_gap_even_if_heartbeat_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=1)
            initial = _lease_snapshot(database, "run-1")
            initial_heartbeat = initial["heartbeat_at"]
            assert isinstance(initial_heartbeat, datetime)

            def recover() -> None:
                time.sleep(0.14)
                heartbeat = datetime.now(UTC)
                _execute(
                    database,
                    """
                    UPDATE runs
                    SET heartbeat_at = ?, lease_expires_at = ?
                    WHERE id = 'run-1'
                    """,
                    (
                        heartbeat.isoformat(),
                        (heartbeat + timedelta(seconds=1)).isoformat(),
                    ),
                )

            thread = threading.Thread(target=recover)
            thread.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "cadence allowance"):
                    _observe_heartbeat_renewals(
                        database,
                        "run-1",
                        expected_lease_seconds=1,
                        expected_heartbeat_seconds=0.02,
                        cadence_allowance_seconds=0.08,
                    )
            finally:
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            recovered = _lease_snapshot(database, "run-1")
            recovered_heartbeat = recovered["heartbeat_at"]
            assert isinstance(recovered_heartbeat, datetime)
            self.assertGreater(recovered_heartbeat, initial_heartbeat)

    def test_terminal_proof_uses_last_pre_terminal_heartbeat(self) -> None:
        now = datetime.now(UTC)
        initial = {
            "status": "running",
            "claimed_at": now,
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=15),
            "terminal_at": None,
        }
        terminal_at = now + timedelta(seconds=10)
        terminal = {
            "status": "succeeded",
            "claimed_at": now,
            # Finalization rewrites this field. The monitor must ignore it.
            "heartbeat_at": terminal_at,
            "lease_expires_at": None,
            "terminal_at": terminal_at,
        }
        with (
            patch(
                "smoke_windows_portable_release._json_request",
                return_value={"status": "succeeded", "stage": "complete"},
            ),
            patch(
                "smoke_windows_portable_release._lease_snapshot",
                return_value=terminal,
            ),
            self.assertRaisesRegex(RuntimeError, "last pre-terminal heartbeat"),
        ):
            _monitor_running_heartbeats_until_terminal(
                "http://127.0.0.1:8765",
                Path("unused.db"),
                "run-1",
                expected_lease_seconds=15,
                expected_heartbeat_seconds=2,
                cadence_allowance_seconds=5,
                original_lease_expiry=now + timedelta(seconds=15),
                initial_running_snapshot=initial,
                initial_observed_at=now,
            )

    def test_continuously_samples_running_heartbeats_until_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=1)
            first_renewal = threading.Event()
            second_renewal = threading.Event()
            monitor_started = threading.Event()

            def renew_and_finish() -> None:
                for index in range(6):
                    heartbeat = datetime.now(UTC)
                    _execute(
                        database,
                        """
                        UPDATE runs
                        SET heartbeat_at = ?, lease_expires_at = ?
                        WHERE id = 'run-1'
                        """,
                        (
                            heartbeat.isoformat(),
                            (heartbeat + timedelta(seconds=1)).isoformat(),
                        ),
                    )
                    if index == 0:
                        first_renewal.set()
                        monitor_started.wait(timeout=1)
                    elif index == 1:
                        second_renewal.set()
                    time.sleep(0.04)
                time.sleep(0.04)
                terminal = datetime.now(UTC)
                _execute(
                    database,
                    """
                    UPDATE runs
                    SET status = 'succeeded', heartbeat_at = ?,
                        lease_expires_at = NULL, terminal_at = ?
                    WHERE id = 'run-1'
                    """,
                    (terminal.isoformat(), terminal.isoformat()),
                )

            def run_status(*_args: object, **_kwargs: object) -> dict[str, str]:
                if not second_renewal.is_set():
                    self.assertTrue(second_renewal.wait(timeout=1))
                status = str(_lease_snapshot(database, "run-1")["status"])
                return {
                    "status": status,
                    "stage": "complete" if status == "succeeded" else "capturing_live_mqtt",
                }

            thread = threading.Thread(target=renew_and_finish)
            thread.start()
            try:
                self.assertTrue(first_renewal.wait(timeout=1))
                monitor_initial = _lease_snapshot(database, "run-1")
                monitor_initial_observed_at = datetime.now(UTC)
                monitor_heartbeat = monitor_initial["heartbeat_at"]
                assert isinstance(monitor_heartbeat, datetime)
                monitor_boundary = monitor_heartbeat
                with patch(
                    "smoke_windows_portable_release._json_request",
                    side_effect=run_status,
                ):
                    monitor_started.set()
                    completed, _stages, observation = (
                        _monitor_running_heartbeats_until_terminal(
                            "http://127.0.0.1:8765",
                            database,
                            "run-1",
                            expected_lease_seconds=1,
                            expected_heartbeat_seconds=0.04,
                            cadence_allowance_seconds=1.0,
                            original_lease_expiry=monitor_boundary,
                            initial_running_snapshot=monitor_initial,
                            initial_observed_at=monitor_initial_observed_at,
                            boundary_grace_seconds=0.01,
                            timeout=2,
                        )
                    )
            finally:
                monitor_started.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(completed["status"], "succeeded")
            self.assertGreaterEqual(observation["continuous_renewal_count"], 3)
            self.assertGreater(
                observation["terminal_from_last_running_heartbeat_seconds"],
                0,
            )

    def test_rejects_pre_boundary_gap_even_if_heartbeat_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            _database(database, lease_seconds=1)
            initial = _lease_snapshot(database, "run-1")
            original_expiry = initial["lease_expires_at"]
            initial_heartbeat = initial["heartbeat_at"]
            assert isinstance(original_expiry, datetime)
            assert isinstance(initial_heartbeat, datetime)

            def recover() -> None:
                time.sleep(0.14)
                heartbeat = datetime.now(UTC)
                _execute(
                    database,
                    """
                    UPDATE runs
                    SET heartbeat_at = ?, lease_expires_at = ?
                    WHERE id = 'run-1'
                    """,
                    (
                        heartbeat.isoformat(),
                        (heartbeat + timedelta(seconds=1)).isoformat(),
                    ),
                )

            thread = threading.Thread(target=recover)
            thread.start()
            try:
                with (
                    patch(
                        "smoke_windows_portable_release._json_request",
                        return_value={
                            "status": "running",
                            "stage": "capturing_live_mqtt",
                        },
                    ),
                    self.assertRaisesRegex(RuntimeError, "cadence allowance"),
                ):
                    _monitor_running_heartbeats_until_terminal(
                        "http://127.0.0.1:8765",
                        database,
                        "run-1",
                        expected_lease_seconds=1,
                        expected_heartbeat_seconds=0.02,
                        cadence_allowance_seconds=0.08,
                        original_lease_expiry=original_expiry,
                        initial_running_snapshot=initial,
                        initial_observed_at=datetime.now(UTC),
                        timeout=2,
                    )
            finally:
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            recovered = _lease_snapshot(database, "run-1")
            recovered_heartbeat = recovered["heartbeat_at"]
            assert isinstance(recovered_heartbeat, datetime)
            self.assertGreater(recovered_heartbeat, initial_heartbeat)

    def test_rejects_json_error_and_thread_traceback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "severe JSON"):
            _assert_no_severe_log_records(
                json.dumps({"level": "ERROR", "message": "boom"}),
                source="app.log",
                structured=True,
            )
        with self.assertRaisesRegex(RuntimeError, "unhandled-error marker"):
            _assert_no_severe_log_records(
                "Exception in thread heartbeat\nTraceback (most recent call last):",
                source="stderr",
                structured=False,
            )

    def test_rejects_secret_in_redirected_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            (logs / "app.log").write_text(
                "\n".join(
                    json.dumps({"level": "INFO", "message": message})
                    for message in (
                        "owned-run heartbeat started for run_id=run-1 executor=inline",
                        "owned-run heartbeat stopped for run_id=run-1 executor=inline",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            stdout.write_text(_PASSWORD_MARKER, encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "forbidden marker"):
                _assert_log_cleanup(
                    root,
                    ["run-1"],
                    stdout_log=stdout,
                    stderr_log=stderr,
                )

    def test_accepts_shared_inline_heartbeat_log_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            (logs / "app.log").write_text(
                "\n".join(
                    json.dumps({"level": "INFO", "message": message})
                    for message in (
                        "owned-run heartbeat started for run_id=run-1 executor=inline",
                        "owned-run heartbeat stopped for run_id=run-1 executor=inline",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")

            result = _assert_log_cleanup(
                root,
                ["run-1"],
                stdout_log=stdout,
                stderr_log=stderr,
            )

            self.assertEqual(result["heartbeat_started"], 1)
            self.assertEqual(result["heartbeat_stopped"], 1)

    def test_rejects_shared_heartbeat_refresh_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            (logs / "app.log").write_text(
                "\n".join(
                    json.dumps({"level": level, "message": message})
                    for level, message in (
                        (
                            "INFO",
                            "owned-run heartbeat started for run_id=run-1 executor=inline",
                        ),
                        (
                            "WARNING",
                            "owned-run heartbeat refresh failed for run_id=run-1 "
                            "executor=inline exception_type=OperationalError; retrying",
                        ),
                        (
                            "INFO",
                            "owned-run heartbeat stopped for run_id=run-1 executor=inline",
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "forbidden marker"):
                _assert_log_cleanup(
                    root,
                    ["run-1"],
                    stdout_log=stdout,
                    stderr_log=stderr,
                )


if __name__ == "__main__":
    unittest.main()
