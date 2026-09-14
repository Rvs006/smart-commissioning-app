"""The register-CSV download routes: the file behind "Save scan as register".

The contract worth pinning is that a download hands back EXACTLY the bytes the
matching save-as-register route imported for the same run. If the two ever used
different helpers, the operator would keep a file that is not the register SCT
actually applies to the next scan.

Driven by calling the route functions directly with the two DB seams patched
(``_load_discovery_run`` and ``DiscoveryRepository``), the way
test_ip_save_as_register_route drives the binder: the sidecar routes carry no
TestClient coverage, and the role/auth dependencies here are declared exactly as
they are on the save routes next door. Importing ``app.api.routes.scanners``
binds discovery.py's module-level engine, so the import is deferred into
``setUp`` to avoid poisoning later DB-backed tests under unittest.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _run(job_type: str, status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-1", job_type=job_type, status=status, project_id="proj", site_id="site"
    )


_IP_DEVICES = [
    {
        "address": "192.0.2.10",
        "name": "plant-controller",
        "device_type": "ip_host",
        "vendor": "ExampleVendor",
        "model": "X1",
        "attributes": {"open_ports": [80, 443], "location": "Level 1"},
    }
]

_BACNET_DEVICES = [
    {
        "address": "192.0.2.20:47808",
        "name": "AHU-1",
        "vendor": "ExampleVendor",
        "model": "B2",
        "attributes": {"device_instance": 1001, "network": 0, "object_count": 12},
    }
]

_MQTT_TOPICS = [
    {
        "topic": "udmi/site/example/AHU-1/event/pointset",
        "attributes": {"device_ref": "AHU-1", "schema": "udmi", "site": "example", "room": "Plant"},
    }
]


class _StubRepository:
    """Stands in for DiscoveryRepository: returns the canned evidence per lane."""

    devices: list = []
    topics: list = []

    def __init__(self, _engine: object) -> None:
        return None

    def list_devices(self, _run_id: str) -> list:
        return list(type(self).devices)

    def list_topics(self, _run_id: str) -> list:
        return list(type(self).topics)


class RegisterCsvDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        from app.api.routes import scanners

        self.scanners = scanners
        self.principal = SimpleNamespace(username="tester")
        _StubRepository.devices = []
        _StubRepository.topics = []

    def _download(self, lane: str, run: SimpleNamespace):
        with patch.object(self.scanners, "_load_discovery_run", return_value=run), patch.object(
            self.scanners, "DiscoveryRepository", _StubRepository
        ):
            return self.scanners._register_csv_download(lane, "run-1", self.principal)

    def _http_error(self, lane: str, run: SimpleNamespace):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            self._download(lane, run)
        return caught.exception

    # -- 200: byte-identical to what save-as-register imported -----------------

    def test_ip_download_matches_the_save_routes_bytes(self) -> None:
        from smart_commissioning_core.engines.ip_scanner_sidecar import (
            _register_csv,
            register_rows_from_devices,
        )

        _StubRepository.devices = _IP_DEVICES
        response = self._download("ip", _run("ip_scanner"))

        expected = _register_csv(register_rows_from_devices(_IP_DEVICES)).encode("utf-8")
        self.assertEqual(response.body, expected)
        self.assertEqual(response.media_type, "text/csv; charset=utf-8")
        self.assertEqual(
            response.headers["content-disposition"], 'attachment; filename="scan-register-run-1.csv"'
        )

    def test_bacnet_download_matches_the_save_routes_bytes(self) -> None:
        from smart_commissioning_core.engines.bacnet_scanner_sidecar import (
            _register_csv,
            register_rows_from_devices,
        )

        _StubRepository.devices = _BACNET_DEVICES
        response = self._download("bacnet", _run("bacnet_scanner"))

        expected = _register_csv(register_rows_from_devices(_BACNET_DEVICES)).encode("utf-8")
        self.assertEqual(response.body, expected)
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="bacnet-scan-register-run-1.csv"',
        )

    def test_mqtt_download_matches_the_save_routes_bytes(self) -> None:
        from smart_commissioning_core.engines.mqtt_scanner_sidecar import (
            _register_csv,
            register_rows_from_topics,
        )

        _StubRepository.topics = _MQTT_TOPICS
        response = self._download("mqtt", _run("mqtt_scanner"))

        expected = _register_csv(register_rows_from_topics(_MQTT_TOPICS)).encode("utf-8")
        self.assertEqual(response.body, expected)
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="mqtt-scan-register-run-1.csv"',
        )

    # -- 404 / 409: the save routes' own semantics ----------------------------

    def test_wrong_job_type_is_404_on_every_lane(self) -> None:
        for lane, wrong_job_type in (
            ("ip", "bacnet_scanner"),
            ("bacnet", "mqtt_scanner"),
            ("mqtt", "ip_scanner"),
        ):
            with self.subTest(lane=lane):
                error = self._http_error(lane, _run(wrong_job_type))
                self.assertEqual(error.status_code, 404)

    def test_unfinished_run_is_409_on_every_lane(self) -> None:
        for lane, job_type in (
            ("ip", "ip_scanner"),
            ("bacnet", "bacnet_scanner"),
            ("mqtt", "mqtt_scanner"),
        ):
            with self.subTest(lane=lane):
                error = self._http_error(lane, _run(job_type, status="running"))
                self.assertEqual(error.status_code, 409)
                self.assertIn("succeeded", error.detail)

    def test_a_run_with_no_saveable_rows_is_409_not_an_empty_file(self) -> None:
        # An empty CSV would look like a register that expects nothing, which the
        # next scan would read as "every device is rogue". Refuse it, as save does.
        error = self._http_error("ip", _run("ip_scanner"))
        self.assertEqual(error.status_code, 409)


if __name__ == "__main__":
    unittest.main()
