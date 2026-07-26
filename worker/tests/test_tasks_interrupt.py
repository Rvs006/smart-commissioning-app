"""Worker delivery, duplicate, cancellation, and interrupt reliability tests."""

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKER_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPOSITORY_ROOT / "core", _WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import tasks  # noqa: E402
from dramatiq.middleware import Shutdown, TimeLimitExceeded  # noqa: E402
from smart_commissioning_core.db.base import Base  # noqa: E402
from smart_commissioning_core.db.engine import (  # noqa: E402
    create_engine_from_url,
    default_sqlite_url,
)
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository  # noqa: E402
from smart_commissioning_core.run_context import RunContextV1  # noqa: E402


def _context(**overrides: object) -> RunContextV1:
    values: dict[str, object] = {
        "project_id": "project-field-9",
        "site_id": "site-roof-2",
        "configuration_snapshot": {"mqtt": {"values": {"Port": 8883}}},
        "configuration_version": 4,
        "registers": [],
        "imports": [],
        "schema_versions": {"udmi": "1.5.2"},
        "engine_parameters": {"authorized": True, "dry_run": True},
        "network_interface": "192.0.2.10/24",
        "connection_settings": {"broker_host": "broker.example", "broker_port": 8883},
        "secret_references": {},
        "requesting_principal": "engineer-7",
        "application_version": "0.1.26",
    }
    values.update(overrides)
    return RunContextV1.model_validate(values)


class WorkerDeliveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temp_dir.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.repository = RunLifecycleRepository(self.engine)
        self.repository_patcher = mock.patch.object(
            tasks, "lifecycle_repository", self.repository
        )
        self.repository_patcher.start()
        self.addCleanup(self.repository_patcher.stop)

    def create_run(self, context: RunContextV1 | None = None) -> tuple[str, str]:
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=context or _context(),
            execution_mode="dramatiq_worker",
        )
        return envelope.run_id, envelope.dispatch_id


class DuplicateDeliveryTests(WorkerDeliveryTestCase):
    def test_one_hundred_duplicate_messages_invoke_engine_once(self) -> None:
        run_id, dispatch_id = self.create_run()
        calls = 0
        lock = threading.Lock()

        def processor(run_id, parameters, *, run_store, persist_records, **_kwargs):
            nonlocal calls
            with lock:
                calls += 1
            self.assertEqual(parameters["project_id"], "project-field-9")
            self.assertEqual(parameters["site_id"], "site-roof-2")
            persist_records(run_id, [{"address": "192.0.2.20"}])
            run_store.update_result_summary(run_id, {"device_count": 1}, merge=False)
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )

        with mock.patch.object(tasks, "process_ip_discovery_run", side_effect=processor):
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda _: tasks.discover_ip_range(run_id, dispatch_id), range(100)))

        self.assertEqual(calls, 1)
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "succeeded")

    def test_wrong_dispatch_exits_before_processor(self) -> None:
        run_id, _ = self.create_run()
        processor = mock.Mock()

        with mock.patch.object(tasks, "process_ip_discovery_run", processor):
            tasks.discover_ip_range(run_id, "dispatch-stale")

        processor.assert_not_called()

    def test_queued_cancellation_exits_before_processor(self) -> None:
        run_id, dispatch_id = self.create_run()
        self.repository.request_cancel(run_id)
        processor = mock.Mock()

        with mock.patch.object(tasks, "process_ip_discovery_run", processor):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "cancelled")


class FrozenContextAndSecretTests(WorkerDeliveryTestCase):
    def test_actor_receives_parameters_only_from_non_default_stored_context(self) -> None:
        context = _context(engine_parameters={"authorized": True, "dry_run": True, "marker": "stored"})
        run_id, dispatch_id = self.create_run(context)
        captured: dict[str, object] = {}

        def processor(run_id, parameters, *, run_store, **_kwargs):
            captured.update(parameters)
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )

        with mock.patch.object(tasks, "process_ip_discovery_run", side_effect=processor):
            tasks.discover_ip_range(run_id, dispatch_id)

        self.assertEqual(captured["marker"], "stored")
        self.assertEqual(captured["project_id"], "project-field-9")
        self.assertEqual(captured["site_id"], "site-roof-2")

    def test_missing_secret_causes_zero_processor_calls(self) -> None:
        context = _context(
            connection_settings={
                "broker_host": "broker.example",
                "broker_port": 8883,
                "password": "secret://mqtt-password-v4",
            },
            secret_references={
                "mqtt_password": {
                    "reference": "secret://mqtt-password-v4",
                    "version": "4",
                }
            },
        )
        run_id, dispatch_id = self.create_run(context)
        processor = mock.Mock()

        with (
            mock.patch.object(tasks, "resolve_worker_secret", return_value=None),
            mock.patch.object(tasks, "process_ip_discovery_run", processor),
            self.assertRaisesRegex(RuntimeError, "secret material"),
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")


class InterruptTests(WorkerDeliveryTestCase):
    def _assert_interrupt(self, interrupt: BaseException, expected_message: str) -> None:
        run_id, dispatch_id = self.create_run()
        with mock.patch.object(
            tasks, "process_ip_discovery_run", side_effect=interrupt
        ):
            with self.assertRaises(type(interrupt)):
                tasks.discover_ip_range(run_id, dispatch_id)

        seal = self.repository.get_seal(run_id)
        self.assertEqual(seal.terminal_status, "failed")
        with self.repository._session_factory() as session:
            from smart_commissioning_core.db.models import Run

            run = session.get(Run, run_id)
            self.assertEqual(run.error_message, expected_message)

    def test_time_limit_interrupt_is_sealed_and_reraised(self) -> None:
        self._assert_interrupt(
            TimeLimitExceeded("time limit exceeded"),
            "run exceeded the worker time limit",
        )

    def test_shutdown_interrupt_is_sealed_and_reraised(self) -> None:
        self._assert_interrupt(
            Shutdown("worker shutdown"),
            "run interrupted by the worker (Shutdown)",
        )


if __name__ == "__main__":
    unittest.main()
