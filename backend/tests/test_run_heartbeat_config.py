"""Configuration bounds for owned-run heartbeat timing."""

import unittest

from app.core.config import Settings
from pydantic import ValidationError


class RunHeartbeatConfigurationTests(unittest.TestCase):
    def test_operator_managed_nmap_is_an_explicit_deployment_opt_in(self) -> None:
        defaults = Settings(_env_file=None)
        self.assertFalse(defaults.nmap_internal_provider_enabled)

        configured = Settings(
            _env_file=None,
            nmap_internal_provider_enabled=True,
        )
        self.assertTrue(configured.nmap_internal_provider_enabled)

    def test_network_executor_identity_is_optional_but_normalized(self) -> None:
        defaults = Settings(_env_file=None)
        self.assertIsNone(defaults.network_executor_id)

        configured = Settings(
            _env_file=None,
            network_executor_id="  field-netns-7  ",
        )
        self.assertEqual(configured.network_executor_id, "field-netns-7")

    def test_secure_defaults_match_release_policy(self) -> None:
        settings = Settings(
            _env_file=None,
            run_lease_seconds=60,
            run_heartbeat_seconds=15,
        )

        self.assertEqual(settings.run_lease_seconds, 60)
        self.assertEqual(settings.run_heartbeat_seconds, 15)

    def test_short_release_acceptance_timing_remains_safe(self) -> None:
        settings = Settings(
            _env_file=None,
            run_lease_seconds=15,
            run_heartbeat_seconds=2,
        )

        self.assertEqual(settings.run_lease_seconds, 15)
        self.assertEqual(settings.run_heartbeat_seconds, 2)

    def test_rejects_short_lease(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least 15"):
            Settings(
                _env_file=None,
                run_lease_seconds=14,
                run_heartbeat_seconds=2,
            )

    def test_rejects_subsecond_heartbeat(self) -> None:
        with self.assertRaisesRegex(ValidationError, "at least 1"):
            Settings(
                _env_file=None,
                run_lease_seconds=60,
                run_heartbeat_seconds=0.5,
            )

    def test_rejects_heartbeat_too_close_to_expiry(self) -> None:
        with self.assertRaisesRegex(ValidationError, "one third"):
            Settings(
                _env_file=None,
                run_lease_seconds=15,
                run_heartbeat_seconds=6,
            )

    def test_rejects_non_finite_heartbeat(self) -> None:
        with self.assertRaisesRegex(ValidationError, "finite"):
            Settings(
                _env_file=None,
                run_lease_seconds=60,
                run_heartbeat_seconds="nan",
            )

    def test_rejects_capture_length_as_lease_override(self) -> None:
        for lease_seconds in (32_400, 172_800):
            with self.subTest(lease_seconds=lease_seconds):
                with self.assertRaisesRegex(ValidationError, "no more than 300"):
                    Settings(
                        _env_file=None,
                        run_lease_seconds=lease_seconds,
                        run_heartbeat_seconds=15,
                    )


if __name__ == "__main__":
    unittest.main()
