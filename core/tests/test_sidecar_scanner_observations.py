"""GAP-C3: progressive device observations emitted from the sidecar scan fold.

The ip/bacnet sidecar adapters consume their vendored SSE server-side. These
tests pin that each adapter's ``_stream_scan`` device / device-update fold emits
viewer-safe ``projection_v1`` device observations through the run store's
progressive-observation sink (the channel the frontend folds into live rows),
bounded by a cap, best-effort so a rejecting store never breaks the scan, and
without touching the final ``{rows, summary}`` the adapter returns.

stdlib ``unittest`` only (CI runs unittest, not pytest); no live Node process
and no network — the SSE stream is faked with an in-memory byte iterator.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
    observation_payload,
)
from smart_commissioning_core.engines import bacnet_scanner_sidecar as bacnet_mod
from smart_commissioning_core.engines import ip_scanner_sidecar as ip_mod
from smart_commissioning_core.engines.sidecar_observations import (
    MAX_PROGRESSIVE_DEVICE_OBSERVATIONS,
    ProgressiveDeviceEmitter,
)


class _RecordingStore:
    """A run store whose progressive-observation sink records every append."""

    def __init__(self) -> None:
        self.observations: list[DiscoveryObservationInputV1] = []

    def append_observation(self, observation: DiscoveryObservationInputV1) -> None:
        self.observations.append(observation)


class _RejectingStore:
    """A run store that rejects sidecar observations (production job-type gate)."""

    def __init__(self) -> None:
        self.calls = 0

    def append_observation(self, observation: DiscoveryObservationInputV1) -> None:
        self.calls += 1
        raise RuntimeError("unsupported_discovery_job_or_protocol")


class _FakeResponse:
    """Context-manager stand-in for urlopen: yields SSE ``data:`` byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):  # noqa: ANN204 - iterator is all _stream_scan needs
        return iter(self._lines)

    def __exit__(self, *_exc: object) -> bool:
        return False


def _sse(events: list[str]) -> list[bytes]:
    return [f"data: {event}\n".encode() for event in events]


def _no_cancel() -> bool:
    return False


class IpDeviceRecordTest(unittest.TestCase):
    def test_none_without_ip(self) -> None:
        self.assertIsNone(ip_mod._ip_device_record({}, project_id="p", site_id="s"))

    def test_record_shape_and_port_normalization(self) -> None:
        record = ip_mod._ip_device_record(
            {"ip": "192.0.2.10", "hostname": "h", "mac": "aa", "vendor": "Acme",
             "openPorts": [443, 80, 80, 70000, 0]},
            project_id="p",
            site_id="s",
        )
        assert record is not None
        # Exactly the observation contract's device record fields.
        self.assertEqual(
            set(record),
            {"project_id", "site_id", "address", "device_type", "name", "vendor", "model", "attributes"},
        )
        self.assertEqual(record["address"], "192.0.2.10")
        self.assertEqual(record["device_type"], "ip_host")
        # Ports deduped, sorted, out-of-range dropped.
        self.assertEqual(record["attributes"]["open_ports"], [80, 443])
        self.assertTrue(record["attributes"]["reachable"])


class BacnetDeviceRecordTest(unittest.TestCase):
    def test_none_without_integer_instance(self) -> None:
        self.assertIsNone(bacnet_mod._bacnet_device_record({}, project_id="p", site_id="s"))
        self.assertIsNone(
            bacnet_mod._bacnet_device_record({"instance": "x"}, project_id="p", site_id="s")
        )

    def test_record_shape(self) -> None:
        record = bacnet_mod._bacnet_device_record(
            {"instance": 1001, "ip": "10.0.0.11", "name": "AHU-1", "vendor": "Acme",
             "model": "V1", "mac": "0a", "vendorId": 7},
            project_id="p",
            site_id="s",
        )
        assert record is not None
        self.assertEqual(record["device_type"], "bacnet_device")
        self.assertEqual(record["address"], "10.0.0.11")
        self.assertEqual(record["attributes"]["device_instance"], 1001)
        self.assertEqual(record["attributes"]["vendor_id"], 7)


class EmitterContractTest(unittest.TestCase):
    def test_emits_contract_valid_projection_observation(self) -> None:
        store = _RecordingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="ip", entity_kind="host")
        record = ip_mod._ip_device_record(
            {"ip": "192.0.2.10", "hostname": "h", "openPorts": [80]},
            project_id="p",
            site_id="s",
        )
        assert record is not None
        emitter.emit("host:192.0.2.10", record)
        self.assertEqual(len(store.observations), 1)
        obs = store.observations[0]
        self.assertEqual(obs.protocol, "ip")
        self.assertEqual(obs.entity_kind, "host")
        self.assertEqual(obs.entity_key, "host:192.0.2.10")
        self.assertEqual(obs.entity_version, 1)
        projection = obs.payload["projection_v1"]
        self.assertEqual(projection["collection"], "devices")
        self.assertEqual(projection["record"]["address"], "192.0.2.10")
        # The emitted payload passes the real repository normalizer (so the shape
        # a production append would persist is contract-valid; only the job-type
        # gate — out of this PR's scope — differs).
        observation_payload(obs.payload)

    def test_re_observation_increments_entity_version_and_holds_position(self) -> None:
        store = _RecordingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="bacnet", entity_kind="device")
        record = bacnet_mod._bacnet_device_record(
            {"instance": 1001, "ip": "10.0.0.11"}, project_id="p", site_id="s"
        )
        assert record is not None
        emitter.emit("device:1001", record)
        emitter.emit("device:1001", record)
        self.assertEqual([o.entity_version for o in store.observations], [1, 2])
        self.assertEqual(
            [o.event_key for o in store.observations], ["device:1001:v1", "device:1001:v2"]
        )

    def test_cap_bounds_total_emissions(self) -> None:
        store = _RecordingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="ip", entity_kind="host")
        for index in range(MAX_PROGRESSIVE_DEVICE_OBSERVATIONS + 25):
            emitter.emit(
                f"host:10.0.{index // 256}.{index % 256}",
                {
                    "project_id": "p", "site_id": "s",
                    "address": f"10.0.{index // 256}.{index % 256}",
                    "device_type": "ip_host", "name": None, "vendor": None, "model": None,
                    "attributes": {"reachable": True},
                },
            )
        self.assertEqual(emitter.emitted, MAX_PROGRESSIVE_DEVICE_OBSERVATIONS)
        self.assertEqual(len(store.observations), MAX_PROGRESSIVE_DEVICE_OBSERVATIONS)

    def test_rejecting_store_latches_off_after_first_failure(self) -> None:
        store = _RejectingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="ip", entity_kind="host")
        record = {"project_id": "p", "site_id": "s", "address": "192.0.2.1",
                  "device_type": "ip_host", "name": None, "vendor": None, "model": None,
                  "attributes": {"reachable": True}}
        emitter.emit("host:192.0.2.1", record)
        emitter.emit("host:192.0.2.2", {**record, "address": "192.0.2.2"})
        # Exactly one attempt reached the store; emission is then disabled so the
        # scan proceeds and the lifecycle-conflict audit is not spammed.
        self.assertEqual(store.calls, 1)
        self.assertFalse(emitter.enabled)
        self.assertEqual(emitter.emitted, 0)

    def test_absent_sink_is_a_silent_no_op(self) -> None:
        emitter = ProgressiveDeviceEmitter(object(), protocol="ip", entity_kind="host")
        self.assertFalse(emitter.enabled)
        emitter.emit("host:192.0.2.1", {"project_id": None, "site_id": None,
                                        "address": "192.0.2.1", "device_type": "ip_host",
                                        "name": None, "vendor": None, "model": None,
                                        "attributes": {"reachable": True}})
        self.assertEqual(emitter.emitted, 0)


class IpStreamScanFoldTest(unittest.TestCase):
    def test_device_events_emit_observations_and_result_is_unchanged(self) -> None:
        store = _RecordingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="ip", entity_kind="host")

        def on_device(device: Any) -> None:
            record = ip_mod._ip_device_record(device, project_id="p", site_id="s")
            if record is not None:
                emitter.emit(f"host:{record['address']}", record)

        lines = _sse([
            '{"type":"start","total":2}',
            '{"type":"device","device":{"ip":"192.0.2.10","hostname":"a","openPorts":[80]}}',
            '{"type":"device","device":{"ip":"192.0.2.11","hostname":"b","openPorts":[443]}}',
            '{"type":"result","rows":[{"ip":"192.0.2.10","rag":"green"}],"summary":{"reachable":2}}',
            '{"type":"complete"}',
        ])
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(lines)):
            result = ip_mod._stream_scan(
                "http://127.0.0.1:1", {"start": "192.0.2.10"}, _no_cancel, on_device=on_device
            )
        # Two provisional rows emitted from the live device events.
        self.assertEqual([o.entity_key for o in store.observations],
                         ["host:192.0.2.10", "host:192.0.2.11"])
        for obs in store.observations:
            self.assertEqual(obs.payload["projection_v1"]["collection"], "devices")
        # The returned final result is exactly the compare `result` event — the
        # emission did not change what the adapter maps and persists.
        self.assertEqual(result["summary"], {"reachable": 2})
        self.assertEqual(result["rows"], [{"ip": "192.0.2.10", "rag": "green"}])

    def test_no_callback_is_backward_compatible(self) -> None:
        lines = _sse([
            '{"type":"device","device":{"ip":"192.0.2.10"}}',
            '{"type":"result","rows":[],"summary":{}}',
            '{"type":"complete"}',
        ])
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(lines)):
            result = ip_mod._stream_scan("http://127.0.0.1:1", {"start": "192.0.2.10"}, _no_cancel)
        self.assertEqual(result, {"rows": [], "summary": {}})


class BacnetStreamScanFoldTest(unittest.TestCase):
    def test_device_and_update_events_emit_and_result_is_unchanged(self) -> None:
        store = _RecordingStore()
        emitter = ProgressiveDeviceEmitter(store, protocol="bacnet", entity_kind="device")

        def on_device(device: Any) -> None:
            record = bacnet_mod._bacnet_device_record(device, project_id="p", site_id="s")
            if record is not None:
                emitter.emit(f"device:{record['attributes']['device_instance']}", record)

        lines = _sse([
            '{"type":"start","adapter":"eth0"}',
            '{"type":"device","device":{"instance":1001,"ip":"10.0.0.11"}}',
            '{"type":"device-update","device":{"instance":1001,"name":"AHU-1","vendor":"Acme"}}',
            '{"type":"router","router":"10.0.0.1","networks":[200]}',
            '{"type":"result","rows":[{"instance":1001,"rag":"green"}],"summary":{"discovered":1}}',
            '{"type":"complete"}',
        ])
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(lines)):
            rows, summary, devices, routers = bacnet_mod._stream_scan(
                "http://127.0.0.1:1", 0, {}, _no_cancel, on_device=on_device
            )
        # Sighting + enrichment update fold onto one entity at rising versions.
        self.assertEqual([o.entity_key for o in store.observations],
                         ["device:1001", "device:1001"])
        self.assertEqual([o.entity_version for o in store.observations], [1, 2])
        # The enrichment update carried the name into the accumulated device dict.
        self.assertEqual(store.observations[1].payload["projection_v1"]["record"]["name"], "AHU-1")
        # Final scan output, device targets and routers are unchanged by emission.
        self.assertEqual(rows, [{"instance": 1001, "rag": "green"}])
        self.assertEqual(summary, {"discovered": 1})
        self.assertEqual(devices, [{"instance": 1001, "ip": "10.0.0.11", "name": "AHU-1", "vendor": "Acme"}])
        self.assertEqual(routers, [{"address": "10.0.0.1", "networks": [200]}])


if __name__ == "__main__":
    unittest.main()
