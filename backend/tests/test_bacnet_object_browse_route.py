"""Unit checks for the bacnet_scanner object-browse route's pure helpers.

The browse route itself follows the sidecar-route convention of no TestClient
coverage (the driving contract + the pure ``map_browse_objects`` mapper are the
tested seam). What is worth pinning here is the one non-trivial parse the route
adds: splitting a persisted device address into (ip, port), because the sidecar
embeds a non-default BACnet port as ``ip:port`` and a wrong split would target
the wrong port on a directed read.

Importing ``app.api.routes.scanners`` binds discovery.py's module-level engine,
so (like test_scanner_report_inventory) the import is deferred into ``setUp`` to
avoid poisoning later DB-backed tests under unittest.
"""

from __future__ import annotations

import unittest


class SplitBacnetAddressTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.api.routes import scanners

        self.scanners = scanners

    def test_bare_ipv4_has_no_port(self) -> None:
        self.assertEqual(self.scanners._split_bacnet_address("10.0.0.5"), ("10.0.0.5", None))

    def test_embedded_non_default_port_is_split_out(self) -> None:
        self.assertEqual(self.scanners._split_bacnet_address("10.0.0.5:47809"), ("10.0.0.5", 47809))

    def test_blank_or_none_address_is_empty(self) -> None:
        self.assertEqual(self.scanners._split_bacnet_address(""), ("", None))
        self.assertEqual(self.scanners._split_bacnet_address(None), ("", None))

    def test_non_numeric_suffix_is_not_treated_as_a_port(self) -> None:
        # A trailing ":something" that is not digits stays part of the address,
        # never a guessed port.
        self.assertEqual(self.scanners._split_bacnet_address("host:abc"), ("host:abc", None))

    def test_optional_int_coerces_or_returns_none(self) -> None:
        self.assertEqual(self.scanners._optional_int(1201), 1201)
        self.assertEqual(self.scanners._optional_int("1201"), 1201)
        self.assertIsNone(self.scanners._optional_int(None))
        self.assertIsNone(self.scanners._optional_int("not-an-int"))


if __name__ == "__main__":
    unittest.main()
