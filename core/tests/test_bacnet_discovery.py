"""Unit tests for the BACnet discovery engine against the SIMULATED backend.

Covers the deterministic fixture backend, the PURE transport-plan builder (the
construction decisions that reach the lab), and the three-lane orchestration —
merge/dedupe, the fallback-only directed lane, and the foreign-device gate.

HONESTY: there is NO real BACnet device or building network here. Every test
runs against :class:`SimulatedBacnetBackend` (deterministic in-memory fixture),
a scripted fake, or asserts guard behaviour of the real backend WITHOUT touching
hardware. The real :class:`Bacpypes3Backend` transport is NOT exercised — only
its import-guard error is checked (skipped if bacpypes3 happens to be
installed).

WHAT THESE TESTS CANNOT PROVE — read before trusting a green run:

    * NOT the BVLL wire behaviour. Not one BACnet frame is emitted here. The
      transport-plan tests pin the string this code hands to bacpypes3; they say
      NOTHING about whether bacpypes3 accepts it, whether a Register-Foreign-
      Device PDU ever leaves the laptop, or whether Forwarded-NPDUs come back.
      ``test_bacpypes3_contract.py`` checks that API surface against the real
      pinned package; only site validation checks the wire.
    * NOT whether a real BBMD accepts our foreign-device registration. The lane-3
      fake answers because the test told it to. Whether the lab's BBMD cooperates
      is the single most likely way a commissioning day goes wrong, and no test
      here moves that probability at all.
    * NOT that a device answers a directed Who-Is with a unicast I-Am. BACnet-135
      permits a local-broadcast I-Am instead, which an off-subnet host cannot
      hear. The fakes answer directed probes because they are told to; real
      controllers may not.

The three lanes are redundant precisely because of the above. A green suite here
means "the orchestration and the reporting are right", never "BACnet discovery
works".
"""

import asyncio
import importlib.util
import inspect
import unittest
from typing import Any

from smart_commissioning_core.engines.bacnet_discovery import (
    BACKEND_SIMULATED,
    DEFAULT_LOCAL_UDP_PORT,
    FD_LOCAL_UDP_PORT,
    LANE_BROADCAST,
    LANE_DIRECTED,
    LANE_FOREIGN_DEVICE,
    MATCH_BASIS_WHO_IS,
    MATCH_BASIS_WHO_IS_DIRECTED,
    BacnetDiscoveryBackend,
    Bacpypes3Backend,
    SimulatedBacnetBackend,
    build_transport_plan,
    format_local_address,
    make_bacnet_discovery_engine,
    process_bacnet_discovery_run,
    split_local_address,
)
from smart_commissioning_core.engines.bacnet_params import (
    BACNET_INSTANCE_MAX,
    BACNET_INSTANCE_MIN,
    DEFAULT_BBMD_PORT,
    DEFAULT_FD_TTL,
    FD_TTL_MAX,
    FD_TTL_MIN,
    MODE_BROADCAST,
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_MODE,
    PARAM_BACNET_TARGETS,
    PARAM_BBMD_ADDRESS,
    PARAM_BBMD_PORT,
    PARAM_FD_TTL,
    BacnetTarget,
)
from smart_commissioning_core.engines.base import EngineContext, ThrottleConfig, run_engine_async
from smart_commissioning_core.engines.safety import ScanNotAuthorized

# THE SEAM RULE: every run-parameter key above is imported from bacnet_params and
# spelled BY NAME, never as a string literal. The backend's route tests import
# the SAME constants, so a key renamed on one side of the route <-> engine seam
# now breaks both suites instead of neither. Re-spelling "bbmd_address" here
# would restore exactly the silent drift that module exists to prevent — the
# kind that passes every test and fails only against a real BBMD, on a network
# where nobody can debug it.

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


class DirectedSimBackend(RecordingSimBackend):
    """RecordingSimBackend that HONOURS the directed address.

    :class:`SimulatedBacnetBackend` ignores ``address`` — it is an in-memory
    fixture, not a network — so it answers a directed Who-Is to ANY address with
    the whole fixture. That cannot express the one thing lane 2 exists for: a
    device reachable only by unicast, and an address where nothing answers.

    Keeps the inherited ``(low, high, address)`` capture (recording exactly once
    on both paths) and adds a per-address script.
    """

    def __init__(
        self,
        devices: Any = None,
        *,
        directed: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(devices)
        self._directed: dict[str, list[dict[str, Any]]] = {
            address: [dict(row) for row in rows] for address, rows in (directed or {}).items()
        }

    async def who_is(
        self, low_limit: int, high_limit: int, address: str | None = None
    ) -> list[dict[str, Any]]:
        if address is None:
            # Broadcast: the inherited path records the call and reads the fixture.
            return await super().who_is(low_limit, high_limit, address)
        self.who_is_calls.append((low_limit, high_limit, address))
        return [
            dict(row)
            for row in self._directed.get(address, [])
            if low_limit <= int(row["device_instance"]) <= high_limit
        ]


class ScriptedBacnetBackend:
    """A minimal scripted backend — CI's stand-in for a second BACnet transport.

    Deliberately NOT a SimulatedBacnetBackend subclass. Lane 3's app must be able
    to answer with devices the local fixture does not contain (a routed device
    only a BBMD can reach is the entire reason lane 3 exists), and the tests need
    to see which app each per-device read went back out through.
    """

    def __init__(
        self,
        *,
        backend_name: str = BACKEND_SIMULATED,
        broadcast: list[dict[str, Any]] | None = None,
        directed: dict[str, list[dict[str, Any]]] | None = None,
        transport_plan: Any = None,
    ) -> None:
        self.backend_name = backend_name
        self.transport_plan = transport_plan
        self._broadcast = [dict(row) for row in (broadcast or [])]
        self._directed = {
            address: [dict(row) for row in rows] for address, rows in (directed or {}).items()
        }
        self.who_is_calls: list[tuple[int, int, str | None]] = []
        self.read_devices: list[Any] = []
        self.closed = 0

    async def who_is(
        self, low_limit: int, high_limit: int, address: str | None = None
    ) -> list[dict[str, Any]]:
        self.who_is_calls.append((low_limit, high_limit, address))
        rows = self._broadcast if address is None else self._directed.get(address, [])
        return [dict(row) for row in rows if low_limit <= int(row["device_instance"]) <= high_limit]

    async def read_object_list(self, device: Any) -> list[dict[str, Any]]:
        self.read_devices.append(device.get("device_instance"))
        return []

    async def read_present_value(self, device: Any, obj: Any) -> Any:
        return None

    def close(self) -> None:
        self.closed += 1


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

    def test_non_dry_run_defaults_to_real_backend_and_never_returns_fixtures(self) -> None:
        # A direct non-dry processor call follows the same honesty rule as the
        # public API: no selector means bacpypes3, never the offline fixtures.
        store = FakeRunStore()
        result = process_bacnet_discovery_run(
            "run_default",
            dict(_AUTHORIZED),
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("Source Interface", str(result["error_message"]))
        self.assertNotIn("backend", store.summary_calls[-1])
        self.assertEqual(store.summary_calls[-1]["device_count"], 0)

    def test_non_dry_run_rejects_explicit_simulated_backend(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        result = process_bacnet_discovery_run(
            "run_simulated_live",
            {**_AUTHORIZED, "bacnet_backend": "simulated"},
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
            persist_records=lambda run_id, records: persisted.append((run_id, list(records))),
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("only available for dry runs", str(result["error_message"]))
        self.assertEqual(persisted, [])

    def test_non_dry_run_rejects_unknown_backend(self) -> None:
        store = FakeRunStore()

        result = process_bacnet_discovery_run(
            "run_unknown_backend",
            {**_AUTHORIZED, "bacnet_backend": "not-a-backend"},
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("Unsupported BACnet backend", str(result["error_message"]))


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
        importlib.util.find_spec("bacpypes3") is not None,
        "bacpypes3 is installed; the honest-failure (missing-dep) branch is unreachable",
    )
    def test_authorized_bacpypes3_run_without_install_fails_not_simulated(self) -> None:
        # HONESTY LAW: an AUTHORIZED, non-dry-run run that selects the real
        # bacpypes3 backend while the dependency is missing must record a REAL
        # failed status (from _ensure_app's RuntimeError at the first Who-Is) —
        # never silently fall back to simulated data. No backend is injected, so
        # selection goes through parameters["bacnet_backend"] == "bacpypes3".
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            persisted.append((run_id, list(records)))

        result = process_bacnet_discovery_run(
            "run_real_missing_dep",
            {**_AUTHORIZED, "bacnet_backend": "bacpypes3", "local_address": "192.0.2.10/24"},
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
            persist_records=persist,
        )

        self.assertEqual(result["status"], "failed")
        # A real, recorded failure — not a silent success with fabricated data.
        self.assertIsNotNone(store.last_error)
        # No simulated fallback: no result summary (no "simulated" backend label)
        # and NO devices/points were persisted.
        self.assertEqual(store.summary_calls, [])
        self.assertEqual(persisted, [])

    def test_authorized_bacpypes3_run_without_source_interface_fails_with_reason(self) -> None:
        # Audit fix: a live bacpypes3 scan with no Source Interface (local_address
        # unset) cannot bind a socket, so it fails with an ACTIONABLE reason on the
        # run's error_message (what the UI surfaces) — never a silent simulated
        # fallback, and deterministic regardless of whether bacpypes3 is installed
        # (the guard fires before any bacpypes3 import). It deliberately does NOT
        # stamp a live-backend provenance label, since no scan actually ran.
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            persisted.append((run_id, list(records)))

        result = process_bacnet_discovery_run(
            "run_no_source_interface",
            {**_AUTHORIZED, "bacnet_backend": "bacpypes3"},  # no local_address
            run_store=store,
            execution_mode="inline_local_fallback",
            throttle=ThrottleConfig(rate_limit_per_sec=None),
            persist_records=persist,
        )

        self.assertEqual(result["status"], "failed")
        # The actionable reason is on error_message (the field the UI renders).
        self.assertIn("Source Interface", str(result["error_message"]))
        self.assertEqual(store.last_error, result["error_message"])
        # No false "Live bacpypes3 scan" provenance: backend is not stamped.
        self.assertNotIn("backend", store.summary_calls[-1])
        self.assertEqual(persisted, [])

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


class LocalAddressParsingTests(unittest.TestCase):
    """The address splitter — PURE, and load-bearing for both the bind and the BBMD."""

    def test_split_local_address_reads_every_shape_this_codebase_produces(self) -> None:
        cases = [
            ("192.168.1.10", ("192.168.1.10", None, None)),
            ("192.168.1.10/24", ("192.168.1.10", "24", None)),
            ("192.168.1.10/24:47808", ("192.168.1.10", "24", 47808)),
            ("192.168.1.10:47808", ("192.168.1.10", None, 47808)),
            ("  192.168.1.10/24  ", ("192.168.1.10", "24", None)),
            ("", ("", None, None)),
        ]
        for raw, expected in cases:
            with self.subTest(address=raw):
                self.assertEqual(split_local_address(raw), expected)

    def test_split_local_address_reports_none_rather_than_guessing(self) -> None:
        # Inventing a port for a shape it cannot read would be exactly the silent
        # substitution this release exists to remove. An IPv6-ish string keeps its
        # colons instead of being truncated into a plausible-looking lie.
        ip, prefix, port = split_local_address("fe80::1:2:3")
        self.assertEqual(ip, "fe80::1:2:3")
        self.assertIsNone(prefix)
        self.assertIsNone(port)

    def test_format_local_address_round_trips_every_shape(self) -> None:
        for raw in ("192.168.1.10", "192.168.1.10/24", "192.168.1.10/24:47809"):
            with self.subTest(address=raw):
                self.assertEqual(format_local_address(*split_local_address(raw)), raw)


class BacnetTransportPlanTests(unittest.TestCase):
    """The PURE construction decisions — CI's ONLY view of what reaches the lab.

    :func:`build_transport_plan` decides the exact string handed to
    ``NetworkPortObject`` and the exact ``fdBBMDAddress`` handed to ``HostNPort``.
    Those two values are the whole difference between a scan that reaches the
    devices and one that silently reaches nothing, and this is the only place they
    can be checked before site validation: CI has neither bacpypes3 nor a BACnet
    network. These tests pin OUR side of that call and cannot prove bacpypes3
    accepts it (see the module docstring).
    """

    _INTERFACE = "192.168.1.10/24"

    def _params(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {**_AUTHORIZED, "local_address": self._INTERFACE}
        params.update(extra)
        return params

    def test_broadcast_plan_hands_bacpypes3_the_operator_address_untouched(self) -> None:
        # THE ZERO-REGRESSION PIN. The local-broadcast lane is the path that works
        # today, so bacpypes3 must receive the operator's Source Interface string
        # byte-identically — NOT helpfully re-rendered with ":47808". Whether
        # NetworkPortObject parses a port suffix is not verbatim-verified, and
        # today's working path is the wrong place to find that out.
        plan = build_transport_plan(self._params())

        self.assertEqual(plan.local_address, "192.168.1.10/24")
        self.assertEqual(plan.mode, MODE_BROADCAST)
        self.assertEqual(plan.bind_ip, "192.168.1.10")
        self.assertEqual(plan.udp_port, DEFAULT_LOCAL_UDP_PORT)
        self.assertFalse(plan.is_foreign_device)
        # No half-configured foreign device on a lane nobody asked to register.
        self.assertIsNone(plan.fd_bbmd_address)
        self.assertIsNone(plan.fd_ttl)
        self.assertNotIn("fd_bbmd_address", plan.as_dict())
        self.assertNotIn("fd_ttl", plan.as_dict())

    def test_enabling_foreign_device_leaves_the_broadcast_lane_byte_identical(self) -> None:
        # The two-app layout's regression pin, asserted as literal equality of the
        # frozen plans: turning on foreign-device registration must not perturb
        # lane 1 by one field. Lane 1 on an FD run IS lane 1 on a plain run.
        plain = build_transport_plan(self._params())
        lane_one_of_an_fd_run = build_transport_plan(
            self._params(
                **{
                    PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                    PARAM_BBMD_ADDRESS: "10.0.0.5",
                    PARAM_BBMD_PORT: 47810,
                    PARAM_FD_TTL: 600,
                }
            ),
            mode=MODE_BROADCAST,
        )
        self.assertEqual(lane_one_of_an_fd_run, plain)

    def test_broadcast_plan_keeps_an_operator_supplied_port(self) -> None:
        plan = build_transport_plan(self._params(local_address="192.168.1.10/24:47812"))
        self.assertEqual(plan.local_address, "192.168.1.10/24:47812")
        self.assertEqual(plan.udp_port, 47812)
        self.assertEqual(plan.bind_ip, "192.168.1.10")

    def test_foreign_device_plan_binds_its_own_port_and_names_the_bbmd_port_explicitly(self) -> None:
        plan = build_transport_plan(
            self._params(
                **{
                    PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                    PARAM_BBMD_ADDRESS: "10.0.0.5",
                    PARAM_BBMD_PORT: 47810,
                    PARAM_FD_TTL: 600,
                }
            ),
            udp_port=FD_LOCAL_UDP_PORT,
        )

        self.assertTrue(plan.is_foreign_device)
        self.assertEqual(plan.mode, MODE_FOREIGN_DEVICE)
        # Lane 3 is the ONE place a port override is unavoidable (lane 1 must keep
        # 47808 to hear I-Am replies), so this is the one plan that re-renders the
        # address.
        self.assertEqual(plan.local_address, "192.168.1.10/24:47809")
        self.assertEqual(plan.udp_port, FD_LOCAL_UDP_PORT)
        self.assertEqual(plan.bind_ip, "192.168.1.10")
        # ALWAYS "ip:port", never a bare IP. HostNPort applies its own default for
        # a bare IP, but that default was never verbatim-verified at the pin — and
        # a wrong BBMD port fails on site looking exactly like a firewall.
        self.assertEqual(plan.fd_bbmd_address, "10.0.0.5:47810")
        self.assertEqual(plan.fd_ttl, 600)
        self.assertEqual(plan.as_dict()["fd_bbmd_address"], "10.0.0.5:47810")
        self.assertEqual(plan.as_dict()["fd_ttl"], 600)

    def test_foreign_device_plan_soft_defaults_the_bbmd_port_and_ttl(self) -> None:
        plan = build_transport_plan(
            self._params(
                **{PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE, PARAM_BBMD_ADDRESS: "10.0.0.5"}
            ),
            udp_port=FD_LOCAL_UDP_PORT,
        )
        self.assertEqual(plan.fd_bbmd_address, f"10.0.0.5:{DEFAULT_BBMD_PORT}")
        self.assertEqual(plan.fd_ttl, DEFAULT_FD_TTL)

    def test_a_legacy_bbmd_address_carrying_a_port_is_not_double_suffixed(self) -> None:
        # A config snapshot predating ConfigurationService's IP validation can hold
        # "10.0.0.5:47810". Blindly appending the port would build the un-routable
        # "10.0.0.5:47810:47808".
        plan = build_transport_plan(
            self._params(
                **{PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE, PARAM_BBMD_ADDRESS: "10.0.0.5:47810"}
            ),
            udp_port=FD_LOCAL_UDP_PORT,
        )
        self.assertEqual(plan.fd_bbmd_address, "10.0.0.5:47810")

    def test_an_explicit_bbmd_port_parameter_wins_over_one_embedded_in_the_address(self) -> None:
        plan = build_transport_plan(
            self._params(
                **{
                    PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                    PARAM_BBMD_ADDRESS: "10.0.0.5:47810",
                    PARAM_BBMD_PORT: 47820,
                }
            ),
            udp_port=FD_LOCAL_UDP_PORT,
        )
        self.assertEqual(plan.fd_bbmd_address, "10.0.0.5:47820")

    def test_foreign_device_without_a_bbmd_address_raises_and_never_degrades_to_broadcast(
        self,
    ) -> None:
        # Quietly scanning the local subnet for a run the operator explicitly asked
        # to send through a BBMD would report a clean scan of the wrong network —
        # the original bug wearing a new hat.
        for blank in (None, "", "   "):
            with self.subTest(bbmd_address=blank):
                with self.assertRaises(ValueError) as cm:
                    build_transport_plan(
                        self._params(
                            **{PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE, PARAM_BBMD_ADDRESS: blank}
                        ),
                        udp_port=FD_LOCAL_UDP_PORT,
                    )
                message = str(cm.exception)
                self.assertIn("BBMD Address", message)
                self.assertIn("Configuration", message)

    def test_a_missing_source_interface_raises_the_actionable_message(self) -> None:
        for blank in (None, "", "   "):
            with self.subTest(local_address=blank):
                with self.assertRaises(ValueError) as cm:
                    build_transport_plan({**_AUTHORIZED, "local_address": blank})
                self.assertIn("Source Interface", str(cm.exception))

    def test_fd_ttl_out_of_range_soft_defaults_rather_than_blocking_a_scan(self) -> None:
        # fdSubscriptionLifetime travels as a BACnet Unsigned16, so FD_TTL_MAX is a
        # wire limit, not a policy. Junk in an old snapshot soft-defaults (a scan is
        # not worth blocking over it); a usable value passes through untouched.
        cases = [
            (None, DEFAULT_FD_TTL),
            (600, 600),
            ("600", 600),  # config values arrive as strings
            (FD_TTL_MIN, FD_TTL_MIN),
            (FD_TTL_MAX, FD_TTL_MAX),
            (0, DEFAULT_FD_TTL),
            (-5, DEFAULT_FD_TTL),
            (FD_TTL_MAX + 1, DEFAULT_FD_TTL),
            ("not-a-number", DEFAULT_FD_TTL),
            # True must never read as TTL 1: a bool sneaking through a type error
            # would put a 1-second subscription on the wire.
            (True, DEFAULT_FD_TTL),
        ]
        for raw, expected in cases:
            with self.subTest(fd_ttl=raw):
                plan = build_transport_plan(
                    self._params(
                        **{
                            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
                            PARAM_BBMD_ADDRESS: "10.0.0.5",
                            PARAM_FD_TTL: raw,
                        }
                    ),
                    udp_port=FD_LOCAL_UDP_PORT,
                )
                self.assertEqual(plan.fd_ttl, expected)

    def test_an_unrecognised_mode_raises_instead_of_falling_through_to_broadcast(self) -> None:
        # A transport setting that is silently ignored is the exact bug v0.1.12
        # exists to fix; it must not reappear as a default-case fallthrough.
        with self.assertRaises(ValueError):
            build_transport_plan(self._params(**{PARAM_BACNET_MODE: "bbmd"}))
        with self.assertRaises(ValueError):
            build_transport_plan(self._params(), mode="bbmd")


class BacnetLaneOrchestrationTests(unittest.TestCase):
    """The three lanes: merge, dedupe, fallback-only unicast, and the FD gate."""

    def _run(
        self,
        store: FakeRunStore,
        ctx: EngineContext,
        backend: Any,
        fd_backend: Any = None,
    ) -> tuple[Any, list[tuple[str, list[dict[str, Any]]]]]:
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            persisted.append((run_id, list(records)))

        engine = make_bacnet_discovery_engine(backend, fd_backend)
        result = asyncio.run(run_engine_async(ctx, engine, persist_records=persist))
        return result, persisted

    def test_a_default_run_sends_exactly_one_broadcast_and_runs_no_other_lane(self) -> None:
        # THE ZERO-REGRESSION PIN at the orchestration layer: with nothing new
        # configured, exactly one broadcast Who-Is goes out — no directed probes,
        # no BBMD registration, not one extra frame on the network.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        result, _ = self._run(store, _ctx(store), backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(backend.who_is_calls, [(BACNET_INSTANCE_MIN, BACNET_INSTANCE_MAX, None)])

        summary = store.summary_calls[-1]
        self.assertEqual(summary["device_count"], 3)
        for asset in summary["discovered_assets"]:
            # Byte-identical provenance to every release before v0.1.12 — the
            # reporting layer must not break the pin the transport layer keeps.
            self.assertEqual(asset["match_basis"], MATCH_BASIS_WHO_IS)
            self.assertEqual(asset["lane"], LANE_BROADCAST)

        lanes = summary["lanes"]
        self.assertTrue(lanes[LANE_BROADCAST]["ran"])
        self.assertFalse(lanes[LANE_DIRECTED]["ran"])
        self.assertEqual(lanes[LANE_FOREIGN_DEVICE], {"ran": False, "reason": "not_configured"})
        self.assertEqual(summary["expected_not_responding"], [])
        self.assertFalse(summary["unicast_fallback_attempted"])

    def test_the_directed_lane_probes_only_targets_the_broadcast_did_not_hear(self) -> None:
        # Lane 2 is FALLBACK-ONLY. 1001 answered the broadcast, so not one unicast
        # frame is spent on it; only the genuinely-silent 9001 is probed. On an OT
        # network the frames you do not send are a feature.
        store = FakeRunStore()
        heard = BacnetTarget(address="10.10.0.11", device_instance=1001, asset_id="AHU-1")
        silent = BacnetTarget(address="10.10.0.99", device_instance=9001, asset_id="GHOST")
        backend = DirectedSimBackend(
            directed={
                "10.10.0.99": [
                    {"device_instance": 9001, "address": "10.10.0.99:47808", "name": "Routed VAV"}
                ]
            }
        )
        params = {**_AUTHORIZED, PARAM_BACNET_TARGETS: [heard.as_dict(), silent.as_dict()]}
        result, _ = self._run(store, _ctx(store, parameters=params), backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            backend.who_is_calls,
            [
                (BACNET_INSTANCE_MIN, BACNET_INSTANCE_MAX, None),
                (BACNET_INSTANCE_MIN, BACNET_INSTANCE_MAX, "10.10.0.99"),
            ],
        )
        self.assertNotIn("10.10.0.11", [call[2] for call in backend.who_is_calls])

    def test_the_directed_lane_finds_a_device_the_broadcast_missed(self) -> None:
        store = FakeRunStore()
        silent = BacnetTarget(address="10.10.0.99", device_instance=9001, asset_id="GHOST")
        backend = DirectedSimBackend(
            directed={
                "10.10.0.99": [
                    {"device_instance": 9001, "address": "10.10.0.99:47808", "name": "Routed VAV"}
                ]
            }
        )
        params = {**_AUTHORIZED, PARAM_BACNET_TARGETS: [silent.as_dict()]}
        result, _ = self._run(store, _ctx(store, parameters=params), backend)

        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["device_count"], 4)
        routed = next(a for a in summary["discovered_assets"] if a["device_instance"] == 9001)
        # A unicast answer is a DIFFERENT fact from a broadcast answer, and the
        # report says which one it was.
        self.assertEqual(routed["match_basis"], MATCH_BASIS_WHO_IS_DIRECTED)
        self.assertEqual(routed["lane"], LANE_DIRECTED)
        self.assertEqual(summary["expected_responding_count"], 1)
        self.assertEqual(summary["expected_not_responding"], [])
        self.assertTrue(summary["unicast_fallback_attempted"])
        self.assertEqual(summary["lanes"][LANE_DIRECTED]["probe_count"], 1)

    def test_a_device_heard_on_two_lanes_is_one_device_and_the_broadcast_wins(self) -> None:
        store = FakeRunStore()
        backend = RecordingSimBackend()
        # The BBMD forwards 1001's I-Am too (it is a broadcast lane), plus a routed
        # 3001 the local broadcast cannot reach.
        fd_backend = ScriptedBacnetBackend(
            broadcast=[
                {"device_instance": 1001, "address": "10.10.0.11:47808", "name": "AHU-1 Controller"},
                {"device_instance": 3001, "address": "10.20.0.7:47808", "name": "Routed AHU"},
            ]
        )
        params = {
            **_AUTHORIZED,
            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
            PARAM_BBMD_ADDRESS: "10.0.0.5",
        }
        result, _ = self._run(store, _ctx(store, parameters=params), backend, fd_backend=fd_backend)

        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        # 1001 is ONE device, not two: 1001, 1002, 2050 + the routed 3001.
        self.assertEqual(summary["device_count"], 4)
        instances = [a["device_instance"] for a in summary["discovered_assets"]]
        self.assertEqual(instances.count(1001), 1)

        first = next(a for a in summary["discovered_assets"] if a["device_instance"] == 1001)
        self.assertEqual(first["lane"], LANE_BROADCAST, "broadcast-first: first heard wins")
        routed = next(a for a in summary["discovered_assets"] if a["device_instance"] == 3001)
        self.assertEqual(routed["lane"], LANE_FOREIGN_DEVICE)
        # Lane 3 IS a broadcast (through the BBMD), so what it hears is stamped as
        # a broadcast match, not a directed one.
        self.assertEqual(routed["match_basis"], MATCH_BASIS_WHO_IS)

        fd_lane = summary["lanes"][LANE_FOREIGN_DEVICE]
        self.assertTrue(fd_lane["ran"])
        self.assertEqual(fd_lane["i_am_count"], 2)
        self.assertEqual(fd_lane["device_count"], 1, "only 3001 was new")
        self.assertEqual(fd_lane["udp_port"], FD_LOCAL_UDP_PORT)

    def test_a_configured_bbmd_address_alone_never_starts_the_foreign_device_lane(self) -> None:
        # THE GATE (COORDINATION decision 6). Every default install carries the
        # seeded — and fictional — BBMD Address, so gating lane 3 on its PRESENCE
        # would make every site register against a BBMD nobody asked for. The gate
        # is the mode, and nothing but the mode.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        fd_backend = ScriptedBacnetBackend(
            broadcast=[{"device_instance": 3001, "address": "10.20.0.7:47808"}]
        )
        params = {**_AUTHORIZED, PARAM_BBMD_ADDRESS: "10.10.25.20"}  # the seeded default, no mode
        result, _ = self._run(store, _ctx(store, parameters=params), backend, fd_backend=fd_backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            fd_backend.who_is_calls, [], "a bbmd_address alone must never register with a BBMD"
        )
        summary = store.summary_calls[-1]
        self.assertEqual(
            summary["lanes"][LANE_FOREIGN_DEVICE], {"ran": False, "reason": "not_configured"}
        )
        self.assertEqual(summary["device_count"], 3, "3001 was never heard: lane 3 never ran")

    def test_targets_outside_the_instance_window_are_neither_probed_nor_called_silent(self) -> None:
        # A narrow-window scan (one known device, instance [N,N]) is the standard
        # first live gate. A target outside the window CANNOT answer by definition,
        # so probing it would burn a timeout and then file a false "expected but did
        # not answer" row — dozens of them against a perfectly healthy lab.
        store = FakeRunStore()
        backend = DirectedSimBackend()
        params = {
            **_AUTHORIZED,
            "device_instance_low": 1001,
            "device_instance_high": 1001,
            PARAM_BACNET_TARGETS: [
                BacnetTarget(address="10.10.0.11", device_instance=1001).as_dict(),
                BacnetTarget(address="10.10.0.99", device_instance=9001, asset_id="GHOST").as_dict(),
            ],
        }
        result, _ = self._run(store, _ctx(store, parameters=params), backend)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(backend.who_is_calls, [(1001, 1001, None)])
        summary = store.summary_calls[-1]
        directed = summary["lanes"][LANE_DIRECTED]
        self.assertEqual(directed["target_count"], 1)
        self.assertEqual(directed["out_of_instance_range_count"], 1)
        self.assertEqual(directed["probe_count"], 0)
        self.assertEqual(summary["expected_device_count"], 1)
        self.assertEqual(summary["expected_not_responding"], [])

    def test_per_device_reads_go_back_out_the_lane_that_heard_the_device(self) -> None:
        # A routed device only the BBMD lane could hear is not necessarily readable
        # from the local-broadcast app. Reading it through the wrong app would turn
        # a device we genuinely found into a wall of read errors.
        store = FakeRunStore()
        backend = ScriptedBacnetBackend(
            broadcast=[{"device_instance": 1001, "address": "10.10.0.11:47808"}]
        )
        fd_backend = ScriptedBacnetBackend(
            broadcast=[{"device_instance": 3001, "address": "10.20.0.7:47808"}]
        )
        params = {
            **_AUTHORIZED,
            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
            PARAM_BBMD_ADDRESS: "10.0.0.5",
        }
        self._run(store, _ctx(store, parameters=params), backend, fd_backend=fd_backend)

        self.assertEqual(backend.read_devices, [1001])
        self.assertEqual(fd_backend.read_devices, [3001])

    def test_register_identity_never_overwrites_what_the_device_announced(self) -> None:
        # What the register CLAIMS and what the device ANNOUNCED are different
        # facts. A report that silently replaces the second with the first is how a
        # mislabelled panel survives commissioning.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        target = BacnetTarget(
            address="10.10.0.11",
            device_instance=1001,
            asset_id="AHU-1",
            asset_name="AHU-1 as named in the register",
            network=2001,
        )
        params = {
            **_AUTHORIZED,
            "device_instance_low": 1001,
            "device_instance_high": 1001,
            PARAM_BACNET_TARGETS: [target.as_dict()],
        }
        _, persisted = self._run(store, _ctx(store, parameters=params), backend)

        _, records = persisted[0]
        device_row = next(r for r in records if r.get("device_type") == "bacnet_device")
        self.assertEqual(device_row["name"], "AHU-1 Controller", "the observed name stays observed")
        attributes = device_row["attributes"]
        self.assertEqual(attributes["register_asset_id"], "AHU-1")
        self.assertEqual(attributes["register_asset_name"], "AHU-1 as named in the register")
        self.assertEqual(attributes["register_address"], "10.10.0.11")
        self.assertEqual(attributes["expected_network"], 2001)

        asset = store.summary_calls[-1]["discovered_assets"][0]
        self.assertEqual(asset["register_asset_id"], "AHU-1")
        self.assertEqual(asset["name"], "AHU-1 Controller")

    def test_an_oversized_register_fails_the_run_before_a_single_packet(self) -> None:
        # Telling the operator their register is too big AFTER spraying frames at a
        # live OT network would be the wrong order.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        rows = [BacnetTarget(f"10.10.0.{i}", 9000 + i).as_dict() for i in range(1, 4)]
        params = {**_AUTHORIZED, "max_targets": 2, PARAM_BACNET_TARGETS: rows}
        result, persisted = self._run(store, _ctx(store, parameters=params), backend)

        self.assertEqual(result["status"], "failed")
        self.assertIn("max_targets=2", str(result["error_message"]))
        self.assertEqual(backend.who_is_calls, [])
        self.assertEqual(persisted, [])

    def test_dry_run_echoes_the_resolved_foreign_device_transport_and_emits_nothing(self) -> None:
        # The in-app pre-flight gate: the operator reads back the transport the live
        # run WOULD use before a packet leaves the laptop.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        params = {
            **_AUTHORIZED,
            "local_address": "192.168.1.10/24",
            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
            PARAM_BBMD_ADDRESS: "10.0.0.5",
            PARAM_BACNET_TARGETS: [BacnetTarget("10.10.0.99", 9001).as_dict()],
        }
        self._run(store, _ctx(store, parameters=params, dry_run=True), backend)

        self.assertEqual(backend.who_is_calls, [], "a dry run must emit no Who-Is")
        plan = store.summary_calls[-1]["dry_run_plan"]
        transport = plan["transport"]
        self.assertEqual(transport["mode"], MODE_FOREIGN_DEVICE)
        lanes = {lane["lane"]: lane for lane in transport["lanes"]}
        self.assertEqual(lanes[LANE_BROADCAST]["local_address"], "192.168.1.10/24")
        self.assertEqual(lanes[LANE_BROADCAST]["udp_port"], DEFAULT_LOCAL_UDP_PORT)
        self.assertEqual(
            lanes[LANE_FOREIGN_DEVICE]["fd_bbmd_address"], f"10.0.0.5:{DEFAULT_BBMD_PORT}"
        )
        self.assertEqual(lanes[LANE_FOREIGN_DEVICE]["udp_port"], FD_LOCAL_UDP_PORT)
        self.assertIn("foreign-device registration via BBMD 10.0.0.5:47808", plan["notes"])
        # "0 targets" here is the cheapest possible catch for a register import that
        # never reached the engine.
        self.assertEqual(plan["unicast_target_count"], 1)
        self.assertIn("bacnet-who-is-directed", plan["actions"])

    def test_dry_run_shows_a_broken_foreign_device_config_instead_of_hiding_it(self) -> None:
        # A preview that refuses to render the problem is a preview that hides it,
        # so the misconfiguration is ECHOED rather than raised: the operator sees it
        # on the page where they can still fix it.
        store = FakeRunStore()
        backend = RecordingSimBackend()
        params = {
            **_AUTHORIZED,
            "local_address": "192.168.1.10/24",
            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,  # ...with no BBMD Address
        }
        result, _ = self._run(store, _ctx(store, parameters=params, dry_run=True), backend)

        self.assertEqual(result["status"], "succeeded")
        plan = store.summary_calls[-1]["dry_run_plan"]
        lanes = {lane["lane"]: lane for lane in plan["transport"]["lanes"]}
        self.assertIn("BBMD Address", lanes[LANE_FOREIGN_DEVICE]["error"])
        self.assertNotIn("error", lanes[LANE_BROADCAST])


if __name__ == "__main__":
    unittest.main()
