"""Unit tests for the shared engine framework (throttle, safety, run_engine).

Everything here runs against in-memory fakes / loopback asyncio — there is NO
real network, BACnet device, or MQTT broker involved. The throttle and the
dry-run helper are verified with a fake "target" that records whether it was
"contacted" so we can assert zero side effects in dry-run, and an in-flight
counter so we can assert the concurrency bound is never exceeded.
"""

import asyncio
import os
import threading
import unittest
from typing import Any
from unittest.mock import patch

from smart_commissioning_core.engines.base import (
    _PERSIST_FAILURE_MESSAGE,
    _RUNTIME_DEADLINE_FAILURE_MESSAGE,
    _SANITIZED_FAILURE_MESSAGE,
    EngineContext,
    EngineResult,
    Throttle,
    ThrottleConfig,
    run_engine,
    run_engine_async,
)
from smart_commissioning_core.engines.safety import (
    ScanNotAuthorized,
    authorization_enforced,
    build_dry_run_plan,
    is_authorized,
    require_scan_authorization,
)
from smart_commissioning_core.records import ValidationIssueRecord


class FakeRunStore:
    """In-memory RunStore capturing every call the run wrapper makes."""

    def __init__(self) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self.last_stage: str | None = None
        self.last_error: str | None = None
        self.last_progress: int | None = None

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        self.status_calls.append(
            {
                "status": status,
                "stage": stage,
                "progress_percent": progress_percent,
                "error_message": error_message,
            }
        )
        self.last_status = status
        self.last_stage = stage
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


def _ctx(
    store: FakeRunStore,
    *,
    parameters: dict[str, Any] | None = None,
    throttle: ThrottleConfig | None = None,
    dry_run: bool = False,
    is_cancelled: Any = None,
) -> EngineContext:
    return EngineContext(
        run_id="run_test",
        parameters=parameters or {},
        run_store=store,
        execution_mode="inline_local_fallback",
        throttle=throttle or ThrottleConfig(),
        dry_run=dry_run,
        _is_cancelled=is_cancelled or (lambda: False),
    )


def _scan_contract(
    *,
    dispatch_seconds: float,
    run_seconds: float,
    job_type: str = "ip_discovery",
) -> dict[str, Any]:
    protocol = "ip" if job_type == "ip_discovery" else "bacnet"
    return {
        "scan_contract_v1": {
            "scan_contract_version": "1.0",
            "job_type": job_type,
            protocol: {
                "policy": {
                    "dispatch_phase_seconds": dispatch_seconds,
                    "run_deadline_seconds": run_seconds,
                }
            },
        }
    }


class ProgressiveObservationSinkTests(unittest.TestCase):
    def test_cancelled_append_drains_the_executor_owned_sink(self) -> None:
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        class BlockingStore(FakeRunStore):
            def append_observation(self, _observation: object) -> object:
                started.set()
                release.wait(timeout=2.0)
                completed.set()
                return {"cursor": 1}

        async def scenario() -> None:
            context = _ctx(BlockingStore())
            task = asyncio.create_task(context.append_observation({"event": 1}))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(
                task.done(),
                "cancellation must wait for the synchronous append to drain",
            )
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(completed.is_set())

        asyncio.run(scenario())


class ThrottleConcurrencyTests(unittest.TestCase):
    def test_throttle_never_exceeds_max_concurrency(self) -> None:
        max_concurrency = 4
        throttle = Throttle(ThrottleConfig(max_concurrency=max_concurrency, rate_limit_per_sec=None))
        state = {"in_flight": 0, "peak": 0}

        async def unit() -> int:
            async with throttle.slot():
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
                await asyncio.sleep(0.01)  # hold the slot so overlap is observable
                state["in_flight"] -= 1
                return 1

        async def main() -> list[int]:
            return list(await asyncio.gather(*[unit() for _ in range(40)]))

        results = asyncio.run(main())
        self.assertEqual(sum(results), 40)
        self.assertLessEqual(state["peak"], max_concurrency, "concurrency bound exceeded")
        self.assertGreater(state["peak"], 1, "test did not actually exercise overlap")

    def test_rate_limiter_spaces_calls(self) -> None:
        # 20/sec => ~50ms minimum spacing. Run 5 units (serial concurrency=1) and
        # assert the total elapsed reflects the enforced spacing.
        throttle = Throttle(ThrottleConfig(max_concurrency=1, rate_limit_per_sec=20.0))
        units = 5

        async def unit() -> None:
            async with throttle.slot():
                return None

        async def main() -> float:
            start = asyncio.get_running_loop().time()
            for _ in range(units):
                await unit()
            return asyncio.get_running_loop().time() - start

        elapsed = asyncio.run(main())
        # (units-1) gaps of ~50ms = ~0.2s minimum. Allow generous slack for CI.
        self.assertGreaterEqual(elapsed, 0.15, f"rate limiter did not space calls (elapsed={elapsed:.3f}s)")

    def test_run_throttled_returns_results_in_order(self) -> None:
        throttle = Throttle(ThrottleConfig(max_concurrency=8, rate_limit_per_sec=None))
        ctx = _ctx(FakeRunStore())

        def factory(value: int):
            async def _coro() -> int:
                await asyncio.sleep(0)
                return value

            return _coro

        async def main() -> list[int]:
            return await throttle.run_throttled([factory(i) for i in range(10)], ctx)

        self.assertEqual(asyncio.run(main()), list(range(10)))

    def test_run_throttled_stops_early_on_cancellation(self) -> None:
        throttle = Throttle(ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None))
        contacted: list[int] = []
        # Cancel after 3 dispatches by flipping the flag the checker reads.
        cancel_state = {"cancel": False}
        ctx = _ctx(FakeRunStore(), is_cancelled=lambda: cancel_state["cancel"])

        def factory(value: int):
            async def _coro() -> int:
                contacted.append(value)
                if len(contacted) >= 3:
                    cancel_state["cancel"] = True
                return value

            return _coro

        async def main() -> list[int]:
            return await throttle.run_throttled([factory(i) for i in range(10)], ctx)

        results = asyncio.run(main())
        # Concurrency=1 means dispatch is serial: after the 3rd unit sets cancel,
        # the loop checks before the 4th dispatch and stops. Partial results only.
        self.assertEqual(results, [0, 1, 2])
        self.assertEqual(contacted, [0, 1, 2], "no further targets should be contacted after cancel")
        self.assertLess(len(contacted), 10, "must stop early, not run all units")

    def test_dispatch_deadline_expiring_during_rate_wait_stops_before_next_unit(self) -> None:
        store = FakeRunStore()
        contacted: list[int] = []
        ctx = _ctx(
            store,
            parameters=_scan_contract(dispatch_seconds=0.01, run_seconds=0.5),
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=20.0),
        )

        def factory(value: int):
            async def _coro() -> int:
                contacted.append(value)
                return value

            return _coro

        async def engine(engine_ctx: EngineContext) -> EngineResult:
            throttle = Throttle(engine_ctx.throttle)
            await throttle.run_throttled([factory(0), factory(1)], engine_ctx)
            return EngineResult()

        result = run_engine(ctx, engine)

        self.assertEqual(contacted, [0])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_progress, 100)
        self.assertEqual(store.last_error, _RUNTIME_DEADLINE_FAILURE_MESSAGE)

    def test_slot_checks_bacnet_dispatch_deadline_after_rate_wait(self) -> None:
        store = FakeRunStore()
        contacted: list[int] = []
        ctx = _ctx(
            store,
            parameters=_scan_contract(
                dispatch_seconds=0.01,
                run_seconds=0.5,
                job_type="bacnet_discovery",
            ),
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=20.0),
        )

        async def engine(engine_ctx: EngineContext) -> EngineResult:
            throttle = Throttle(engine_ctx.throttle)
            async with throttle.slot():
                contacted.append(0)
            async with throttle.slot():
                contacted.append(1)
            return EngineResult()

        result = run_engine(ctx, engine)

        self.assertEqual(contacted, [0])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_error, _RUNTIME_DEADLINE_FAILURE_MESSAGE)

    def test_throttle_config_validates(self) -> None:
        with self.assertRaises(ValueError):
            ThrottleConfig(max_concurrency=0)
        with self.assertRaises(ValueError):
            ThrottleConfig(rate_limit_per_sec=0)
        with self.assertRaises(ValueError):
            ThrottleConfig(connect_timeout_s=0)


class SafetyTests(unittest.TestCase):
    def test_require_scan_authorization_raises_without_auth(self) -> None:
        with self.assertRaises(ScanNotAuthorized):
            require_scan_authorization({})
        with self.assertRaises(ScanNotAuthorized):
            require_scan_authorization({"authorized": False})
        with self.assertRaises(ScanNotAuthorized):
            require_scan_authorization(None)
        # structured form missing authorized_by is NOT authorized
        with self.assertRaises(ScanNotAuthorized):
            require_scan_authorization({"scan_authorization": {"authorized": True}})

    def test_require_scan_authorization_passes_with_boolean_shorthand(self) -> None:
        require_scan_authorization({"authorized": True})  # must not raise
        self.assertTrue(is_authorized({"authorized": True}))

    def test_require_scan_authorization_passes_with_structured_form(self) -> None:
        params = {
            "scan_authorization": {
                "authorized": True,
                "authorized_by": "jane.engineer@acme.example",
            }
        }
        require_scan_authorization(params)  # must not raise
        self.assertTrue(is_authorized(params))

    def test_scan_not_authorized_message_does_not_leak_parameters(self) -> None:
        try:
            require_scan_authorization({"password": "s3cret", "authorized": False})
        except ScanNotAuthorized as error:
            self.assertNotIn("s3cret", str(error))
        else:
            self.fail("expected ScanNotAuthorized")

    def test_authorization_enforced_default_and_env_toggle(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(authorization_enforced(), "absent env -> enforced")
        for truthy in ("1", "true", "on", "yes", "ENFORCE"):
            with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": truthy}):
                self.assertTrue(authorization_enforced())
        for falsy in ("0", "false", "off", "no", "", "  Off  "):
            with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": falsy}):
                self.assertFalse(authorization_enforced())

    def test_frictionless_mode_disables_the_gate(self) -> None:
        # With the gate off, an unauthorized (or empty) parameters set passes and
        # require_scan_authorization does not raise. Evidence is unaffected here.
        with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": "0"}):
            self.assertTrue(is_authorized({}))
            self.assertTrue(is_authorized(None))
            self.assertTrue(is_authorized({"authorized": False}))
            require_scan_authorization({})  # must not raise
            require_scan_authorization(None)  # must not raise

    def test_enforced_mode_still_rejects_unauthorized(self) -> None:
        # Explicit-enforce keeps the original contract intact.
        with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": "1"}):
            self.assertFalse(is_authorized({}))
            with self.assertRaises(ScanNotAuthorized):
                require_scan_authorization({})

    def test_build_dry_run_plan_has_no_side_effects(self) -> None:
        # A fake target records if it was 'contacted'. Building a dry-run plan
        # must enumerate targets WITHOUT calling them.
        contacted: list[str] = []

        class FakeTarget:
            def __init__(self, address: str) -> None:
                self.address = address

            def contact(self) -> None:  # pragma: no cover - must never be called
                contacted.append(self.address)

            def __repr__(self) -> str:
                return self.address

        targets = [FakeTarget("10.0.0.1"), FakeTarget("10.0.0.2")]
        plan = build_dry_run_plan(
            engine="ip_discovery",
            targets=[t.address for t in targets],
            actions=["tcp-connect:47808"],
            notes="floor-3 VLAN only",
        )

        self.assertEqual(contacted, [], "dry-run plan must not contact any target")
        self.assertEqual(plan["engine"], "ip_discovery")
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["target_count"], 2)
        self.assertEqual(plan["targets"], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(plan["actions"], ["tcp-connect:47808"])
        self.assertEqual(plan["notes"], "floor-3 VLAN only")


class RunEngineTests(unittest.TestCase):
    def test_happy_path_writes_assets_issues_records_and_succeeds(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            persisted.append((run_id, list(records)))

        assets = [{"asset_id": "AHU-1", "ip_address": "10.0.0.5", "match_basis": "ip"}]
        records = [{"address": "10.0.0.5", "device_type": "ahu"}]
        issue = ValidationIssueRecord.model_validate(
            {"issue_id": "i1", "issue_type": "x", "severity": "low", "description": "d"}
        )

        def engine(ctx: EngineContext) -> EngineResult:
            return EngineResult(
                discovered_assets=assets,
                structured_records=records,
                issues=[issue],
                result_summary_extra={"scanned": 1},
            )

        result = run_engine(_ctx(store), engine, persist_records=persist)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.last_progress, 100)
        # running set first, then terminal
        self.assertEqual(store.status_calls[0]["status"], "running")
        self.assertEqual(store.last_status, "succeeded")
        # summary carries discovered_assets + execution_mode + dry_run + extras
        summary = store.summary_calls[-1]
        self.assertEqual(summary["discovered_assets"], assets)
        self.assertEqual(summary["execution_mode"], "inline_local_fallback")
        self.assertFalse(summary["dry_run"])
        self.assertEqual(summary["scanned"], 1)
        # issues replaced
        self.assertEqual(store.issues_calls[-1], [issue])
        # structured records persisted
        self.assertEqual(persisted, [("run_test", records)])

    def test_engine_exception_sets_failed_with_sanitized_message(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> EngineResult:
            raise RuntimeError("connection refused for user=admin password=hunter2 at 10.0.0.9")

        result = run_engine(_ctx(store), engine)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_progress, 100)
        self.assertIsNotNone(store.last_error)
        self.assertNotIn("hunter2", store.last_error)
        self.assertNotIn("admin", store.last_error)
        self.assertNotIn("10.0.0.9", store.last_error)

    def test_sets_cancelled_when_is_cancelled_flips(self) -> None:
        store = FakeRunStore()
        cancel_state = {"cancel": False}

        def engine(ctx: EngineContext) -> EngineResult:
            # Simulate observing cancellation mid-run.
            cancel_state["cancel"] = True
            return EngineResult(discovered_assets=[{"asset_id": "partial"}])

        result = run_engine(
            _ctx(store, is_cancelled=lambda: cancel_state["cancel"]),
            engine,
        )

        self.assertEqual(result["status"], "cancelled")
        # partial results are still persisted
        self.assertEqual(store.summary_calls[-1]["discovered_assets"], [{"asset_id": "partial"}])

    def test_status_override_forces_terminal_status(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> EngineResult:
            return EngineResult(status_override="cancelled")

        result = run_engine(_ctx(store), engine)
        self.assertEqual(result["status"], "cancelled")

    def test_dry_run_flag_propagates_to_summary_and_skips_io(self) -> None:
        store = FakeRunStore()
        contacted: list[str] = []

        def engine(ctx: EngineContext) -> EngineResult:
            # An honest active engine: when dry_run, enumerate a plan, no I/O.
            self.assertTrue(ctx.dry_run)
            plan = build_dry_run_plan(engine="ip_discovery", targets=["10.0.0.1"])
            return EngineResult(result_summary_extra={"dry_run_plan": plan})

        result = run_engine(_ctx(store, dry_run=True), engine)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(contacted, [])
        summary = store.summary_calls[-1]
        self.assertTrue(summary["dry_run"])
        self.assertIn("dry_run_plan", summary)
        self.assertEqual(summary["dry_run_plan"]["targets"], ["10.0.0.1"])

    def test_async_engine_is_awaited(self) -> None:
        store = FakeRunStore()

        async def engine(ctx: EngineContext) -> EngineResult:
            await asyncio.sleep(0)
            return EngineResult(discovered_assets=[{"asset_id": "async"}])

        async def main():
            return await run_engine_async(_ctx(store), engine)

        result = asyncio.run(main())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.summary_calls[-1]["discovered_assets"], [{"asset_id": "async"}])

    def test_bacnet_run_deadline_bounds_a_quiet_long_engine(self) -> None:
        store = FakeRunStore()
        ctx = _ctx(
            store,
            parameters=_scan_contract(
                dispatch_seconds=0.01,
                run_seconds=0.03,
                job_type="bacnet_discovery",
            ),
        )

        async def engine(_ctx: EngineContext) -> EngineResult:
            await asyncio.sleep(0.1)
            return EngineResult(discovered_assets=[{"private": "must-not-persist"}])

        async def main() -> tuple[dict[str, Any], float]:
            started = asyncio.get_running_loop().time()
            result = await run_engine_async(ctx, engine)
            return result, asyncio.get_running_loop().time() - started

        result, elapsed = asyncio.run(main())

        self.assertEqual(result["status"], "failed")
        self.assertLess(elapsed, 0.09)
        self.assertEqual(store.summary_calls, [])
        self.assertEqual(store.last_error, _RUNTIME_DEADLINE_FAILURE_MESSAGE)
        self.assertLessEqual(len(store.last_error or ""), 200)
        self.assertNotIn("must-not-persist", store.last_error or "")

    def test_run_deadline_cancels_units_while_dispatch_waits_for_a_slot(self) -> None:
        store = FakeRunStore()
        events: list[str] = []
        ctx = _ctx(
            store,
            parameters=_scan_contract(dispatch_seconds=0.25, run_seconds=0.3),
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
        )

        async def first_unit() -> int:
            events.append("started")
            try:
                await asyncio.sleep(1)
                events.append("completed")
                return 1
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

        async def second_unit() -> int:
            events.append("second-started")
            return 2

        async def engine(engine_ctx: EngineContext) -> EngineResult:
            await Throttle(engine_ctx.throttle).run_throttled(
                [lambda: first_unit(), lambda: second_unit()],
                engine_ctx,
            )
            return EngineResult()

        async def main() -> dict[str, Any]:
            result = await run_engine_async(ctx, engine)
            await asyncio.sleep(0.02)
            return result

        result = asyncio.run(main())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(events, ["started", "cancelled"])

    def test_legacy_context_without_scan_contract_remains_compatible(self) -> None:
        store = FakeRunStore()

        async def engine(_ctx: EngineContext) -> EngineResult:
            await asyncio.sleep(0.04)
            return EngineResult(discovered_assets=[{"asset_id": "legacy"}])

        result = asyncio.run(run_engine_async(_ctx(store, parameters={}), engine))

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            store.summary_calls[-1]["discovered_assets"],
            [{"asset_id": "legacy"}],
        )

    def test_malformed_present_scan_contract_fails_before_engine_runs(self) -> None:
        store = FakeRunStore()
        invoked = False
        parameters = _scan_contract(dispatch_seconds=1, run_seconds=2)
        del parameters["scan_contract_v1"]["ip"]["policy"]["run_deadline_seconds"]

        def engine(_ctx: EngineContext) -> EngineResult:
            nonlocal invoked
            invoked = True
            return EngineResult()

        with self.assertLogs("smart_commissioning_core.engines.base", level="ERROR"):
            result = run_engine(_ctx(store, parameters=parameters), engine)

        self.assertFalse(invoked)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_error, _SANITIZED_FAILURE_MESSAGE)
        self.assertEqual(store.summary_calls, [])

    def test_non_engine_result_return_is_treated_as_failure(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> Any:
            return {"not": "an EngineResult"}

        result = run_engine(_ctx(store), engine)
        self.assertEqual(result["status"], "failed")

    def test_is_cancelled_swallows_checker_errors(self) -> None:
        store = FakeRunStore()

        def boom() -> bool:
            raise RuntimeError("checker broke")

        ctx = _ctx(store, is_cancelled=boom)
        self.assertFalse(ctx.is_cancelled(), "a broken checker must read as not-cancelled")


class _TerminalFailedWriteStore(FakeRunStore):
    """A run store that raises on the terminal 'failed' write only.

    Models a poisoned / pending-rollback SQLAlchemy session after a failed flush:
    the initial 'running' write still succeeds, but the terminal 'failed' write
    the wrapper attempts next raises. The wrapper must swallow that so nothing
    escapes run_engine and the run is never fossilized at 'running'.
    """

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if status == "failed":
            raise RuntimeError("session is in a pending-rollback state")
        return super().update_run_status(
            run_id,
            status=status,
            stage=stage,
            progress_percent=progress_percent,
            error_message=error_message,
        )


class RunEnginePersistenceSafetyTests(unittest.TestCase):
    """A run must reach a TERMINAL status on every path — never stuck 'running'.

    These pin the base-framework half of the live-BACnet fix: a persist failure
    (the raw bacpypes3 value that would not serialize into a JSON column) and the
    BaseException classes that bypass a plain ``except Exception`` both used to
    leave the run fossilized at 'running' with the POST 500ing. No network,
    BACnet, or database here — a scripted engine + in-memory run store only.
    """

    _BASE_LOGGER = "smart_commissioning_core.engines.base"

    def test_persist_failure_yields_failed_with_distinct_message(self) -> None:
        store = FakeRunStore()

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            # A poisoned repository write — e.g. a value that will not serialize.
            raise RuntimeError("could not serialize present_value for user=admin")

        def engine(ctx: EngineContext) -> EngineResult:
            return EngineResult(
                discovered_assets=[{"asset_id": "a"}],
                structured_records=[{"point_id": "p1"}],
                result_summary_extra={"device_count": 1},
            )

        with self.assertLogs(self._BASE_LOGGER, level="ERROR") as logs:
            result = run_engine(_ctx(store), engine, persist_records=persist)

        # Terminal 'failed', never left 'running'.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_status, "failed")
        self.assertEqual(store.last_progress, 100)
        # DISTINCT, saving-focused message — not the generic engine-crash text.
        self.assertEqual(store.last_error, _PERSIST_FAILURE_MESSAGE)
        self.assertNotEqual(store.last_error, _SANITIZED_FAILURE_MESSAGE)
        # The result_summary is written BEFORE the records, so it is not lost.
        self.assertEqual(store.summary_calls[-1]["device_count"], 1)
        # Raw exception text (which could echo credentials) never reaches the run.
        self.assertNotIn("admin", store.last_error or "")
        # The traceback was logged where the message says it will be.
        self.assertTrue(
            any("persist" in line.lower() for line in logs.output),
            "the persist failure traceback must be logged",
        )

    def test_terminal_write_failure_after_persist_failure_does_not_escape(self) -> None:
        store = _TerminalFailedWriteStore()

        def persist(run_id: str, records: list[dict[str, Any]]) -> None:
            raise RuntimeError("persist blew up")

        def engine(ctx: EngineContext) -> EngineResult:
            return EngineResult(structured_records=[{"point_id": "p1"}])

        # Must NOT raise even though BOTH the persist AND the terminal write fail.
        with self.assertLogs(self._BASE_LOGGER, level="ERROR") as logs:
            result = run_engine(_ctx(store), engine, persist_records=persist)

        self.assertIsNone(result, "a failed terminal write returns None, not an exception")
        # The 'running' write still landed; the failing 'failed' write was swallowed.
        self.assertEqual(store.status_calls[0]["status"], "running")
        self.assertNotIn("failed", [call["status"] for call in store.status_calls])
        joined = "\n".join(logs.output).lower()
        self.assertIn("persist", joined, "the persist failure must be logged")
        self.assertIn("terminal", joined, "the swallowed terminal-write failure must be logged")

    def test_cancelled_error_without_cancel_request_sets_failed(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> EngineResult:
            raise asyncio.CancelledError()

        with self.assertLogs(self._BASE_LOGGER, level="WARNING"):
            result = run_engine(_ctx(store), engine)

        # No cancel was requested, so a CancelledError is an honest 'failed'.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_status, "failed")
        self.assertNotIn("running", [call["status"] for call in store.status_calls[1:]])

    def test_cancelled_error_with_cancel_request_sets_cancelled(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> EngineResult:
            raise asyncio.CancelledError()

        result = run_engine(_ctx(store, is_cancelled=lambda: True), engine)

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(store.last_status, "cancelled")
        self.assertIsNone(store.last_error, "a cancelled run carries no error message")

    def test_keyboard_interrupt_records_failed_then_reraises(self) -> None:
        store = FakeRunStore()

        def engine(ctx: EngineContext) -> EngineResult:
            raise KeyboardInterrupt()

        # Best-effort terminal write, then the interrupt must propagate.
        with self.assertRaises(KeyboardInterrupt):
            run_engine(_ctx(store), engine)

        self.assertEqual(store.last_status, "failed")
        self.assertEqual(store.last_progress, 100)


if __name__ == "__main__":
    unittest.main()
