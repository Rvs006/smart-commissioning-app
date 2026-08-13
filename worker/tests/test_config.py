import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))

from app import config  # noqa: E402


class WorkerHeartbeatConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        config.get_settings.cache_clear()

    def _load(self, *, lease: str, heartbeat: str):
        config.get_settings.cache_clear()
        with mock.patch.dict(
            os.environ,
            {
                "RUN_LEASE_SECONDS": lease,
                "RUN_HEARTBEAT_SECONDS": heartbeat,
            },
        ):
            return config.get_settings()

    def test_secure_defaults_and_release_acceptance_values(self) -> None:
        defaults = self._load(lease="60", heartbeat="15")
        self.assertEqual(defaults.lease_seconds, 60)
        self.assertEqual(defaults.heartbeat_seconds, 15.0)

        acceptance = self._load(lease="15", heartbeat="2")
        self.assertEqual(acceptance.lease_seconds, 15)
        self.assertEqual(acceptance.heartbeat_seconds, 2.0)

    def test_deployment_identity_matches_backend_default_and_environment(self) -> None:
        defaults = self._load(lease="60", heartbeat="15")
        self.assertEqual(defaults.deployment_id, "smart-commissioning-local")

        config.get_settings.cache_clear()
        with mock.patch.dict(
            os.environ,
            {
                "RUN_LEASE_SECONDS": "60",
                "RUN_HEARTBEAT_SECONDS": "15",
                "SMART_COMMISSIONING_DEPLOYMENT_ID": "  field-deployment-7  ",
            },
            clear=True,
        ):
            configured = config.get_settings()
        self.assertEqual(configured.deployment_id, "field-deployment-7")

    def test_network_executor_identity_is_explicit_and_trimmed(self) -> None:
        defaults = self._load(lease="60", heartbeat="15")
        self.assertIsNone(defaults.network_executor_id)

        config.get_settings.cache_clear()
        with mock.patch.dict(
            os.environ,
            {
                "RUN_LEASE_SECONDS": "60",
                "RUN_HEARTBEAT_SECONDS": "15",
                "SMART_COMMISSIONING_NETWORK_EXECUTOR_ID": "  field-netns-7  ",
            },
            clear=True,
        ):
            configured = config.get_settings()
        self.assertEqual(configured.network_executor_id, "field-netns-7")

    def test_rejects_empty_or_oversized_deployment_identity(self) -> None:
        for deployment_id in ("   ", "x" * 256):
            config.get_settings.cache_clear()
            with self.subTest(length=len(deployment_id)):
                with mock.patch.dict(
                    os.environ,
                    {
                        "RUN_LEASE_SECONDS": "60",
                        "RUN_HEARTBEAT_SECONDS": "15",
                        "SMART_COMMISSIONING_DEPLOYMENT_ID": deployment_id,
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "deployment_id must contain between 1 and 255 characters",
                    ):
                        config.get_settings()

    def test_rejects_unsafe_values_instead_of_silently_clamping(self) -> None:
        cases = (
            (("14", "2"), "at least 15"),
            (("301", "15"), "no more than 300"),
            (("60", "0.5"), "at least 1"),
            (("15", "6"), "one third"),
            (("60", "nan"), "finite"),
        )
        for (lease, heartbeat), message in cases:
            with self.subTest(lease=lease, heartbeat=heartbeat):
                with self.assertRaisesRegex(ValueError, message):
                    self._load(lease=lease, heartbeat=heartbeat)


if __name__ == "__main__":
    unittest.main()
