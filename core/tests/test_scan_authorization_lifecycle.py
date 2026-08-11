"""Sealed preview, one-use authorization, relation, and claim fencing tests."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    ActiveProtocolSlot,
    Run,
    RunExecutionContext,
    RunLink,
    ScanAuthorization,
)
from smart_commissioning_core.db.run_lifecycle import (
    RunLifecycleRepository,
    ScanAuthorizationError,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import select

_DIGEST = "a" * 64
_RESOURCE = "nic:192.0.2.10"


def _context(
    *,
    dry_run: bool,
    required_window_seconds: float | None = None,
    retry_parent_run_id: str | None = None,
) -> RunContextV1:
    scan_contract: dict[str, object] = {
        "scan_contract_version": "1.0",
        "packet_plan_sha256": _DIGEST,
        "resource_keys": [_RESOURCE],
    }
    if required_window_seconds is not None:
        scan_contract["ip"] = {
            "work_estimate": {
                "required_authorization_window_seconds": required_window_seconds,
            }
        }
    if retry_parent_run_id is not None:
        scan_contract["relation_snapshot"] = {
            "relation": "retry",
            "parent_run_id": retry_parent_run_id,
        }
    return RunContextV1.model_validate(
        {
            "project_id": "project-a",
            "site_id": "site-a",
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [],
            "schema_versions": {},
            "engine_parameters": {
                "dry_run": dry_run,
                "scan_contract_v1": scan_contract,
            },
            "network_interface": "192.0.2.10/24",
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "engineer-a",
            "application_version": "0.1.41",
            "protocol_key": None,
        }
    )


class ScanAuthorizationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temp_dir.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.repository = RunLifecycleRepository(self.engine)
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        self.preview_run_id = self._sealed_preview()

    def _sealed_preview(self, context: RunContextV1 | None = None) -> str:
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=context or _context(dry_run=True),
            now=self.now,
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            now=self.now,
        )
        assert lease is not None
        finalized = self.repository.finalize_run(
            envelope.run_id,
            lease.owner_token,
            TerminalResultV1(
                status="succeeded",
                stage="dry_run_complete",
                summary={"preview": True},
            ),
            now=self.now,
        )
        self.assertTrue(finalized.applied)
        return envelope.run_id

    def _authorization(self, *, authorization_id: str = "auth-one"):
        return self.repository.create_scan_authorization(
            preview_run_id=self.preview_run_id,
            authorization_id=authorization_id,
            approved_by="admin-a",
            ticket="CHG-1042",
            purpose="Controlled plant-room discovery",
            not_before=self.now,
            not_after=self.now + timedelta(hours=1),
            now=self.now,
        )

    def _create_live(self, authorization_id: str, *, run_id: str):
        return self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=_context(dry_run=False),
            run_id=run_id,
            authorization_id=authorization_id,
            preview_run_id=self.preview_run_id,
            now=self.now,
        )

    def test_live_create_atomically_consumes_authorization_and_links_preview(self) -> None:
        authorization = self._authorization()
        envelope = self._create_live(authorization.authorization_id, run_id="run-live-one")

        with session_factory(self.engine)() as session:
            stored = session.get(ScanAuthorization, authorization.authorization_id)
            link = session.scalar(
                select(RunLink).where(RunLink.child_run_id == envelope.run_id)
            )
            slots = list(
                session.scalars(
                    select(ActiveProtocolSlot).where(
                        ActiveProtocolSlot.run_id == envelope.run_id
                    )
                )
            )
        self.assertEqual(stored.use_count, 1)
        self.assertEqual(stored.consumed_run_id, envelope.run_id)
        self.assertEqual(link.parent_run_id, self.preview_run_id)
        self.assertEqual(link.relation, "preview")
        self.assertEqual([slot.protocol_key for slot in slots], [_RESOURCE])

    def test_authorization_window_covers_the_sealed_conservative_bound(self) -> None:
        preview_run_id = self._sealed_preview(
            _context(dry_run=True, required_window_seconds=1_800.5)
        )
        with self.assertRaisesRegex(ScanAuthorizationError, "shorter"):
            self.repository.create_scan_authorization(
                preview_run_id=preview_run_id,
                approved_by="admin-a",
                ticket="CHG-1043",
                purpose="Bounded retry",
                not_before=self.now,
                not_after=self.now + timedelta(seconds=1_800),
                now=self.now,
            )

        accepted = self.repository.create_scan_authorization(
            preview_run_id=preview_run_id,
            approved_by="admin-a",
            ticket="CHG-1044",
            purpose="Bounded retry",
            not_before=self.now,
            not_after=self.now + timedelta(seconds=1_801),
            now=self.now,
        )
        self.assertEqual(accepted.preview_run_id, preview_run_id)

    def test_authorization_recomputes_the_preview_context_digest(self) -> None:
        with session_factory(self.engine).begin() as session:
            stored = session.get(RunExecutionContext, self.preview_run_id)
            assert stored is not None
            tampered = dict(stored.context_json)
            tampered["application_version"] = "tampered-after-seal"
            stored.context_json = tampered

        with self.assertRaisesRegex(ScanAuthorizationError, "seal is invalid"):
            self._authorization(authorization_id="auth-tampered-preview")

    def test_retry_preview_and_live_run_have_relational_and_frozen_links(self) -> None:
        parent_authorization = self._authorization(authorization_id="auth-parent")
        parent = self._create_live(
            parent_authorization.authorization_id,
            run_id="run-retry-parent",
        )
        parent_lease = self.repository.claim_run(
            parent.run_id,
            parent.dispatch_id,
            now=self.now,
        )
        assert parent_lease is not None
        self.assertTrue(
            self.repository.finalize_run(
                parent.run_id,
                parent_lease.owner_token,
                TerminalResultV1(
                    status="failed",
                    stage="network_unreachable",
                    summary={"retry_allowed": True},
                ),
                now=self.now,
            ).applied
        )

        retry_context = _context(
            dry_run=True,
            retry_parent_run_id=parent.run_id,
        )
        retry_preview_id = self._sealed_preview(retry_context)
        retry_authorization = self.repository.create_scan_authorization(
            preview_run_id=retry_preview_id,
            approved_by="admin-a",
            ticket="CHG-1045",
            purpose="Retry after unreachable path",
            not_before=self.now,
            not_after=self.now + timedelta(hours=1),
            now=self.now,
        )
        retry_live = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=_context(
                dry_run=False,
                retry_parent_run_id=parent.run_id,
            ),
            run_id="run-retry-live",
            authorization_id=retry_authorization.authorization_id,
            preview_run_id=retry_preview_id,
            now=self.now,
        )

        preview_links = self.repository.list_run_links(retry_preview_id)
        live_links = self.repository.list_run_links(retry_live.run_id)
        self.assertEqual(
            [(item["relation"], item["parent_run_id"]) for item in preview_links],
            [("retry", parent.run_id)],
        )
        self.assertCountEqual(
            [(item["relation"], item["parent_run_id"]) for item in live_links],
            [("preview", retry_preview_id), ("retry", parent.run_id)],
        )

    def test_retry_parent_must_be_a_sealed_same_scope_live_run(self) -> None:
        with self.assertRaisesRegex(ScanAuthorizationError, "retry parent"):
            self.repository.create_run_with_context(
                job_type="ip_discovery",
                context=_context(
                    dry_run=True,
                    retry_parent_run_id=self.preview_run_id,
                ),
                run_id="run-invalid-retry",
                now=self.now,
            )

    def test_retry_parent_context_digest_is_recomputed(self) -> None:
        authorization = self._authorization(authorization_id="auth-retry-tamper")
        parent = self._create_live(
            authorization.authorization_id,
            run_id="run-retry-tampered-parent",
        )
        lease = self.repository.claim_run(
            parent.run_id,
            parent.dispatch_id,
            now=self.now,
        )
        assert lease is not None
        self.assertTrue(
            self.repository.finalize_run(
                parent.run_id,
                lease.owner_token,
                TerminalResultV1(
                    status="failed",
                    stage="network_unreachable",
                    summary={"retry_allowed": True},
                ),
                now=self.now,
            ).applied
        )
        with session_factory(self.engine).begin() as session:
            stored = session.get(RunExecutionContext, parent.run_id)
            assert stored is not None
            tampered = dict(stored.context_json)
            tampered["application_version"] = "tampered-after-seal"
            stored.context_json = tampered

        with self.assertRaisesRegex(ScanAuthorizationError, "retry parent seal is invalid"):
            self.repository.create_run_with_context(
                job_type="ip_discovery",
                context=_context(
                    dry_run=True,
                    retry_parent_run_id=parent.run_id,
                ),
                run_id="run-retry-from-tampered-parent",
                now=self.now,
            )

    def test_wrong_digest_and_scope_are_rejected_without_a_run(self) -> None:
        authorization = self._authorization()
        drifted = _context(dry_run=False).model_copy(deep=True)
        drifted.engine_parameters["scan_contract_v1"]["packet_plan_sha256"] = "b" * 64
        with self.assertRaisesRegex(ScanAuthorizationError, "packet plan"):
            self.repository.create_run_with_context(
                job_type="ip_discovery",
                context=drifted,
                run_id="run-drifted",
                authorization_id=authorization.authorization_id,
                preview_run_id=self.preview_run_id,
                now=self.now,
            )
        with session_factory(self.engine)() as session:
            self.assertIsNone(session.get(Run, "run-drifted"))

    def test_concurrent_one_use_has_exactly_one_winner(self) -> None:
        authorization = self._authorization()

        def create(index: int) -> str:
            try:
                self._create_live(
                    authorization.authorization_id,
                    run_id=f"run-concurrent-{index}",
                )
            except ScanAuthorizationError:
                return "rejected"
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(create, range(2)))

        self.assertEqual(sorted(outcomes), ["created", "rejected"])
        with session_factory(self.engine)() as session:
            stored = session.get(ScanAuthorization, authorization.authorization_id)
            created = list(
                session.scalars(
                    select(Run).where(Run.id.like("run-concurrent-%"))
                )
            )
        self.assertEqual(stored.use_count, 1)
        self.assertEqual(len(created), 1)

    def test_revocation_or_queue_expiry_fails_claim_and_releases_slots(self) -> None:
        authorization = self._authorization()
        live = self._create_live(authorization.authorization_id, run_id="run-revoked")
        self.repository.revoke_scan_authorization(
            authorization.authorization_id,
            revoked_by="admin-a",
            reason="Change window withdrawn",
            now=self.now + timedelta(minutes=1),
        )

        lease = self.repository.claim_run(
            live.run_id,
            live.dispatch_id,
            now=self.now + timedelta(minutes=2),
        )
        self.assertIsNone(lease)
        with session_factory(self.engine)() as session:
            run = session.get(Run, live.run_id)
            slots = list(
                session.scalars(
                    select(ActiveProtocolSlot).where(
                        ActiveProtocolSlot.run_id == live.run_id
                    )
                )
            )
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "authorization_invalid")
        self.assertEqual(slots, [])

    def test_queue_delay_beyond_window_fails_without_claiming_or_traffic(self) -> None:
        authorization = self.repository.create_scan_authorization(
            preview_run_id=self.preview_run_id,
            approved_by="admin-a",
            ticket="CHG-1046",
            purpose="Short controlled window",
            not_before=self.now,
            not_after=self.now + timedelta(seconds=10),
            now=self.now,
        )
        live = self._create_live(authorization.authorization_id, run_id="run-expired")

        lease = self.repository.claim_run(
            live.run_id,
            live.dispatch_id,
            now=self.now + timedelta(seconds=11),
        )

        self.assertIsNone(lease)
        with session_factory(self.engine)() as session:
            run = session.get(Run, live.run_id)
            slots = list(
                session.scalars(
                    select(ActiveProtocolSlot).where(
                        ActiveProtocolSlot.run_id == live.run_id
                    )
                )
            )
        self.assertEqual((run.status, run.stage), ("failed", "authorization_invalid"))
        self.assertEqual(slots, [])


if __name__ == "__main__":
    unittest.main()
