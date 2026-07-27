import argparse
import contextlib
import io
import subprocess
import unittest
from unittest import mock

import httpx
import smoke_sync_v2


class SmokeSyncV2UnitTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            compose_file=["infra/docker-compose.yml", "infra/docker-compose.sync.yml"],
            env_file="infra/.env",
            compose_project="public-test",
            hub_service="hub-api",
        )

    def test_provisioning_returns_last_line_without_printing_key(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Created credential with one scope.\nCopy once:\nsync-key-one-time-public-test\n",
            stderr="",
        )
        output = io.StringIO()
        with mock.patch(
            "smoke_sync_v2.subprocess.run", return_value=completed
        ), contextlib.redirect_stdout(output):
            key = smoke_sync_v2._provision_credential(
                self._args(),
                edge_id="edge-public",
                signing_key_fingerprint="0123456789abcdef",
            )
        self.assertEqual(key, "sync-key-one-time-public-test")
        self.assertNotIn(key, output.getvalue())

    def test_receipt_assertion_requires_complete_contract(self) -> None:
        response = httpx.Response(
            200,
            json={
                "protocol_version": "2.0",
                "bundle_id": "bundle-1",
                "receipts": [{"class": "accepted"}],
                "acknowledged_run_ids": ["run-1"],
                "all_acknowledged": True,
            },
            request=httpx.Request("POST", "https://hub.invalid"),
        )
        payload = smoke_sync_v2._assert_receipts(
            response,
            expected_bundle_id="bundle-1",
            expected_classes=["accepted"],
            expected_acknowledged_run_ids=["run-1"],
            expected_all_acknowledged=True,
        )
        self.assertEqual(payload["bundle_id"], "bundle-1")

        with self.assertRaises(RuntimeError):
            smoke_sync_v2._assert_receipts(
                response,
                expected_bundle_id="bundle-1",
                expected_classes=["accepted"],
                expected_acknowledged_run_ids=[],
                expected_all_acknowledged=False,
            )

    def test_compose_prefix_keeps_all_override_files(self) -> None:
        self.assertEqual(
            smoke_sync_v2._compose_prefix(self._args()),
            [
                "docker",
                "compose",
                "-f",
                "infra/docker-compose.yml",
                "-f",
                "infra/docker-compose.sync.yml",
                "--env-file",
                "infra/.env",
                "-p",
                "public-test",
            ],
        )


if __name__ == "__main__":
    unittest.main()
