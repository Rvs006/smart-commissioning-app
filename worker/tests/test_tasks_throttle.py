"""Worker coverage for the shared active-scan throttle contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKER_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPOSITORY_ROOT / "core", _WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import tasks  # noqa: E402


class WorkerThrottleTests(unittest.TestCase):
    def test_worker_applies_the_same_legacy_narrowing_contract(self) -> None:
        throttle = tasks._build_throttle(
            {
                "scan_max_concurrency": 4,
                "scan_rate_limit_per_sec": 0.01,
                "scan_connect_timeout_s": 1.5,
            }
        )

        self.assertEqual(throttle.max_concurrency, 4)
        self.assertEqual(throttle.rate_limit_per_sec, 0.01)
        self.assertEqual(throttle.connect_timeout_s, 1.5)

    def test_worker_uses_the_frozen_effective_throttle_exactly(self) -> None:
        throttle = tasks._build_throttle(
            {
                "scan_max_concurrency": -1,
                "scan_rate_limit_per_sec": float("nan"),
                "scan_connect_timeout_s": "1e9999",
                "scan_contract_v1": {
                    "effective_throttle": {
                        "max_concurrency": 3,
                        "rate_limit_per_sec": 1.25,
                        "connect_timeout_s": 2.5,
                    }
                },
            }
        )

        self.assertEqual(throttle.max_concurrency, 3)
        self.assertEqual(throttle.rate_limit_per_sec, 1.25)
        self.assertEqual(throttle.connect_timeout_s, 2.5)

    def test_worker_rejects_invalid_legacy_and_frozen_values(self) -> None:
        invalid = (
            {"scan_max_concurrency": 0},
            {"scan_rate_limit_per_sec": float("inf")},
            {"scan_connect_timeout_s": -1},
            {
                "scan_contract_v1": {
                    "effective_throttle": {
                        "max_concurrency": 17,
                        "rate_limit_per_sec": 1.0,
                        "connect_timeout_s": 1.0,
                    }
                }
            },
        )

        for parameters in invalid:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                tasks._build_throttle(parameters)

    def test_worker_legacy_zero_rate_uses_the_policy_default(self) -> None:
        throttle = tasks._build_throttle({"scan_rate_limit_per_sec": 0})

        self.assertEqual(throttle.rate_limit_per_sec, 10.0)


if __name__ == "__main__":
    unittest.main()
