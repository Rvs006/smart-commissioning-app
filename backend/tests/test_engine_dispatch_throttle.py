"""Fail-closed request narrowing for active-scan throttle policy."""

from __future__ import annotations

import math
import unittest

from app.services.engine_dispatch import build_throttle


class BuildThrottleTests(unittest.TestCase):
    def test_request_can_narrow_but_cannot_widen_policy(self) -> None:
        narrowed = build_throttle(
            {
                "scan_max_concurrency": 4,
                "scan_rate_limit_per_sec": 2,
                "scan_connect_timeout_s": 1.5,
            },
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )
        self.assertEqual(narrowed.max_concurrency, 4)
        self.assertEqual(narrowed.rate_limit_per_sec, 2)
        self.assertEqual(narrowed.connect_timeout_s, 1.5)

        clamped = build_throttle(
            {
                "scan_max_concurrency": 100,
                "scan_rate_limit_per_sec": 100,
                "scan_connect_timeout_s": 100,
            },
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )
        self.assertEqual(clamped.max_concurrency, 16)
        self.assertEqual(clamped.rate_limit_per_sec, 10)
        self.assertEqual(clamped.connect_timeout_s, 5)

    def test_omitted_and_zero_legacy_overrides_use_policy_defaults(self) -> None:
        for parameters in ({}, {"scan_rate_limit_per_sec": 0}):
            with self.subTest(parameters=parameters):
                throttle = build_throttle(
                    parameters,
                    max_concurrency=16,
                    rate_limit_per_sec=10,
                    connect_timeout_s=5,
                )
                self.assertEqual(throttle.max_concurrency, 16)
                self.assertEqual(throttle.rate_limit_per_sec, 10)
                self.assertEqual(throttle.connect_timeout_s, 5)

    def test_frozen_effective_throttle_wins_without_reinterpreting_request_values(self) -> None:
        throttle = build_throttle(
            {
                "scan_max_concurrency": -1,
                "scan_rate_limit_per_sec": float("nan"),
                "scan_connect_timeout_s": "1e9999",
                "scan_contract_v1": {
                    "effective_throttle": {
                        "max_concurrency": 4,
                        "rate_limit_per_sec": 2.0,
                        "connect_timeout_s": 1.5,
                    }
                },
            },
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )

        self.assertEqual(throttle.max_concurrency, 4)
        self.assertEqual(throttle.rate_limit_per_sec, 2.0)
        self.assertEqual(throttle.connect_timeout_s, 1.5)

    def test_frozen_effective_throttle_is_strict_and_within_policy(self) -> None:
        valid = {
            "max_concurrency": 4,
            "rate_limit_per_sec": 2.0,
            "connect_timeout_s": 1.5,
        }
        invalid_overrides = (
            {"max_concurrency": 0},
            {"max_concurrency": -1},
            {"max_concurrency": 17},
            {"max_concurrency": str(2**80)},
            {"rate_limit_per_sec": 0},
            {"rate_limit_per_sec": -1},
            {"rate_limit_per_sec": 11},
            {"rate_limit_per_sec": float("nan")},
            {"rate_limit_per_sec": float("inf")},
            {"rate_limit_per_sec": 10**10000},
            {"connect_timeout_s": 0},
            {"connect_timeout_s": -1},
            {"connect_timeout_s": 6},
            {"connect_timeout_s": float("nan")},
            {"connect_timeout_s": float("inf")},
            {"connect_timeout_s": 10**10000},
        )
        invalid_effective_values = (
            None,
            [],
            {"max_concurrency": 4, "rate_limit_per_sec": 2.0},
            *(valid | override for override in invalid_overrides),
        )

        for effective in invalid_effective_values:
            with self.subTest(effective=effective), self.assertRaises(ValueError):
                build_throttle(
                    {"scan_contract_v1": {"effective_throttle": effective}},
                    max_concurrency=16,
                    rate_limit_per_sec=10,
                    connect_timeout_s=5,
                )

    def test_negative_non_finite_and_overflow_values_are_rejected(self) -> None:
        invalid = (
            {"scan_max_concurrency": 0},
            {"scan_max_concurrency": -1},
            {"scan_max_concurrency": str(2**80)},
            {"scan_rate_limit_per_sec": -1},
            {"scan_rate_limit_per_sec": float("nan")},
            {"scan_rate_limit_per_sec": float("inf")},
            {"scan_rate_limit_per_sec": "1e9999"},
            {"scan_connect_timeout_s": -1},
            {"scan_connect_timeout_s": 0},
            {"scan_connect_timeout_s": float("nan")},
            {"scan_connect_timeout_s": float("inf")},
        )
        for parameters in invalid:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                build_throttle(
                    parameters,
                    max_concurrency=16,
                    rate_limit_per_sec=10,
                    connect_timeout_s=5,
                )

    def test_returned_floats_are_always_finite(self) -> None:
        throttle = build_throttle(
            {"scan_rate_limit_per_sec": "0.25", "scan_connect_timeout_s": "2.5"},
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )
        self.assertTrue(math.isfinite(throttle.rate_limit_per_sec or 0))
        self.assertTrue(math.isfinite(throttle.connect_timeout_s))

    def test_small_positive_rate_is_not_widened_by_a_floor(self) -> None:
        throttle = build_throttle(
            {"scan_rate_limit_per_sec": 0.01},
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )

        self.assertEqual(throttle.rate_limit_per_sec, 0.01)

    def test_operator_nmap_uses_its_provider_guard_instead_of_the_builtin_guard(self) -> None:
        throttle = build_throttle(
            {
                "scan_contract_v1": {
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "operator_managed_nmap",
                        "provider_state": {
                            "provider": "operator_managed_nmap",
                            "execution_enabled": True,
                        },
                    },
                    "effective_throttle": {
                        "max_concurrency": 1,
                        "rate_limit_per_sec": 1.0,
                        "connect_timeout_s": 1.0,
                    },
                },
            },
            max_concurrency=16,
            rate_limit_per_sec=10,
            connect_timeout_s=5,
        )

        self.assertEqual(throttle.max_concurrency, 1)


if __name__ == "__main__":
    unittest.main()
