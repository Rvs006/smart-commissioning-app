"""Transactional lifecycle-v2 repository for SQLite and Postgres."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smart_commissioning_core.db.db_run_store import (
    get_or_create_project_and_site,
    new_run_id,
)
from smart_commissioning_core.db.engine import query_session_factory, session_factory
from smart_commissioning_core.db.models import (
    RUN_DISCOVERY_OBSERVATION_PAYLOAD_MAX_BYTES,
    ActiveProtocolSlot,
    DiscoveredDevice,
    DiscoveredPoint,
    DiscoveredTopic,
    Run,
    RunDiscoveryObservation,
    RunDiscoveryObservationState,
    RunDispatch,
    RunExecutionContext,
    RunIdempotencyKey,
    RunIssue,
    RunLifecycleConflict,
    RunLink,
    RunResult,
    RunSeal,
    ScanAuthorization,
    User,
    UserScopeGrant,
)
from smart_commissioning_core.discovery_observations import (
    OBSERVATION_STREAM_EMPTY_SHA256,
    DiscoveryObservationFoldV1,
    DiscoveryObservationInputV1,
    DiscoveryObservationViewV1,
    ObservationAppendOutcomeV1,
    ObservationCutoffV1,
    extend_observation_stream_sha256,
    fold_discovery_observations,
    observation_payload,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import (
    RunContextV1,
    canonical_context_sha256,
    canonical_json_bytes,
    json_safe_value,
)
from smart_commissioning_core.run_lifecycle import (
    CancelOutcomeV1,
    DispatchEnvelopeV1,
    FinalizeOutcome,
    RunDispatchV1,
    RunLeaseV1,
    RunSealViewV1,
    ScanAuthorizationV1,
    StoredRunContextV1,
    TerminalResultV1,
)

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
DISCOVERY_OBSERVATION_FOLD_MAX_ROWS = 50_000
DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
DISCOVERY_OBSERVATION_QUERY_PAGE_MAX_BYTES = 384 * 1024
_DISCOVERY_PROTOCOL_BY_JOB_TYPE = MappingProxyType(
    {
        "ip_discovery": "ip",
        "bacnet_discovery": "bacnet",
    }
)


@dataclass(frozen=True)
class _DiscoveryObservationStateSnapshot:
    run_id: str
    attempt: int
    observation_count: int
    canonical_payload_bytes: int
    terminal_cursor: int
    observation_stream_sha256: str
    observation_row_limit: int
    observation_payload_byte_limit: int
    sealed_observation_budget: bool

    def cutoff(self) -> ObservationCutoffV1:
        return ObservationCutoffV1(
            run_id=self.run_id,
            attempt=self.attempt,
            terminal_cursor=self.terminal_cursor,
            observation_count=self.observation_count,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _resource_keys(context: RunContextV1) -> tuple[str, ...]:
    """Return the deterministic active-resource set frozen into one context."""

    keys: set[str] = set()
    scan_contract = context.engine_parameters.get("scan_contract_v1")
    if isinstance(scan_contract, Mapping):
        raw_keys = scan_contract.get("resource_keys", ())
        if not isinstance(raw_keys, (list, tuple)):
            raise ValueError("scan_contract_v1.resource_keys must be a list")
        for raw_key in raw_keys:
            if not isinstance(raw_key, str):
                raise ValueError("scan_contract_v1.resource_keys must contain strings")
            key = raw_key.strip()
            if not key:
                raise ValueError("scan_contract_v1.resource_keys must not contain blanks")
            if len(key) > 80:
                raise ValueError("scan_contract_v1.resource_keys entries cannot exceed 80 characters")
            keys.add(key)
    if context.protocol_key is not None:
        keys.add(context.protocol_key)
    dry_run = context.engine_parameters.get("dry_run")
    if isinstance(dry_run, str):
        is_dry_run = dry_run.strip().casefold() in {"1", "true", "yes", "on"}
    else:
        is_dry_run = bool(dry_run)
    if is_dry_run:
        return ()
    return tuple(sorted(keys))


class ProtocolConflictError(RuntimeError):
    """Raised before dispatch when a canonical protocol key is already reserved."""

    def __init__(self, protocol_key: str, active_run_id: str) -> None:
        self.protocol_key = protocol_key
        self.active_run_id = active_run_id
        super().__init__(f"protocol key is active for run {active_run_id}")


class ScanAuthorizationError(ValueError):
    """A live run could not consume or continue under its sealed approval."""


class IdempotencyConflictError(ValueError):
    """A retry key was reused with a request that has different semantics."""


class ActiveControlDeniedError(RuntimeError):
    """The current executor may not schedule another outbound attempt."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"active control denied: {reason}")


def _active_control_loss_reason(error: ActiveControlDeniedError) -> str:
    """Translate the shared control policy's stable denials into evidence."""

    text = str(error).casefold()
    if "authorization" in text and "revok" in text:
        return "authorization_revoked"
    if "authorization" in text and ("ended" in text or "expir" in text):
        return "authorization_expired"
    if "grant" in text or "initiating user" in text or "engineer or admin" in text:
        return "grant_revoked"
    if "cancel" in text:
        return "stop_requested"
    if "owner" in text or "attempt" in text or "lease" in text:
        return "ownership_lost"
    return "control_store_error"


class DiscoveryObservationConflictError(RuntimeError):
    """A provisional observation failed its lifecycle or identity fence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"discovery observation rejected: {reason}")


class DiscoveryObservationIntegrityError(RuntimeError):
    """Stored observation evidence no longer matches its public commitment."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"discovery observation integrity check failed: {reason}")


class DiscoveryObservationFoldLimitError(RuntimeError):
    """A terminal fold exceeded its frozen in-process safety budget."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"discovery observation fold rejected: {reason}")


def _scan_contract(context: RunContextV1) -> Mapping[str, Any]:
    value = context.engine_parameters.get("scan_contract_v1")
    if not isinstance(value, Mapping):
        raise ScanAuthorizationError("sealed preview is missing scan_contract_v1")
    return value


def _packet_plan_sha256(context: RunContextV1) -> str:
    value = str(_scan_contract(context).get("packet_plan_sha256") or "").strip().lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ScanAuthorizationError("sealed preview has an invalid packet plan digest")
    return value


def _idempotency_metadata(
    *,
    requesting_principal: str | None,
    operation: str | None,
    idempotency_key: str | None,
    request_sha256: str | None,
) -> tuple[str, str, str, str] | None:
    """Validate a complete retry identity or preserve the unkeyed legacy path."""

    values = (requesting_principal, operation, idempotency_key, request_sha256)
    if values == (None, None, None, None):
        return None
    if any(value is None for value in values):
        raise ValueError("idempotency metadata must be supplied together")
    principal = str(requesting_principal).strip()
    normalized_operation = str(operation).strip()
    normalized_key = str(idempotency_key).strip()
    normalized_sha256 = str(request_sha256).strip().lower()
    if (
        not principal
        or not normalized_operation
        or not normalized_key
        or len(principal) > 255
        or len(normalized_operation) > 64
        or len(normalized_key) > 255
    ):
        raise ValueError("idempotency metadata is invalid")
    if (
        len(normalized_sha256) != 64
        or any(character not in "0123456789abcdef" for character in normalized_sha256)
    ):
        raise ValueError("idempotency request fingerprint is invalid")
    return principal, normalized_operation, normalized_key, normalized_sha256


def _is_dry_context(context: RunContextV1) -> bool:
    value = context.engine_parameters.get("dry_run")
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _sealed_ip_observation_budget(
    context: RunContextV1,
    *,
    job_type: str,
) -> tuple[int, int] | None:
    """Return one modern IP plan's immutable row and payload limits.

    Historical discovery contexts and non-IP jobs retain the lifecycle-wide
    fold ceilings. Once an IP context carries an observation budget, malformed
    values fail closed instead of silently widening back to those global caps.
    """

    if job_type != "ip_discovery":
        return None
    contract = context.engine_parameters.get("scan_contract_v1")
    if not isinstance(contract, Mapping) or contract.get("job_type") != "ip_discovery":
        return None
    ip_contract = contract.get("ip")
    if not isinstance(ip_contract, Mapping):
        return None
    raw_budget = ip_contract.get("observation_budget")
    if raw_budget is None:
        return None
    if not isinstance(raw_budget, Mapping):
        raise ValueError("sealed IP observation budget is invalid")
    row_limit = raw_budget.get("planned_observation_rows")
    payload_limit = raw_budget.get("planned_observation_payload_bytes")
    if (
        type(row_limit) is not int
        or row_limit <= 0
        or type(payload_limit) is not int
        or payload_limit <= 0
    ):
        raise ValueError("sealed IP observation budget is invalid")
    return row_limit, payload_limit


def _required_authorization_window_seconds(context: RunContextV1) -> float:
    """Read the conservative profile bound from a versioned discovery contract."""
    contract = _scan_contract(context)
    for protocol in ("ip", "bacnet"):
        protocol_contract = contract.get(protocol)
        if not isinstance(protocol_contract, Mapping):
            continue
        estimate = protocol_contract.get("work_estimate")
        if not isinstance(estimate, Mapping):
            continue
        raw_value = estimate.get("required_authorization_window_seconds")
        if raw_value in (None, ""):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as error:
            raise ScanAuthorizationError("sealed preview has an invalid authorization-window estimate") from error
        if not math.isfinite(value) or value <= 0:
            raise ScanAuthorizationError("sealed preview has an invalid authorization-window estimate")
        return value
    return 0.0


def _retry_parent_run_id(context: RunContextV1) -> str | None:
    contract = context.engine_parameters.get("scan_contract_v1")
    if contract is None:
        return None
    if not isinstance(contract, Mapping):
        raise ScanAuthorizationError("scan contract has an invalid relation snapshot")
    snapshot = contract.get("relation_snapshot")
    if snapshot is None:
        return None
    # Property-expansion children have their own strict reader below.  Let that
    # reader validate its sealed relation instead of treating it as a malformed
    # retry before child creation can reach the property-parent check.
    if isinstance(snapshot, Mapping) and snapshot.get("relation") == "property_expansion":
        return None
    if not isinstance(snapshot, Mapping) or snapshot.get("relation") != "retry":
        raise ScanAuthorizationError("scan contract has an invalid relation snapshot")
    parent_run_id = str(snapshot.get("parent_run_id") or "").strip()
    if not parent_run_id or len(parent_run_id) > 64:
        raise ScanAuthorizationError("scan contract has an invalid retry parent")
    if set(snapshot) != {"relation", "parent_run_id"}:
        raise ScanAuthorizationError("scan contract has an invalid relation snapshot")
    return parent_run_id


def _property_parent_run_id(context: RunContextV1) -> str | None:
    """Read the parent id for a sealed BACnet property-expansion child."""

    contract = context.engine_parameters.get("scan_contract_v1")
    if contract is None:
        return None
    if not isinstance(contract, Mapping):
        raise ScanAuthorizationError("scan contract has an invalid relation snapshot")
    snapshot = contract.get("relation_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("relation") != "property_expansion":
        return None
    parent_run_id = str(snapshot.get("parent_run_id") or "").strip()
    if not parent_run_id or len(parent_run_id) > 64 or set(snapshot) != {"relation", "parent_run_id"}:
        raise ScanAuthorizationError("scan contract has an invalid property parent")
    return parent_run_id


class RunLifecycleRepository:
    """Own context, outbox, lease, protocol slot, finalization, and recovery."""

    def __init__(
        self,
        engine: Engine,
        *,
        fault_injector: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)
        self._query_session_factory = query_session_factory(engine)
        self._fault_injector = fault_injector
        self._clock = clock or _utcnow

    # -- sealed preview authorizations -------------------------------------

    def create_scan_authorization(
        self,
        *,
        preview_run_id: str,
        approved_by: str,
        ticket: str,
        purpose: str,
        not_before: datetime,
        not_after: datetime,
        authorization_id: str | None = None,
        now: datetime | None = None,
    ) -> ScanAuthorizationV1:
        authorization_id = authorization_id or f"auth_{uuid4().hex}"
        approved_by = approved_by.strip()
        ticket = ticket.strip()
        purpose = purpose.strip()
        if not approved_by:
            raise ScanAuthorizationError("scan authorization requires an approver")
        if not ticket:
            raise ScanAuthorizationError("scan authorization requires a change ticket")
        if not purpose:
            raise ScanAuthorizationError("scan authorization requires a purpose")
        if len(authorization_id) > 64 or len(approved_by) > 255 or len(ticket) > 255:
            raise ScanAuthorizationError("scan authorization metadata exceeds its size limit")
        created_at = now or _utcnow()
        if not_before.tzinfo is None or not_after.tzinfo is None:
            raise ScanAuthorizationError("scan authorization window must include a timezone")
        if not_after <= not_before:
            raise ScanAuthorizationError("scan authorization end must be after its start")
        if not_after <= created_at:
            raise ScanAuthorizationError("scan authorization window has already ended")

        with self._session_factory.begin() as session:
            preview, context, result, seal = self._load_sealed_preview(session, preview_run_id)
            required_window = _required_authorization_window_seconds(context)
            actual_window = (not_after - not_before).total_seconds()
            if actual_window < required_window:
                raise ScanAuthorizationError(
                    "scan authorization window is shorter than the sealed preview "
                    f"requires ({required_window:g} seconds)"
                )
            packet_digest = _packet_plan_sha256(context)
            row = ScanAuthorization(
                authorization_id=authorization_id,
                preview_run_id=preview_run_id,
                project_id=preview.project_id,
                site_id=preview.site_id,
                packet_plan_sha256=packet_digest,
                approved_by=approved_by,
                ticket=ticket,
                purpose=purpose,
                not_before=not_before,
                not_after=not_after,
                max_uses=1,
                use_count=0,
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return self._scan_authorization_view(row)

    def get_scan_authorization(self, authorization_id: str) -> ScanAuthorizationV1:
        with self._session_factory() as session:
            row = session.get(ScanAuthorization, authorization_id)
            if row is None:
                raise FileNotFoundError(authorization_id)
            return self._scan_authorization_view(row)

    def list_scan_authorizations(
        self,
        *,
        project_id: str | None = None,
        site_id: str | None = None,
        preview_run_id: str | None = None,
    ) -> list[ScanAuthorizationV1]:
        statement = select(ScanAuthorization)
        if project_id is not None:
            statement = statement.where(ScanAuthorization.project_id == project_id)
        if site_id is not None:
            statement = statement.where(ScanAuthorization.site_id == site_id)
        if preview_run_id is not None:
            statement = statement.where(ScanAuthorization.preview_run_id == preview_run_id)
        statement = statement.order_by(
            ScanAuthorization.created_at.desc(),
            ScanAuthorization.authorization_id.desc(),
        )
        with self._session_factory() as session:
            return [self._scan_authorization_view(row) for row in session.scalars(statement).all()]

    def revoke_scan_authorization(
        self,
        authorization_id: str,
        *,
        revoked_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> ScanAuthorizationV1:
        revoked_by = revoked_by.strip()
        reason = reason.strip()
        if not revoked_by or not reason:
            raise ScanAuthorizationError("revocation requires an actor and reason")
        with self._session_factory.begin() as session:
            row = session.scalar(
                select(ScanAuthorization)
                .where(ScanAuthorization.authorization_id == authorization_id)
                .with_for_update()
            )
            if row is None:
                raise FileNotFoundError(authorization_id)
            if row.revoked_at is None:
                row.revoked_at = now or _utcnow()
                row.revoked_by = revoked_by
                row.revoke_reason = reason
                session.flush()
            return self._scan_authorization_view(row)

    @staticmethod
    def _scan_authorization_view(row: ScanAuthorization) -> ScanAuthorizationV1:
        return ScanAuthorizationV1(
            authorization_id=row.authorization_id,
            preview_run_id=row.preview_run_id,
            project_id=row.project_id,
            site_id=row.site_id,
            packet_plan_sha256=row.packet_plan_sha256,
            approved_by=row.approved_by,
            ticket=row.ticket,
            purpose=row.purpose,
            not_before=row.not_before,
            not_after=row.not_after,
            max_uses=row.max_uses,
            use_count=row.use_count,
            consumed_run_id=row.consumed_run_id,
            revoked_at=row.revoked_at,
            revoked_by=row.revoked_by,
            revoke_reason=row.revoke_reason,
            created_at=row.created_at,
        )

    @staticmethod
    def _load_sealed_preview(
        session: Session,
        preview_run_id: str,
    ) -> tuple[Run, RunContextV1, RunResult, RunSeal]:
        preview = session.get(Run, preview_run_id)
        context_row = session.get(RunExecutionContext, preview_run_id)
        result = session.get(RunResult, preview_run_id)
        seal = session.get(RunSeal, preview_run_id)
        if preview is None or context_row is None or result is None or seal is None:
            raise ScanAuthorizationError("scan authorization requires a terminal sealed preview")
        context = RunContextV1.model_validate(context_row.context_json)
        if not _is_dry_context(context):
            raise ScanAuthorizationError("authorization source run is not a dry preview")
        if preview.status != "succeeded" or result.terminal_status != "succeeded":
            raise ScanAuthorizationError("authorization source preview did not succeed")
        if (
            canonical_context_sha256(context) != context_row.context_sha256
            or seal.context_sha256 != context_row.context_sha256
        ):
            raise ScanAuthorizationError("authorization source preview seal is invalid")
        try:
            actual_result_sha256 = TerminalResultV1.model_validate(result.result_payload).sha256()
        except Exception as error:
            raise ScanAuthorizationError("authorization source preview result is invalid") from error
        if actual_result_sha256 != result.result_sha256 or seal.result_sha256 != result.result_sha256:
            raise ScanAuthorizationError("authorization source preview seal is invalid")
        return preview, context, result, seal

    # -- run/context/outbox -------------------------------------------------

    def find_idempotency_replay(
        self,
        *,
        project_id: str,
        site_id: str,
        requesting_principal: str,
        operation: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> DispatchEnvelopeV1 | None:
        """Return the canonical run for a prior keyed submission, if any.

        This read is intentionally usable before request-specific live-scan
        checks. A legitimate retry must still resolve after its one-use scan
        authorization was consumed by the original creation.
        """

        metadata = _idempotency_metadata(
            requesting_principal=requesting_principal,
            operation=operation,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        assert metadata is not None
        with self._query_session_factory() as session:
            return self._idempotency_replay_in_session(
                session,
                project_id=project_id,
                site_id=site_id,
                requesting_principal=metadata[0],
                operation=metadata[1],
                idempotency_key=metadata[2],
                request_sha256=metadata[3],
            )

    @staticmethod
    def _idempotency_replay_in_session(
        session: Session,
        *,
        project_id: str,
        site_id: str,
        requesting_principal: str,
        operation: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> DispatchEnvelopeV1 | None:
        row = session.scalar(
            select(RunIdempotencyKey).where(
                RunIdempotencyKey.requesting_principal == requesting_principal,
                RunIdempotencyKey.project_id == project_id,
                RunIdempotencyKey.site_id == site_id,
                RunIdempotencyKey.operation == operation,
                RunIdempotencyKey.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        if row.request_sha256 != request_sha256:
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different request."
            )
        run = session.get(Run, row.run_id)
        context = session.get(RunExecutionContext, row.run_id)
        dispatch = session.scalar(select(RunDispatch).where(RunDispatch.run_id == row.run_id))
        if (
            run is None
            or context is None
            or dispatch is None
            or run.project_id != project_id
            or run.site_id != site_id
        ):
            raise RuntimeError("idempotency record has no complete canonical run")
        return DispatchEnvelopeV1(
            run_id=row.run_id,
            dispatch_id=dispatch.dispatch_id,
            context_sha256=context.context_sha256,
            replayed=True,
        )

    def create_run_with_context(
        self,
        *,
        job_type: str,
        context: RunContextV1 | Mapping[str, Any],
        execution_mode: str | None = None,
        edge_id: str | None = None,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        authorization_id: str | None = None,
        preview_run_id: str | None = None,
        parent_run_id: str | None = None,
        relation: str | None = None,
        idempotency_principal: str | None = None,
        idempotency_operation: str | None = None,
        idempotency_key: str | None = None,
        idempotency_request_sha256: str | None = None,
        now: datetime | None = None,
    ) -> DispatchEnvelopeV1:
        captured = context if isinstance(context, RunContextV1) else RunContextV1.model_validate(context)
        idempotency = _idempotency_metadata(
            requesting_principal=idempotency_principal,
            operation=idempotency_operation,
            idempotency_key=idempotency_key,
            request_sha256=idempotency_request_sha256,
        )
        if idempotency is not None and captured.requesting_principal != idempotency[0]:
            raise ValueError("idempotency principal must match the captured run context")
        created_at = now or _utcnow()
        actual_run_id = run_id or new_run_id(created_at)
        actual_dispatch_id = dispatch_id or f"dispatch_{uuid4().hex}"
        context_sha256 = canonical_context_sha256(captured)
        resource_keys = _resource_keys(captured)
        retry_parent_run_id = _retry_parent_run_id(captured)
        property_parent_run_id = _property_parent_run_id(captured)
        if parent_run_id is not None:
            parent_run_id = str(parent_run_id).strip()
            if relation not in {"property_expansion", "retry"}:
                raise ScanAuthorizationError("run parent relation is invalid")
            expected_parent = (
                property_parent_run_id if relation == "property_expansion" else retry_parent_run_id
            )
            if expected_parent != parent_run_id:
                raise ScanAuthorizationError("run parent relation does not match its frozen context")
        elif property_parent_run_id is not None:
            parent_run_id = property_parent_run_id
            relation = "property_expansion"
        if (authorization_id is None) != (preview_run_id is None):
            raise ScanAuthorizationError("live creation requires both authorization_id and preview_run_id")
        if authorization_id is not None and _is_dry_context(captured):
            raise ScanAuthorizationError("a dry preview cannot consume an authorization")
        try:
            with self._session_factory.begin() as session:
                if idempotency is not None:
                    replay = self._idempotency_replay_in_session(
                        session,
                        project_id=captured.project_id,
                        site_id=captured.site_id,
                        requesting_principal=idempotency[0],
                        operation=idempotency[1],
                        idempotency_key=idempotency[2],
                        request_sha256=idempotency[3],
                    )
                    if replay is not None:
                        return replay
                get_or_create_project_and_site(session, captured.project_id, captured.site_id)
                authorization: ScanAuthorization | None = None
                if authorization_id is not None and preview_run_id is not None:
                    authorization = self._validate_authorization_for_create(
                        session,
                        authorization_id=authorization_id,
                        preview_run_id=preview_run_id,
                        context=captured,
                        now=created_at,
                    )
                if retry_parent_run_id is not None:
                    self._validate_retry_parent(
                        session,
                        parent_run_id=retry_parent_run_id,
                        context=captured,
                        job_type=job_type,
                    )
                if property_parent_run_id is not None:
                    self._validate_property_parent(
                        session,
                        parent_run_id=property_parent_run_id,
                        context=captured,
                        job_type=job_type,
                    )
                session.add(
                    Run(
                        id=actual_run_id,
                        project_id=captured.project_id,
                        site_id=captured.site_id,
                        job_type=job_type,
                        status="queued",
                        stage="awaiting_dispatch",
                        progress_percent=0,
                        parameters=dict(captured.engine_parameters),
                        result_summary={},
                        execution_mode=execution_mode,
                        edge_id=edge_id,
                        error_message=None,
                        cancel_requested=False,
                        attempt=0,
                        state_version=0,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                )
                if idempotency is not None:
                    session.add(
                        RunIdempotencyKey(
                            run_id=actual_run_id,
                            requesting_principal=idempotency[0],
                            project_id=captured.project_id,
                            site_id=captured.site_id,
                            operation=idempotency[1],
                            idempotency_key=idempotency[2],
                            request_sha256=idempotency[3],
                            created_at=created_at,
                        )
                    )
                session.flush()
                if authorization is not None:
                    consumed = session.execute(
                        update(ScanAuthorization)
                        .where(
                            ScanAuthorization.authorization_id == authorization.authorization_id,
                            ScanAuthorization.use_count == 0,
                            ScanAuthorization.consumed_run_id.is_(None),
                            ScanAuthorization.revoked_at.is_(None),
                            ScanAuthorization.not_before <= created_at,
                            ScanAuthorization.not_after > created_at,
                        )
                        .values(
                            use_count=1,
                            consumed_run_id=actual_run_id,
                        )
                    )
                    if consumed.rowcount != 1:
                        raise ScanAuthorizationError("scan authorization was already used, revoked, or expired")
                    session.add(
                        RunLink(
                            parent_run_id=preview_run_id,
                            child_run_id=actual_run_id,
                            relation="preview",
                            authorization_id=authorization.authorization_id,
                            created_at=created_at,
                        )
                    )
                if retry_parent_run_id is not None:
                    session.add(
                        RunLink(
                            parent_run_id=retry_parent_run_id,
                            child_run_id=actual_run_id,
                            relation="retry",
                            authorization_id=(authorization.authorization_id if authorization is not None else None),
                            created_at=created_at,
                        )
                    )
                session.add(
                    RunExecutionContext(
                        run_id=actual_run_id,
                        schema_version=captured.schema_version,
                        context_json=captured.model_dump(mode="json"),
                        context_sha256=context_sha256,
                        created_at=created_at,
                    )
                )
                if property_parent_run_id is not None:
                    session.add(
                        RunLink(
                            parent_run_id=property_parent_run_id,
                            child_run_id=actual_run_id,
                            relation="property_expansion",
                            authorization_id=(authorization.authorization_id if authorization is not None else None),
                            created_at=created_at,
                        )
                    )
                session.add(
                    RunDispatch(
                        dispatch_id=actual_dispatch_id,
                        run_id=actual_run_id,
                        state="pending",
                        publish_attempts=0,
                        created_at=created_at,
                    )
                )
                for protocol_key in resource_keys:
                    session.add(
                        ActiveProtocolSlot(
                            protocol_key=protocol_key,
                            run_id=actual_run_id,
                            owner_token=None,
                            acquired_at=created_at,
                        )
                    )
                session.flush()
        except ScanAuthorizationError:
            if idempotency is not None:
                replay = self.find_idempotency_replay(
                    project_id=captured.project_id,
                    site_id=captured.site_id,
                    requesting_principal=idempotency[0],
                    operation=idempotency[1],
                    idempotency_key=idempotency[2],
                    request_sha256=idempotency[3],
                )
                if replay is not None:
                    return replay
            raise
        except IntegrityError as error:
            if idempotency is not None:
                replay = self.find_idempotency_replay(
                    project_id=captured.project_id,
                    site_id=captured.site_id,
                    requesting_principal=idempotency[0],
                    operation=idempotency[1],
                    idempotency_key=idempotency[2],
                    request_sha256=idempotency[3],
                )
                if replay is not None:
                    return replay
            for protocol_key in resource_keys:
                active_run_id = self.get_protocol_conflict(protocol_key)
                if active_run_id is not None:
                    raise ProtocolConflictError(protocol_key, active_run_id) from error
            raise
        return DispatchEnvelopeV1(
            run_id=actual_run_id,
            dispatch_id=actual_dispatch_id,
            context_sha256=context_sha256,
            replayed=False,
        )

    @staticmethod
    def _validate_retry_parent(
        session: Session,
        *,
        parent_run_id: str,
        context: RunContextV1,
        job_type: str,
    ) -> None:
        parent = session.get(Run, parent_run_id)
        parent_context_row = session.get(RunExecutionContext, parent_run_id)
        result = session.get(RunResult, parent_run_id)
        seal = session.get(RunSeal, parent_run_id)
        if parent is None or parent_context_row is None or result is None or seal is None:
            raise ScanAuthorizationError("retry parent must be a sealed run")
        try:
            parent_context = RunContextV1.model_validate(parent_context_row.context_json)
            actual_result_sha256 = TerminalResultV1.model_validate(result.result_payload).sha256()
        except Exception as error:
            raise ScanAuthorizationError("retry parent has invalid sealed evidence") from error
        if _is_dry_context(parent_context):
            raise ScanAuthorizationError("retry parent must be a live run")
        if parent.project_id != context.project_id or parent.site_id != context.site_id or parent.job_type != job_type:
            raise ScanAuthorizationError("retry parent must have the same project, site, and job type")
        if (
            parent.status not in _TERMINAL_STATUSES
            or result.terminal_status != parent.status
            or seal.terminal_status != parent.status
            or canonical_context_sha256(parent_context) != parent_context_row.context_sha256
            or seal.context_sha256 != parent_context_row.context_sha256
            or actual_result_sha256 != result.result_sha256
            or seal.result_sha256 != result.result_sha256
        ):
            raise ScanAuthorizationError("retry parent seal is invalid")

    @staticmethod
    def _validate_property_parent(
        session: Session,
        *,
        parent_run_id: str,
        context: RunContextV1,
        job_type: str,
    ) -> None:
        """Require a succeeded, same-scope BACnet seal before child creation."""

        parent = session.get(Run, parent_run_id)
        parent_context_row = session.get(RunExecutionContext, parent_run_id)
        result = session.get(RunResult, parent_run_id)
        seal = session.get(RunSeal, parent_run_id)
        if parent is None or parent_context_row is None or result is None or seal is None:
            raise ScanAuthorizationError("property expansion parent must be a sealed BACnet run")
        if (
            job_type != "bacnet_discovery"
            or parent.job_type != "bacnet_discovery"
            or parent.project_id != context.project_id
            or parent.site_id != context.site_id
            or parent.status != "succeeded"
            or result.terminal_status != "succeeded"
            or seal.terminal_status != "succeeded"
        ):
            raise ScanAuthorizationError("property expansion parent must be a succeeded same-scope BACnet seal")
        try:
            parent_context = RunContextV1.model_validate(parent_context_row.context_json)
        except Exception as error:
            raise ScanAuthorizationError("property expansion parent context is invalid") from error
        if canonical_context_sha256(parent_context) != parent_context_row.context_sha256:
            raise ScanAuthorizationError("property expansion parent context seal is invalid")
        parent_contract = _scan_contract(parent_context).get("bacnet")
        if not isinstance(parent_contract, Mapping):
            raise ScanAuthorizationError("property expansion parent has no frozen BACnet contract")

    def list_run_links(
        self,
        run_id: str,
        *,
        direction: str = "parents",
    ) -> list[dict[str, object]]:
        """Return stable relational provenance without consulting context JSON."""
        if direction not in {"parents", "children", "all"}:
            raise ValueError("direction must be 'parents', 'children', or 'all'")
        if direction == "parents":
            statement = select(RunLink).where(RunLink.child_run_id == run_id)
        elif direction == "children":
            statement = select(RunLink).where(RunLink.parent_run_id == run_id)
        else:
            statement = select(RunLink).where(or_(RunLink.parent_run_id == run_id, RunLink.child_run_id == run_id))
        statement = statement.order_by(
            RunLink.relation,
            RunLink.parent_run_id,
            RunLink.child_run_id,
        )
        with self._session_factory() as session:
            return [
                {
                    "parent_run_id": link.parent_run_id,
                    "child_run_id": link.child_run_id,
                    "relation": link.relation,
                    "authorization_id": link.authorization_id,
                    "created_at": link.created_at,
                }
                for link in session.scalars(statement).all()
            ]

    def _validate_authorization_for_create(
        self,
        session: Session,
        *,
        authorization_id: str,
        preview_run_id: str,
        context: RunContextV1,
        now: datetime,
    ) -> ScanAuthorization:
        authorization = session.scalar(
            select(ScanAuthorization).where(ScanAuthorization.authorization_id == authorization_id).with_for_update()
        )
        if authorization is None:
            raise ScanAuthorizationError("scan authorization was not found")
        if authorization.preview_run_id != preview_run_id:
            raise ScanAuthorizationError("scan authorization does not belong to the selected preview")
        preview, preview_context, _result, _seal = self._load_sealed_preview(session, preview_run_id)
        if (
            authorization.project_id != context.project_id
            or authorization.site_id != context.site_id
            or preview.project_id != context.project_id
            or preview.site_id != context.site_id
        ):
            raise ScanAuthorizationError("scan authorization does not belong to the requested project and site")
        preview_digest = _packet_plan_sha256(preview_context)
        live_digest = _packet_plan_sha256(context)
        if authorization.packet_plan_sha256 != preview_digest or live_digest != preview_digest:
            raise ScanAuthorizationError("live packet plan does not match the authorized sealed preview")
        if authorization.revoked_at is not None:
            raise ScanAuthorizationError("scan authorization has been revoked")
        if authorization.use_count >= authorization.max_uses:
            raise ScanAuthorizationError("scan authorization has already been used")
        if now < authorization.not_before:
            raise ScanAuthorizationError("scan authorization window has not started")
        if now >= authorization.not_after:
            raise ScanAuthorizationError("scan authorization window has ended")
        return authorization

    def get_context(self, run_id: str) -> StoredRunContextV1:
        with self._session_factory() as session:
            row = session.get(RunExecutionContext, run_id)
            if row is None:
                raise FileNotFoundError(run_id)
            return StoredRunContextV1(
                run_id=run_id,
                context=RunContextV1.model_validate(row.context_json),
                context_sha256=row.context_sha256,
                created_at=row.created_at,
            )

    def get_dispatch(self, dispatch_id: str) -> RunDispatchV1:
        with self._session_factory() as session:
            row = session.get(RunDispatch, dispatch_id)
            if row is None:
                raise FileNotFoundError(dispatch_id)
            return self._dispatch_view(row)

    def get_dispatch_for_run(self, run_id: str) -> RunDispatchV1:
        """Return the one durable dispatch identity committed with ``run_id``."""
        with self._session_factory() as session:
            row = session.scalar(select(RunDispatch).where(RunDispatch.run_id == run_id))
            if row is None:
                raise FileNotFoundError(run_id)
            return self._dispatch_view(row)

    def list_pending_dispatches(self, *, limit: int = 100) -> list[RunDispatchV1]:
        statement = (
            select(RunDispatch)
            .where(RunDispatch.state == "pending")
            .order_by(RunDispatch.created_at, RunDispatch.dispatch_id)
            .limit(max(1, limit))
        )
        with self._session_factory() as session:
            return [self._dispatch_view(row) for row in session.scalars(statement)]

    def mark_dispatch_published(self, dispatch_id: str, *, now: datetime | None = None) -> bool:
        published_at = now or _utcnow()
        with self._session_factory.begin() as session:
            result = session.execute(
                update(RunDispatch)
                .where(RunDispatch.dispatch_id == dispatch_id)
                .values(
                    state="published",
                    published_at=published_at,
                    publish_attempts=RunDispatch.publish_attempts + 1,
                )
            )
            return result.rowcount == 1

    # -- claim/heartbeat/progress ------------------------------------------

    def claim_run(
        self,
        run_id: str,
        dispatch_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
        owner_token: str | None = None,
    ) -> RunLeaseV1 | None:
        lease_seconds = max(1, int(lease_seconds))
        token = owner_token or token_urlsafe(32)
        with self._session_factory.begin() as session:
            # A new owner must receive the full requested lease after any row
            # lock wait. Sampling before the lock could create a lease that was
            # already partly, or completely, spent when the claim committed.
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            dispatch = session.get(RunDispatch, dispatch_id)
            context = session.get(RunExecutionContext, run_id)
            if run is None or dispatch is None or dispatch.run_id != run_id or context is None:
                self._add_conflict(
                    session,
                    run_id,
                    operation="claim",
                    reason="invalid_dispatch_or_context",
                    owner_token=token,
                )
                return None
            claimed_at = now or _utcnow()
            authorization = session.scalar(select(ScanAuthorization).where(ScanAuthorization.consumed_run_id == run_id))
            if authorization is not None:
                denial_reason = self._authorization_denial_reason(authorization, claimed_at)
                if denial_reason is not None:
                    guarded = session.execute(
                        update(Run)
                        .where(
                            Run.id == run_id,
                            Run.status == "queued",
                            Run.owner_token.is_(None),
                            Run.terminal_at.is_(None),
                        )
                        .values(status="failed")
                        .execution_options(synchronize_session=False)
                    )
                    if guarded.rowcount == 1:
                        session.expire(run)
                        run = self._load_run(session, run_id)
                        terminal = self._terminal_snapshot(
                            session,
                            run,
                            status="failed",
                            stage="authorization_invalid",
                            summary={
                                "authorization_id": authorization.authorization_id,
                                "authorization_valid": False,
                                "reason": denial_reason,
                                "acceptance_eligible": False,
                            },
                            error_message=denial_reason,
                        )
                        self._apply_finalization(
                            session,
                            run,
                            terminal,
                            terminal.sha256(),
                            context.context_sha256,
                            claimed_at,
                        )
                    return None
            expires_at = claimed_at + timedelta(seconds=lease_seconds)
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "queued",
                    Run.owner_token.is_(None),
                    Run.terminal_at.is_(None),
                )
                .values(
                    status="running",
                    stage="worker_claimed",
                    progress_percent=0,
                    owner_token=token,
                    attempt=Run.attempt + 1,
                    claimed_at=claimed_at,
                    heartbeat_at=claimed_at,
                    lease_expires_at=expires_at,
                    state_version=Run.state_version + 1,
                    updated_at=claimed_at,
                )
            )
            if result.rowcount != 1:
                return None
            session.execute(
                update(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run_id).values(owner_token=token)
            )
            session.expire(run)
            run = self._load_run(session, run_id)
            return RunLeaseV1(
                run_id=run_id,
                dispatch_id=dispatch_id,
                owner_token=token,
                attempt=run.attempt,
                claimed_at=claimed_at,
                heartbeat_at=claimed_at,
                lease_expires_at=expires_at,
                context_sha256=context.context_sha256,
            )

    @staticmethod
    def _authorization_denial_reason(
        authorization: ScanAuthorization,
        observed_at: datetime,
    ) -> str | None:
        if authorization.revoked_at is not None:
            return "scan authorization was revoked before execution claim"
        if authorization.use_count != 1 or authorization.consumed_run_id is None:
            return "scan authorization consumption record is incomplete"
        if observed_at < authorization.not_before:
            return "scan authorization window has not started"
        if observed_at >= authorization.not_after:
            return "scan authorization window ended before execution claim"
        return None

    def heartbeat(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        with self._session_factory.begin() as session:
            # Acquire the lifecycle row before sampling wall time. SQLite's
            # BEGIN IMMEDIATE and PostgreSQL's FOR UPDATE can both wait behind
            # another writer. A timestamp captured before that wait could let
            # an already-expired owner renew after the lease deadline.
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                return False
            heartbeat_at = now or _utcnow()
            expires_at = heartbeat_at + timedelta(seconds=max(1, int(lease_seconds)))
            result = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.terminal_at.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > heartbeat_at,
                )
                .values(
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=expires_at,
                    state_version=Run.state_version + 1,
                    updated_at=heartbeat_at,
                )
            )
            if result.rowcount == 1:
                return True
            self._add_conflict(
                session,
                run_id,
                operation="heartbeat",
                reason="stale_owner_or_terminal",
                owner_token=owner_token,
            )
            return False

    def update_progress(
        self,
        run_id: str,
        owner_token: str,
        *,
        stage: str | None = None,
        progress_percent: int | None = None,
        summary: Mapping[str, Any] | None = None,
        merge_summary: bool = True,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        with self._session_factory.begin() as session:
            # Lock first, then decide whether the lease is still valid. This
            # prevents a progress write begun before expiry from committing
            # after a database-lock wait carried it across the deadline.
            run = self._load_run(session, run_id, for_update=True)
            updated_at = now or _utcnow()
            if run.status != "running" or run.owner_token != owner_token or run.terminal_at is not None:
                self._add_conflict(
                    session,
                    run_id,
                    operation="progress",
                    reason="stale_owner_or_terminal",
                    owner_token=owner_token,
                )
                return False
            values: dict[str, Any] = {
                "error_message": error_message,
                "state_version": Run.state_version + 1,
                "updated_at": updated_at,
            }
            if stage is not None:
                values["stage"] = stage
            if progress_percent is not None:
                values["progress_percent"] = max(0, min(100, int(progress_percent)))
            if summary is not None:
                current = run.result_summary if isinstance(run.result_summary, dict) else {}
                values["result_summary"] = {**current, **dict(summary)} if merge_summary else dict(summary)
            guarded = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.terminal_at.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > updated_at,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount == 1:
                return True
            self._add_conflict(
                session,
                run_id,
                operation="progress",
                reason="stale_owner_or_terminal",
                owner_token=owner_token,
            )
            return False

    def is_cancel_requested(self, run_id: str, owner_token: str | None = None) -> bool:
        with self._query_session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return True
            if owner_token is not None and (run.owner_token != owner_token or run.status != "running"):
                return True
            return bool(run.cancel_requested)

    def require_active_control(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """Fail closed unless one executor still owns an authorized live run.

        This is deliberately read-only. It does not update ``heartbeat_at``,
        ``lease_expires_at``, progress, or any other liveness field. The one
        transaction validates the owner/attempt/lease/cancel fence, the exact
        preview authorization relation and window, and the initiating named
        user's current activation and scope grant. A database read error is not
        caught here, so control-store failure also prevents dispatch.
        """

        observed_at = now or _utcnow()
        with self._query_session_factory() as session:
            control = session.execute(
                select(Run, RunExecutionContext)
                .join(RunExecutionContext, RunExecutionContext.run_id == Run.id)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.attempt == attempt,
                    Run.cancel_requested.is_(False),
                    Run.terminal_at.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > observed_at,
                )
            ).one_or_none()
            if control is None:
                raise ActiveControlDeniedError("run owner, attempt, status, cancellation, or lease is no longer active")
            run, context_row = control
            self._require_active_control_in_session(
                session,
                run,
                context_row,
                owner_token=owner_token,
                attempt=attempt,
                observed_at=observed_at,
            )

    def _require_active_control_in_session(
        self,
        session: Session,
        run: Run,
        context_row: RunExecutionContext,
        *,
        owner_token: str,
        attempt: int,
        observed_at: datetime,
        lock_related: bool = False,
    ) -> None:
        """Apply the one active-control policy inside a caller's transaction.

        Finalization calls this with the lifecycle row already locked.  The
        related approval and initiator rows are then locked before success is
        allowed, closing the gap between a last packet and its terminal seal.
        Query-only dispatch checks deliberately keep ``lock_related`` false.
        """

        if (
            run.status != "running"
            or run.owner_token != owner_token
            or run.attempt != attempt
            or run.cancel_requested
            or run.terminal_at is not None
            or run.lease_expires_at is None
            or run.lease_expires_at <= observed_at
        ):
            raise ActiveControlDeniedError("run owner, attempt, status, cancellation, or lease is no longer active")
        context = RunContextV1.model_validate(context_row.context_json)
        if canonical_context_sha256(context) != context_row.context_sha256:
            raise ActiveControlDeniedError("frozen run context integrity check failed")
        if run.project_id != context.project_id or run.site_id != context.site_id:
            raise ActiveControlDeniedError("frozen run scope no longer matches")

        if _is_dry_context(context):
            return

        authorization_statement = (
            select(ScanAuthorization, RunLink)
            .join(
                RunLink,
                RunLink.authorization_id == ScanAuthorization.authorization_id,
            )
            .where(
                ScanAuthorization.consumed_run_id == run.id,
                RunLink.child_run_id == run.id,
                RunLink.relation == "preview",
            )
        )
        if lock_related:
            authorization_statement = authorization_statement.with_for_update()
        authorization_row = session.execute(authorization_statement).one_or_none()
        if authorization_row is None:
            raise ActiveControlDeniedError("live scan authorization linkage is missing")
        authorization, link = authorization_row
        if (
            authorization.preview_run_id != link.parent_run_id
            or link.authorization_id != authorization.authorization_id
            or authorization.project_id != run.project_id
            or authorization.site_id != run.site_id
            or authorization.consumed_run_id != run.id
            or authorization.use_count != 1
            or authorization.max_uses != 1
        ):
            raise ActiveControlDeniedError("live scan authorization linkage is invalid")
        if authorization.revoked_at is not None:
            raise ActiveControlDeniedError("scan authorization was revoked")
        if observed_at < authorization.not_before:
            raise ActiveControlDeniedError("scan authorization window has not started")
        if observed_at >= authorization.not_after:
            raise ActiveControlDeniedError("scan authorization window has ended")
        try:
            packet_plan_sha256 = _packet_plan_sha256(context)
        except ScanAuthorizationError as error:
            raise ActiveControlDeniedError("frozen scan authorization contract is invalid") from error
        if authorization.packet_plan_sha256 != packet_plan_sha256:
            raise ActiveControlDeniedError("frozen packet plan no longer matches its authorization")

        self._require_active_initiator(session, run, context, lock_related=lock_related)

    @staticmethod
    def _require_active_initiator(
        session: Session,
        run: Run,
        context: RunContextV1,
        *,
        lock_related: bool = False,
    ) -> None:
        try:
            contract = _scan_contract(context)
        except ScanAuthorizationError as error:
            raise ActiveControlDeniedError("frozen scan contract is unavailable") from error
        source = contract.get("principal_source")
        user_id = contract.get("initiating_user_id")
        deployment_role = contract.get("deployment_role")

        if source == "user_key":
            if not isinstance(user_id, str) or not user_id.strip():
                raise ActiveControlDeniedError("stable initiating user identity is missing")
            user_statement = select(User).where(User.id == user_id.strip())
            if lock_related:
                user_statement = user_statement.with_for_update()
            user = session.scalar(user_statement)
            if user is None or not user.is_active:
                raise ActiveControlDeniedError("initiating user is not active")
            current_role = user.role
            if current_role not in {"engineer", "admin"}:
                raise ActiveControlDeniedError("initiating user is no longer an engineer or admin")
            if current_role == "admin":
                return
            grant_statement = (
                select(UserScopeGrant.grant_id)
                .where(
                    UserScopeGrant.user_id == user.id,
                    UserScopeGrant.project_id == run.project_id,
                    UserScopeGrant.site_id == run.site_id,
                    UserScopeGrant.active_marker.is_(True),
                )
                .limit(1)
            )
            if lock_related:
                grant_statement = grant_statement.with_for_update()
            active_grant = session.scalar(grant_statement)
            if active_grant is None:
                raise ActiveControlDeniedError("initiating user's project/site scope grant is not active")
            return

        if source in {"local", "shared_key"}:
            if user_id not in (None, ""):
                raise ActiveControlDeniedError("synthetic principal cannot carry a named user identity")
            if deployment_role != "standalone":
                raise ActiveControlDeniedError("synthetic principal is allowed only for a frozen standalone deployment")
            return

        raise ActiveControlDeniedError("initiating principal source is invalid")

    # -- progressive discovery observations -------------------------------

    def append_discovery_observation(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
        observation: DiscoveryObservationInputV1 | Mapping[str, Any],
    ) -> ObservationAppendOutcomeV1:
        """Append one viewer-safe observation without renewing executor liveness."""

        captured = (
            observation
            if isinstance(observation, DiscoveryObservationInputV1)
            else DiscoveryObservationInputV1.model_validate(observation)
        )
        normalized_payload, encoded_payload, payload_sha256 = observation_payload(captured.payload)
        if len(encoded_payload) > RUN_DISCOVERY_OBSERVATION_PAYLOAD_MAX_BYTES:
            raise ValueError("discovery observation payload exceeds the canonical UTF-8 byte limit")

        rejected: DiscoveryObservationConflictError | None = None
        outcome: ObservationAppendOutcomeV1 | None = None
        with self._session_factory.begin() as session:
            # Every append for one run serializes through its lifecycle row. On
            # SQLite BEGIN IMMEDIATE provides the corresponding writer order.
            run = self._load_run(session, run_id, for_update=True)
            observed_at = self._clock()
            reason = self._discovery_append_denial_reason(
                session,
                run,
                owner_token=owner_token,
                attempt=attempt,
                protocol=captured.protocol,
                observed_at=observed_at,
            )
            if reason is not None:
                self._add_conflict(
                    session,
                    run_id,
                    operation="append_discovery_observation",
                    reason=reason,
                    owner_token=owner_token,
                    attempted_sha256=payload_sha256,
                )
                rejected = DiscoveryObservationConflictError(reason)
            else:
                matches = self._matching_discovery_observations(
                    session,
                    run_id,
                    attempt,
                    captured,
                )
                if matches:
                    if len(matches) == 1 and self._observation_matches(
                        matches[0], captured, normalized_payload, payload_sha256
                    ):
                        outcome = ObservationAppendOutcomeV1(
                            cursor=matches[0].id,
                            idempotent=True,
                        )
                    else:
                        self._add_conflict(
                            session,
                            run_id,
                            operation="append_discovery_observation",
                            reason="observation_identity_conflict",
                            owner_token=owner_token,
                            attempted_sha256=payload_sha256,
                        )
                        rejected = DiscoveryObservationConflictError("observation_identity_conflict")
                elif (
                    prefix_denial_reason := self._discovery_prefix_append_denial_reason(
                        session,
                        run,
                        attempt,
                        candidate_payload_bytes=len(encoded_payload),
                    )
                ) is not None:
                    self._add_conflict(
                        session,
                        run_id,
                        operation="append_discovery_observation",
                        reason=prefix_denial_reason,
                        owner_token=owner_token,
                        attempted_sha256=payload_sha256,
                    )
                    rejected = DiscoveryObservationConflictError(prefix_denial_reason)
                else:
                    committed_at = self._clock()
                    final_reason = self._discovery_append_fence_denial_reason(
                        run,
                        owner_token=owner_token,
                        attempt=attempt,
                        observed_at=committed_at,
                    )
                    if final_reason is not None:
                        self._add_conflict(
                            session,
                            run_id,
                            operation="append_discovery_observation",
                            reason=final_reason,
                            owner_token=owner_token,
                            attempted_sha256=payload_sha256,
                        )
                        rejected = DiscoveryObservationConflictError(final_reason)
                    else:
                        row = RunDiscoveryObservation(
                            run_id=run_id,
                            attempt=attempt,
                            protocol=captured.protocol,
                            entity_kind=captured.entity_kind,
                            entity_key=captured.entity_key,
                            entity_version=captured.entity_version,
                            event_key=captured.event_key,
                            phase=captured.phase,
                            outcome=captured.outcome,
                            payload_schema_version=captured.payload_schema_version,
                            payload=normalized_payload,
                            payload_sha256=payload_sha256,
                            observed_at=captured.observed_at,
                            created_at=committed_at,
                        )
                        try:
                            # The run-row lock makes a same-run unique race
                            # unreachable on supported databases. Keep the insert
                            # in a savepoint anyway, so a future isolation or
                            # dialect change can semantically reread a collision
                            # without losing this transaction's conflict audit.
                            with session.begin_nested():
                                session.add(row)
                                session.flush()
                        except IntegrityError:
                            matches = self._matching_discovery_observations(
                                session,
                                run_id,
                                attempt,
                                captured,
                            )
                            if len(matches) == 1 and self._observation_matches(
                                matches[0],
                                captured,
                                normalized_payload,
                                payload_sha256,
                            ):
                                outcome = ObservationAppendOutcomeV1(
                                    cursor=matches[0].id,
                                    idempotent=True,
                                )
                            elif matches:
                                self._add_conflict(
                                    session,
                                    run_id,
                                    operation="append_discovery_observation",
                                    reason="observation_identity_conflict",
                                    owner_token=owner_token,
                                    attempted_sha256=payload_sha256,
                                )
                                rejected = DiscoveryObservationConflictError(
                                    "observation_identity_conflict"
                                )
                            else:
                                raise
                        else:
                            state = session.get(
                                RunDiscoveryObservationState,
                                (run_id, attempt),
                            )
                            previous_commitment = (
                                state.observation_stream_sha256
                                if state is not None
                                else OBSERVATION_STREAM_EMPTY_SHA256
                            )
                            commitment = extend_observation_stream_sha256(
                                previous_commitment,
                                self._accepted_discovery_observation_view(
                                    row,
                                    observation=captured,
                                    normalized_payload=normalized_payload,
                                    payload_sha256=payload_sha256,
                                    created_at=committed_at,
                                ),
                            )
                            if state is None:
                                state = RunDiscoveryObservationState(
                                    run_id=run_id,
                                    attempt=attempt,
                                    observation_count=1,
                                    canonical_payload_bytes=len(encoded_payload),
                                    terminal_cursor=row.id,
                                    observation_stream_sha256=commitment,
                                )
                                session.add(state)
                            else:
                                state.observation_count += 1
                                state.canonical_payload_bytes += len(encoded_payload)
                                state.terminal_cursor = row.id
                                state.observation_stream_sha256 = commitment
                            outcome = ObservationAppendOutcomeV1(cursor=row.id)

        # Lifecycle conflicts must be durable, so raise only after their audit
        # transaction commits.
        if rejected is not None:
            raise rejected
        if outcome is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("discovery observation append produced no outcome")
        return outcome

    def get_discovery_observation_cutoff(
        self,
        run_id: str,
        attempt: int,
        *,
        project_id: str | None = None,
        site_id: str | None = None,
    ) -> ObservationCutoffV1:
        """Read one scoped attempt's stable cursor/count watermark."""

        with self._query_session_factory() as session:
            self._require_scoped_run_attempt(
                session,
                run_id,
                attempt,
                project_id=project_id,
                site_id=site_id,
            )
            terminal_cursor, observation_count = session.execute(
                select(
                    func.coalesce(func.max(RunDiscoveryObservation.id), 0),
                    func.count(RunDiscoveryObservation.id),
                ).where(
                    RunDiscoveryObservation.run_id == run_id,
                    RunDiscoveryObservation.attempt == attempt,
                )
            ).one()
        return ObservationCutoffV1(
            run_id=run_id,
            attempt=attempt,
            terminal_cursor=int(terminal_cursor or 0),
            observation_count=int(observation_count or 0),
        )

    @staticmethod
    def _validated_discovery_state_snapshot(
        session: Session,
        run_id: str,
        attempt: int,
    ) -> _DiscoveryObservationStateSnapshot:
        run = session.get(Run, run_id)
        context_row = session.get(RunExecutionContext, run_id)
        if run is None or context_row is None:
            raise DiscoveryObservationIntegrityError("run_scope_authority_missing")
        try:
            context = RunContextV1.model_validate(context_row.context_json)
            sealed_budget = _sealed_ip_observation_budget(
                context,
                job_type=run.job_type,
            )
        except (TypeError, ValueError) as error:
            raise DiscoveryObservationIntegrityError(
                "observation_budget_invalid"
            ) from error
        if (
            canonical_context_sha256(context) != context_row.context_sha256
            or run.project_id != context.project_id
            or run.site_id != context.site_id
        ):
            raise DiscoveryObservationIntegrityError("run_scope_authority_invalid")
        observation_row_limit = (
            sealed_budget[0]
            if sealed_budget is not None
            else DISCOVERY_OBSERVATION_FOLD_MAX_ROWS
        )
        observation_payload_byte_limit = (
            sealed_budget[1]
            if sealed_budget is not None
            else DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES
        )
        state = session.get(
            RunDiscoveryObservationState,
            (run_id, attempt),
        )
        terminal_cursor, observation_count = session.execute(
            select(
                func.coalesce(func.max(RunDiscoveryObservation.id), 0),
                func.count(RunDiscoveryObservation.id),
            ).where(
                RunDiscoveryObservation.run_id == run_id,
                RunDiscoveryObservation.attempt == attempt,
            )
        ).one()
        actual_cursor = int(terminal_cursor or 0)
        actual_count = int(observation_count or 0)
        if state is None:
            if actual_cursor != 0 or actual_count != 0:
                raise DiscoveryObservationIntegrityError(
                    "observation_state_mismatch"
                )
            return _DiscoveryObservationStateSnapshot(
                run_id=run_id,
                attempt=attempt,
                observation_count=0,
                canonical_payload_bytes=0,
                terminal_cursor=0,
                observation_stream_sha256=OBSERVATION_STREAM_EMPTY_SHA256,
                observation_row_limit=observation_row_limit,
                observation_payload_byte_limit=observation_payload_byte_limit,
                sealed_observation_budget=sealed_budget is not None,
            )
        snapshot = _DiscoveryObservationStateSnapshot(
            run_id=run_id,
            attempt=attempt,
            observation_count=int(state.observation_count),
            canonical_payload_bytes=int(state.canonical_payload_bytes),
            terminal_cursor=int(state.terminal_cursor),
            observation_stream_sha256=state.observation_stream_sha256,
            observation_row_limit=observation_row_limit,
            observation_payload_byte_limit=observation_payload_byte_limit,
            sealed_observation_budget=sealed_budget is not None,
        )
        if (
            snapshot.observation_count != actual_count
            or snapshot.terminal_cursor != actual_cursor
        ):
            raise DiscoveryObservationIntegrityError(
                "observation_state_mismatch"
            )
        return snapshot

    def list_discovery_observations(
        self,
        run_id: str,
        attempt: int,
        *,
        after_cursor: int = 0,
        limit: int = 250,
        project_id: str | None = None,
        site_id: str | None = None,
    ) -> list[DiscoveryObservationViewV1]:
        """Return a scoped cursor page; cursor orders rows but never versions them."""

        bounded_limit = max(1, min(250, int(limit)))
        bounded_after = max(0, int(after_cursor))
        with self._query_session_factory() as session:
            self._require_scoped_run_attempt(
                session,
                run_id,
                attempt,
                project_id=project_id,
                site_id=site_id,
            )
            rows = session.scalars(
                select(RunDiscoveryObservation)
                .where(
                    RunDiscoveryObservation.run_id == run_id,
                    RunDiscoveryObservation.attempt == attempt,
                    RunDiscoveryObservation.id > bounded_after,
                )
                .order_by(RunDiscoveryObservation.id)
                .limit(bounded_limit)
                .execution_options(yield_per=1)
            )
            return self._bounded_discovery_observation_page(rows)

    def get_owned_discovery_protocol(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
    ) -> str | None:
        """Identify a discovery sink without granting it repository authority."""

        with self._query_session_factory() as session:
            job_type = session.scalar(
                select(Run.job_type).where(
                    Run.id == run_id,
                    Run.owner_token == owner_token,
                    Run.attempt == attempt,
                )
            )
        return _DISCOVERY_PROTOCOL_BY_JOB_TYPE.get(job_type)

    @staticmethod
    def _require_scoped_run_attempt(
        session: Session,
        run_id: str,
        attempt: int,
        *,
        project_id: str | None,
        site_id: str | None,
    ) -> None:
        statement = select(Run.id).where(Run.id == run_id, Run.attempt == attempt)
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        if site_id is not None:
            statement = statement.where(Run.site_id == site_id)
        if session.scalar(statement) is None:
            raise FileNotFoundError(run_id)

    @staticmethod
    def _accepted_discovery_observation_view(
        row: RunDiscoveryObservation,
        *,
        observation: DiscoveryObservationInputV1,
        normalized_payload: dict[str, Any],
        payload_sha256: str,
        created_at: datetime,
    ) -> DiscoveryObservationViewV1:
        """Build the just-accepted view from its canonical append boundary."""

        return DiscoveryObservationViewV1(
            cursor=row.id,
            run_id=row.run_id,
            attempt=row.attempt,
            protocol=observation.protocol,
            entity_kind=observation.entity_kind,
            entity_key=observation.entity_key,
            entity_version=observation.entity_version,
            event_key=observation.event_key,
            phase=observation.phase,
            outcome=observation.outcome,
            payload_schema_version=observation.payload_schema_version,
            payload=normalized_payload,
            payload_sha256=payload_sha256,
            observed_at=observation.observed_at,
            created_at=created_at,
        )

    @staticmethod
    def _discovery_observation_view(
        row: RunDiscoveryObservation,
    ) -> DiscoveryObservationViewV1:
        try:
            normalized_payload, _encoded_payload, payload_sha256 = observation_payload(
                dict(row.payload)
            )
        except (TypeError, ValueError) as error:
            raise DiscoveryObservationIntegrityError("payload_invalid") from error
        if payload_sha256 != row.payload_sha256:
            raise DiscoveryObservationIntegrityError("payload_digest_mismatch")
        return DiscoveryObservationViewV1(
            cursor=row.id,
            run_id=row.run_id,
            attempt=row.attempt,
            protocol=row.protocol,
            entity_kind=row.entity_kind,
            entity_key=row.entity_key,
            entity_version=row.entity_version,
            event_key=row.event_key,
            phase=row.phase,
            outcome=row.outcome,
            payload_schema_version=row.payload_schema_version,
            payload=normalized_payload,
            payload_sha256=row.payload_sha256,
            observed_at=row.observed_at,
            created_at=row.created_at,
        )

    @classmethod
    def _bounded_discovery_observation_page(
        cls,
        rows: Any,
    ) -> list[DiscoveryObservationViewV1]:
        page: list[DiscoveryObservationViewV1] = []
        encoded_bytes = 2
        for row in rows:
            view = cls._discovery_observation_view(row)
            view_bytes = len(canonical_json_bytes(view.model_dump(mode="json"))) + 1
            if encoded_bytes + view_bytes > DISCOVERY_OBSERVATION_QUERY_PAGE_MAX_BYTES:
                if not page:
                    raise ValueError("one discovery observation exceeds the query page budget")
                break
            page.append(view)
            encoded_bytes += view_bytes
        return page

    @staticmethod
    def _matching_discovery_observations(
        session: Session,
        run_id: str,
        attempt: int,
        observation: DiscoveryObservationInputV1,
    ) -> list[RunDiscoveryObservation]:
        identity = or_(
            RunDiscoveryObservation.event_key == observation.event_key,
            and_(
                RunDiscoveryObservation.entity_kind == observation.entity_kind,
                RunDiscoveryObservation.entity_key == observation.entity_key,
                RunDiscoveryObservation.entity_version == observation.entity_version,
            ),
        )
        return list(
            session.scalars(
                select(RunDiscoveryObservation)
                .where(
                    RunDiscoveryObservation.run_id == run_id,
                    RunDiscoveryObservation.attempt == attempt,
                    identity,
                )
                .order_by(RunDiscoveryObservation.id)
            )
        )

    @staticmethod
    def _observation_matches(
        row: RunDiscoveryObservation,
        observation: DiscoveryObservationInputV1,
        normalized_payload: dict[str, Any],
        payload_sha256: str,
    ) -> bool:
        return (
            row.protocol == observation.protocol
            and row.entity_kind == observation.entity_kind
            and row.entity_key == observation.entity_key
            and row.entity_version == observation.entity_version
            and row.event_key == observation.event_key
            and row.phase == observation.phase
            and row.outcome == observation.outcome
            and row.payload_schema_version == observation.payload_schema_version
            and row.payload == normalized_payload
            and row.payload_sha256 == payload_sha256
            and row.observed_at == observation.observed_at
        )

    @staticmethod
    def _discovery_prefix_append_denial_reason(
        session: Session,
        run: Run,
        attempt: int,
        *,
        candidate_payload_bytes: int,
    ) -> str | None:
        """Check lifecycle-owned attempt bounds without refolding prior rows."""

        if DISCOVERY_OBSERVATION_FOLD_MAX_ROWS <= 0:
            return "observation_row_budget_exhausted"
        run_id = run.id
        context_row = session.get(RunExecutionContext, run_id)
        try:
            context = (
                RunContextV1.model_validate(context_row.context_json)
                if context_row is not None
                else None
            )
            sealed_budget = (
                _sealed_ip_observation_budget(context, job_type=run.job_type)
                if context is not None
                else None
            )
        except (TypeError, ValueError):
            return "observation_budget_invalid"
        state = session.get(
            RunDiscoveryObservationState,
            (run_id, attempt),
        )
        if state is None:
            orphan_cursor = session.scalar(
                select(RunDiscoveryObservation.id)
                .where(
                    RunDiscoveryObservation.run_id == run_id,
                    RunDiscoveryObservation.attempt == attempt,
                )
                .limit(1)
            )
            if orphan_cursor is not None:
                return "observation_prefix_invalid"
            row_count = 0
            payload_bytes = 0
        else:
            row_count = int(state.observation_count)
            payload_bytes = int(state.canonical_payload_bytes)
            commitment = state.observation_stream_sha256
            latest = session.get(RunDiscoveryObservation, state.terminal_cursor)
            if (
                row_count < 1
                or payload_bytes < 1
                or state.terminal_cursor < 1
                or len(commitment) != 64
                or any(character not in "0123456789abcdef" for character in commitment)
                or latest is None
                or latest.run_id != run_id
                or latest.attempt != attempt
            ):
                return "observation_prefix_invalid"
        if row_count >= DISCOVERY_OBSERVATION_FOLD_MAX_ROWS:
            return "observation_row_budget_exhausted"
        if sealed_budget is not None and row_count >= sealed_budget[0]:
            return "observation_row_budget_exhausted"
        if (
            payload_bytes + candidate_payload_bytes
            > DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES
        ):
            return "observation_payload_budget_exhausted"
        if (
            sealed_budget is not None
            and payload_bytes + candidate_payload_bytes > sealed_budget[1]
        ):
            return "observation_payload_budget_exhausted"
        return None

    @staticmethod
    def _discovery_append_fence_denial_reason(
        run: Run,
        *,
        owner_token: str,
        attempt: int,
        observed_at: datetime,
    ) -> str | None:
        if (
            run.status != "running"
            or run.owner_token != owner_token
            or run.attempt != attempt
            or run.terminal_at is not None
            or run.result_sha256 is not None
            or run.lease_expires_at is None
            or run.lease_expires_at <= observed_at
        ):
            return "stale_owner_attempt_or_terminal"
        return None

    @classmethod
    def _discovery_append_denial_reason(
        cls,
        session: Session,
        run: Run,
        *,
        owner_token: str,
        attempt: int,
        protocol: str,
        observed_at: datetime,
    ) -> str | None:
        fence_reason = cls._discovery_append_fence_denial_reason(
            run,
            owner_token=owner_token,
            attempt=attempt,
            observed_at=observed_at,
        )
        if fence_reason is not None:
            return fence_reason
        if session.get(RunResult, run.id) is not None or session.get(RunSeal, run.id) is not None:
            return "stale_owner_attempt_or_terminal"
        expected_protocol = _DISCOVERY_PROTOCOL_BY_JOB_TYPE.get(run.job_type)
        if expected_protocol is None or protocol != expected_protocol:
            return "unsupported_discovery_job_or_protocol"
        context_row = session.get(RunExecutionContext, run.id)
        if context_row is None:
            return "run_scope_authority_missing"
        try:
            context = RunContextV1.model_validate(context_row.context_json)
        except Exception:
            return "run_scope_authority_invalid"
        if (
            canonical_context_sha256(context) != context_row.context_sha256
            or run.project_id != context.project_id
            or run.site_id != context.site_id
        ):
            return "run_scope_authority_invalid"
        return None

    # -- cancellation/finalization -----------------------------------------

    def request_cancel(self, run_id: str, *, now: datetime | None = None) -> CancelOutcomeV1:
        requested_at = now or _utcnow()
        with self._session_factory.begin() as session:
            run = self._load_run(session, run_id)
            if run.status in _TERMINAL_STATUSES:
                return CancelOutcomeV1(run_id=run_id, state="terminal", changed=False)
            if run.status == "queued":
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "queued",
                        Run.owner_token.is_(None),
                        Run.terminal_at.is_(None),
                        Run.state_version == run.state_version,
                    )
                    .values(
                        status="cancelled",
                        cancel_requested=True,
                        updated_at=requested_at,
                    )
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:
                    session.expire(run)
                    run = self._load_run(session, run_id)
                else:
                    session.expire(run)
                    run = self._load_run(session, run_id)
            if run.status == "cancelled" and run.terminal_at is None:
                result = TerminalResultV1(
                    status="cancelled",
                    stage="cancelled_before_start",
                    summary={"cancelled_before_start": True},
                )
                context = session.get(RunExecutionContext, run_id)
                if context is None:
                    raise RuntimeError(f"run {run_id} has no execution context")
                self._apply_finalization(
                    session,
                    run,
                    result,
                    result.sha256(),
                    context.context_sha256,
                    requested_at,
                )
                return CancelOutcomeV1(run_id=run_id, state="cancelled", changed=True)
            if run.status in _TERMINAL_STATUSES:
                return CancelOutcomeV1(run_id=run_id, state="terminal", changed=False)
            if run.cancel_requested:
                return CancelOutcomeV1(
                    run_id=run_id,
                    state="cancellation_requested",
                    changed=False,
                )
            guarded = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.terminal_at.is_(None),
                    Run.cancel_requested.is_(False),
                )
                .values(
                    cancel_requested=True,
                    state_version=Run.state_version + 1,
                    updated_at=requested_at,
                )
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount == 1:
                return CancelOutcomeV1(
                    run_id=run_id,
                    state="cancellation_requested",
                    changed=True,
                )
            session.expire(run)
            run = self._load_run(session, run_id)
            state = "terminal" if run.status in _TERMINAL_STATUSES else "cancellation_requested"
            return CancelOutcomeV1(run_id=run_id, state=state, changed=False)

    def finalize_discovery_run(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
        result: TerminalResultV1 | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> FinalizeOutcome:
        """Fold and atomically seal one discovery observation prefix.

        Capturing the cutoff and committing the terminal state are deliberately
        short writer transactions. Cursor paging and the deterministic fold run
        between them, so heartbeat renewal is not blocked by result assembly.
        """

        requested = result if isinstance(result, TerminalResultV1) else TerminalResultV1.model_validate(result)
        while True:
            cutoff_or_conflict = self._capture_discovery_cutoff(
                run_id,
                owner_token,
                attempt,
                now=now,
            )
            if isinstance(cutoff_or_conflict, FinalizeOutcome):
                return cutoff_or_conflict
            fold = self._fold_discovery_prefix(cutoff_or_conflict)
            committed = self._commit_discovery_finalization(
                run_id,
                owner_token,
                attempt,
                requested,
                fold,
                cutoff_or_conflict,
                now=now,
            )
            if committed is not None:
                return committed

    def _capture_discovery_cutoff(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
        *,
        now: datetime | None,
    ) -> _DiscoveryObservationStateSnapshot | FinalizeOutcome:
        with self._session_factory.begin() as session:
            run = self._load_run(session, run_id, for_update=True)
            observed_at = now or self._clock()
            existing = session.get(RunResult, run_id)
            if existing is None:
                reason = self._discovery_finalization_denial_reason(
                    session,
                    run,
                    owner_token=owner_token,
                    attempt=attempt,
                    observed_at=observed_at,
                )
                if reason is not None:
                    self._add_conflict(
                        session,
                        run_id,
                        operation="finalize_discovery",
                        reason=reason,
                        owner_token=owner_token,
                    )
                    return FinalizeOutcome(conflict=True, reason=reason)
            elif run.owner_token != owner_token or run.attempt != attempt:
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize_discovery",
                    reason="terminal_result_conflict",
                    owner_token=owner_token,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason="terminal_result_conflict",
                    result_sha256=existing.result_sha256,
                )
            return self._validated_discovery_state_snapshot(
                session,
                run_id,
                attempt,
            )

    def _fold_discovery_prefix(
        self,
        state: _DiscoveryObservationStateSnapshot,
    ) -> DiscoveryObservationFoldV1:
        cutoff = state.cutoff()
        if cutoff.observation_count > DISCOVERY_OBSERVATION_FOLD_MAX_ROWS:
            raise DiscoveryObservationFoldLimitError("row_limit")
        if state.canonical_payload_bytes > DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES:
            raise DiscoveryObservationFoldLimitError("payload_byte_limit")
        if (
            state.sealed_observation_budget
            and cutoff.observation_count > state.observation_row_limit
        ):
            raise DiscoveryObservationFoldLimitError("sealed_row_limit")
        if (
            state.sealed_observation_budget
            and state.canonical_payload_bytes
            > state.observation_payload_byte_limit
        ):
            raise DiscoveryObservationFoldLimitError("sealed_payload_byte_limit")
        observations: list[DiscoveryObservationViewV1] = []
        after_cursor = 0
        payload_bytes = 0
        while len(observations) < cutoff.observation_count:
            with self._query_session_factory() as session:
                rows = session.scalars(
                    select(RunDiscoveryObservation)
                    .where(
                        RunDiscoveryObservation.run_id == cutoff.run_id,
                        RunDiscoveryObservation.attempt == cutoff.attempt,
                        RunDiscoveryObservation.id > after_cursor,
                        RunDiscoveryObservation.id <= cutoff.terminal_cursor,
                    )
                    .order_by(RunDiscoveryObservation.id)
                    .limit(250)
                    .execution_options(yield_per=1)
                )
                page = self._bounded_discovery_observation_page(rows)
            if not page:
                break
            payload_bytes += sum(len(canonical_json_bytes(item.payload)) for item in page)
            if payload_bytes > DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES:
                raise DiscoveryObservationFoldLimitError("payload_byte_limit")
            observations.extend(page)
            after_cursor = page[-1].cursor
        fold = fold_discovery_observations(
            observations,
            terminal_cursor=cutoff.terminal_cursor,
            expected_count=cutoff.observation_count,
            run_id=cutoff.run_id,
            attempt=cutoff.attempt,
        )
        if (
            payload_bytes != state.canonical_payload_bytes
            or fold.observation_stream_sha256 != state.observation_stream_sha256
        ):
            raise DiscoveryObservationIntegrityError("observation_state_mismatch")
        return fold

    def _commit_discovery_finalization(
        self,
        run_id: str,
        owner_token: str,
        attempt: int,
        requested: TerminalResultV1,
        fold: DiscoveryObservationFoldV1,
        expected_state: _DiscoveryObservationStateSnapshot,
        *,
        now: datetime | None,
    ) -> FinalizeOutcome | None:
        with self._session_factory.begin() as session:
            run = self._load_run(session, run_id, for_update=True)
            terminal_at = now or self._clock()
            current_state = self._validated_discovery_state_snapshot(
                session,
                run_id,
                attempt,
            )
            if current_state != expected_state:
                return None

            terminal = self._discovery_terminal_result(run, requested, fold)
            terminal = self._fence_success_terminal(
                session,
                run,
                owner_token=owner_token,
                attempt=attempt,
                terminal=terminal,
                observed_at=terminal_at,
            )
            result_sha256 = terminal.sha256()
            existing = session.get(RunResult, run_id)
            if existing is not None:
                if (
                    run.owner_token == owner_token
                    and run.attempt == attempt
                    and existing.terminal_status == terminal.status
                    and existing.result_sha256 == result_sha256
                ):
                    return FinalizeOutcome(
                        idempotent=True,
                        result_sha256=result_sha256,
                    )
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize_discovery",
                    reason="terminal_result_conflict",
                    owner_token=owner_token,
                    attempted_status=terminal.status,
                    attempted_sha256=result_sha256,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason="terminal_result_conflict",
                    result_sha256=existing.result_sha256,
                )

            reason = self._discovery_finalization_denial_reason(
                session,
                run,
                owner_token=owner_token,
                attempt=attempt,
                observed_at=terminal_at,
            )
            if reason is not None:
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize_discovery",
                    reason=reason,
                    owner_token=owner_token,
                    attempted_status=terminal.status,
                    attempted_sha256=result_sha256,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason=reason,
                )

            guard = [
                Run.id == run_id,
                Run.status == "running",
                Run.owner_token == owner_token,
                Run.attempt == attempt,
                Run.terminal_at.is_(None),
                Run.result_sha256.is_(None),
                Run.lease_expires_at.is_not(None),
                Run.lease_expires_at > terminal_at,
            ]
            if terminal.status != "cancelled":
                guard.append(Run.cancel_requested.is_(False))
            guarded = session.execute(
                update(Run).where(*guard).values(status=terminal.status).execution_options(synchronize_session=False)
            )
            if guarded.rowcount != 1:
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize_discovery",
                    reason="stale_owner_attempt_or_terminal",
                    owner_token=owner_token,
                    attempted_status=terminal.status,
                    attempted_sha256=result_sha256,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason="stale_owner_attempt_or_terminal",
                )
            session.expire(run)
            run = self._load_run(session, run_id)
            context = session.get(RunExecutionContext, run_id)
            if context is None:
                raise RuntimeError(f"run {run_id} has no execution context")
            self._apply_finalization(
                session,
                run,
                terminal,
                result_sha256,
                context.context_sha256,
                terminal_at,
            )
            return FinalizeOutcome(applied=True, result_sha256=result_sha256)

    @staticmethod
    def _discovery_terminal_result(
        run: Run,
        requested: TerminalResultV1,
        fold: DiscoveryObservationFoldV1,
    ) -> TerminalResultV1:
        terminal = requested
        if run.cancel_requested and requested.status != "cancelled":
            cancelled_summary = dict(requested.summary)
            cancelled_summary.update(
                {
                    "validation_incomplete": True,
                    "acceptance_eligible": False,
                }
            )
            terminal = requested.model_copy(
                update={
                    "status": "cancelled",
                    "stage": "engine_cancelled",
                    "summary": cancelled_summary,
                    "error_message": None,
                }
            )
        summary = {
            **dict(terminal.summary),
            **dict(fold.summary),
            "observation_evidence_v1": fold.evidence().model_dump(mode="json"),
        }
        # Provider projections may contribute domain metrics, but they cannot
        # overturn lifecycle truth established by cancellation or recovery.
        for lifecycle_key in (
            "lease_recovered",
            "expired_attempt",
            "provider_drained",
            "validation_incomplete",
            "acceptance_eligible",
        ):
            if lifecycle_key in terminal.summary:
                summary[lifecycle_key] = terminal.summary[lifecycle_key]
        selected_devices = fold.devices if "devices" in fold.projected_collections else terminal.devices
        scoped_devices = tuple(
            {
                **dict(device),
                "project_id": run.project_id,
                "site_id": run.site_id,
            }
            for device in selected_devices
        )
        return terminal.model_copy(
            update={
                "summary": summary,
                "issues": (fold.issues if "issues" in fold.projected_collections else terminal.issues),
                "devices": scoped_devices,
                "points": (fold.points if "points" in fold.projected_collections else terminal.points),
                "topics": (fold.topics if "topics" in fold.projected_collections else terminal.topics),
            }
        )

    @staticmethod
    def _discovery_finalization_denial_reason(
        session: Session,
        run: Run,
        *,
        owner_token: str,
        attempt: int,
        observed_at: datetime,
    ) -> str | None:
        if run.job_type not in {"ip_discovery", "bacnet_discovery"}:
            return "unsupported_discovery_job"
        if (
            run.status != "running"
            or run.owner_token != owner_token
            or run.attempt != attempt
            or run.terminal_at is not None
            or run.result_sha256 is not None
            or run.lease_expires_at is None
            or run.lease_expires_at <= observed_at
            or session.get(RunResult, run.id) is not None
            or session.get(RunSeal, run.id) is not None
        ):
            return "stale_owner_attempt_or_terminal"
        context_row = session.get(RunExecutionContext, run.id)
        if context_row is None:
            return "run_scope_authority_missing"
        try:
            context = RunContextV1.model_validate(context_row.context_json)
        except Exception:
            return "run_scope_authority_invalid"
        if (
            canonical_context_sha256(context) != context_row.context_sha256
            or run.project_id != context.project_id
            or run.site_id != context.site_id
        ):
            return "run_scope_authority_invalid"
        return None

    def finalize_run(
        self,
        run_id: str,
        owner_token: str,
        result: TerminalResultV1 | Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> FinalizeOutcome:
        terminal = result if isinstance(result, TerminalResultV1) else TerminalResultV1.model_validate(result)
        result_sha256 = terminal.sha256()
        with self._session_factory.begin() as session:
            # First finalization is lease-fenced at the instant the lifecycle
            # row becomes writable, not when the caller began waiting for it.
            # Identical terminal replay remains idempotent below.
            run = self._load_run(session, run_id, for_update=True)
            if run.cancel_requested and terminal.status != "cancelled":
                summary = dict(terminal.summary)
                summary["validation_incomplete"] = True
                summary["acceptance_eligible"] = False
                terminal = terminal.model_copy(
                    update={
                        "status": "cancelled",
                        "stage": "engine_cancelled",
                        "summary": summary,
                        "error_message": None,
                    }
                )
                result_sha256 = terminal.sha256()
            terminal_at = now or _utcnow()
            terminal = self._fence_success_terminal(
                session,
                run,
                owner_token=owner_token,
                attempt=run.attempt,
                terminal=terminal,
                observed_at=terminal_at,
            )
            result_sha256 = terminal.sha256()
            existing = session.get(RunResult, run_id)
            if existing is None:
                guard = [
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == owner_token,
                    Run.terminal_at.is_(None),
                    Run.result_sha256.is_(None),
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at > terminal_at,
                ]
                if terminal.status != "cancelled":
                    guard.append(Run.cancel_requested.is_(False))
                guarded = session.execute(
                    update(Run)
                    .where(*guard)
                    .values(status=terminal.status)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount == 1:
                    run = self._load_run(session, run_id)
                    context = session.get(RunExecutionContext, run_id)
                    if context is None:
                        raise RuntimeError(f"run {run_id} has no execution context")
                    self._apply_finalization(
                        session,
                        run,
                        terminal,
                        result_sha256,
                        context.context_sha256,
                        terminal_at,
                    )
                    return FinalizeOutcome(applied=True, result_sha256=result_sha256)
            run = self._load_run(session, run_id)
            existing = session.get(RunResult, run_id)
            if run.status in _TERMINAL_STATUSES or existing is not None:
                if (
                    run.owner_token == owner_token
                    and existing is not None
                    and existing.terminal_status == terminal.status
                    and existing.result_sha256 == result_sha256
                ):
                    return FinalizeOutcome(idempotent=True, result_sha256=result_sha256)
                self._add_conflict(
                    session,
                    run_id,
                    operation="finalize",
                    reason="terminal_result_conflict",
                    owner_token=owner_token,
                    attempted_status=terminal.status,
                    attempted_sha256=result_sha256,
                )
                return FinalizeOutcome(
                    conflict=True,
                    reason="terminal_result_conflict",
                    result_sha256=existing.result_sha256 if existing else run.result_sha256,
                )
            self._add_conflict(
                session,
                run_id,
                operation="finalize",
                reason="stale_owner",
                owner_token=owner_token,
                attempted_status=terminal.status,
                attempted_sha256=result_sha256,
            )
            return FinalizeOutcome(
                conflict=True,
                reason="stale_owner",
                result_sha256=None,
            )

    def _fence_success_terminal(
        self,
        session: Session,
        run: Run,
        *,
        owner_token: str,
        attempt: int,
        terminal: TerminalResultV1,
        observed_at: datetime,
    ) -> TerminalResultV1:
        """Turn a stale successful completion into auditable failed evidence.

        The caller holds ``Run`` first, samples trusted time after that lock,
        then this routine locks the linked authorization and active initiator
        state.  Cancellation and prior engine failures deliberately bypass the
        check so Stop and recovery retain their existing terminal paths.
        """

        if (
            terminal.status != "succeeded"
            or run.job_type not in {"ip_discovery", "bacnet_discovery"}
        ):
            return terminal
        context_row = session.get(RunExecutionContext, run.id)
        if context_row is None:
            return terminal
        # Historical V1 discovery fixtures predate the sealed scan contract and
        # have no authorization linkage to fence. Keep their terminal bytes
        # readable; every modern preview-bound run carries this contract.
        raw_parameters = context_row.context_json.get("engine_parameters")
        if not isinstance(raw_parameters, Mapping) or not isinstance(
            raw_parameters.get("scan_contract_v1"), Mapping
        ):
            return terminal
        try:
            self._require_active_control_in_session(
                session,
                run,
                context_row,
                owner_token=owner_token,
                attempt=attempt,
                observed_at=observed_at,
                lock_related=True,
            )
        except ActiveControlDeniedError as error:
            control_reason = _active_control_loss_reason(error)
            summary = dict(terminal.summary)
            summary.update(
                {
                    "control_reason": control_reason,
                    "provider_drained": True,
                    "validation_incomplete": True,
                    "acceptance_eligible": False,
                }
            )
            return terminal.model_copy(
                update={
                    "status": "failed",
                    "stage": "active_control_lost",
                    "summary": summary,
                    "error_message": "Discovery stopped because active control was lost.",
                }
            )
        return terminal

    # -- recovery/protocol/audit -------------------------------------------

    def recover_expired_leases(self, *, now: datetime | None = None, limit: int = 100) -> list[str]:
        observed_at = now or _utcnow()
        with self._query_session_factory() as session:
            candidates = list(
                session.execute(
                    select(Run.id, Run.job_type)
                    .where(
                        Run.status == "running",
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at <= observed_at,
                    )
                    .order_by(Run.lease_expires_at, Run.id)
                    .limit(max(1, limit))
                ).all()
            )
        recovered: list[str] = []
        for run_id, job_type in candidates:
            if job_type in {"ip_discovery", "bacnet_discovery"}:
                try:
                    did_recover = self._recover_expired_discovery_run(
                        run_id,
                        observed_at,
                    )
                except DiscoveryObservationFoldLimitError as error:
                    did_recover = self._quarantine_expired_discovery_run(
                        run_id,
                        observed_at,
                        reason=f"fold_{error.reason}",
                    )
                except (DiscoveryObservationIntegrityError, TypeError, ValueError):
                    did_recover = self._quarantine_expired_discovery_run(
                        run_id,
                        observed_at,
                        reason="invalid_prefix",
                    )
                if did_recover:
                    recovered.append(run_id)
                continue
            with self._session_factory.begin() as session:
                run = self._load_run(session, run_id, for_update=True)
                if run.status != "running" or run.lease_expires_at is None or run.lease_expires_at > observed_at:
                    continue
                status = "cancelled" if run.cancel_requested else "failed"
                stage = "lease_expired_cancelled" if run.cancel_requested else "lease_expired"
                summary = dict(run.result_summary or {})
                summary.update(
                    {
                        "lease_recovered": True,
                        "expired_attempt": run.attempt,
                    }
                )
                terminal = self._terminal_snapshot(
                    session,
                    run,
                    status=status,
                    stage=stage,
                    summary=summary,
                    error_message=(None if status == "cancelled" else "execution owner lease expired"),
                )
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "running",
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at <= observed_at,
                        Run.state_version == run.state_version,
                    )
                    .values(status=terminal.status)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:
                    continue
                session.expire(run)
                run = self._load_run(session, run_id)
                context = session.get(RunExecutionContext, run_id)
                if context is None:
                    raise RuntimeError(f"run {run_id} has no execution context")
                self._apply_finalization(
                    session,
                    run,
                    terminal,
                    terminal.sha256(),
                    context.context_sha256,
                    observed_at,
                )
                recovered.append(run_id)
        return recovered

    def _quarantine_expired_discovery_run(
        self,
        run_id: str,
        observed_at: datetime,
        *,
        reason: str,
    ) -> bool:
        """Seal an unreadable expired prefix as failed, without projecting it."""

        with self._session_factory.begin() as session:
            run = self._load_run(session, run_id, for_update=True)
            if (
                run.status != "running"
                or run.job_type not in {"ip_discovery", "bacnet_discovery"}
                or run.owner_token is None
                or run.attempt < 1
                or run.terminal_at is not None
                or run.result_sha256 is not None
                or run.lease_expires_at is None
                or run.lease_expires_at > observed_at
                or session.get(RunResult, run_id) is not None
                or session.get(RunSeal, run_id) is not None
            ):
                return False
            terminal_cursor, observation_count = session.execute(
                select(
                    func.coalesce(func.max(RunDiscoveryObservation.id), 0),
                    func.count(RunDiscoveryObservation.id),
                ).where(
                    RunDiscoveryObservation.run_id == run_id,
                    RunDiscoveryObservation.attempt == run.attempt,
                )
            ).one()
            terminal_status = "cancelled" if run.cancel_requested else "failed"
            summary = {
                **dict(run.result_summary or {}),
                "lease_recovered": True,
                "expired_attempt": run.attempt,
                "provider_drained": False,
                "validation_incomplete": True,
                "acceptance_eligible": False,
                "observation_prefix_quarantined": True,
                "observation_quarantine_v1": {
                    "schema_version": "1.0",
                    "attempt": run.attempt,
                    "observation_count": int(observation_count or 0),
                    "terminal_cursor": int(terminal_cursor or 0),
                    "reason": reason,
                },
            }
            terminal = self._terminal_snapshot(
                session,
                run,
                status=terminal_status,
                stage=(
                    "lease_expired_cancelled_observation_quarantined"
                    if terminal_status == "cancelled"
                    else "lease_expired_observation_quarantined"
                ),
                summary=summary,
                error_message=(
                    None
                    if terminal_status == "cancelled"
                    else "committed observation evidence failed recovery validation"
                ),
            )
            self._add_conflict(
                session,
                run_id,
                operation="recover_expired_discovery",
                reason=f"observation_prefix_quarantined_{reason}",
                owner_token=run.owner_token,
            )
            guarded = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == "running",
                    Run.owner_token == run.owner_token,
                    Run.attempt == run.attempt,
                    Run.lease_expires_at.is_not(None),
                    Run.lease_expires_at <= observed_at,
                    Run.terminal_at.is_(None),
                    Run.result_sha256.is_(None),
                )
                .values(status=terminal.status)
                .execution_options(synchronize_session=False)
            )
            if guarded.rowcount != 1:
                return False
            session.expire(run)
            run = self._load_run(session, run_id)
            context = session.get(RunExecutionContext, run_id)
            if context is None:
                raise RuntimeError(f"run {run_id} has no execution context")
            self._apply_finalization(
                session,
                run,
                terminal,
                terminal.sha256(),
                context.context_sha256,
                observed_at,
            )
            return True

    def _recover_expired_discovery_run(
        self,
        run_id: str,
        observed_at: datetime,
    ) -> bool:
        """Seal an expired attempt's committed prefix without claiming drain."""

        while True:
            with self._query_session_factory() as session:
                captured = session.execute(
                    select(
                        Run.owner_token,
                        Run.attempt,
                        Run.status,
                        Run.lease_expires_at,
                    ).where(Run.id == run_id)
                ).one_or_none()
                if captured is None:
                    return False
                owner_token, attempt, status, lease_expires_at = captured
                if (
                    status != "running"
                    or owner_token is None
                    or attempt < 1
                    or lease_expires_at is None
                    or lease_expires_at > observed_at
                ):
                    return False
                state = self._validated_discovery_state_snapshot(
                    session,
                    run_id,
                    attempt,
                )
            fold = self._fold_discovery_prefix(state)

            with self._session_factory.begin() as session:
                run = self._load_run(session, run_id, for_update=True)
                current_state = self._validated_discovery_state_snapshot(
                    session,
                    run_id,
                    attempt,
                )
                if current_state != state:
                    continue
                if (
                    run.status != "running"
                    or run.job_type not in {"ip_discovery", "bacnet_discovery"}
                    or run.owner_token != owner_token
                    or run.attempt != attempt
                    or run.terminal_at is not None
                    or run.result_sha256 is not None
                    or run.lease_expires_at is None
                    or run.lease_expires_at > observed_at
                    or session.get(RunResult, run_id) is not None
                    or session.get(RunSeal, run_id) is not None
                ):
                    return False
                terminal_status = "cancelled" if run.cancel_requested else "failed"
                legacy = self._terminal_snapshot(
                    session,
                    run,
                    status=terminal_status,
                    stage=("lease_expired_cancelled" if terminal_status == "cancelled" else "lease_expired"),
                    summary={
                        **dict(run.result_summary or {}),
                        "lease_recovered": True,
                        "expired_attempt": attempt,
                        "provider_drained": False,
                        "validation_incomplete": True,
                        "acceptance_eligible": False,
                    },
                    error_message=(None if terminal_status == "cancelled" else "execution owner lease expired"),
                )
                terminal = self._discovery_terminal_result(run, legacy, fold)
                guarded = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == "running",
                        Run.owner_token == owner_token,
                        Run.attempt == attempt,
                        Run.lease_expires_at.is_not(None),
                        Run.lease_expires_at <= observed_at,
                        Run.terminal_at.is_(None),
                        Run.result_sha256.is_(None),
                    )
                    .values(status=terminal.status)
                    .execution_options(synchronize_session=False)
                )
                if guarded.rowcount != 1:
                    return False
                session.expire(run)
                run = self._load_run(session, run_id)
                context = session.get(RunExecutionContext, run_id)
                if context is None:
                    raise RuntimeError(f"run {run_id} has no execution context")
                self._apply_finalization(
                    session,
                    run,
                    terminal,
                    terminal.sha256(),
                    context.context_sha256,
                    observed_at,
                )
                return True

    def get_protocol_conflict(self, protocol_key: str) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ActiveProtocolSlot.run_id).where(ActiveProtocolSlot.protocol_key == protocol_key)
            )

    def acquire_protocol_slot(
        self,
        run_id: str,
        protocol_key: str,
        *,
        owner_token: str | None = None,
        now: datetime | None = None,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                self._load_run(session, run_id)
                session.add(
                    ActiveProtocolSlot(
                        protocol_key=protocol_key,
                        run_id=run_id,
                        owner_token=owner_token,
                        acquired_at=now or _utcnow(),
                    )
                )
                session.flush()
        except IntegrityError as error:
            active_run_id = self.get_protocol_conflict(protocol_key)
            if active_run_id is not None:
                raise ProtocolConflictError(protocol_key, active_run_id) from error
            raise

    def get_seal(self, run_id: str) -> RunSealViewV1:
        with self._session_factory() as session:
            seal = session.get(RunSeal, run_id)
            if seal is None:
                raise FileNotFoundError(run_id)
            return RunSealViewV1(
                run_id=run_id,
                terminal_status=seal.terminal_status,
                context_sha256=seal.context_sha256,
                result_sha256=seal.result_sha256,
                sealed_at=seal.sealed_at,
            )

    def conflict_count(self, run_id: str) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(RunLifecycleConflict).where(RunLifecycleConflict.run_id == run_id)
                )
                or 0
            )

    # -- internal transaction helpers --------------------------------------

    def _apply_finalization(
        self,
        session: Session,
        run: Run,
        terminal: TerminalResultV1,
        result_sha256: str,
        context_sha256: str,
        terminal_at: datetime,
    ) -> None:
        payload = json_safe_value(terminal)
        summary = dict(payload["summary"])
        issues = list(payload["issues"])
        devices = list(payload["devices"])
        points = list(payload["points"])
        topics = list(payload["topics"])
        terminal_stage = str(payload["stage"])
        error_message = payload.get("error_message")
        session.add(
            RunResult(
                run_id=run.id,
                schema_version=terminal.schema_version,
                terminal_status=terminal.status,
                terminal_stage=terminal_stage,
                summary=summary,
                result_payload=payload,
                result_sha256=result_sha256,
                created_at=terminal_at,
            )
        )
        session.flush()
        self._inject_fault("after_result")

        for model in (RunIssue, DiscoveredDevice, DiscoveredPoint, DiscoveredTopic):
            session.execute(delete(model).where(model.run_id == run.id))
        session.flush()
        for position, issue in enumerate(issues):
            record = ValidationIssueRecord.model_validate(issue)
            session.add(
                RunIssue(
                    run_id=run.id,
                    position=position,
                    **record.model_dump(),
                )
            )
        for position, device in enumerate(devices):
            session.add(self._device_row(run.id, position, device, terminal_at))
        for position, point in enumerate(points):
            session.add(self._point_row(run.id, position, point, terminal_at))
        for position, topic in enumerate(topics):
            session.add(self._topic_row(run.id, position, topic, terminal_at))
        session.flush()
        self._inject_fault("after_evidence")

        run.status = terminal.status
        run.stage = terminal_stage
        run.progress_percent = 100
        run.result_summary = summary
        run.error_message = error_message
        run.terminal_at = terminal_at
        run.result_sha256 = result_sha256
        run.heartbeat_at = terminal_at
        run.lease_expires_at = None
        run.state_version += 1
        run.updated_at = terminal_at
        session.add(
            RunSeal(
                run_id=run.id,
                terminal_status=terminal.status,
                context_sha256=context_sha256,
                result_sha256=result_sha256,
                sealed_at=terminal_at,
            )
        )
        session.execute(delete(ActiveProtocolSlot).where(ActiveProtocolSlot.run_id == run.id))
        session.flush()
        self._inject_fault("before_commit")

    def _terminal_snapshot(
        self,
        session: Session,
        run: Run,
        *,
        status: str,
        stage: str,
        summary: dict[str, Any],
        error_message: str | None,
    ) -> TerminalResultV1:
        issues = [
            ValidationIssueRecord.model_validate(
                {field: getattr(issue, field) for field in ValidationIssueRecord.model_fields}
            ).model_dump(mode="json")
            for issue in session.scalars(
                select(RunIssue).where(RunIssue.run_id == run.id).order_by(RunIssue.position, RunIssue.id)
            )
        ]
        devices = [
            self._device_payload(row)
            for row in session.scalars(
                select(DiscoveredDevice)
                .where(DiscoveredDevice.run_id == run.id)
                .order_by(DiscoveredDevice.position, DiscoveredDevice.id)
            )
        ]
        points = [
            self._point_payload(row)
            for row in session.scalars(
                select(DiscoveredPoint)
                .where(DiscoveredPoint.run_id == run.id)
                .order_by(DiscoveredPoint.position, DiscoveredPoint.id)
            )
        ]
        topics = [
            self._topic_payload(row)
            for row in session.scalars(
                select(DiscoveredTopic)
                .where(DiscoveredTopic.run_id == run.id)
                .order_by(DiscoveredTopic.position, DiscoveredTopic.id)
            )
        ]
        return TerminalResultV1.model_validate(
            {
                "status": status,
                "stage": stage,
                "summary": summary,
                "issues": issues,
                "devices": devices,
                "points": points,
                "topics": topics,
                "error_message": error_message,
            }
        )

    @staticmethod
    def _device_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredDevice:
        columns = ("project_id", "site_id", "address", "device_type", "name", "vendor", "model")
        return DiscoveredDevice(
            run_id=run_id,
            position=position,
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
            **{key: payload.get(key) for key in columns},
        )

    @staticmethod
    def _point_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredPoint:
        return DiscoveredPoint(
            run_id=run_id,
            position=position,
            device_ref=payload.get("device_ref"),
            point_id=payload.get("point_id"),
            point_name=payload.get("point_name"),
            observed_value=dict(payload.get("observed_value") or {}),
            units=payload.get("units"),
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
        )

    @staticmethod
    def _topic_row(
        run_id: str,
        position: int,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DiscoveredTopic:
        return DiscoveredTopic(
            run_id=run_id,
            position=position,
            topic=str(payload.get("topic") or ""),
            last_payload=dict(payload.get("last_payload") or {}),
            message_count=int(payload.get("message_count") or 0),
            attributes=dict(payload.get("attributes") or {}),
            created_at=created_at,
        )

    @staticmethod
    def _device_payload(row: DiscoveredDevice) -> dict[str, Any]:
        return {
            "project_id": row.project_id,
            "site_id": row.site_id,
            "address": row.address,
            "device_type": row.device_type,
            "name": row.name,
            "vendor": row.vendor,
            "model": row.model,
            "attributes": dict(row.attributes or {}),
        }

    @staticmethod
    def _point_payload(row: DiscoveredPoint) -> dict[str, Any]:
        return {
            "device_ref": row.device_ref,
            "point_id": row.point_id,
            "point_name": row.point_name,
            "observed_value": dict(row.observed_value or {}),
            "units": row.units,
            "attributes": dict(row.attributes or {}),
        }

    @staticmethod
    def _topic_payload(row: DiscoveredTopic) -> dict[str, Any]:
        return {
            "topic": row.topic,
            "last_payload": dict(row.last_payload or {}),
            "message_count": row.message_count,
            "attributes": dict(row.attributes or {}),
        }

    def _add_conflict(
        self,
        session: Session,
        run_id: str,
        *,
        operation: str,
        reason: str,
        owner_token: str | None = None,
        attempted_status: str | None = None,
        attempted_sha256: str | None = None,
    ) -> None:
        if session.get(Run, run_id) is None:
            return
        fingerprint = hashlib.sha256(owner_token.encode("utf-8")).hexdigest()[:16] if owner_token else None
        session.add(
            RunLifecycleConflict(
                run_id=run_id,
                operation=operation,
                reason=reason,
                owner_token_fingerprint=fingerprint,
                attempted_status=attempted_status,
                attempted_sha256=attempted_sha256,
                observed_at=_utcnow(),
            )
        )

    def _load_run(self, session: Session, run_id: str, *, for_update: bool = False) -> Run:
        statement = select(Run).where(Run.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        run = session.scalar(statement)
        if run is None:
            raise FileNotFoundError(run_id)
        return run

    def _inject_fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    @staticmethod
    def _dispatch_view(row: RunDispatch) -> RunDispatchV1:
        return RunDispatchV1(
            dispatch_id=row.dispatch_id,
            run_id=row.run_id,
            state=row.state,
            publish_attempts=row.publish_attempts,
            created_at=row.created_at,
            published_at=row.published_at,
        )
