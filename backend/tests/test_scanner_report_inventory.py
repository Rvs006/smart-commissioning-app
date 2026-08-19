"""_discovery_inventory renders ip/bacnet/mqtt *_scanner runs (M5 reporting).

Frozen-snapshot path only: the run carries ``source_run_snapshots`` (validated
into RunRecords) and ``source_discovery_snapshots`` (the persisted device/point/
topic rows), so no DB or engine is touched. Every frozen row is salted with the
repository ``id`` / ``created_at`` a real read would carry; the tests prove those
sentinels never reach a rendered cell (byte-reproducibility of the signed
artifact).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.api.routes.reports import (
    _BACNET_DEVICE_COLUMNS,
    _BACNET_POINT_COLUMNS,
    _IP_INVENTORY_COLUMNS,
    _MQTT_TOPIC_COLUMNS,
    _discovery_inventory,
)

# Sentinel values a live DiscoveryRepository read would attach to each row. If
# any of these strings surfaces in a rendered cell, byte-reproducibility is
# broken (a repository id or wall-clock leaked into the artifact).
_FORBIDDEN_KEYS = {"id", "created_at", "updated_at", "terminal_at", "timestamp"}
_SENTINEL_ID = "row-id-should-never-render"
_SENTINEL_TS = "2026-08-19T12:34:56+00:00"


def _salt(row: dict[str, object]) -> dict[str, object]:
    """Add the repository id/created_at a real read carries, for leak checks."""
    return {**row, "id": _SENTINEL_ID, "created_at": _SENTINEL_TS}


def _run(job_type: str, run_id: str, snapshot: dict[str, list[dict[str, object]]], summary: dict[str, object]):
    record = {
        "run_id": run_id,
        "job_type": job_type,
        "status": "succeeded",
        "stage": "completed",
        "progress_percent": 100,
        "created_at": _SENTINEL_TS,
        "updated_at": _SENTINEL_TS,
        "project_id": "proj",
        "site_id": "site",
        "parameters": {},
        "result_summary": summary,
    }
    parameters = {
        "source_run_snapshots": [record],
        "source_discovery_snapshots": {run_id: snapshot},
    }
    return SimpleNamespace(parameters=parameters)


def _section(sections: list[dict[str, object]], title_columns: tuple[str, ...]) -> dict[str, object]:
    for section in sections:
        if section["columns"] == title_columns:
            return section
    raise AssertionError(f"no section with columns {title_columns}")


def _assert_no_leak(testcase: unittest.TestCase, sections: list[dict[str, object]]) -> None:
    for section in sections:
        for row in section["rows"]:
            testcase.assertFalse(
                _FORBIDDEN_KEYS & set(row),
                f"rendered row exposes an id/timestamp key: {sorted(set(row))}",
            )
            for value in row.values():
                testcase.assertNotIn(_SENTINEL_ID, str(value))
                testcase.assertNotIn(_SENTINEL_TS, str(value))


class ScannerReportInventoryTest(unittest.TestCase):
    def test_ip_scanner_renders_ip_columns_from_scanner_keys(self) -> None:
        run = _run(
            "ip_scanner",
            "ip-run",
            {
                "devices": [
                    _salt(
                        {
                            "device_type": "ip_host",
                            "address": "10.0.0.5",
                            "name": "host-a",
                            "attributes": {
                                "mac_address": "aa:bb:cc:dd:ee:ff",
                                "open_ports": [80, 443],
                                # ip_scanner's own port-diff key names.
                                "extra_ports": [8080],
                                "missing_ports": [22],
                            },
                        }
                    )
                ]
            },
            {"hosts_scanned": 3, "hosts_responsive": 1},
        )
        sections = _discovery_inventory(run)
        self.assertIsNotNone(sections)
        section = _section(sections, _IP_INVENTORY_COLUMNS)
        self.assertEqual(len(section["rows"]), 1)
        row = section["rows"][0]
        self.assertEqual(row["Address"], "10.0.0.5")
        self.assertEqual(row["Open Ports"], "80, 443")
        # Read from extra_ports / missing_ports, not the *_open_ports names.
        self.assertEqual(row["Unexpected Open"], "8080")
        self.assertEqual(row["Missing Expected"], "22")
        # No forbidden-port list is persisted by the scanner -> blank.
        self.assertEqual(row["Forbidden Open"], "—")
        counts = _section(sections, ("Source Run", "Type", "Status", "Counts"))["rows"][0]["Counts"]
        self.assertEqual(counts, "3 hosts scanned, 1 responsive")
        _assert_no_leak(self, sections)

    def test_bacnet_scanner_renders_device_and_point_columns(self) -> None:
        run = _run(
            "bacnet_scanner",
            "bac-run",
            {
                "devices": [
                    _salt(
                        {
                            "device_type": "bacnet_device",
                            "address": "10.0.0.9",
                            "name": "ahu-1",
                            "vendor": "acme",
                            "model": "x1",
                            "attributes": {
                                "device_instance": 100,
                                "asset_id": "bacnet-device-100",
                                # scanner writes register_state, not register_asset_name.
                                "register_state": "match",
                            },
                        }
                    )
                ],
                "points": [
                    _salt(
                        {
                            "device_ref": "bacnet-device-100",
                            "point_id": "analogInput-1",
                            "point_name": "SupplyTemp",
                            "observed_value": {"value": 21.5},
                            "units": "degC",
                        }
                    )
                ],
            },
            {"devices_discovered": 1},
        )
        sections = _discovery_inventory(run)
        device_section = _section(sections, _BACNET_DEVICE_COLUMNS)
        device_row = device_section["rows"][0]
        self.assertEqual(device_row["Instance"], "100")
        # register_state renders where register_asset_name is absent.
        self.assertEqual(device_row["Register Asset"], "match")
        # Point count keys off asset_id == point.device_ref.
        self.assertEqual(device_row["Points"], "1")
        point_section = _section(sections, _BACNET_POINT_COLUMNS)
        point_row = point_section["rows"][0]
        self.assertEqual(point_row["Device"], "bacnet-device-100")
        self.assertEqual(point_row["Value"], "21.5")
        self.assertEqual(point_row["Units"], "degC")
        _assert_no_leak(self, sections)

    def test_mqtt_scanner_renders_topic_columns_and_summed_messages(self) -> None:
        run = _run(
            "mqtt_scanner",
            "mqtt-run",
            {
                "topics": [
                    _salt(
                        {
                            "topic": "site/ahu/points",
                            "message_count": 4,
                            "attributes": {"device_ref": "ahu-1"},
                        }
                    ),
                    _salt(
                        {
                            "topic": "site/vav/points",
                            "message_count": 6,
                            "attributes": {"device_ref": "vav-2"},
                        }
                    ),
                ]
            },
            # mqtt_scanner does not persist messages_captured.
            {"topics_discovered": 2},
        )
        sections = _discovery_inventory(run)
        section = _section(sections, _MQTT_TOPIC_COLUMNS)
        self.assertEqual(len(section["rows"]), 2)
        self.assertEqual(section["rows"][0]["Topic"], "site/ahu/points")
        self.assertEqual(section["rows"][0]["Device Ref"], "ahu-1")
        counts = _section(sections, ("Source Run", "Type", "Status", "Counts"))["rows"][0]["Counts"]
        # topics_discovered from summary; messages summed from per-topic counts.
        self.assertEqual(counts, "2 topics, 10 messages")
        _assert_no_leak(self, sections)


if __name__ == "__main__":
    unittest.main()
