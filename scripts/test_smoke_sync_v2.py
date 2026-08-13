import argparse
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import smoke_sync_v2
from smart_commissioning_core.sync_v2 import (
    SyncV2Descriptor,
    validate_sync_v2_item,
)


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

    def test_named_admin_provisioning_uses_offline_compose_exec_and_returns_last_line(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Created named administrator.\n"
                "Copy once:\n"
                "named-admin-key-one-time-public-test\n"
            ),
            stderr="",
        )
        output = io.StringIO()
        with mock.patch(
            "smoke_sync_v2.subprocess.run",
            return_value=completed,
        ) as run, contextlib.redirect_stdout(output):
            key = smoke_sync_v2._provision_named_admin(
                self._args(),
                username="sync-smoke-admin-public",
            )

        self.assertEqual(key, "named-admin-key-one-time-public-test")
        self.assertNotIn(key, output.getvalue())
        run.assert_called_once_with(
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
                "exec",
                "-T",
                "hub-api",
                "python",
                "-m",
                "app.scripts.bootstrap_admin",
                "--username",
                "sync-smoke-admin-public",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_named_admin_provisioning_failure_does_not_disclose_captured_secret(
        self,
    ) -> None:
        captured_secret = "captured-bootstrap-secret-public-test"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout=f"unexpected output\n{captured_secret}\n",
            stderr=f"unexpected error {captured_secret}\n",
        )
        with mock.patch("smoke_sync_v2.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "exit 2") as raised:
                smoke_sync_v2._provision_named_admin(
                    self._args(),
                    username="sync-smoke-admin-public",
                )
        self.assertNotIn(captured_secret, str(raised.exception))

    def test_report_access_requires_named_admin_on_hub_and_is_byte_reproducible(
        self,
    ) -> None:
        shared_key = "shared-bootstrap-key-public-test"
        named_key = "named-admin-key-public-test"
        report_id = "report-public-test"
        artifact = b"%PDF-1.4\npublic-test\n%%EOF\n"
        observed: list[tuple[str, str]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            key = request.headers.get("X-API-Key", "")
            observed.append((request.url.path, key))
            if request.url.path.endswith("/me"):
                return httpx.Response(
                    200,
                    json={"global_scope": key == named_key},
                    request=request,
                )
            if key == shared_key:
                return httpx.Response(404, request=request)
            return httpx.Response(200, content=artifact, request=request)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            smoke_sync_v2._assert_hub_report_access(
                client,
                hub="https://hub.invalid/api/v1",
                shared_api_key=shared_key,
                named_admin_key=named_key,
                report_id=report_id,
                expected_artifact=artifact,
            )

        self.assertEqual(
            observed,
            [
                ("/api/v1/me", shared_key),
                (f"/api/v1/reports/{report_id}/download", shared_key),
                ("/api/v1/me", named_key),
                (f"/api/v1/reports/{report_id}/download", named_key),
                (f"/api/v1/reports/{report_id}/download", named_key),
            ],
        )

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

    def test_conflict_bundle_rewrites_digest_derived_item_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-smoke-conflict-") as temporary:
            edge = smoke_sync_v2.EdgeFixture(Path(temporary))
            try:
                run_id = edge.report(
                    smoke_sync_v2._PROJECT,
                    smoke_sync_v2._SITE,
                    "conflict-unit",
                )
                original = edge.bundle([run_id])
                original_manifest, _ = smoke_sync_v2._read_bundle(original)

                conflict = smoke_sync_v2._conflict_bundle(original, edge.bundle_key)
                manifest, members = smoke_sync_v2._read_bundle(conflict)
                descriptor = SyncV2Descriptor.model_validate(manifest["items"][0])

                self.assertNotEqual(
                    descriptor.item_id,
                    original_manifest["items"][0]["item_id"],
                )
                self.assertEqual(
                    descriptor.item_member,
                    f"items/{descriptor.item_id}.json",
                )
                validate_sync_v2_item(members[descriptor.item_member], descriptor)
            finally:
                edge.close()


if __name__ == "__main__":
    unittest.main()
