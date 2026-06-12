"""Unit tests for the BACnet discovery engine against the SIMULATED backend.

HONESTY: there is NO real BACnet device or building network here. Every test
runs against :class:`SimulatedBacnetBackend` (deterministic in-memory fixture)
or asserts guard behaviour of the real backend WITHOUT touching hardware. The
real :class:`Bacpypes3Backend` transport is NOT exercised — only its
import-guard error is checked (skipped if bacpypes3 happens to be installed).
"""

import asyncio
import importlib.util
import inspect
import unittest
from typing import Any

from smart_commissioning_core.engines.bacnet_discovery import (
    BACKEND_SIMULATED,
    BacnetDiscoveryBackend,
    Bacpypes3Backend,
    SimulatedBacnetBackend,
    make_bacnet_discovery_engine,
    process_bacnet_discovery_run,
)
from smart_commissioning_core.engines.base import EngineContext, ThrottleConfig, run_engine_async
from smart_commissioning_core.engines.safety import ScanNotAuthorized

_AUTHORIZED = {
    "scan_authorization": {
        "authorized": True,
        "authorized_by": "test.engineer@example.com",
    }
}


class FakeRunStore:
    """In-memory RunStore capturing every call the run wrapper makes."""

    def __init__(self) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self.last_progress: int | None = None
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
        self.last_progress = progress_percent
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
        self.summary_calls.append(dict(result_summary))
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        self.issues_calls.append(list(issues))
        return {"run_id": run_id, "issues": list(issues)}


class RecordingSimBackend(SimulatedBacnetBackend):
    """Simulated backend that records whether who_is was actually called."""

    def __init__(self, devices: Any = None) -> None:
        super().__init__(devices)
        self.who_is_calls: list[tuple[int, int, str | None]] = []

    async def who_is(
        self, low_limit: int, high_limit: int, address: str | None = None
    ) -> list[dict[str, Any]]:
        self.who_is_calls.append((low_limit, high_limit, address))
        return await super().who_is(low_limit, high_limit, address)


def _ctx(
    store: FakeRunStore,
    *,
    parameters: dict[str, Any] | None = None,
    dry_run: bool = False,
    is_cancelled: Any = None,
    throttle: ThrottleConfig | None = None,
) -> EngineContext:
    return EngineContext(
        run_id="run_bacnet_test",
        parameters=parameters if parameters is not None else dict(_AUTHORIZED),
        run_store=store,
        execution_mode="inline_local_fallback",
        throttle=throttle or ThrottleConfig(rate_limit_per_sec=None),
        dry_run=dry_run,
        _is_cancelled=is_cancelled or (lambda: False),
    )


class SimulatedBackendUnitTests(unittest.TestCase):
    """Direct tests of the deterministic fixture backend (no engine)."""

    def test_who_is_filters_by_instance_range(self) -> None:
        backend = SimulatedBacnetBackend()

        async def main() -> list[dict[str, Any]]:
            return await backend.who_is(0, 4194303)

        all_devices = asyncio.run(main())
        self.assertEqual({d["device_instance"] for d in all_devices}, {1001, 1002, 2050})
        # who_is must NOT leak the embedded objects list (real Who-Is returns
        # device metadata only; points come from read_object_list).
        for device in all_devices:
            self.assertNotIn("objects", device)

        async def narrowed() -> list[dict[str, Any]]:
            return await backend.who_is(1000, 1500)

        narrow = asyncio.run(narrowed())
        self.assertEqual({d["device_instance"] for d in narrow}, {1001, 1002})

    def test_read_object_list_and_present_value(self) -> None:
        backend = SimulatedBacnetBackend()

        async def main() -> tuple[list[dict[str, Any]], Any]:
            devices = await backend.who_is(1001, 1001)
            device = devices[0]
            objects = await backend.read_object_list(device)
            value = await backend.read_present_value(device, objects[0])
            return objects, value

        objects, value = asyncio.run(main())
        self.assertEqual(len(objects), 3)
        # object list must not carry the present_value (fetched separately).
        for obj in objects:
            self.assertNotIn("present_value", obj)
        self.assertEqual(value, 18.6)


class BacnetDiscoveryEngineTests(unittest.TestCase):
    def _run(
        self,
        store: FakeRunStore,
        ctx: EngineContext,
        backend: SimulatedBacnetBackend,
    ) -> tuple[Any, list[tuple[str, list[dict[str, Any]]]]]:
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            persisted.append((run_id, list(records)))

        engine = make_bacnet_discovery_engine(backend)
        result = asyncio.run(run_engine_async(ctx, engine, persist_records=persist))
        return result, persisted

    def test_who_is_produces_assets_and_records_and_labels_simulated(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        result, persisted = self._run(store, _ctx(store), backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.last_progress, 100)

        summary = store.summary_calls[-1]
        # Result is clearly labelled simulated so nobody mistakes it for a real scan.
        self.assertEqual(summary["backend"], BACKEND_SIMULATED)
        self.assertEqual(summary["device_count"], 3)
        # 3 + 2 + 1 present-value points across the fixture devices.
        self.assertEqual(summary["point_count"], 6)

        assets = summary["discovered_assets"]
        self.assertEqual(len(assets), 3)
        first = assets[0]
        self.assertEqual(first["asset_id"], "bacnet-device-1001")
        self.assertEqual(first["match_basis"], "bacnet_who_is")
        self.assertEqual(first["backend"], BACKEND_SIMULATED)
        self.assertEqual(first["point_count"], 3)

        # Structured records: 3 device rows + 6 point rows, devices first.
        self.assertEqual(len(persisted), 1)
        _, records = persisted[0]
        device_rows = [r for r in records if r.get("device_type") == "bacnet_device"]
        point_rows = [r for r in records if "point_id" in r]
        self.assertEqual(len(device_rows), 3)
        self.assertEqual(len(point_rows), 6)
        # Device-row shape matches DiscoveryRepository named columns + attributes.
        self.assertEqual(device_rows[0]["name"], "AHU-1 Controller")
        self.assertEqual(device_rows[0]["vendor"], "Acme Controls")
        self.assertEqual(device_rows[0]["attributes"]["device_instance"], 1001)
        # Point-row shape: observed_value is a JSON object wrapping the value.
        sample_point = next(r for r in point_rows if r["point_id"] == "analog-input,1")
        self.assertEqual(sample_point["device_ref"], "bacnet-device-1001")
        self.assertEqual(sample_point["observed_value"], {"value": 18.6})
        self.assertEqual(sample_point["units"], "degreesCelsius")

    def test_dry_run_does_not_broadcast(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        result, persisted = self._run(store, _ctx(store, dry_run=True), backend)

        self.assertEqual(result["status"], "succeeded")
        # The core assertion: who_is must NOT be called under dry_run.
        self.assertEqual(backend.who_is_calls, [], "dry run must not broadcast Who-Is")

        summary = store.summary_calls[-1]
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["backend"], BACKEND_SIMULATED)
        plan = summary["dry_run_plan"]
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["engine"], "bacnet_discovery")
        self.assertIn("bacnet-who-is-broadcast", plan["actions"])
        # No discovered devices and no structured records were persisted.
        self.assertEqual(summary["discovered_assets"], [])
        self.assertEqual(persisted, [])

    def test_dry_run_honours_instance_range_in_plan(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        params = dict(_AUTHORIZED)
        params.update({"device_instance_low": 1000, "device_instance_high": 1999})
        self._run(store, _ctx(store, parameters=params, dry_run=True), backend)

        plan = store.summary_calls[-1]["dry_run_plan"]
        target = plan["targets"][0]
        self.assertEqual(target["device_instance_low"], 1000)
        self.assertEqual(target["device_instance_high"], 1999)

    def test_authorization_required_unauthorized_run_fails(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        # No authorization in parameters -> engine raises -> framework records failed.
        result, persisted = self._run(store, _ctx(store, parameters={}), backend)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(backend.who_is_calls, [], "unauthorized run must not broadcast")
        self.assertEqual(persisted, [])
        # Sanitized failure message — no parameter contents leaked.
        self.assertIsNotNone(store.last_error)

    def test_engine_raises_scan_not_authorized_directly(self) -> None:
        # Drive the engine callable directly (outside run_engine) to confirm the
        # specific authorization exception is what is raised.
        store = FakeRunStore()
        engine = make_bacnet_discovery_engine(SimulatedBacnetBackend())
        ctx = _ctx(store, parameters={"authorized": False})

        with self.assertRaises(ScanNotAuthorized):
            asyncio.run(engine(ctx))

    def test_boolean_shorthand_authorization_accepted(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        result, _ = self._run(store, _ctx(store, parameters={"authorized": True}), backend)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(backend.who_is_calls), 1)

    def test_instance_range_narrows_discovery(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        params = dict(_AUTHORIZED)
        params.update({"device_instance_low": 2000, "device_instance_high": 3000})
        self._run(store, _ctx(store, parameters=params), backend)

        summary = store.summary_calls[-1]
        self.assertEqual(summary["device_count"], 1)
        self.assertEqual(summary["discovered_assets"][0]["device_instance"], 2050)
        self.assertEqual(backend.who_is_calls, [(2000, 3000, None)])

    def test_cancellation_yields_partial_and_cancelled_status(self) -> None:
        store = FakeRunStore()
        # Cancel as soon as the first device unit runs, so subsequent device
        # dispatches stop early (concurrency=1 makes dispatch serial).
        cancel_state = {"cancel": False}

        class CancellingBackend(RecordingSimBackend):
            async def read_object_list(self, device: Any) -> list[dict[str, Any]]:
                cancel_state["cancel"] = True
                return await super().read_object_list(device)

        backend = CancellingBackend()
        ctx = _ctx(
            store,
            is_cancelled=lambda: cancel_state["cancel"],
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
        )
        result, _ = self._run(store, ctx, backend)

        self.assertEqual(result["status"], "cancelled")
        summary = store.summary_calls[-1]
        self.assertTrue(summary.get("partial"))
        # who_is still ran (cancellation observed during per-device phase), but
        # not all three devices were processed -> partial results.
        self.assertEqual(len(backend.who_is_calls), 1)
        self.assertLess(summary["device_count"], 3)

    def test_per_point_read_error_does_not_abort_device(self) -> None:
        store = FakeRunStore()

        class FlakyBackend(RecordingSimBackend):
            async def read_present_value(self, device: Any, obj: Any) -> Any:
                if obj.get("object_identifier") == "analog-input,2":
                    raise RuntimeError("simulated read failure")
                return await super().read_present_value(device, obj)

        backend = FlakyBackend()
        params = dict(_AUTHORIZED)
        params.update({"device_instance_low": 1001, "device_instance_high": 1001})
        result, persisted = self._run(store, _ctx(store, parameters=params), backend)

        self.assertEqual(result["status"], "succeeded")
        _, records = persisted[0]
        point_rows = [r for r in records if "point_id" in r]
        # All 3 points still recorded; the failing one carries a read_error and
        # an empty observed_value.
        self.assertEqual(len(point_rows), 3)
        failed = next(r for r in point_rows if r["point_id"] == "analog-input,2")
        self.assertEqual(failed["observed_value"], {})
        self.assertEqual(failed["attributes"]["read_error"], "present_value_read_failed")

    def test_process_entrypoint_defaults_to_simulated_backend(self) -> None:
        # process_bacnet_discovery_run with NO backend must default to the
        # offline simulated backend and run end-to-end.
        store = FakeRunStore()
        result = process_bacnet_discovery_run(
            "run_default",
            dict(_AUTHORIZED),
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.summary_calls[-1]["backend"], BACKEND_SIMULATED)
        self.assertEqual(store.summary_calls[-1]["device_count"], 3)


class Bacpypes3BackendGuardTests(unittest.TestCase):
    """Guard behaviour of the real backend WITHOUT any hardware.

    We never drive a real Who-Is. We only confirm that selecting the real
    backend without bacpypes3 installed raises a clear RuntimeError (with an
    install hint), not a bare ImportError. Skipped if bacpypes3 is installed.
    """

    @unittest.skipIf(
        importlib.util.find_spec("bacpypes3") is not None,
        "bacpypes3 is installed; the import-guard branch cannot be exercised",
    )
    def test_selecting_bacpypes3_without_install_raises_runtime_error(self) -> None:
        backend = Bacpypes3Backend(local_address="192.0.2.10/24")
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(backend.who_is(0, 100))
        message = str(cm.exception)
        self.assertIn("bacpypes3", message)
        # Actionable install hint is surfaced.
        self.assertIn("install", message.lower())

    @unittest.skipIf(
        importlib.util.find_spec("bacpypes3") is None,
        "bacpypes3 not installed (expected in this offline environment)",
    )
    def test_bacpypes3_missing_local_address_raises_runtime_error(self) -> None:
        # Only runs if bacpypes3 happens to be installed: missing local_address
        # must raise a clear RuntimeError rather than crash deeper in the stack.
        backend = Bacpypes3Backend(local_address=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(backend.who_is(0, 100))


class Bacpypes3BackendSignatureTests(unittest.TestCase):
    """Static introspection of the real backend — NO network, NO bacpypes3.

    These tests pin the adapter's async surface so a future edit that breaks the
    coroutine signatures (the shapes the engine drives, and the ones validated
    against the bacpypes3 docs) fails loudly here, long before anyone gets to a
    real controller. Nothing is awaited and bacpypes3 is never imported — we only
    inspect the class, which constructs without the optional dependency.
    """

    def test_backend_name_is_bacpypes3(self) -> None:
        self.assertEqual(Bacpypes3Backend.backend_name, "bacpypes3")

    def test_backend_satisfies_discovery_protocol(self) -> None:
        # Constructing the backend must NOT import bacpypes3 (import is lazy in
        # _ensure_app); the instance must still structurally satisfy the Protocol.
        backend = Bacpypes3Backend(local_address="192.0.2.10/24")
        self.assertIsInstance(backend, BacnetDiscoveryBackend)

    def test_transport_methods_are_coroutines(self) -> None:
        for name in ("who_is", "read_object_list", "read_present_value"):
            method = getattr(Bacpypes3Backend, name, None)
            self.assertTrue(callable(method), f"{name} must exist")
            self.assertTrue(
                inspect.iscoroutinefunction(method),
                f"{name} must be an async def (the engine awaits it)",
            )

    def test_close_is_synchronous(self) -> None:
        close = getattr(Bacpypes3Backend, "close", None)
        self.assertTrue(callable(close), "close must exist")
        # VERIFIED against bacpypes3 docs: Application.close() is sync, so the
        # adapter's close() is a plain (non-async) method.
        self.assertFalse(
            inspect.iscoroutinefunction(close),
            "close must be synchronous (bacpypes3 Application.close() is sync)",
        )

    def test_who_is_signature(self) -> None:
        params = list(inspect.signature(Bacpypes3Backend.who_is).parameters)
        self.assertEqual(params, ["self", "low_limit", "high_limit", "address"])

    def test_read_object_list_signature(self) -> None:
        params = list(inspect.signature(Bacpypes3Backend.read_object_list).parameters)
        self.assertEqual(params, ["self", "device"])

    def test_read_present_value_signature(self) -> None:
        params = list(inspect.signature(Bacpypes3Backend.read_present_value).parameters)
        self.assertEqual(params, ["self", "device", "obj"])


if __name__ == "__main__":
    unittest.main()
