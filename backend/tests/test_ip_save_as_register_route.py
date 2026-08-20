"""Unit checks for the IP save-as-register route's register binder.

The save-as-register route itself follows the sidecar-route convention of no
TestClient coverage (the pure ``register_rows_from_devices`` projection + the
ImportService pipeline are the tested seam). What is worth pinning here is
``_bind_ip_scanner_register``: the newest-accepted selection, that it drops a
client-smuggled id, and that it leaves the key absent when no usable register
exists. Driven through the injectable ``records`` param, so no DB is touched.

Importing ``app.api.routes.scanners`` binds discovery.py's module-level engine,
so (like test_scanner_report_inventory) the import is deferred into ``setUp`` to
avoid poisoning later DB-backed tests under unittest.
"""

from __future__ import annotations

import unittest


class BindIpScannerRegisterTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.api.routes import scanners

        self.bind = scanners._bind_ip_scanner_register

    def test_binds_newest_accepted_register(self) -> None:
        params: dict = {}
        # ImportRepository.list returns newest-first; the first with accepted rows wins.
        records = [
            {"import_id": "imp_new", "accepted_rows": [{"IP Address": "192.0.2.5"}]},
            {"import_id": "imp_old", "accepted_rows": [{"IP Address": "192.0.2.6"}]},
        ]
        self.bind("proj", "site", params, records=records)
        self.assertEqual(params["register_import_id"], "imp_new")

    def test_skips_registers_with_no_accepted_rows(self) -> None:
        params: dict = {}
        records = [
            {"import_id": "imp_empty", "accepted_rows": []},
            {"import_id": "imp_good", "accepted_rows": [{"IP Address": "192.0.2.5"}]},
        ]
        self.bind("proj", "site", params, records=records)
        self.assertEqual(params["register_import_id"], "imp_good")

    def test_no_usable_register_leaves_key_absent(self) -> None:
        params: dict = {}
        self.bind("proj", "site", params, records=[{"import_id": "imp_empty", "accepted_rows": []}])
        self.assertNotIn("register_import_id", params)

    def test_drops_a_client_smuggled_import_id(self) -> None:
        # A caller cannot pin another workspace's register by supplying its id.
        params: dict = {"register_import_id": "imp_smuggled"}
        self.bind("proj", "site", params, records=[])
        self.assertNotIn("register_import_id", params)


if __name__ == "__main__":
    unittest.main()
