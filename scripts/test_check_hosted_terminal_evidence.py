#!/usr/bin/env python3
"""Unit contracts for hosted terminal result/seal cardinality checks."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from check_hosted_terminal_evidence import terminal_snapshot, validate_terminal_evidence


class HostedTerminalEvidenceTests(unittest.TestCase):
    def fixture(self):
        digest = "a" * 64
        run = SimpleNamespace(
            status="cancelled",
            execution_mode="dramatiq_worker",
            terminal_at=datetime.now(UTC),
            result_sha256=digest,
            stage="engine_cancelled",
            error_message=None,
        )
        result = SimpleNamespace(
            terminal_status="cancelled",
            result_sha256=digest,
            summary={"execution_mode": "dramatiq_worker"},
        )
        seal = SimpleNamespace(terminal_status="cancelled", result_sha256=digest)
        run.id = "run-1"
        result.terminal_stage = "engine_cancelled"
        result.created_at = datetime.now(UTC)
        seal.context_sha256 = "b" * 64
        seal.sealed_at = datetime.now(UTC)
        return run, result, seal

    def test_one_coherent_result_and_seal_pass(self) -> None:
        run, result, seal = self.fixture()
        self.assertEqual(
            validate_terminal_evidence(
                run,
                [result],
                [seal],
                expected_status="cancelled",
                expected_execution_mode="dramatiq_worker",
            ),
            [],
        )

    def test_duplicates_and_lease_expiry_fail(self) -> None:
        run, result, seal = self.fixture()
        run.stage = "lease_expired"
        failures = validate_terminal_evidence(
            run,
            [result, result],
            [seal, seal],
            expected_status="cancelled",
            expected_execution_mode="dramatiq_worker",
        )
        self.assertTrue(any("lease expiry" in failure for failure in failures))
        self.assertTrue(any("2 terminal results" in failure for failure in failures))
        self.assertTrue(any("2 seals" in failure for failure in failures))

    def test_recovery_can_explicitly_allow_expected_lease_stage(self) -> None:
        run, result, seal = self.fixture()
        run.status = result.terminal_status = seal.terminal_status = "failed"
        run.stage = result.terminal_stage = "lease_expired"
        result.summary = {}
        self.assertEqual(
            validate_terminal_evidence(
                run,
                [result],
                [seal],
                expected_status="failed",
                expected_execution_mode=None,
                expected_stages=frozenset({"lease_expired", "worker_heartbeat_expired"}),
                allow_lease_expiry=True,
            ),
            [],
        )

    def test_snapshot_carries_digest_and_terminal_timestamps(self) -> None:
        run, result, seal = self.fixture()
        snapshot = terminal_snapshot(run, result, seal)
        self.assertEqual(snapshot["result_count"], 1)
        self.assertEqual(snapshot["seal_count"], 1)
        self.assertEqual(snapshot["run_result_sha256"], "a" * 64)
        self.assertIsNotNone(snapshot["terminal_at"])
        self.assertIsNotNone(snapshot["seal_sealed_at"])


if __name__ == "__main__":
    unittest.main()
