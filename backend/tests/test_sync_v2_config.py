"""Fail-closed configuration bounds for Sync v2 request processing."""

import os
import unittest
from unittest import mock

from app.core.config import Settings
from pydantic import ValidationError


class SyncV2ConfigurationTests(unittest.TestCase):
    def test_deployment_identity_matches_worker_default_and_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            defaults = Settings(_env_file=None)
        self.assertEqual(defaults.deployment_id, "smart-commissioning-local")

        with mock.patch.dict(
            os.environ,
            {"SMART_COMMISSIONING_DEPLOYMENT_ID": "  field-deployment-7  "},
            clear=True,
        ):
            configured = Settings(_env_file=None)
        self.assertEqual(configured.deployment_id, "field-deployment-7")

    def test_rejects_empty_or_oversized_deployment_identity(self) -> None:
        for deployment_id in ("   ", "x" * 256):
            with self.subTest(length=len(deployment_id)):
                with self.assertRaisesRegex(
                    ValidationError,
                    "deployment_id must contain between 1 and 255 characters",
                ):
                    Settings(_env_file=None, deployment_id=deployment_id)

    def test_secure_defaults_and_sender_secret_are_available(self) -> None:
        settings = Settings(_env_file=None, sync_hub_api_key="test-only-secret")

        self.assertEqual(settings.sync_hub_api_key, "test-only-secret")
        self.assertEqual(settings.max_sync_bundle_bytes, 20 * 1024 * 1024)
        self.assertEqual(settings.max_sync_uncompressed_bytes, 200 * 1024 * 1024)
        self.assertEqual(settings.max_sync_items, 500)

    def test_rejects_bundle_limit_below_one_mib(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least 1 MiB"):
            Settings(_env_file=None, max_sync_bundle_bytes=(1024 * 1024) - 1)

    def test_rejects_uncompressed_limit_below_bundle_limit(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least max_sync_bundle_bytes"):
            Settings(
                _env_file=None,
                max_sync_bundle_bytes=2 * 1024 * 1024,
                max_sync_uncompressed_bytes=(2 * 1024 * 1024) - 1,
            )

    def test_rejects_uncompressed_limit_above_two_gib(self) -> None:
        with self.assertRaisesRegex(ValidationError, "no greater than 2 GiB"):
            Settings(
                _env_file=None,
                max_sync_uncompressed_bytes=(2 * 1024 * 1024 * 1024) + 1,
            )

    def test_rejects_item_count_outside_safe_bounds(self) -> None:
        for max_sync_items in (0, 10_001):
            with self.subTest(max_sync_items=max_sync_items):
                with self.assertRaisesRegex(ValidationError, "between 1 and 10000"):
                    Settings(_env_file=None, max_sync_items=max_sync_items)


if __name__ == "__main__":
    unittest.main()
