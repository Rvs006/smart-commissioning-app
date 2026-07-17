"""JSON-safe value normalization at the BACnet engine boundary.

Real bacpypes3 reads return LIBRARY objects, not primitives: a binary-input
present-value is an enumerated object, a vendor id is an ``Unsigned``. Left raw,
they reach a JSON repository column and raise on serialization AFTER the live
scan already completed on the wire — the 100%-reproducible field crash that left
every real discovery run's POST 500ing and the run fossilized at "running".
Simulated / dry-run data is plain primitives, which is why only real runs died.

HONESTY: there is NO real BACnet device or building network here. The live
objects are stood in by :class:`_RawEnum` (str()-able, deliberately NOT
JSON-serializable — the tests assert that premise), and the live-backend OUTPUT
shape is reproduced by a scripted fake. These tests prove the normalization and
the persistence-safety contract; they say nothing about the BACnet wire.
"""

import asyncio
import json
import unittest
from typing import Any

from smart_commissioning_core.engines.bacnet_discovery import (
    BACKEND_BACPYPES3,
    Bacpypes3Backend,
    _json_safe_value,
    process_bacnet_discovery_run,
)
from smart_commissioning_core.engines.bacnet_params import BACNET_INSTANCE_MAX


class _RawEnum:
    """Stand-in for a bacpypes3 enumerated / Unsigned value.

    str()-able to an honest observed token, but NOT JSON-serializable — exactly
    the shape that poisons a repository write when stored raw.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def __str__(self) -> str:
        return self._token


class FakeRunStore:
    """In-memory RunStore capturing every call the run wrapper makes."""

    def __init__(self) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self.last_error: str | None = None

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        self.status_calls.append({"status": status, "stage": stage})
        self.last_status = status
        self.last_error = error_message
        return {
            "run_id": run_id,
            "status": status,
            "stage": stage,
            "progress_percent": progress_percent,
            "error_message": error_message,
            "result_summary": dict(self.record_summary),
        }

    def update_result_summary(
        self, run_id: str, result_summary: dict[str, Any], *, merge: bool = True
    ) -> dict[str, Any]:
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        return {"run_id": run_id, "issues": list(issues)}


class LiveShapedBackend:
    """Fake backend reproducing the REAL bacpypes3 backend's output shape.

    ``who_is`` returns clean device metadata (as Bacpypes3Backend.who_is does
    after its own vendor_id normalization); ``read_object_list`` returns the
    identifier/type-only objects the real ReadProperty two-step yields; and
    ``read_present_value`` returns RAW, non-JSON-serializable library objects —
    the part the engine must normalize before persistence.
    """

    backend_name = BACKEND_BACPYPES3

    def __init__(self) -> None:
        self._device = {
            "device_instance": 1001,
            "address": "10.0.0.11:47808",
            "vendor_id": 260,
        }

    async def who_is(
        self, low_limit: int, high_limit: int, address: str | None = None
    ) -> list[dict[str, Any]]:
        if address is not None:
            return []
        if low_limit <= 1001 <= high_limit:
            return [dict(self._device)]
        return []

    async def read_object_list(self, device: Any) -> list[dict[str, Any]]:
        return [
            {"object_identifier": "binary-input,1", "object_type": "binary-input"},
            {"object_identifier": "analog-input,2", "object_type": "analog-input"},
        ]

    async def read_present_value(self, device: Any, obj: Any) -> Any:
        if obj["object_identifier"] == "binary-input,1":
            return _RawEnum("active")
        return _RawEnum("22.4")


class _FakeIAm:
    """A scripted I-Am APDU carrying a RAW vendorID, like real hardware."""

    def __init__(self, instance: int, source: str, vendor: Any) -> None:
        self.iAmDeviceIdentifier = ("device", instance)
        self.pduSource = source
        self.vendorID = vendor


class _FakeApp:
    """Stands in for a bacpypes3 Application so who_is skips the real import."""

    def __init__(self, i_ams: list[_FakeIAm]) -> None:
        self._i_ams = i_ams

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: Any = None,
        timeout: float | None = None,
    ) -> list[_FakeIAm]:
        return list(self._i_ams)


class JsonSafeValueTests(unittest.TestCase):
    def test_primitives_pass_through_untouched(self) -> None:
        # Identity, not just equality: a primitive is returned as the SAME object,
        # so the honest observed number/string is never re-rendered.
        for value in (None, True, False, 0, -3, 42, 3.14, "active", ""):
            self.assertIs(_json_safe_value(value), value)

    def test_non_primitive_is_coerced_with_str(self) -> None:
        self.assertEqual(_json_safe_value(_RawEnum("active")), "active")
        self.assertIsInstance(_json_safe_value(_RawEnum("active")), str)

    def test_coerced_value_is_json_serializable(self) -> None:
        # Premise: the raw object is NOT serializable on its own.
        with self.assertRaises(TypeError):
            json.dumps({"value": _RawEnum("active")})
        # After coercion it round-trips.
        self.assertEqual(
            json.dumps({"value": _json_safe_value(_RawEnum("active"))}),
            '{"value": "active"}',
        )


class Bacpypes3WhoIsVendorIdTests(unittest.TestCase):
    def test_who_is_normalizes_raw_vendor_id(self) -> None:
        backend = Bacpypes3Backend(local_address="192.168.1.10/24")
        # Inject a fake Application so _ensure_app returns without importing
        # bacpypes3 (broadcast plan => _ensure_registered is a no-op).
        backend._app = _FakeApp([_FakeIAm(1001, "10.0.0.11", _RawEnum("260"))])

        devices = asyncio.run(backend.who_is(0, BACNET_INSTANCE_MAX, None))

        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertEqual(device["device_instance"], 1001)
        # The raw vendorID is coerced to its honest string token at the boundary.
        self.assertEqual(device["vendor_id"], "260")
        self.assertIsInstance(device["vendor_id"], str)
        json.dumps(device)  # the whole device dict is now JSON-safe


class LiveShapedPersistenceTests(unittest.TestCase):
    """A live-shaped run with non-serializable point values must reach a terminal
    status and, after normalization, SUCCEED with the coerced string values."""

    def test_nonserializable_point_values_persist_after_normalization(self) -> None:
        persisted: list[dict[str, Any]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            # Mimic a JSON repository column: every record must round-trip. Raw
            # bacpypes3 objects would raise here — that is the bug under test.
            for record in records:
                json.dumps(record)
            persisted.extend(records)

        store = FakeRunStore()
        params = {
            "local_address": "192.168.1.10/24",
            "scan_authorization": {"authorized": True, "authorized_by": "t@example.com"},
        }

        result = process_bacnet_discovery_run(
            "run_live",
            params,
            run_store=store,
            execution_mode="inline_local_fallback",
            backend=LiveShapedBackend(),
            persist_records=persist,
        )

        # Terminal, and specifically SUCCEEDED — never fossilized at 'running'.
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.last_status, "succeeded")
        self.assertNotEqual(store.last_status, "running")

        point_records = {
            record["point_id"]: record["observed_value"]
            for record in persisted
            if "observed_value" in record
        }
        # The enumerated / analog present-values were coerced to honest strings,
        # never re-encoded to invented numbers.
        self.assertEqual(point_records["binary-input,1"], {"value": "active"})
        self.assertEqual(point_records["analog-input,2"], {"value": "22.4"})


if __name__ == "__main__":
    unittest.main()
