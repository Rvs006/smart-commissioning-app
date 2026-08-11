import tempfile
import unittest
from pathlib import Path

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.repositories import (
    DiscoveryRepository,
    SealedDiscoveryProjectionError,
)
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1


class DiscoveryProjectionImmutabilityTests(unittest.TestCase):
    def test_compatibility_replace_cannot_mutate_a_sealed_projection(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        engine = create_engine_from_url(default_sqlite_url(Path(temp_dir.name)))
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        lifecycle = RunLifecycleRepository(engine)
        context = RunContextV1(
            project_id="project-sealed",
            site_id="site-sealed",
            configuration_snapshot={},
            configuration_version=1,
            engine_parameters={"authorized": True},
            requesting_principal="projection-test",
            application_version="0.1.41",
        )
        envelope = lifecycle.create_run_with_context(
            job_type="mqtt_discovery",
            context=context,
            execution_mode="dramatiq_worker",
        )
        lease = lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token="projection-owner",
        )
        self.assertIsNotNone(lease)
        terminal = TerminalResultV1(
            status="succeeded",
            stage="engine_complete",
            devices=({"address": "192.0.2.10", "attributes": {"open": True}},),
        )
        outcome = lifecycle.finalize_run(
            envelope.run_id,
            lease.owner_token,
            terminal,
        )
        self.assertTrue(outcome.applied)
        repository = DiscoveryRepository(engine)

        with self.assertRaises(SealedDiscoveryProjectionError):
            repository.replace_devices(
                envelope.run_id,
                [{"address": "192.0.2.99", "attributes": {"tampered": True}}],
            )

        self.assertEqual(repository.list_devices(envelope.run_id)[0]["address"], "192.0.2.10")
        self.assertEqual(lifecycle.conflict_count(envelope.run_id), 1)


if __name__ == "__main__":
    unittest.main()
