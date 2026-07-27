#!/usr/bin/env python3
"""Tests for hosted release log inspection."""

from __future__ import annotations

import unittest

from check_hosted_release_logs import inspect_logs


class HostedReleaseLogTests(unittest.TestCase):
    def test_accepts_expected_recovery_and_deferred_publication(self) -> None:
        text = "\n".join(
            (
                "queue publish deferred for run_id=run_fixture",
                "worker heartbeat stale; awaiting confirmation for run_id=run_fixture",
                "worker heartbeat expired for run_id=run_fixture",
                "Skipping duplicate or stale delivery",
            )
        )
        self.assertEqual(inspect_logs(text, secret_values=["secret-value"]), [])

    def test_rejects_secret_and_private_key(self) -> None:
        failures = inspect_logs(
            "redis password secret-value\n-----BEGIN PRIVATE KEY-----",
            secret_values=["secret-value"],
        )
        self.assertIn("environment secret value", failures)
        self.assertIn("private key material", failures)

    def test_rejects_repeated_heartbeat_failures(self) -> None:
        text = "\n".join("owned-run heartbeat refresh failed" for _ in range(4))
        self.assertIn("repeated heartbeat failures (4)", inspect_logs(text, secret_values=[]))

    def test_rejects_credential_url(self) -> None:
        failures = inspect_logs(
            "redis://release-user:release-password@redis:6379/0", secret_values=[]
        )
        self.assertIn("credential-bearing URL", failures)


if __name__ == "__main__":
    unittest.main()
