import threading
import unittest
from types import SimpleNamespace

from smart_commissioning_core.owned_run_heartbeat import (
    OwnedRunHeartbeat,
    OwnedRunHeartbeatPolicy,
    abandon_active_owned_run_heartbeats,
)


class _FakeOwnedStore:
    def __init__(self, plan: list[object] | None = None) -> None:
        self.lease = SimpleNamespace(run_id="run-heartbeat-test")
        self._plan = list(plan or [])
        self._lock = threading.Lock()
        self._terminal_outcome = None
        self.ownership_lost = False
        self.calls = 0
        self.called = threading.Event()

    @property
    def terminal_outcome(self):
        return self._terminal_outcome

    def heartbeat(self, *, lease_seconds: int) -> bool:
        del lease_seconds
        with self._lock:
            self.calls += 1
            action = self._plan.pop(0) if self._plan else True
        self.called.set()
        if isinstance(action, BaseException):
            raise action
        return bool(action)

    def mark_ownership_lost(self) -> None:
        self.ownership_lost = True


class OwnedRunHeartbeatPolicyTests(unittest.TestCase):
    def test_secure_defaults_and_release_acceptance_values(self) -> None:
        self.assertEqual(OwnedRunHeartbeatPolicy(), OwnedRunHeartbeatPolicy(60, 15))
        self.assertEqual(OwnedRunHeartbeatPolicy(15, 2).interval_seconds, 2.0)

    def test_rejects_unsafe_production_values(self) -> None:
        cases = (
            ((14, 2), "at least 15"),
            ((301, 15), "no more than 300"),
            ((60, 0.5), "at least 1"),
            ((15, 6), "one third"),
            ((60, float("nan")), "finite"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, message):
                    OwnedRunHeartbeatPolicy(*arguments)


class OwnedRunHeartbeatGuardTests(unittest.TestCase):
    def tearDown(self) -> None:
        abandon_active_owned_run_heartbeats()

    def test_transient_exception_is_redacted_and_retried(self) -> None:
        canary = "postgresql://user:password@example.invalid/database"
        store = _FakeOwnedStore([RuntimeError(canary), True])
        heartbeat = OwnedRunHeartbeat(
            store,
            lease_seconds=1,
            interval_seconds=0.01,
        )
        with self.assertLogs(
            "smart_commissioning_core.owned_run_heartbeat", level="INFO"
        ) as captured:
            heartbeat.start()
            deadline = threading.Event()
            for _ in range(300):
                if store.calls >= 2:
                    break
                deadline.wait(0.01)
            heartbeat.stop_and_join()

        self.assertGreaterEqual(store.calls, 2)
        logs = "\n".join(captured.output)
        self.assertNotIn(canary, logs)
        self.assertIn("retrying", logs)
        self.assertIn("recovered", logs)
        self.assertFalse(heartbeat.is_alive)

    def test_false_heartbeat_fences_stale_owner(self) -> None:
        store = _FakeOwnedStore([False])
        heartbeat = OwnedRunHeartbeat(
            store,
            lease_seconds=1,
            interval_seconds=0.01,
        )
        heartbeat.start()
        self.assertTrue(store.called.wait(2.0))
        heartbeat.stop_and_join()

        self.assertTrue(store.ownership_lost)
        self.assertTrue(heartbeat.ownership_lost)
        self.assertFalse(heartbeat.is_alive)

    def test_process_shutdown_abandons_and_joins_active_guard(self) -> None:
        store = _FakeOwnedStore()
        heartbeat = OwnedRunHeartbeat(
            store,
            lease_seconds=1,
            interval_seconds=0.1,
        )
        heartbeat.start()

        self.assertEqual(abandon_active_owned_run_heartbeats(), 1)
        self.assertTrue(store.ownership_lost)
        self.assertTrue(heartbeat.ownership_lost)
        self.assertFalse(heartbeat.is_alive)
        self.assertEqual(abandon_active_owned_run_heartbeats(), 0)


if __name__ == "__main__":
    unittest.main()
