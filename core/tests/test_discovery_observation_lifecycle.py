import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    ActiveProtocolSlot,
    DiscoveredDevice,
    Run,
    RunDiscoveryObservation,
    RunDiscoveryObservationState,
    RunExecutionContext,
    RunIssue,
    RunResult,
    RunSeal,
)
from smart_commissioning_core.db.run_lifecycle import (
    DiscoveryObservationConflictError,
    DiscoveryObservationFoldLimitError,
    RunLifecycleRepository,
)
from smart_commissioning_core.discovery_observations import (
    OBSERVATION_STREAM_EMPTY_SHA256,
    DiscoveryObservationInputV1,
    extend_observation_stream_sha256,
    observation_payload,
)
from smart_commissioning_core.engines import ip_scan
from smart_commissioning_core.engines.ip import ProviderIdentityEvidenceV1
from smart_commissioning_core.owned_run_heartbeat import OwnedRunHeartbeat
from smart_commissioning_core.owned_run_store import OwnedRunStore, RunFinalizingError
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import (
    RunContextV1,
    canonical_context_sha256,
    canonical_json_bytes,
)
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import select, update


def _context(
    protocol: str,
    *,
    observation_rows: int | None = None,
    observation_payload_bytes: int | None = None,
) -> RunContextV1:
    engine_parameters: dict = {"authorized": True, "dry_run": True}
    if observation_rows is not None and observation_payload_bytes is not None:
        engine_parameters["scan_contract_v1"] = {
            "job_type": "ip_discovery",
            "ip": {
                "observation_budget": {
                    "planned_observation_rows": observation_rows,
                    "planned_observation_payload_bytes": observation_payload_bytes,
                }
            },
        }
    return RunContextV1.model_validate(
        {
            "project_id": "project-north",
            "site_id": "site-17",
            "configuration_snapshot": {},
            "configuration_version": 7,
            "registers": [],
            "imports": [],
            "schema_versions": {},
            "engine_parameters": engine_parameters,
            "network_interface": "192.0.2.10/24",
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "user-42",
            "application_version": "0.1.41",
            "protocol_key": f"{protocol}:test-source",
        }
    )


def _observation(
    *,
    protocol: str = "ip",
    event_key: str = "host:192.0.2.20:v1",
    entity_version: int = 1,
    payload: dict | None = None,
) -> DiscoveryObservationInputV1:
    return DiscoveryObservationInputV1(
        protocol=protocol,
        entity_kind="host" if protocol == "ip" else "device",
        entity_key="192.0.2.20" if protocol == "ip" else "device:4001",
        entity_version=entity_version,
        event_key=event_key,
        phase="reachability",
        outcome="observed",
        payload_schema_version="1.0",
        payload=payload or {"reachable": False, "response_ms": 0},
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
    )


class DiscoveryObservationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temporary.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.clock_now = datetime(2026, 8, 11, 9, 0, 10, tzinfo=UTC)
        self.repository = RunLifecycleRepository(
            self.engine,
            clock=lambda: self.clock_now,
        )

    def create_claimed_run(
        self,
        protocol: str = "ip",
        *,
        claimed_at: datetime | None = None,
        observation_rows: int | None = None,
        observation_payload_bytes: int | None = None,
    ):
        envelope = self.repository.create_run_with_context(
            job_type=f"{protocol}_discovery",
            context=_context(
                protocol,
                observation_rows=observation_rows,
                observation_payload_bytes=observation_payload_bytes,
            ),
            execution_mode="inline",
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            lease_seconds=60,
            now=claimed_at or datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
            owner_token="owner-a",
        )
        self.assertIsNotNone(lease)
        return envelope.run_id, lease

    def test_real_sealed_ip_run_keeps_max_provider_identity_projections_within_preview(self) -> None:
        """The runtime identity seam cannot exceed the immutable preview budget."""
        from app.services.discovery_contract_service import (
            resolve_ip_discovery_parameters,
        )

        source_identity = {
            "schema_version": "1.0",
            "selection": "explicit",
            "executor_scope": "test-network-executor",
            "interface_id": "test-if:loopback",
            "interface_name": "Loopback",
            "source_ip": "127.0.0.1",
            "prefix_length": 8,
            "local_address": "127.0.0.1/8",
            "default_route_metric": None,
        }
        targets = ("192.0.2.90",)
        rows = [
            {
                "Expected IP address": f"192.0.2.{10 + index}",
                "Asset ID": f"AHU-{index}",
            }
            for index in range(1)
        ]
        repository_rows = {
            "import_id": "identity-budget-register",
            "import_type": "ip_register",
            "project_id": "project-north",
            "site_id": "site-17",
            "original_filename": "identity-budget-register.csv",
            "summary": {
                "accepted_rows": len(rows),
                "authority_schema_version": "1.0",
                "file_sha256": "a" * 64,
            },
            "accepted_rows": rows,
        }

        class _ImportRepository:
            def list(self, **_filters: object) -> list[dict[str, object]]:
                return [repository_rows]

            def get(self, import_id: str) -> dict[str, object]:
                self_outer.assertEqual(import_id, "identity-budget-register")
                return repository_rows

        self_outer = self
        parameters = resolve_ip_discovery_parameters(
            {
                "authorized": True,
                "source_ip": "127.0.0.1",
                "local_address": "127.0.0.1/8",
                "source_interface_identity_v1": source_identity,
                "target_expressions": [
                    {"kind": "address", "address": target}
                    for target in targets
                ],
                "ports": [443],
                "ip_register_import_id": "identity-budget-register",
            },
            project_id="project-north",
            site_id="site-17",
            import_repository=_ImportRepository(),
        )
        context = RunContextV1.model_validate(
            {
                "project_id": "project-north",
                "site_id": "site-17",
                "configuration_snapshot": {},
                "configuration_version": 7,
                "registers": [],
                "imports": [],
                "schema_versions": {},
                "engine_parameters": parameters,
                "network_interface": "127.0.0.1/8",
                "connection_settings": {},
                "secret_references": {},
                "requesting_principal": "user-42",
                "application_version": "0.1.41",
                "protocol_key": "ip:test-source",
            }
        )
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery", context=context, execution_mode="inline"
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            lease_seconds=60,
            now=datetime.now(UTC),
            owner_token="owner-a",
        )
        self.assertIsNotNone(lease)
        assert lease is not None
        store = OwnedRunStore(self.repository, lease)
        # This test exercises the sealed observation path; authorization-link
        # enforcement has dedicated lifecycle coverage elsewhere.
        store.require_active_control = lambda **_kwargs: None  # type: ignore[method-assign]

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        async def identity(host: str) -> ProviderIdentityEvidenceV1:
            return ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                asset_id=f"AHU-{targets.index(host)}",
                hostname=ip_scan.max_provider_identity_hostname_v1(),
                mac_address=ip_scan.IP_PROVIDER_IDENTITY_MAX_MAC_ADDRESS,
                corroborating_fields=("asset_id",),
            )

        terminal = ip_scan.process_ip_discovery_run(
            envelope.run_id,
            parameters,
            run_store=store,
            execution_mode="inline",
            connect=connected,
            import_loader=lambda _import_id: rows,
            identity_observer=identity,
        )

        budget = parameters["scan_contract_v1"]["ip"]["observation_budget"]
        self.assertEqual(terminal["status"], "succeeded", terminal)
        with session_factory(self.engine)() as session:
            state = session.get(RunDiscoveryObservationState, (envelope.run_id, lease.attempt))
            seal = session.get(RunSeal, envelope.run_id)
        self.assertIsNotNone(state)
        self.assertIsNotNone(seal)
        assert state is not None
        final_reasons = [
            item.payload["ip_v1"]["reason"]
            for item in self.repository.list_discovery_observations(
                envelope.run_id,
                lease.attempt,
            )
            if item.phase == "finalize" and "ip_v1" in item.payload
        ]
        self.assertEqual(len(final_reasons), len(targets))
        self.assertTrue(
            all(
                len(reason) <= ip_scan.IP_STATUS_DETAIL_MAX_CHARS
                for reason in final_reasons
            )
        )
        self.assertLessEqual(state.canonical_payload_bytes, budget["planned_observation_payload_bytes"])

    def test_append_replay_is_idempotent_and_does_not_renew_lease(self) -> None:
        run_id, lease = self.create_claimed_run()
        observation = _observation()

        first = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            observation,
        )
        self.clock_now = datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC)
        replay = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            observation,
        )

        self.assertFalse(first.idempotent)
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.cursor, first.cursor)
        with session_factory(self.engine)() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )
        self.assertEqual(run.heartbeat_at, lease.heartbeat_at)
        self.assertEqual(run.lease_expires_at, lease.lease_expires_at)
        self.assertEqual(state.observation_count, 1)

        page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            project_id="project-north",
            site_id="site-17",
        )
        self.assertEqual(len(page), 1)
        self.assertIs(page[0].payload["reachable"], False)
        self.assertEqual(page[0].payload["response_ms"], 0)

    def test_ip_append_enforces_the_frozen_row_budget_after_replay(self) -> None:
        run_id, lease = self.create_claimed_run(
            observation_rows=1,
            observation_payload_bytes=1_000,
        )
        observation = _observation(event_key="sealed-row-budget-v1")

        first = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            observation,
        )
        replay = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            observation,
        )
        with self.assertRaises(DiscoveryObservationConflictError) as rejected:
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(
                    event_key="sealed-row-budget-v2",
                    entity_version=2,
                ),
            )

        self.assertFalse(first.idempotent)
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.cursor, first.cursor)
        self.assertEqual(rejected.exception.reason, "observation_row_budget_exhausted")
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            1,
        )

    def test_ip_append_enforces_the_frozen_payload_budget_at_one_byte_over(self) -> None:
        first = _observation(
            event_key="sealed-byte-budget-v1",
            payload={"value": "a"},
        )
        second = _observation(
            event_key="sealed-byte-budget-v2",
            entity_version=2,
            payload={"value": "b"},
        )
        self.assertEqual(len(canonical_json_bytes(first.payload)), 13)
        self.assertEqual(len(canonical_json_bytes(second.payload)), 13)

        exact_run_id, exact_lease = self.create_claimed_run(
            observation_rows=2,
            observation_payload_bytes=26,
        )
        self.repository.append_discovery_observation(
            exact_run_id,
            exact_lease.owner_token,
            exact_lease.attempt,
            first,
        )
        exact = self.repository.append_discovery_observation(
            exact_run_id,
            exact_lease.owner_token,
            exact_lease.attempt,
            second,
        )

        over_run_id, over_lease = self.create_claimed_run(
            observation_rows=2,
            observation_payload_bytes=25,
        )
        self.repository.append_discovery_observation(
            over_run_id,
            over_lease.owner_token,
            over_lease.attempt,
            first,
        )
        with self.assertRaises(DiscoveryObservationConflictError) as rejected:
            self.repository.append_discovery_observation(
                over_run_id,
                over_lease.owner_token,
                over_lease.attempt,
                second,
            )

        self.assertFalse(exact.idempotent)
        self.assertEqual(rejected.exception.reason, "observation_payload_budget_exhausted")
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                exact_run_id,
                exact_lease.attempt,
            ).observation_count,
            2,
        )
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                over_run_id,
                over_lease.attempt,
            ).observation_count,
            1,
        )

    def test_first_append_creates_the_attempt_lifecycle_state(self) -> None:
        run_id, lease = self.create_claimed_run()
        observation = _observation(event_key="state-first-append")

        appended = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            observation,
        )

        with session_factory(self.engine)() as session:
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )
        self.assertIsNotNone(state)
        self.assertEqual(state.observation_count, 1)
        self.assertEqual(
            state.canonical_payload_bytes,
            len(canonical_json_bytes(observation.payload)),
        )
        self.assertEqual(state.terminal_cursor, appended.cursor)
        self.assertRegex(state.observation_stream_sha256, r"^[0-9a-f]{64}$")

    def test_append_commits_the_validated_view_without_rehashing_its_payload(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run()
        observation = _observation(
            event_key="canonical-append-view",
            payload={"response_ms": 2.5, "reachable": True},
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_payload = lifecycle_module.observation_payload
        with mock.patch.object(
            lifecycle_module,
            "observation_payload",
            wraps=real_payload,
        ) as normalized:
            appended = self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                observation,
            )

        self.assertEqual(normalized.call_count, 1)
        expected_payload, _encoded_payload, expected_digest = observation_payload(
            observation.payload
        )
        page = self.repository.list_discovery_observations(run_id, lease.attempt)
        self.assertEqual(page[0].cursor, appended.cursor)
        self.assertEqual(page[0].payload, expected_payload)
        self.assertEqual(page[0].payload_sha256, expected_digest)
        expected_commitment = extend_observation_stream_sha256(
            OBSERVATION_STREAM_EMPTY_SHA256,
            page[0],
        )
        with session_factory(self.engine)() as session:
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )
        self.assertEqual(state.observation_stream_sha256, expected_commitment)

        finalized = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            TerminalResultV1(status="succeeded", stage="complete"),
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )

        self.assertTrue(finalized.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(
            result.summary["observation_evidence_v1"][
                "observation_stream_sha256"
            ],
            expected_commitment,
        )

    def test_append_uses_the_repository_clock_for_its_commit_time(self) -> None:
        committed_at = datetime(2026, 8, 11, 9, 0, 12, tzinfo=UTC)
        repository = RunLifecycleRepository(
            self.engine,
            clock=lambda: committed_at,
        )
        run_id, lease = self.create_claimed_run()

        outcome = repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="repository-clock"),
        )

        page = repository.list_discovery_observations(run_id, lease.attempt)
        self.assertEqual(outcome.cursor, page[0].cursor)
        self.assertEqual(page[0].created_at, committed_at)
        with self.assertRaises(TypeError):
            repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(event_key="caller-time-override"),
                now=lease.claimed_at,
            )

    def test_append_rechecks_the_lease_after_validation_before_insert(self) -> None:
        run_id, lease = self.create_claimed_run()
        clock_samples = iter(
            (
                lease.lease_expires_at - timedelta(microseconds=1),
                lease.lease_expires_at,
            )
        )
        repository = RunLifecycleRepository(
            self.engine,
            clock=lambda: next(clock_samples),
        )

        with self.assertRaises(DiscoveryObservationConflictError) as rejected:
            repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(event_key="lease-crossed-during-validation"),
            )

        self.assertEqual(
            rejected.exception.reason,
            "stale_owner_attempt_or_terminal",
        )
        self.assertEqual(
            repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            0,
        )

    def test_conflicting_event_or_version_identity_is_audited_and_rejected(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(),
        )

        with self.assertRaises(DiscoveryObservationConflictError) as event_conflict:
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(payload={"reachable": True, "response_ms": 1}),
            )
        with self.assertRaises(DiscoveryObservationConflictError) as version_conflict:
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(event_key="different-event"),
            )

        self.assertEqual(event_conflict.exception.reason, "observation_identity_conflict")
        self.assertEqual(version_conflict.exception.reason, "observation_identity_conflict")
        self.assertEqual(self.repository.conflict_count(run_id), 2)
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(run_id, lease.attempt).observation_count,
            1,
        )

    def test_concurrent_identical_appends_commit_one_cursor(self) -> None:
        run_id, lease = self.create_claimed_run()
        observation = _observation(event_key="concurrent-host-v1")

        def append_once(_marker: int):
            return self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                observation,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(append_once, range(2)))

        self.assertEqual({outcome.cursor for outcome in outcomes}, {1})
        self.assertEqual(sum(outcome.idempotent for outcome in outcomes), 1)
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id, lease.attempt
            ).observation_count,
            1,
        )
        with session_factory(self.engine)() as session:
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )
        self.assertEqual(state.observation_count, 1)

    def test_cursor_pages_preserve_out_of_order_entity_versions_and_scope(self) -> None:
        run_id, lease = self.create_claimed_run()
        version_two = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="host-v2", entity_version=2),
        )
        self.clock_now = datetime(2026, 8, 11, 9, 0, 11, tzinfo=UTC)
        version_one = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="host-v1", entity_version=1),
        )

        first_page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            limit=1,
            project_id="project-north",
            site_id="site-17",
        )
        second_page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            after_cursor=first_page[0].cursor,
            limit=1,
            project_id="project-north",
            site_id="site-17",
        )

        self.assertEqual(first_page[0].cursor, version_two.cursor)
        self.assertEqual(first_page[0].entity_version, 2)
        self.assertEqual(second_page[0].cursor, version_one.cursor)
        self.assertEqual(second_page[0].entity_version, 1)
        cutoff = self.repository.get_discovery_observation_cutoff(
            run_id,
            lease.attempt,
            project_id="project-north",
            site_id="site-17",
        )
        self.assertEqual(cutoff.terminal_cursor, version_one.cursor)
        self.assertEqual(cutoff.observation_count, 2)
        with self.assertRaises(FileNotFoundError):
            self.repository.list_discovery_observations(
                run_id,
                lease.attempt,
                project_id="other-project",
                site_id="site-17",
            )

    def test_repository_page_stops_before_large_rows_materialize_half_a_megabyte(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run()
        for version in range(1, 9):
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                _observation(
                    event_key=f"large-page-row-v{version}",
                    entity_version=version,
                    payload={"value": "x" * 60_000},
                ),
            )

        page = self.repository.list_discovery_observations(
            run_id,
            lease.attempt,
            limit=250,
        )

        encoded_bytes = sum(
            len(canonical_json_bytes(item.model_dump(mode="json"))) for item in page
        )
        self.assertLessEqual(encoded_bytes, 384 * 1024)
        self.assertLess(len(page), 8)

    def test_repository_page_rejects_a_row_with_a_tampered_payload_digest(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="tampered-page-row"),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == run_id)
                .values(payload={"reachable": True, "response_ms": 1})
            )

        with self.assertRaises(RuntimeError) as raised:
            self.repository.list_discovery_observations(run_id, lease.attempt)

        self.assertEqual(
            type(raised.exception).__name__,
            "DiscoveryObservationIntegrityError",
        )
        self.assertEqual(
            getattr(raised.exception, "reason", None),
            "payload_digest_mismatch",
        )
        self.assertNotIn("reachable", str(raised.exception))

    def test_stale_owner_attempt_and_expired_lease_cannot_append(self) -> None:
        run_id, lease = self.create_claimed_run()
        before_expiry = datetime(2026, 8, 11, 9, 0, 10, tzinfo=UTC)
        after_expiry = lease.lease_expires_at + timedelta(seconds=1)

        for case_number, (owner_token, attempt, observed_at) in enumerate(
            (
                ("stale-owner", lease.attempt, before_expiry),
                (lease.owner_token, lease.attempt + 1, before_expiry),
                (lease.owner_token, lease.attempt, after_expiry),
            ),
            start=1,
        ):
            self.clock_now = observed_at
            with self.assertRaises(DiscoveryObservationConflictError):
                self.repository.append_discovery_observation(
                    run_id,
                    owner_token,
                    attempt,
                    _observation(event_key=f"rejected:{case_number}"),
                )

        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(run_id, lease.attempt).observation_count,
            0,
        )
        self.assertEqual(self.repository.conflict_count(run_id), 3)

    def test_append_caps_canonical_utf8_payload_bytes_before_transaction(self) -> None:
        run_id, lease = self.create_claimed_run()
        oversized = _observation(payload={"value": "é" * 32_768})

        with self.assertRaisesRegex(ValueError, "UTF-8 byte limit"):
            self.repository.append_discovery_observation(
                run_id,
                lease.owner_token,
                lease.attempt,
                oversized,
            )

        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id, lease.attempt
            ).observation_count,
            0,
        )
        self.assertEqual(self.repository.conflict_count(run_id), 0)

    def test_append_rejects_a_prefix_that_would_exceed_the_fold_row_budget(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="row-budget-v1"),
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        with mock.patch.object(
            lifecycle_module,
            "DISCOVERY_OBSERVATION_FOLD_MAX_ROWS",
            1,
        ):
            with self.assertRaises(DiscoveryObservationConflictError) as rejected:
                self.repository.append_discovery_observation(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    _observation(event_key="row-budget-v2", entity_version=2),
                )

        self.assertEqual(rejected.exception.reason, "observation_row_budget_exhausted")
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            1,
        )

    def test_append_rejects_a_prefix_that_would_exceed_canonical_payload_bytes(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="byte-budget-v1", payload={"value": "a"}),
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        with mock.patch.object(
            lifecycle_module,
            "DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES",
            20,
        ):
            with self.assertRaises(DiscoveryObservationConflictError) as rejected:
                self.repository.append_discovery_observation(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    _observation(
                        event_key="byte-budget-v2",
                        entity_version=2,
                        payload={"value": "b"},
                    ),
                )

        self.assertEqual(
            rejected.exception.reason,
            "observation_payload_budget_exhausted",
        )
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            1,
        )

    def test_append_uses_lifecycle_state_without_refolding_prior_rows(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="state-owned-prefix-v1"),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == run_id)
                .values(payload_sha256="0" * 64)
            )

        second = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(
                event_key="state-owned-prefix-v2",
                entity_version=2,
            ),
        )

        self.assertEqual(second.cursor, 2)
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id,
                lease.attempt,
            ).observation_count,
            2,
        )

    def test_reclaimed_attempt_builds_an_isolated_observation_state(self) -> None:
        run_id, first_lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            first_lease.owner_token,
            first_lease.attempt,
            _observation(event_key="attempt-isolated-event"),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    status="queued",
                    stage="queued",
                    owner_token=None,
                    claimed_at=None,
                    heartbeat_at=None,
                    lease_expires_at=None,
                )
            )
        second_lease = self.repository.claim_run(
            run_id,
            first_lease.dispatch_id,
            lease_seconds=60,
            now=datetime(2026, 8, 11, 9, 1, tzinfo=UTC),
            owner_token="owner-b",
        )
        self.assertIsNotNone(second_lease)

        second = self.repository.append_discovery_observation(
            run_id,
            second_lease.owner_token,
            second_lease.attempt,
            _observation(event_key="attempt-isolated-event"),
        )

        self.assertEqual(second_lease.attempt, first_lease.attempt + 1)
        self.assertFalse(second.idempotent)
        self.assertEqual(
            len(
                self.repository.list_discovery_observations(
                    run_id,
                    second_lease.attempt,
                )
            ),
            1,
        )
        with session_factory(self.engine)() as session:
            states = list(
                session.scalars(
                    select(RunDiscoveryObservationState)
                    .where(RunDiscoveryObservationState.run_id == run_id)
                    .order_by(RunDiscoveryObservationState.attempt)
                )
            )
        self.assertEqual(
            [(state.attempt, state.observation_count) for state in states],
            [(first_lease.attempt, 1), (second_lease.attempt, 1)],
        )

    def test_discovery_finalizer_seals_fold_and_releases_every_slot(self) -> None:
        run_id, lease = self.create_claimed_run()
        for key in ("ip:192.0.2.10", "ip:192.0.2.11"):
            self.repository.acquire_protocol_slot(
                run_id,
                key,
                owner_token=lease.owner_token,
            )
        device = {
            "address": "192.0.2.20",
            "device_type": "ip_host",
            "attributes": {"reachable": False, "response_ms": 0},
        }
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="host",
                entity_key="192.0.2.20",
                entity_version=1,
                event_key="projection:device:192.0.2.20:v1",
                phase="finalize",
                outcome="observed",
                payload_schema_version="1.0",
                payload={
                    "projection_v1": {
                        "collection": "devices",
                        "record": device,
                    }
                },
            ),
        )
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            DiscoveryObservationInputV1(
                protocol="ip",
                entity_kind="lane",
                entity_key="summary",
                entity_version=1,
                event_key="projection:summary:v1",
                phase="finalize",
                outcome="observed",
                payload_schema_version="1.0",
                payload={
                    "projection_v1": {
                        "collection": "summary",
                        "record": {"discovered_devices": 1, "reachable": False},
                    }
                },
            ),
        )
        legacy_issue = {
            "issue_id": "issue-1",
            "asset_id": "192.0.2.20",
            "issue_type": "reachability",
            "severity": "low",
            "description": "Host did not answer.",
        }
        terminal = TerminalResultV1(
            status="succeeded",
            stage="engine_complete",
            summary={"provider": "builtin", "discovered_devices": 99},
            issues=(legacy_issue,),
            devices=({"address": "legacy-device"},),
        )

        outcome = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            terminal,
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )

        self.assertTrue(outcome.applied)
        result = self.repository.get_seal(run_id)
        self.assertEqual(result.terminal_status, "succeeded")
        with session_factory(self.engine)() as session:
            stored = session.get(RunResult, run_id)
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )
            slots = list(
                session.scalars(
                    select(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run_id)
                )
            )
            devices = list(
                session.scalars(
                    select(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
                )
            )
            issues = list(
                session.scalars(select(RunIssue).where(RunIssue.run_id == run_id))
            )
        self.assertEqual(slots, [])
        self.assertEqual([row.address for row in devices], ["192.0.2.20"])
        self.assertEqual([row.issue_id for row in issues], ["issue-1"])
        self.assertEqual(stored.summary["provider"], "builtin")
        self.assertEqual(stored.summary["discovered_devices"], 1)
        self.assertIs(stored.summary["reachable"], False)
        evidence = stored.summary["observation_evidence_v1"]
        self.assertEqual(evidence["attempt"], lease.attempt)
        self.assertEqual(evidence["observation_count"], 2)
        self.assertEqual(evidence["terminal_cursor"], 2)
        self.assertEqual(len(evidence["observation_stream_sha256"]), 64)
        self.assertEqual(
            evidence["observation_stream_sha256"],
            state.observation_stream_sha256,
        )

    def test_retention_pruning_preserves_internal_terminal_attempt_state(self) -> None:
        run_id, lease = self.create_claimed_run()
        appended = self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="retained-terminal-state"),
        )
        outcome = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            TerminalResultV1(status="succeeded", stage="complete"),
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )
        self.assertTrue(outcome.applied)
        with session_factory(self.engine).begin() as session:
            session.execute(
                RunDiscoveryObservation.__table__.delete().where(
                    RunDiscoveryObservation.run_id == run_id
                )
            )

        cutoff = self.repository.get_discovery_observation_cutoff(
            run_id,
            lease.attempt,
        )
        with session_factory(self.engine)() as session:
            state = session.get(
                RunDiscoveryObservationState,
                (run_id, lease.attempt),
            )

        self.assertEqual(cutoff.observation_count, 0)
        self.assertEqual(cutoff.terminal_cursor, 0)
        self.assertIsNotNone(state)
        self.assertEqual(state.observation_count, 1)
        self.assertEqual(state.terminal_cursor, appended.cursor)
        self.assertEqual(
            self.repository.list_discovery_observations(run_id, lease.attempt),
            [],
        )

    def test_terminal_device_scope_is_overwritten_from_the_locked_run(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(
                payload={
                    "projection_v1": {
                        "collection": "devices",
                        "record": {
                            "project_id": "attacker-project",
                            "site_id": "attacker-site",
                            "address": "192.0.2.20",
                            "device_type": "ip_host",
                        },
                    }
                }
            ),
        )

        outcome = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            TerminalResultV1(status="succeeded", stage="complete"),
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )

        self.assertTrue(outcome.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
            device = session.scalar(
                select(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
            )
        self.assertEqual(
            result.result_payload["devices"][0]["project_id"],
            "project-north",
        )
        self.assertEqual(result.result_payload["devices"][0]["site_id"], "site-17")
        self.assertEqual(device.project_id, "project-north")
        self.assertEqual(device.site_id, "site-17")

    def test_discovery_finalizer_refolds_when_a_late_cursor_arrives(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="host-v1"),
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_fold = lifecycle_module.fold_discovery_observations
        fold_calls = 0

        def append_during_first_fold(*args, **kwargs):
            nonlocal fold_calls
            fold_calls += 1
            folded = real_fold(*args, **kwargs)
            if fold_calls == 1:
                self.repository.append_discovery_observation(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    DiscoveryObservationInputV1(
                        protocol="ip",
                        entity_kind="diagnostic",
                        entity_key="late",
                        entity_version=1,
                        event_key="late-diagnostic-v1",
                        phase="finalize",
                        outcome="observed",
                        payload_schema_version="1.0",
                        payload={"late": True},
                    ),
                )
            return folded

        with mock.patch.object(
            lifecycle_module,
            "fold_discovery_observations",
            side_effect=append_during_first_fold,
        ):
            outcome = self.repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(status="succeeded", stage="complete"),
                now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
            )

        self.assertTrue(outcome.applied)
        self.assertEqual(fold_calls, 2)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            2,
        )
        self.assertEqual(
            result.summary["observation_evidence_v1"]["terminal_cursor"],
            2,
        )

    def test_discovery_finalizer_requires_fold_and_lifecycle_state_parity(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="state-parity-v1"),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(RunDiscoveryObservationState)
                .where(
                    RunDiscoveryObservationState.run_id == run_id,
                    RunDiscoveryObservationState.attempt == lease.attempt,
                )
                .values(observation_stream_sha256="0" * 64)
            )

        with self.assertRaises(RuntimeError) as raised:
            self.repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(status="succeeded", stage="complete"),
                now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
            )

        self.assertEqual(
            type(raised.exception).__name__,
            "DiscoveryObservationIntegrityError",
        )
        self.assertEqual(
            getattr(raised.exception, "reason", None),
            "observation_state_mismatch",
        )
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "running")
            self.assertIsNone(session.get(RunResult, run_id))
            self.assertIsNone(session.get(RunSeal, run_id))

    def test_projected_empty_collection_replaces_stale_terminal_buffer(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="non-projecting-evidence"),
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_fold = lifecycle_module.fold_discovery_observations

        def empty_device_projection(*args, **kwargs):
            return real_fold(*args, **kwargs).model_copy(
                update={
                    "projected_collections": frozenset({"devices"}),
                    "devices": (),
                }
            )

        with mock.patch.object(
            lifecycle_module,
            "fold_discovery_observations",
            side_effect=empty_device_projection,
        ):
            outcome = self.repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(
                    status="succeeded",
                    stage="complete",
                    devices=({"address": "stale-buffered-device"},),
                ),
                now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
            )

        self.assertTrue(outcome.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
            devices = list(
                session.scalars(
                    select(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
                )
            )
        self.assertEqual(result.result_payload["devices"], [])
        self.assertEqual(devices, [])

    def test_over_frozen_ip_budget_prefix_is_quarantined_during_recovery(self) -> None:
        run_id, lease = self.create_claimed_run(
            observation_rows=1,
            observation_payload_bytes=1_000,
        )
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="sealed-tamper-v1"),
        )
        with session_factory(self.engine).begin() as session:
            context_row = session.get(RunExecutionContext, run_id)
            original_context_json = copy.deepcopy(context_row.context_json)
            original_context_sha256 = context_row.context_sha256
            widened_context_json = copy.deepcopy(original_context_json)
            widened_context_json["engine_parameters"]["scan_contract_v1"]["ip"][
                "observation_budget"
            ]["planned_observation_rows"] = 2
            widened_context = RunContextV1.model_validate(widened_context_json)
            session.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == run_id)
                .values(
                    context_json=widened_context.model_dump(mode="json"),
                    context_sha256=canonical_context_sha256(widened_context),
                )
            )
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(event_key="sealed-tamper-v2", entity_version=2),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == run_id)
                .values(
                    context_json=original_context_json,
                    context_sha256=original_context_sha256,
                )
            )

        with self.assertRaises(DiscoveryObservationFoldLimitError) as rejected:
            self.repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(status="succeeded", stage="complete"),
                now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
            )

        self.assertEqual(rejected.exception.reason, "sealed_row_limit")
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "running")
            self.assertIsNone(session.get(RunResult, run_id))
            self.assertIsNone(session.get(RunSeal, run_id))

        recovered = self.repository.recover_expired_leases(
            now=datetime(2026, 8, 11, 9, 1, 1, tzinfo=UTC)
        )

        self.assertEqual(recovered, [run_id])
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            seal = session.get(RunSeal, run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(result.terminal_status, "failed")
        self.assertEqual(result.terminal_stage, "lease_expired_observation_quarantined")
        self.assertIs(result.summary["observation_prefix_quarantined"], True)
        self.assertEqual(
            result.summary["observation_quarantine_v1"]["reason"],
            "fold_sealed_row_limit",
        )
        self.assertEqual(seal.terminal_status, "failed")

    def test_fold_row_and_canonical_payload_budgets_fail_closed(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(payload={"value": "bounded"}),
        )
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        with mock.patch.object(
            lifecycle_module,
            "DISCOVERY_OBSERVATION_FOLD_MAX_ROWS",
            0,
        ):
            with self.assertRaises(DiscoveryObservationFoldLimitError) as row_limit:
                self.repository.finalize_discovery_run(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    TerminalResultV1(status="succeeded", stage="complete"),
                    now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
                )
        self.assertEqual(row_limit.exception.reason, "row_limit")

        with mock.patch.object(
            lifecycle_module,
            "DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES",
            1,
        ):
            with self.assertRaises(DiscoveryObservationFoldLimitError) as byte_limit:
                self.repository.finalize_discovery_run(
                    run_id,
                    lease.owner_token,
                    lease.attempt,
                    TerminalResultV1(status="succeeded", stage="complete"),
                    now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
                )
        self.assertEqual(byte_limit.exception.reason, "payload_byte_limit")
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "running")
            self.assertIsNone(session.get(RunResult, run_id))
            self.assertIsNone(session.get(RunSeal, run_id))

    def test_persisted_cancellation_truth_wins_discovery_finalization(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(),
        )
        self.repository.request_cancel(
            run_id,
            now=datetime(2026, 8, 11, 9, 0, 11, tzinfo=UTC),
        )

        outcome = self.repository.finalize_discovery_run(
            run_id,
            lease.owner_token,
            lease.attempt,
            TerminalResultV1(
                status="succeeded",
                stage="complete",
                summary={"acceptance_eligible": True},
            ),
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )

        self.assertTrue(outcome.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(result.terminal_status, "cancelled")
        self.assertEqual(result.terminal_stage, "engine_cancelled")
        self.assertIs(result.summary["acceptance_eligible"], False)
        self.assertIs(result.summary["validation_incomplete"], True)
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            1,
        )

    def test_discovery_finalization_fault_rolls_back_every_terminal_write(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.acquire_protocol_slot(
            run_id,
            "ip:192.0.2.10",
            owner_token=lease.owner_token,
        )
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(),
        )

        def inject(stage: str) -> None:
            if stage == "after_result":
                raise RuntimeError("finalization fault")

        repository = RunLifecycleRepository(self.engine, fault_injector=inject)
        with self.assertRaisesRegex(RuntimeError, "finalization fault"):
            repository.finalize_discovery_run(
                run_id,
                lease.owner_token,
                lease.attempt,
                TerminalResultV1(status="succeeded", stage="complete"),
                now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
            )

        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "running")
            self.assertIsNone(session.get(RunResult, run_id))
            self.assertIsNone(session.get(RunSeal, run_id))
            self.assertIsNotNone(
                session.scalar(
                    select(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run_id)
                )
            )
        self.assertEqual(
            self.repository.get_discovery_observation_cutoff(
                run_id, lease.attempt
            ).observation_count,
            1,
        )

    def test_owned_store_bacnet_bridge_preserves_lanes_duplicates_and_partial_error(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run("bacnet")
        store = OwnedRunStore(self.repository, lease)
        records = [
            {
                "address": "192.0.2.30:47808",
                "device_type": "bacnet_device",
                "attributes": {
                    "device_instance": 4001,
                    "lane": "local",
                    "duplicate_instance": True,
                },
            },
            {
                "address": "198.51.100.30:47808",
                "device_type": "bacnet_device",
                "attributes": {
                    "device_instance": 4001,
                    "lane": "directed",
                    "duplicate_instance": True,
                },
            },
            {
                "device_ref": "192.0.2.30:47808",
                "point_id": "analog-input:1",
                "point_name": "Alarm",
                "observed_value": {"present_value": 0, "in_alarm": False},
                "attributes": {"property_error": "units_unavailable"},
            },
        ]
        written = store.replace_devices_and_points(run_id, records)
        store.replace_issues(
            run_id,
            [
                ValidationIssueRecord(
                    issue_id="property-error-1",
                    asset_id="device:4001",
                    issue_type="bacnet_property_error",
                    severity="low",
                    description="Units could not be read.",
                )
            ],
        )

        terminal = store.update_run_status(
            run_id,
            status="succeeded",
            stage="bacnet_complete",
            progress_percent=100,
        )

        self.assertEqual(written, 3)
        self.assertEqual(terminal["status"], "succeeded")
        page = self.repository.list_discovery_observations(run_id, lease.attempt)
        self.assertEqual(len(page), 4)
        self.assertTrue(all(item.protocol == "bacnet" for item in page))
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
            devices = list(
                session.scalars(
                    select(DiscoveredDevice)
                    .where(DiscoveredDevice.run_id == run_id)
                    .order_by(DiscoveredDevice.position)
                )
            )
        self.assertEqual([row.address for row in devices], [
            "192.0.2.30:47808",
            "198.51.100.30:47808",
        ])
        point = result.result_payload["points"][0]
        self.assertEqual(point["observed_value"]["present_value"], 0)
        self.assertIs(point["observed_value"]["in_alarm"], False)
        self.assertEqual(point["attributes"]["property_error"], "units_unavailable")
        self.assertEqual(result.result_payload["issues"][0]["issue_id"], "property-error-1")
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            4,
        )

    def test_owned_store_repeated_replacement_uses_stable_position_versions(self) -> None:
        run_id, lease = self.create_claimed_run()
        store = OwnedRunStore(self.repository, lease)
        records = [
            {"address": "192.0.2.10", "device_type": "ip_host"},
            {"address": "192.0.2.11", "device_type": "ip_host"},
        ]

        store.replace_devices(run_id, records)
        store.replace_devices(run_id, records)
        store.update_run_status(run_id, status="succeeded", stage="ip_complete")

        page = self.repository.list_discovery_observations(run_id, lease.attempt)
        self.assertEqual(
            [item.entity_key for item in page],
            [
                "projection_v1:devices:00000000",
                "projection_v1:devices:00000001",
                "projection_v1:devices:00000000",
                "projection_v1:devices:00000001",
            ],
        )
        self.assertEqual([item.entity_version for item in page], [1, 1, 2, 2])
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(
            [item["address"] for item in result.result_payload["devices"]],
            ["192.0.2.10", "192.0.2.11"],
        )

    def test_owned_store_reordered_replacement_preserves_new_position_order(self) -> None:
        run_id, lease = self.create_claimed_run()
        store = OwnedRunStore(self.repository, lease)
        first = {"address": "192.0.2.10", "device_type": "ip_host"}
        second = {"address": "192.0.2.11", "device_type": "ip_host"}

        store.replace_devices(run_id, [first, second])
        store.replace_devices(run_id, [second, first])
        store.update_run_status(run_id, status="succeeded", stage="ip_complete")

        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(
            [item["address"] for item in result.result_payload["devices"]],
            ["192.0.2.11", "192.0.2.10"],
        )

    def test_owned_store_empty_replacement_tombstones_prior_positions(self) -> None:
        run_id, lease = self.create_claimed_run()
        store = OwnedRunStore(self.repository, lease)

        store.replace_devices(
            run_id,
            [
                {"address": "192.0.2.10", "device_type": "ip_host"},
                {"address": "192.0.2.11", "device_type": "ip_host"},
            ],
        )
        store.replace_devices(run_id, [])
        store.update_run_status(run_id, status="succeeded", stage="ip_complete")

        page = self.repository.list_discovery_observations(run_id, lease.attempt)
        self.assertEqual(len(page), 4)
        self.assertEqual(
            [item.payload["projection_v1"]["present"] for item in page[-2:]],
            [False, False],
        )
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
            devices = list(
                session.scalars(
                    select(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
                )
            )
        self.assertEqual(result.result_payload["devices"], [])
        self.assertEqual(devices, [])

    def test_owned_store_closes_sink_but_heartbeat_remains_live_during_fold(self) -> None:
        run_id, lease = self.create_claimed_run(
            claimed_at=datetime.now(UTC),
        )
        store = OwnedRunStore(self.repository, lease)
        fold_entered = threading.Event()
        release_fold = threading.Event()
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_fold = lifecycle_module.fold_discovery_observations

        def quiet_fold(*args, **kwargs):
            fold_entered.set()
            if not release_fold.wait(3.0):
                raise TimeoutError("fold was not released")
            return real_fold(*args, **kwargs)

        finalizer_result: dict[str, object] = {}
        finalizer_error: list[BaseException] = []

        def finalize() -> None:
            try:
                finalizer_result["value"] = store.update_run_status(
                    run_id,
                    status="succeeded",
                    stage="ip_complete",
                    progress_percent=100,
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                finalizer_error.append(error)

        with mock.patch.object(
            lifecycle_module,
            "fold_discovery_observations",
            side_effect=quiet_fold,
        ):
            thread = threading.Thread(target=finalize)
            thread.start()
            self.assertTrue(fold_entered.wait(3.0))
            self.assertTrue(store.heartbeat(lease_seconds=60))
            with self.assertRaises(RunFinalizingError):
                store.replace_devices(run_id, [{"address": "192.0.2.99"}])
            with self.assertRaises(RunFinalizingError):
                store.append_observation(_observation(event_key="too-late"))
            self.assertFalse(store.mark_ownership_lost())
            self.assertFalse(store.ownership_lost)
            release_fold.set()
            thread.join(3.0)

        self.assertFalse(thread.is_alive())
        if finalizer_error:
            raise finalizer_error[0]
        self.assertEqual(finalizer_result["value"]["status"], "succeeded")
        self.assertTrue(store.terminal_outcome.applied)
        self.assertFalse(store.ownership_lost)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            0,
        )

    def test_expired_discovery_recovery_seals_honest_committed_prefix(self) -> None:
        run_id, lease = self.create_claimed_run()
        self.repository.acquire_protocol_slot(
            run_id,
            "ip:192.0.2.10",
            owner_token=lease.owner_token,
        )
        projected = _observation(
            payload={
                "projection_v1": {
                    "collection": "devices",
                    "record": {
                        "address": "192.0.2.20",
                        "device_type": "ip_host",
                        "attributes": {"reachable": False},
                    },
                }
            }
        )
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            projected,
        )

        recovered = self.repository.recover_expired_leases(
            now=lease.lease_expires_at + timedelta(seconds=1)
        )

        self.assertEqual(recovered, [run_id])
        with session_factory(self.engine)() as session:
            run = session.get(Run, run_id)
            result = session.get(RunResult, run_id)
            slots = list(
                session.scalars(
                    select(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run_id)
                )
            )
            devices = list(
                session.scalars(
                    select(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
                )
            )
        self.assertEqual(run.status, "failed")
        self.assertEqual(slots, [])
        self.assertEqual([device.address for device in devices], ["192.0.2.20"])
        self.assertIs(result.summary["lease_recovered"], True)
        self.assertIs(result.summary["provider_drained"], False)
        self.assertIs(result.summary["validation_incomplete"], True)
        self.assertIs(result.summary["acceptance_eligible"], False)
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            1,
        )

    def test_corrupt_expired_prefix_is_quarantined_without_blocking_later_runs(
        self,
    ) -> None:
        corrupt_run_id, corrupt_lease = self.create_claimed_run()
        healthy_run_id, healthy_lease = self.create_claimed_run()
        self.repository.append_discovery_observation(
            corrupt_run_id,
            corrupt_lease.owner_token,
            corrupt_lease.attempt,
            _observation(event_key="corrupt-before-expiry"),
        )
        self.repository.append_discovery_observation(
            healthy_run_id,
            healthy_lease.owner_token,
            healthy_lease.attempt,
            _observation(event_key="healthy-before-expiry"),
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(RunDiscoveryObservation)
                .where(RunDiscoveryObservation.run_id == corrupt_run_id)
                .values(payload_sha256="0" * 64)
            )

        recovered = self.repository.recover_expired_leases(
            now=max(corrupt_lease.lease_expires_at, healthy_lease.lease_expires_at)
            + timedelta(seconds=1)
        )

        self.assertEqual(set(recovered), {corrupt_run_id, healthy_run_id})
        with session_factory(self.engine)() as session:
            corrupt_run = session.get(Run, corrupt_run_id)
            corrupt_result = session.get(RunResult, corrupt_run_id)
            healthy_result = session.get(RunResult, healthy_run_id)
            self.assertIsNotNone(session.get(RunSeal, corrupt_run_id))
        self.assertEqual(corrupt_run.status, "failed")
        self.assertIs(corrupt_result.summary["observation_prefix_quarantined"], True)
        quarantine = corrupt_result.summary["observation_quarantine_v1"]
        self.assertEqual(quarantine["attempt"], corrupt_lease.attempt)
        self.assertEqual(quarantine["observation_count"], 1)
        self.assertEqual(quarantine["terminal_cursor"], 1)
        self.assertEqual(quarantine["reason"], "invalid_prefix")
        self.assertEqual(self.repository.conflict_count(corrupt_run_id), 1)
        self.assertEqual(
            healthy_result.summary["observation_evidence_v1"]["observation_count"],
            1,
        )

    def test_false_heartbeat_during_finalizing_defers_to_successful_finalizer(
        self,
    ) -> None:
        run_id, lease = self.create_claimed_run(claimed_at=datetime.now(UTC))
        store = OwnedRunStore(self.repository, lease)
        fold_entered = threading.Event()
        release_fold = threading.Event()
        heartbeat_called = threading.Event()
        from smart_commissioning_core.db import run_lifecycle as lifecycle_module

        real_fold = lifecycle_module.fold_discovery_observations

        def blocked_fold(*args, **kwargs):
            fold_entered.set()
            if not release_fold.wait(3.0):
                raise TimeoutError("fold was not released")
            return real_fold(*args, **kwargs)

        finalizer_error: list[BaseException] = []

        def finalize() -> None:
            try:
                store.update_run_status(
                    run_id,
                    status="succeeded",
                    stage="complete",
                    progress_percent=100,
                )
            except BaseException as error:  # pragma: no cover - surfaced below
                finalizer_error.append(error)

        def false_heartbeat(*_args, **_kwargs) -> bool:
            heartbeat_called.set()
            return False

        with (
            mock.patch.object(
                lifecycle_module,
                "fold_discovery_observations",
                side_effect=blocked_fold,
            ),
            mock.patch.object(
                self.repository,
                "heartbeat",
                side_effect=false_heartbeat,
            ),
        ):
            finalizer = threading.Thread(target=finalize)
            finalizer.start()
            self.assertTrue(fold_entered.wait(3.0))
            heartbeat = OwnedRunHeartbeat(
                store,
                lease_seconds=1,
                interval_seconds=0.01,
            )
            heartbeat.start()
            self.assertTrue(heartbeat_called.wait(2.0))
            self.assertFalse(heartbeat.ownership_lost)
            self.assertFalse(store.ownership_lost)
            release_fold.set()
            finalizer.join(3.0)
            heartbeat.stop_and_join()

        self.assertFalse(finalizer.is_alive())
        if finalizer_error:
            raise finalizer_error[0]
        self.assertTrue(store.terminal_outcome.applied)
        self.assertFalse(store.ownership_lost)
        self.assertFalse(heartbeat.ownership_lost)

    def test_expired_discovery_recovery_preserves_persisted_cancellation(self) -> None:
        run_id, lease = self.create_claimed_run("bacnet")
        self.repository.append_discovery_observation(
            run_id,
            lease.owner_token,
            lease.attempt,
            _observation(protocol="bacnet", event_key="bacnet-before-stop"),
        )
        self.repository.request_cancel(
            run_id,
            now=datetime(2026, 8, 11, 9, 0, 20, tzinfo=UTC),
        )

        self.assertEqual(
            self.repository.recover_expired_leases(
                now=lease.lease_expires_at + timedelta(seconds=1)
            ),
            [run_id],
        )

        with session_factory(self.engine)() as session:
            result = session.get(RunResult, run_id)
        self.assertEqual(result.terminal_status, "cancelled")
        self.assertEqual(result.terminal_stage, "lease_expired_cancelled")
        self.assertIsNone(result.result_payload["error_message"])
        self.assertIs(result.summary["provider_drained"], False)
        self.assertEqual(
            result.summary["observation_evidence_v1"]["observation_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
