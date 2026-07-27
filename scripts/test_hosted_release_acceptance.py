#!/usr/bin/env python3
"""Unit contracts for the hosted release acceptance helper."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import hosted_release_acceptance as acceptance


class HostedReleaseAcceptanceTests(unittest.TestCase):
    def test_mqtt_capture_is_authorized_scoped_and_silent(self) -> None:
        request = acceptance._mqtt_request(70)
        parameters = request["parameters"]
        self.assertIs(parameters["authorized"], True)
        self.assertEqual(parameters["broker_host"], "mqtt-acceptance")
        self.assertEqual(parameters["topic_filter"], "release/quiet/#")
        self.assertEqual(parameters["capture_seconds"], 70)

    def test_execution_mode_reads_result_summary(self) -> None:
        run = {"result_summary": {"execution_mode": "dramatiq_worker"}}
        acceptance._assert_execution_mode(run, "dramatiq_worker")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._assert_execution_mode(run, "inline_local_fallback")

    def test_live_lease_failure_is_rejected(self) -> None:
        with self.assertRaisesRegex(acceptance.AcceptanceError, "lease expiry"):
            acceptance._assert_no_lease_failure(
                {"stage": "lease_expired", "error_message": "execution owner lease expired"}
            )

    def test_terminal_snapshot_excludes_mutable_timestamps(self) -> None:
        snapshot = acceptance._terminal_snapshot(
            {
                "run_id": "run_fixture",
                "status": "failed",
                "stage": "worker_heartbeat_expired",
                "progress_percent": 100,
                "error_message": "worker stopped",
                "result_summary": {"execution_mode": "dramatiq_worker"},
                "updated_at": "ignored",
            }
        )
        self.assertNotIn("updated_at", snapshot)
        self.assertEqual(snapshot["stage"], "worker_heartbeat_expired")

    def test_run_path_supports_restart_snapshot_kinds(self) -> None:
        self.assertEqual(
            acceptance._run_path("validation", "run-1"),
            "validation/runs/run-1",
        )
        self.assertEqual(
            acceptance._run_path("discovery", "run-2"),
            "discovery/runs/run-2",
        )

    def test_capture_must_outlast_observation_window(self) -> None:
        client = object()
        args = type(
            "Args",
            (),
            {"capture_seconds": 60, "minimum_running_seconds": 65, "timeout": 5},
        )()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "must exceed"):
            acceptance._long_capture(client, args)

    def test_cancel_existing_proves_active_api_restart_continuity(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.cancelled = False

            def request(self, method, path, body=None):
                if method == "POST":
                    self.cancelled = True
                    return {"status": "cancellation_requested"}
                if self.cancelled:
                    return {
                        "run_id": "run-1",
                        "status": "cancelled",
                        "result_summary": {"execution_mode": "dramatiq_worker"},
                    }
                return {"run_id": "run-1", "status": "running"}

        with TemporaryDirectory() as directory:
            reference = Path(directory) / "run.json"
            acceptance._write_json(
                reference,
                {"run_id": "run-1", "kind": "discovery"},
            )
            args = type(
                "Args",
                (),
                {
                    "input": reference,
                    "timeout": 5,
                    "expected_execution_mode": "dramatiq_worker",
                },
            )()
            acceptance._cancel_existing_capture(Client(), args)


if __name__ == "__main__":
    unittest.main()
