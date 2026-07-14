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

        self.assertEqual(len(store.status_updates), 1)
        update = store.status_updates[0]
        self.assertEqual(update["run_id"], "run-udmi-1")
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["stage"], "udmi_fixture_validation_failed")
        self.assertEqual(update["progress_percent"], 100)
        self.assertEqual(update["error_message"], "live capture exceeded the worker time limit")

    def test_shutdown_interrupt_gets_honest_generic_message(self) -> None:
        store = self._run(Shutdown("worker shutdown"))

        self.assertEqual(len(store.status_updates), 1)
        update = store.status_updates[0]
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["error_message"], "run interrupted by the worker (Shutdown)")

    def test_ordinary_exception_is_left_to_the_processor(self) -> None:
        # The processor's own `except Exception` path records those failures;
        # the actor guard must not double-write a status for them.
        store = self._run(ValueError("boom"))

        self.assertEqual(store.status_updates, [])


class MqttDiscoveryInterruptTests(unittest.TestCase):
    def test_time_limit_interrupt_records_failed_and_reraises(self) -> None:
        store = FakeRunStore()
        with mock.patch.object(tasks, "run_store", store), mock.patch.object(
            tasks, "process_mqtt_discovery_run", side_effect=TimeLimitExceeded("time limit exceeded")
        ):
            with self.assertRaises(TimeLimitExceeded):
                tasks.discover_mqtt("run-mqtt-1", {})

        self.assertEqual(len(store.status_updates), 1)
        update = store.status_updates[0]
        self.assertEqual(update["run_id"], "run-mqtt-1")
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["stage"], "engine_failed")
        self.assertEqual(update["error_message"], "live capture exceeded the worker time limit")


if __name__ == "__main__":
    unittest.main()
