"""Executor-owned active-control fencing for every outbound dispatch."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    Run,
    RunExecutionContext,
    RunResult,
    RunSeal,
    User,
    UserScopeGrant,
)
from smart_commissioning_core.db.run_lifecycle import (
    ActiveControlDeniedError,
    RunLifecycleRepository,
)
from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    Throttle,
    ThrottleConfig,
    run_engine_async,
)
from smart_commissioning_core.owned_run_store import OwnedRunStore
from smart_commissioning_core.run_context import RunContextV1, canonical_context_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import event, select

_DIGEST = "c" * 64
_PROJECT_ID = "project-active-control"
_SITE_ID = "site-active-control"
_USER_ID = "user-active-control"
_USERNAME = "engineer-active-control"


def _context(*, dry_run: bool, resource_key: str = "nic:192.0.2.10") -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": _PROJECT_ID,
            "site_id": _SITE_ID,
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [],
            "schema_versions": {},
            "engine_parameters": {
                "dry_run": dry_run,
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "packet_plan_sha256": _DIGEST,
                    "resource_keys": [resource_key],
                    "initiating_user_id": _USER_ID,
                    "principal_source": "user_key",
                    "deployment_role": "hub",
                },
            },
            "network_interface": "192.0.2.10/24",
            "connection_settings": {},
            "secret_references": {},
            # Display/audit text is deliberately different from the stable ID.
            "requesting_principal": _USERNAME,
            "application_version": "0.1.41",
            "protocol_key": None,
        }
    )


class ActiveControlRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temp_dir.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.repository = RunLifecycleRepository(self.engine)
        self.now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        self.preview_run_id = self._sealed_preview()
        self._seed_user_and_grant()
        self.authorization_id, self.live_run_id, self.owned = self._claimed_live_run()

    def _sealed_preview(self) -> str:
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=_context(dry_run=True),
            now=self.now,
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            now=self.now,
        )
        assert lease is not None
        outcome = self.repository.finalize_run(
            envelope.run_id,
            lease.owner_token,
            TerminalResultV1(
                status="succeeded",
                stage="dry_run_complete",
                summary={"preview": True},
            ),
            now=self.now,
        )
        self.assertTrue(outcome.applied)
        return envelope.run_id

    def _seed_user_and_grant(self) -> None:
        with session_factory(self.engine).begin() as session:
            session.add(
                User(
                    id=_USER_ID,
                    username=_USERNAME,
                    role="engineer",
                    api_key_hash="d" * 64,
                    is_active=True,
                    created_at=self.now,
                )
            )
            session.add(
                UserScopeGrant(
                    grant_id="grant-active-control",
                    user_id=_USER_ID,
                    project_id=_PROJECT_ID,
                    site_id=_SITE_ID,
                    active_marker=True,
                    granted_by="admin-one",
                    reason="Authorized plant-room commissioning",
                    granted_at=self.now,
                )
            )

    def _claimed_live_run(self) -> tuple[str, str, OwnedRunStore]:
        authorization = self.repository.create_scan_authorization(
            preview_run_id=self.preview_run_id,
            authorization_id="auth-active-control",
            approved_by="admin-one",
            ticket="CHG-4100",
            purpose="Controlled IP discovery",
            not_before=self.now,
            not_after=self.now + timedelta(seconds=10),
            now=self.now,
        )
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            context=_context(dry_run=False),
            run_id="run-active-control",
            authorization_id=authorization.authorization_id,
            preview_run_id=self.preview_run_id,
            now=self.now,
        )
        lease = self.repository.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            lease_seconds=60,
            now=self.now,
        )
        assert lease is not None
        return authorization.authorization_id, envelope.run_id, OwnedRunStore(
            self.repository, lease
        )

    def test_active_control_accepts_exact_owner_without_renewing_liveness(self) -> None:
        with session_factory(self.engine)() as session:
            before = session.get(Run, self.live_run_id)
            liveness = (
                before.heartbeat_at,
                before.lease_expires_at,
                before.updated_at,
                before.state_version,
            )

        self.owned.require_active_control(now=self.now + timedelta(seconds=5))

        with session_factory(self.engine)() as session:
            after = session.get(Run, self.live_run_id)
            self.assertEqual(
                (
                    after.heartbeat_at,
                    after.lease_expires_at,
                    after.updated_at,
                    after.state_version,
                ),
                liveness,
            )

    def test_control_reads_use_query_only_sessions(self) -> None:
        statements: list[str] = []

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _many: bool,
        ) -> None:
            statements.append(statement.strip().upper())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.assertFalse(self.owned.is_cancel_requested(self.live_run_id))
            self.owned.require_active_control(now=self.now + timedelta(seconds=1))
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertGreaterEqual(statements.count("PRAGMA QUERY_ONLY=ON"), 2)
        self.assertNotIn("BEGIN IMMEDIATE", statements)

    def test_held_active_control_reader_does_not_reserve_the_writer_lock(self) -> None:
        real_factory = self.repository._query_session_factory
        reader_is_held = threading.Event()
        release_reader = threading.Event()

        class HeldQuerySession:
            def __init__(self) -> None:
                self._context = real_factory()

            def __enter__(self):
                return self._context.__enter__()

            def __exit__(self, *args: object) -> object:
                reader_is_held.set()
                if not release_reader.wait(3):
                    raise TimeoutError("control reader was not released")
                return self._context.__exit__(*args)

        self.repository._query_session_factory = HeldQuerySession
        self.addCleanup(
            setattr,
            self.repository,
            "_query_session_factory",
            real_factory,
        )
        reader_errors: list[BaseException] = []

        def hold_control_read() -> None:
            try:
                self.repository.require_active_control(
                    self.live_run_id,
                    self.owned.lease.owner_token,
                    self.owned.lease.attempt,
                    now=self.now + timedelta(seconds=1),
                )
            except BaseException as error:  # pragma: no cover - asserted below
                reader_errors.append(error)

        reader = threading.Thread(target=hold_control_read)
        reader.start()
        self.assertTrue(reader_is_held.wait(3))

        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []

        def write_while_reader_is_held() -> None:
            try:
                with session_factory(self.engine).begin() as session:
                    session.get(Run, self.live_run_id).stage = "writer-was-not-blocked"
            except BaseException as error:  # pragma: no cover - asserted below
                writer_errors.append(error)
            finally:
                writer_finished.set()

        writer = threading.Thread(target=write_while_reader_is_held)
        writer.start()
        completed_while_held = writer_finished.wait(3)
        release_reader.set()
        reader.join(5)
        writer.join(5)

        self.assertTrue(completed_while_held)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertFalse(reader_errors, reader_errors)
        self.assertFalse(writer_errors, writer_errors)

    def test_owner_attempt_cancel_and_lease_are_checked_together(self) -> None:
        with self.assertRaisesRegex(ActiveControlDeniedError, "active control"):
            self.repository.require_active_control(
                self.live_run_id,
                "wrong-owner-token",
                self.owned.lease.attempt,
                now=self.now + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ActiveControlDeniedError, "active control"):
            self.repository.require_active_control(
                self.live_run_id,
                self.owned.lease.owner_token,
                self.owned.lease.attempt + 1,
                now=self.now + timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ActiveControlDeniedError, "active control"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=61))

        self.repository.request_cancel(
            self.live_run_id, now=self.now + timedelta(seconds=2)
        )
        with self.assertRaisesRegex(ActiveControlDeniedError, "active control"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=3))

    def test_revoked_authorization_denies_mid_run(self) -> None:
        self.repository.revoke_scan_authorization(
            self.authorization_id,
            revoked_by="admin-one",
            reason="Window withdrawn",
            now=self.now + timedelta(seconds=5),
        )
        with self.assertRaisesRegex(ActiveControlDeniedError, "authorization"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=6))

    def test_authorization_window_expiry_denies_mid_run(self) -> None:
        with self.assertRaisesRegex(ActiveControlDeniedError, "window has ended"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=11))

    def test_successful_discovery_seal_fails_after_authorization_revocation(self) -> None:
        """A last in-flight probe cannot turn a revoked run into success."""

        # This is the deterministic SQLite race seam: the engine's post-drain
        # read succeeds, then the approval is revoked before its success seal.
        self.owned.require_active_control(now=self.now + timedelta(seconds=4))
        self.repository.revoke_scan_authorization(
            self.authorization_id,
            revoked_by="admin-one",
            reason="Window withdrawn while the final probe was in flight",
            now=self.now + timedelta(seconds=5),
        )

        outcome = self.repository.finalize_discovery_run(
            self.live_run_id,
            self.owned.lease.owner_token,
            self.owned.lease.attempt,
            TerminalResultV1(status="succeeded", stage="engine_complete"),
            now=self.now + timedelta(seconds=6),
        )

        self.assertTrue(outcome.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, self.live_run_id)
            seal = session.get(RunSeal, self.live_run_id)
        self.assertEqual((result.terminal_status, seal.terminal_status), ("failed", "failed"))
        self.assertEqual(result.terminal_stage, "active_control_lost")
        self.assertEqual(result.summary["control_reason"], "authorization_revoked")
        self.assertIs(result.summary["provider_drained"], True)
        self.assertIs(result.summary["acceptance_eligible"], False)

    def test_successful_discovery_seal_fails_after_initiator_grant_revocation(self) -> None:
        """The terminal success fence locks and rechecks the active scope grant."""

        with session_factory(self.engine).begin() as session:
            grant = session.scalar(
                select(UserScopeGrant).where(UserScopeGrant.user_id == _USER_ID)
            )
            grant.active_marker = None
            grant.revoked_by = "admin-one"
            grant.revoke_reason = "Scope withdrawn while the final probe was in flight"
            grant.revoked_at = self.now + timedelta(seconds=5)

        outcome = self.repository.finalize_discovery_run(
            self.live_run_id,
            self.owned.lease.owner_token,
            self.owned.lease.attempt,
            TerminalResultV1(status="succeeded", stage="engine_complete"),
            now=self.now + timedelta(seconds=6),
        )

        self.assertTrue(outcome.applied)
        with session_factory(self.engine)() as session:
            result = session.get(RunResult, self.live_run_id)
        self.assertEqual(result.terminal_status, "failed")
        self.assertEqual(result.summary["control_reason"], "grant_revoked")
        self.assertIs(result.summary["acceptance_eligible"], False)

    def test_deactivated_user_or_revoked_grant_denies_mid_run(self) -> None:
        with session_factory(self.engine).begin() as session:
            user = session.get(User, _USER_ID)
            user.is_active = False
        with self.assertRaisesRegex(ActiveControlDeniedError, "initiating user"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=2))

        with session_factory(self.engine).begin() as session:
            user = session.get(User, _USER_ID)
            user.is_active = True
            grant = session.scalar(
                select(UserScopeGrant).where(UserScopeGrant.user_id == _USER_ID)
            )
            grant.active_marker = None
            grant.revoked_by = "admin-one"
            grant.revoke_reason = "Access removed"
            grant.revoked_at = self.now + timedelta(seconds=3)
        with self.assertRaisesRegex(ActiveControlDeniedError, "scope grant"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=4))

    def _claimed_live_run_without_linkage(self) -> OwnedRunStore:
        # A live run with no admin-approval linkage (authorization_id and
        # preview_run_id both None). Legal to create; the active-control fence
        # normally denies it as "linkage is missing".
        envelope = self.repository.create_run_with_context(
            job_type="ip_discovery",
            # Distinct protocol key so this run does not conflict with the linked
            # live run held by setUp.
            context=_context(dry_run=False, resource_key="nic:198.51.100.10"),
            run_id="run-frictionless",
            now=self.now,
        )
        lease = self.repository.claim_run(
            envelope.run_id, envelope.dispatch_id, lease_seconds=60, now=self.now
        )
        assert lease is not None
        return OwnedRunStore(self.repository, lease)

    def test_missing_linkage_denied_when_authorization_enforced(self) -> None:
        owned = self._claimed_live_run_without_linkage()
        with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": "1"}):
            with self.assertRaisesRegex(ActiveControlDeniedError, "linkage is missing"):
                owned.require_active_control(now=self.now + timedelta(seconds=1))

    def test_missing_linkage_allowed_when_frictionless(self) -> None:
        owned = self._claimed_live_run_without_linkage()
        with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": "0"}):
            # No approval row, but the initiator (active user + active grant) is
            # valid, so the run keeps control.
            owned.require_active_control(now=self.now + timedelta(seconds=1))

    def test_frictionless_still_denies_a_revoked_initiator(self) -> None:
        # Removing the approval must NOT remove the identity fence: a deactivated
        # initiator is still denied even in a frictionless deployment.
        owned = self._claimed_live_run_without_linkage()
        with session_factory(self.engine).begin() as session:
            session.get(User, _USER_ID).is_active = False
        with patch.dict(os.environ, {"SCT_REQUIRE_SCAN_AUTHORIZATION": "0"}):
            with self.assertRaisesRegex(ActiveControlDeniedError, "initiating user"):
                owned.require_active_control(now=self.now + timedelta(seconds=1))

    def test_demoted_named_engineer_cannot_retain_active_control(self) -> None:
        self.owned.require_active_control(now=self.now + timedelta(seconds=1))

        for role in ("reviewer", "viewer"):
            with self.subTest(role=role):
                with session_factory(self.engine).begin() as session:
                    session.get(User, _USER_ID).role = role

                with self.assertRaisesRegex(ActiveControlDeniedError, "engineer or admin"):
                    self.owned.require_active_control(now=self.now + timedelta(seconds=2))

                with session_factory(self.engine).begin() as session:
                    session.get(User, _USER_ID).role = "engineer"

    def test_noncanonical_named_user_role_fails_closed(self) -> None:
        for role in ("ENGINEER", "engineer ", " ADMIN "):
            with self.subTest(role=role):
                with session_factory(self.engine).begin() as session:
                    session.get(User, _USER_ID).role = role

                with self.assertRaisesRegex(ActiveControlDeniedError, "engineer or admin"):
                    self.owned.require_active_control(now=self.now + timedelta(seconds=2))

                with session_factory(self.engine).begin() as session:
                    session.get(User, _USER_ID).role = "engineer"

    def test_named_admin_is_global_but_must_remain_active(self) -> None:
        with session_factory(self.engine).begin() as session:
            user = session.get(User, _USER_ID)
            user.role = "admin"
            grant = session.scalar(
                select(UserScopeGrant).where(UserScopeGrant.user_id == _USER_ID)
            )
            grant.active_marker = None
            grant.revoked_by = "admin-two"
            grant.revoke_reason = "Global role makes this grant unnecessary"
            grant.revoked_at = self.now + timedelta(seconds=1)
        self.owned.require_active_control(now=self.now + timedelta(seconds=2))

        with session_factory(self.engine).begin() as session:
            session.get(User, _USER_ID).is_active = False
        with self.assertRaisesRegex(ActiveControlDeniedError, "initiating user"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=3))

    def test_missing_stable_named_user_identity_fails_closed(self) -> None:
        context = _context(dry_run=False).model_copy(deep=True)
        del context.engine_parameters["scan_contract_v1"]["initiating_user_id"]
        with session_factory(self.engine).begin() as session:
            stored = session.get(RunExecutionContext, self.live_run_id)
            stored.context_json = context.model_dump(mode="json")
            stored.context_sha256 = canonical_context_sha256(context)
        with self.assertRaisesRegex(ActiveControlDeniedError, "stable initiating user"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=2))

    def test_context_identity_tamper_fails_integrity_check(self) -> None:
        context = _context(dry_run=False).model_copy(deep=True)
        context.engine_parameters["scan_contract_v1"]["initiating_user_id"] = "attacker"
        with session_factory(self.engine).begin() as session:
            stored = session.get(RunExecutionContext, self.live_run_id)
            stored.context_json = context.model_dump(mode="json")
        with self.assertRaisesRegex(ActiveControlDeniedError, "integrity"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=2))

    def test_synthetic_principal_requires_frozen_standalone_role(self) -> None:
        context = _context(dry_run=False).model_copy(deep=True)
        contract = context.engine_parameters["scan_contract_v1"]
        contract["initiating_user_id"] = None
        contract["principal_source"] = "local"
        contract["deployment_role"] = "hub"
        with session_factory(self.engine).begin() as session:
            row = session.get(Run, self.live_run_id)
            row.parameters = dict(context.engine_parameters)
            stored = session.get(RunExecutionContext, self.live_run_id)
            stored.context_json = context.model_dump(mode="json")
            stored.context_sha256 = canonical_context_sha256(context)

        with self.assertRaisesRegex(ActiveControlDeniedError, "standalone"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=2))

        contract["deployment_role"] = "standalone"
        with session_factory(self.engine).begin() as session:
            stored = session.get(RunExecutionContext, self.live_run_id)
            stored.context_json = context.model_dump(mode="json")
            stored.context_sha256 = canonical_context_sha256(context)
        self.owned.require_active_control(now=self.now + timedelta(seconds=3))

    def test_control_store_error_propagates_instead_of_authorizing(self) -> None:
        original_factory = self.repository._query_session_factory

        class UnavailableStore:
            def __init__(self) -> None:
                raise RuntimeError("control store unavailable")

        self.repository._query_session_factory = UnavailableStore
        self.addCleanup(
            setattr,
            self.repository,
            "_query_session_factory",
            original_factory,
        )
        with self.assertRaisesRegex(RuntimeError, "control store unavailable"):
            self.owned.require_active_control(now=self.now + timedelta(seconds=2))


class _MemoryRunStore:
    def __init__(self, *, active_control_error: Exception | None = None) -> None:
        self.active_control_error = active_control_error
        self.active_control_calls = 0
        self.status = "queued"

    def require_active_control(self) -> None:
        self.active_control_calls += 1
        if self.active_control_error is not None:
            raise self.active_control_error

    def update_run_status(self, _run_id: str, *, status: str, **_kwargs: Any) -> dict[str, Any]:
        self.status = status
        return {"status": status}

    def update_result_summary(self, _run_id: str, _summary: dict[str, Any]) -> None:
        return None

    def replace_issues(self, _run_id: str, _issues: list[Any]) -> None:
        return None


class ActiveControlThrottleTests(unittest.TestCase):
    @staticmethod
    def _ctx(store: _MemoryRunStore) -> EngineContext:
        return EngineContext(
            run_id="run-throttle-active-control",
            parameters={},
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=4, rate_limit_per_sec=None),
            dry_run=False,
        )

    def test_run_throttled_fails_closed_before_factory_on_store_error(self) -> None:
        store = _MemoryRunStore(active_control_error=RuntimeError("control store down"))
        ctx = self._ctx(store)
        contacted: list[int] = []

        def factory(value: int):
            async def unit() -> int:
                contacted.append(value)
                return value

            return unit

        with self.assertRaisesRegex(RuntimeError, "control store down"):
            asyncio.run(
                Throttle(ctx.throttle).run_throttled(
                    [factory(value) for value in range(5)], ctx
                )
            )
        self.assertEqual(contacted, [])
        self.assertEqual(store.active_control_calls, 1)

    def test_run_throttled_checks_each_scheduled_outbound_unit(self) -> None:
        store = _MemoryRunStore()
        ctx = self._ctx(store)
        contacted: list[int] = []

        def factory(value: int):
            async def unit() -> int:
                contacted.append(value)
                return value

            return unit

        results = asyncio.run(
            Throttle(ctx.throttle).run_throttled(
                [factory(value) for value in range(5)], ctx
            )
        )
        self.assertEqual(results, list(range(5)))
        self.assertEqual(contacted, list(range(5)))
        self.assertEqual(store.active_control_calls, 5)

    def test_direct_throttle_slot_uses_the_current_engine_owner(self) -> None:
        store = _MemoryRunStore(active_control_error=RuntimeError("revoked"))
        ctx = self._ctx(store)
        contacted: list[str] = []

        async def engine(_ctx: EngineContext) -> EngineResult:
            async with Throttle(ctx.throttle).slot():
                contacted.append("packet")
            return EngineResult()

        with self.assertLogs(
            "smart_commissioning_core.engines.base", level="ERROR"
        ):
            result = asyncio.run(run_engine_async(ctx, engine))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.status, "failed")
        self.assertEqual(contacted, [])
        self.assertEqual(store.active_control_calls, 1)


if __name__ == "__main__":
    unittest.main()
