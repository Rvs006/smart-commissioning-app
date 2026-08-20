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


class BindScannerRegisterTypeTest(unittest.TestCase):
    """The generic binder (IP/BACnet/MQTT sidecars share it) must query the
    exact register import type it was asked for. Without records injected it hits
    ImportRepository.list, so a wrong or renamed type string breaks loudly here.
    """

    def setUp(self) -> None:
        from app.api.routes import scanners

        self.scanners = scanners

    def _assert_binds_type(self, import_type: str) -> None:
        from unittest.mock import patch

        captured: dict = {}

        class _Repo:
            def __init__(self, *_a, **_k) -> None:
                pass

            def list(self, *, project_id: str, site_id: str, import_type: str) -> list:
                captured["import_type"] = import_type
                return [{"import_id": "imp_new", "accepted_rows": [{"col": "v"}]}]

        params: dict = {}
        with patch.object(self.scanners, "ImportRepository", _Repo):
            self.scanners._bind_scanner_register("p", "s", params, import_type=import_type)
        self.assertEqual(captured["import_type"], import_type)
        self.assertEqual(params["register_import_id"], "imp_new")

    def test_binds_the_newest_accepted_of_the_requested_type(self) -> None:
        for import_type in ("bacnet_scanner_register", "mqtt_scanner_register", "ip_scanner_register"):
            with self.subTest(import_type=import_type):
                self._assert_binds_type(import_type)


if __name__ == "__main__":
    unittest.main()
