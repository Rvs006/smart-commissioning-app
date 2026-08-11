"""Executor-owned active-control fencing for every outbound dispatch."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.models import (
    Run,
    RunExecutionContext,
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
from sqlalchemy import select

_DIGEST = "c" * 64
_PROJECT_ID = "project-active-control"
_SITE_ID = "site-active-control"
_USER_ID = "user-active-control"
_USERNAME = "engineer-active-control"


def _context(*, dry_run: bool) -> RunContextV1:
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
                    "resource_keys": ["nic:192.0.2.10"],
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
        original_factory = self.repository._session_factory

        class UnavailableStore:
            @staticmethod
            def begin():
                raise RuntimeError("control store unavailable")

        self.repository._session_factory = UnavailableStore()
        self.addCleanup(setattr, self.repository, "_session_factory", original_factory)
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
