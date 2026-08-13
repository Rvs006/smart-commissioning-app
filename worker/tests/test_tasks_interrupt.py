"""Worker delivery, duplicate, cancellation, and interrupt reliability tests."""

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from smart_commissioning_core.owned_run_heartbeat import (  # noqa: E402
    OWNED_CONTEXT_FAILURE_MESSAGE,
    OWNED_CONTEXT_FAILURE_STAGE,
)
from smart_commissioning_core.owned_run_store import OwnedRunStore  # noqa: E402
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

    def create_run(
        self,
        context: RunContextV1 | None = None,
        *,
        job_type: str = "ip_discovery",
    ) -> tuple[str, str]:
        envelope = self.repository.create_run_with_context(
            job_type=job_type,
            context=context or _context(),
            execution_mode="dramatiq_worker",
        )
        return envelope.run_id, envelope.dispatch_id


class DuplicateDeliveryTests(WorkerDeliveryTestCase):
    def test_duplicate_delivery_log_names_the_run(self) -> None:
        run_id, dispatch_id = self.create_run()
        with (
            mock.patch.object(self.repository, "claim_run", return_value=None),
            self.assertLogs(tasks.logger.name, level="INFO") as captured,
        ):
            tasks.discover_ip_range(run_id, dispatch_id)
        self.assertIn(
            f"Skipping duplicate or stale delivery run_id={run_id}",
            "\n".join(captured.output),
        )

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
        with self.repository._session_factory() as session:
            from smart_commissioning_core.db.models import RunResult, RunSeal
            from sqlalchemy import func, select

            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(RunResult)
                    .where(RunResult.run_id == run_id)
                ),
                1,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(RunSeal)
                    .where(RunSeal.run_id == run_id)
                ),
                1,
            )

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
    def test_ip_actor_passes_the_context_bound_import_loader(self) -> None:
        run_id, dispatch_id = self.create_run()
        captured: dict[str, object] = {}

        def processor(
            run_id,
            _parameters,
            *,
            run_store,
            import_loader,
            **_kwargs,
        ):
            captured["import_loader"] = import_loader
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )

        with mock.patch.object(
            tasks,
            "process_ip_discovery_run",
            side_effect=processor,
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        self.assertTrue(callable(captured["import_loader"]))

    def test_live_ip_actor_rechecks_frozen_source_interface_before_processor(self) -> None:
        worker_scope = "worker-field-9"
        worker_settings = replace(tasks.settings, network_executor_id=worker_scope)
        parameters = {
            "authorized": True,
            "dry_run": False,
            "source_ip": "192.0.2.10",
            "local_address": "192.0.2.10/24",
            "source_interface_identity_v1": {
                "schema_version": "1.0",
                "selection": "explicit",
                "executor_scope": worker_scope,
                "interface_id": "if-field-9",
                "interface_name": "Field NIC",
                "source_ip": "192.0.2.10",
                "prefix_length": 24,
                "local_address": "192.0.2.10/24",
                "default_route_metric": None,
            },
        }
        run_id, dispatch_id = self.create_run(_context(engine_parameters=parameters))
        order: list[str] = []

        def processor(run_id, _parameters, *, run_store, **_kwargs):
            order.append("processor")
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )

        def guard(_parameters, *, expected_executor_scope):
            self.assertEqual(expected_executor_scope, worker_scope)
            order.append("guard")

        with (
            mock.patch.object(tasks, "settings", worker_settings),
            mock.patch.object(tasks, "guard_frozen_source_interface", side_effect=guard),
            mock.patch.object(tasks, "process_ip_discovery_run", side_effect=processor),
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        self.assertEqual(order, ["guard", "processor"])

    def test_source_interface_drift_fails_before_worker_ip_processor(self) -> None:
        worker_scope = "worker-field-9"
        worker_settings = replace(tasks.settings, network_executor_id=worker_scope)
        parameters = {
            "authorized": True,
            "dry_run": False,
            "source_ip": "192.0.2.10",
            "local_address": "192.0.2.10/24",
            "source_interface_identity_v1": {
                "schema_version": "1.0",
                "selection": "explicit",
                "executor_scope": worker_scope,
                "interface_id": "if-field-9",
                "interface_name": "Field NIC",
                "source_ip": "192.0.2.10",
                "prefix_length": 24,
                "local_address": "192.0.2.10/24",
                "default_route_metric": None,
            },
        }
        run_id, dispatch_id = self.create_run(_context(engine_parameters=parameters))
        processor = mock.Mock()

        with (
            mock.patch.object(tasks, "settings", worker_settings),
            mock.patch.object(
                tasks,
                "guard_frozen_source_interface",
                side_effect=ValueError("frozen source interface is unavailable"),
            ),
            mock.patch.object(tasks, "process_ip_discovery_run", processor),
        ):
            with self.assertRaisesRegex(ValueError, "source interface is unavailable"):
                tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")

    def test_live_network_actor_without_executor_scope_fails_before_processor(self) -> None:
        parameters = {
            "authorized": True,
            "dry_run": False,
            "source_ip": "192.0.2.10",
            "local_address": "192.0.2.10/24",
            "source_interface_identity_v1": {
                "schema_version": "1.0",
                "selection": "explicit",
                "executor_scope": "worker-field-9",
                "interface_id": "if-field-9",
                "interface_name": "Field NIC",
                "source_ip": "192.0.2.10",
                "prefix_length": 24,
                "local_address": "192.0.2.10/24",
                "default_route_metric": None,
            },
        }
        run_id, dispatch_id = self.create_run(_context(engine_parameters=parameters))
        processor = mock.Mock()

        with (
            mock.patch.object(
                tasks,
                "settings",
                replace(tasks.settings, network_executor_id=None),
            ),
            mock.patch.object(tasks, "process_ip_discovery_run", processor),
            self.assertRaisesRegex(ValueError, "queued network executor does not match"),
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")

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
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")

    def test_parameter_resolution_failure_is_sealed_as_context_failure(self) -> None:
        run_id, dispatch_id = self.create_run()
        processor = mock.Mock()

        with (
            mock.patch.object(
                tasks,
                "resolve_context_parameters",
                side_effect=ValueError("malformed frozen parameter"),
            ),
            mock.patch.object(tasks, "process_ip_discovery_run", processor),
        ):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        seal = self.repository.get_seal(run_id)
        self.assertEqual(seal.terminal_status, "failed")
        with self.repository._session_factory() as session:
            from smart_commissioning_core.db.models import Run

            run = session.get(Run, run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.stage, OWNED_CONTEXT_FAILURE_STAGE)
            self.assertEqual(run.error_message, OWNED_CONTEXT_FAILURE_MESSAGE)

    def test_tampered_context_json_is_rejected_before_processor(self) -> None:
        from smart_commissioning_core.db.models import RunExecutionContext
        from sqlalchemy import update

        run_id, dispatch_id = self.create_run()
        with self.engine.begin() as connection:
            stored = connection.execute(
                RunExecutionContext.__table__.select().where(
                    RunExecutionContext.run_id == run_id
                )
            ).mappings().one()
            tampered = dict(stored["context_json"])
            tampered["engine_parameters"] = {"authorized": False, "dry_run": True}
            connection.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == run_id)
                .values(context_json=tampered)
            )
        processor = mock.Mock()

        with mock.patch.object(tasks, "process_ip_discovery_run", processor):
            tasks.discover_ip_range(run_id, dispatch_id)

        processor.assert_not_called()
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "failed")

    def test_mqtt_config_uses_canonical_context_channel(self) -> None:
        run_id, dispatch_id = self.create_run(
            _context(
                engine_parameters={
                    "topic": "sample/device/config",
                    "payload": "{}",
                    "confirmed": True,
                },
                connection_settings={},
                protocol_key=None,
            ),
            job_type="mqtt_config_publish",
        )
        channels: list[str] = []
        deployment_ids: list[str] = []

        def resolve(context, lease, *, deployment_id, channel, secret_resolver):
            del context, lease, secret_resolver
            channels.append(channel)
            deployment_ids.append(deployment_id)
            return {"topic": "sample/device/config", "payload": "{}", "confirmed": True}

        def processor(run_id, _parameters, *, run_store, **_kwargs):
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="mqtt_config_publish_complete",
                progress_percent=100,
            )

        with (
            mock.patch.object(tasks, "resolve_context_parameters", side_effect=resolve),
            mock.patch.object(
                tasks,
                "process_mqtt_config_publish_run",
                side_effect=processor,
            ),
        ):
            tasks.publish_mqtt_config(run_id, dispatch_id)

        self.assertEqual(channels, ["mqtt_config_publish"])
        self.assertEqual(deployment_ids, ["smart-commissioning-local"])
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "succeeded")


class SharedWorkerHeartbeatTests(WorkerDeliveryTestCase):
    def test_heartbeat_starts_before_context_load_and_retries_db_error(self) -> None:
        run_id, dispatch_id = self.create_run()
        context_loading = threading.Event()
        release_context = threading.Event()
        multiple_heartbeats = threading.Event()
        processor_called = threading.Event()
        actor_errors: list[BaseException] = []
        calls = 0
        calls_lock = threading.Lock()
        original_get_context = self.repository.get_context
        original_heartbeat = OwnedRunStore.heartbeat

        def blocked_context(value: str):
            context_loading.set()
            if not release_context.wait(3.0):
                raise TimeoutError("context load was not released")
            return original_get_context(value)

        def transient_heartbeat(store: OwnedRunStore, *, lease_seconds: int) -> bool:
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                raise RuntimeError("credential-canary-must-not-reach-logs")
            renewed = original_heartbeat(store, lease_seconds=lease_seconds)
            if current >= 2:
                multiple_heartbeats.set()
            return renewed

        def processor(run_id, _parameters, *, run_store, **_kwargs):
            processor_called.set()
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="engine_complete",
                progress_percent=100,
            )

        def run_actor() -> None:
            try:
                tasks.discover_ip_range(run_id, dispatch_id)
            except BaseException as error:  # pragma: no cover - surfaced below
                actor_errors.append(error)

        with (
            mock.patch.object(self.repository, "get_context", side_effect=blocked_context),
            mock.patch.object(OwnedRunStore, "heartbeat", transient_heartbeat),
            mock.patch.object(tasks, "_WORKER_LEASE_SECONDS", 1),
            mock.patch.object(tasks, "_WORKER_HEARTBEAT_SECONDS", 0.02),
            mock.patch.object(tasks, "process_ip_discovery_run", side_effect=processor),
            self.assertLogs(
                "smart_commissioning_core.owned_run_heartbeat", level="INFO"
            ) as captured,
        ):
            actor = threading.Thread(target=run_actor, name="worker-context-test")
            actor.start()
            self.assertTrue(context_loading.wait(3.0))
            self.assertTrue(multiple_heartbeats.wait(3.0))
            self.assertFalse(processor_called.is_set())
            release_context.set()
            actor.join(3.0)

        self.assertFalse(actor.is_alive())
        if actor_errors:
            raise actor_errors[0]
        self.assertTrue(processor_called.is_set())
        self.assertEqual(self.repository.get_seal(run_id).terminal_status, "succeeded")
        logs = "\n".join(captured.output)
        self.assertNotIn("credential-canary-must-not-reach-logs", logs)
        self.assertIn("retrying", logs)
        self.assertIn("recovered", logs)
        self.assertFalse(
            any(
                thread.name == f"run-heartbeat-{run_id}"
                for thread in threading.enumerate()
            )
        )

    def test_confirmed_worker_ownership_loss_fences_terminal_write(self) -> None:
        from datetime import timedelta

        from smart_commissioning_core.db.models import Run

        run_id, dispatch_id = self.create_run()
        processor_started = threading.Event()
        release_processor = threading.Event()
        actor_errors: list[BaseException] = []

        def processor(run_id, _parameters, *, run_store, **_kwargs):
            processor_started.set()
            if not release_processor.wait(3.0):
                raise TimeoutError("processor was not released")
            return run_store.update_run_status(
                run_id,
                status="succeeded",
                stage="late_success",
                progress_percent=100,
            )

        def run_actor() -> None:
            try:
                tasks.discover_ip_range(run_id, dispatch_id)
            except BaseException as error:  # pragma: no cover - surfaced below
                actor_errors.append(error)

        with (
            mock.patch.object(OwnedRunStore, "heartbeat", return_value=False),
            mock.patch.object(tasks, "_WORKER_LEASE_SECONDS", 1),
            mock.patch.object(tasks, "_WORKER_HEARTBEAT_SECONDS", 0.02),
            mock.patch.object(tasks, "process_ip_discovery_run", side_effect=processor),
        ):
            actor = threading.Thread(target=run_actor, name="worker-owner-loss-test")
            actor.start()
            self.assertTrue(processor_started.wait(3.0))
            with self.repository._session_factory() as session:
                row = session.get(Run, run_id)
                recovery_at = row.lease_expires_at + timedelta(milliseconds=1)
            self.assertEqual(
                self.repository.recover_expired_leases(now=recovery_at),
                [run_id],
            )
            release_processor.set()
            actor.join(3.0)

        self.assertFalse(actor.is_alive())
        if actor_errors:
            raise actor_errors[0]
        seal = self.repository.get_seal(run_id)
        self.assertEqual(seal.terminal_status, "failed")


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
