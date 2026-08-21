"""Unit checks for the Advanced-panel proxy classification (pure helpers)."""

from __future__ import annotations

import unittest


class ScannerRawPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.services import scanner_raw_policy

        self.policy = scanner_raw_policy

    def test_proto_map_covers_the_three_sidecars(self) -> None:
        self.assertEqual(set(self.policy.SIDECAR_BY_PROTO), {"ip", "bacnet", "mqtt"})

    def test_get_is_always_read(self) -> None:
        self.assertEqual(self.policy.classify("GET", "api/scan?start=1"), "read")
        self.assertEqual(self.policy.classify("GET", "api/stream"), "read")

    def test_static_assets_are_reads(self) -> None:
        self.assertEqual(self.policy.classify("GET", ""), "read")
        self.assertEqual(self.policy.classify("GET", "app.js"), "read")

    def test_known_post_reads_stay_reads(self) -> None:
        for path in ("api/register", "api/compare", "api/connect", "api/subscribe", "api/export"):
            self.assertEqual(self.policy.classify("POST", path), "read", path)
        self.assertEqual(self.policy.classify("DELETE", "api/register"), "read")

    def test_device_writes_are_writes(self) -> None:
        self.assertEqual(self.policy.classify("POST", "api/publish"), "write")
        self.assertEqual(self.policy.classify("POST", "api/config"), "write")

    def test_unknown_non_get_fails_closed_to_write(self) -> None:
        # A future upstream endpoint (e.g. a BACnet override) is guarded on day one.
        self.assertEqual(self.policy.classify("POST", "api/override"), "write")
        self.assertEqual(self.policy.classify("PUT", "api/anything"), "write")

    def test_should_record_evidence_worthy_actions(self) -> None:
        for path in ("api/scan", "api/objects", "api/export", "api/compare", "api/connect"):
            self.assertTrue(self.policy.should_record("GET", path), path)
        self.assertTrue(self.policy.should_record("POST", "api/register"))
        self.assertTrue(self.policy.should_record("DELETE", "api/register"))

    def test_should_not_record_chatter(self) -> None:
        for path in ("api/health", "api/status", "api/stream", "api/search", "api/focus", "api/adapters"):
            self.assertFalse(self.policy.should_record("GET", path), path)
        # A plain read of the current register is not an import/clear.
        self.assertFalse(self.policy.should_record("GET", "api/register"))

    def test_is_api_path(self) -> None:
        self.assertTrue(self.policy.is_api_path("api/scan"))
        self.assertFalse(self.policy.is_api_path("app.js"))
        self.assertFalse(self.policy.is_api_path(""))


if __name__ == "__main__":
    unittest.main()
