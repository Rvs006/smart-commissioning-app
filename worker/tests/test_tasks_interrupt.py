"""Interrupt guard on the long-capture actors (stranded-run fix).

Dramatiq's time-limit kill raises ``TimeLimitExceeded`` — an ``Interrupt`` /
``BaseException`` — inside the actor thread, so the engine/processor
``except Exception`` failure paths never see it and the run row would stay
'running' forever while the frontend polls. These tests drive the actors
against a FAKE run store and a processor stub that raises the interrupt,
asserting the actor records an honest terminal 'failed' AND re-raises so
dramatiq's own message accounting stays correct. No Redis, no broker, no real
database writes.

Run explicitly (the worker has no packaged test suite):
    python -m unittest discover -s worker/tests  (with worker on sys.path)
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

# Make the worker package importable when run from the repo root.
_WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))

from app import tasks  # noqa: E402
from dramatiq.middleware import Shutdown, TimeLimitExceeded  # noqa: E402


class FakeRunStore:
    """Records update_run_status calls; no database behind it."""

    def __init__(self) -> None:
        self.status_updates: list[dict] = []

    def update_run_status(self, run_id, *, status, stage=None, progress_percent=None, error_message=None):
        update = {
            "run_id": run_id,
            "status": status,
            "stage": stage,
            "progress_percent": progress_percent,
            "error_message": error_message,
        }
        self.status_updates.append(update)
        return update


class UdmiInterruptTests(unittest.TestCase):
    def _run(self, side_effect) -> FakeRunStore:
        store = FakeRunStore()
        with mock.patch.object(tasks, "run_store", store), mock.patch.object(
            tasks, "process_udmi_validation_run", side_effect=side_effect
        ):
            with self.assertRaises(type(side_effect)):
                tasks.validate_udmi_payloads("run-udmi-1", {})
        return store

    def test_time_limit_interrupt_records_failed_and_reraises(self) -> None:
        store = self._run(TimeLimitExceeded("time limit exceeded"))

        update = next(item for item in store.status_updates if item["status"] == "failed")
        self.assertEqual(update["run_id"], "run-udmi-1")
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["stage"], "udmi_fixture_validation_failed")
        self.assertEqual(update["progress_percent"], 100)
        self.assertEqual(update["error_message"], "run exceeded the worker time limit")

    def test_shutdown_interrupt_gets_honest_generic_message(self) -> None:
        store = self._run(Shutdown("worker shutdown"))

        update = next(item for item in store.status_updates if item["status"] == "failed")
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["error_message"], "run interrupted by the worker (Shutdown)")

    def test_ordinary_exception_is_left_to_the_processor(self) -> None:
        # The processor's own `except Exception` path records those failures;
        # the actor guard must not double-write a status for them.
        store = self._run(ValueError("boom"))

        self.assertFalse(any(item["status"] == "failed" for item in store.status_updates))


class MqttDiscoveryInterruptTests(unittest.TestCase):
    def test_time_limit_interrupt_records_failed_and_reraises(self) -> None:
        store = FakeRunStore()
        with mock.patch.object(tasks, "run_store", store), mock.patch.object(
            tasks, "process_mqtt_discovery_run", side_effect=TimeLimitExceeded("time limit exceeded")
        ):
            with self.assertRaises(TimeLimitExceeded):
                tasks.discover_mqtt("run-mqtt-1", {})

        update = next(item for item in store.status_updates if item["status"] == "failed")
        self.assertEqual(update["run_id"], "run-mqtt-1")
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["stage"], "engine_failed")
        self.assertEqual(update["error_message"], "run exceeded the worker time limit")


class ShortDiscoveryInterruptTests(unittest.TestCase):
    def _assert_interrupt_is_terminal(self, actor, processor_name: str, run_id: str) -> None:
        store = FakeRunStore()
        with mock.patch.object(tasks, "run_store", store), mock.patch.object(
            tasks, processor_name, side_effect=TimeLimitExceeded("time limit exceeded")
        ):
            with self.assertRaises(TimeLimitExceeded):
                actor(run_id, {})

        update = next(item for item in store.status_updates if item["status"] == "failed")
        self.assertEqual(update["run_id"], run_id)
        self.assertEqual(update["stage"], "engine_failed")
        self.assertEqual(update["error_message"], "run exceeded the worker time limit")

    def test_ip_interrupt_records_failed_and_reraises(self) -> None:
        self._assert_interrupt_is_terminal(
            tasks.discover_ip_range, "process_ip_discovery_run", "run-ip-1"
        )

    def test_bacnet_interrupt_records_failed_and_reraises(self) -> None:
        self._assert_interrupt_is_terminal(
            tasks.discover_bacnet, "process_bacnet_discovery_run", "run-bacnet-1"
        )


class WorkerHeartbeatTests(unittest.TestCase):
    def test_live_actor_refreshes_its_heartbeat(self) -> None:
        refreshed = threading.Event()

        class HeartbeatStore(FakeRunStore):
            def update_run_status(self, run_id, **kwargs):
                update = super().update_run_status(run_id, **kwargs)
                running_updates = [
                    item for item in self.status_updates if item["status"] == "running"
                ]
                if len(running_updates) >= 2:
                    refreshed.set()
                return update

        store = HeartbeatStore()

        @tasks._with_worker_heartbeat
        def live_actor(_run_id: str, _parameters: dict) -> None:
            self.assertTrue(refreshed.wait(timeout=0.5), "periodic heartbeat did not arrive")

        with (
            mock.patch.object(tasks, "run_store", store),
            mock.patch.object(tasks, "_WORKER_HEARTBEAT_SECONDS", 0.01),
        ):
            live_actor("run-live", {})

        self.assertGreaterEqual(
            len([item for item in store.status_updates if item["status"] == "running"]),
            2,
        )

    def test_terminal_run_is_not_executed_when_a_late_message_arrives(self) -> None:
        called: list[str] = []

        class TerminalStore:
            def update_run_status(self, run_id, **_kwargs):
                return {"run_id": run_id, "status": "failed"}

        @tasks._with_worker_heartbeat
        def late_actor(run_id: str, _parameters: dict) -> None:
            called.append(run_id)

        with mock.patch.object(tasks, "run_store", TerminalStore()):
            result = late_actor("run-expired", {})

        self.assertIsNone(result)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
