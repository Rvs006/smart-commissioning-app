"""Compatibility RunStore adapter bound to one fenced lifecycle owner."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
    ObservationAppendOutcomeV1,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import json_safe_value
from smart_commissioning_core.run_lifecycle import FinalizeOutcome, RunLeaseV1, TerminalResultV1

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_CONTEXT_FAILURE_STAGE = "execution_context_unavailable"


class OwnershipLostError(RuntimeError):
    """Raised when an active engine tries to write after losing its lease."""


class RunFinalizingError(RuntimeError):
    """Raised when a provider mutates buffers after the observation sink closes."""


class OwnedRunStore:
    """Adapt legacy engine writes to the lifecycle-v2 transaction contract.

    Progress remains visible through token-fenced updates. Final summary,
    issues, devices, points, topics, and status are buffered and passed to one
    ``finalize_run`` call when the processor performs its terminal status write.
    """

    def __init__(self, repository: RunLifecycleRepository, lease: RunLeaseV1) -> None:
        self._repository = repository
        self.lease = lease
        self._summary: dict[str, Any] = {}
        self._issues: list[dict[str, Any]] = []
        self._devices: list[dict[str, Any]] = []
        self._points: list[dict[str, Any]] = []
        self._topics: list[dict[str, Any]] = []
        self._terminal_outcome: FinalizeOutcome | None = None
        self._ownership_lost = threading.Event()
        self._closing = False
        self._active_mutations = 0
        self._discovery_protocol_resolved = False
        self._discovery_protocol: str | None = None
        self._projection_generations: dict[str, int] = {}
        self._projection_widths: dict[str, int] = {}
        # Heartbeat and terminal finalization must not cross. Without this lock,
        # finalization can commit, heartbeat can observe the now-terminal row,
        # and the in-memory store can be marked stale just before the finalizer
        # records its successful outcome.
        self._lifecycle_lock = threading.RLock()

    @property
    def terminal_outcome(self) -> FinalizeOutcome | None:
        with self._lifecycle_lock:
            return self._terminal_outcome

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def mark_ownership_lost(self) -> bool:
        """Fence this in-memory executor after a heartbeat confirms loss."""

        with self._lifecycle_lock:
            if self._closing and self._terminal_outcome is None:
                return False
            self._ownership_lost.set()
            return True

    def heartbeat(self, *, lease_seconds: int = 60) -> bool:
        with self._lifecycle_lock:
            if self.ownership_lost:
                return False
        return self._repository.heartbeat(
            self.lease.run_id,
            self.lease.owner_token,
            lease_seconds=lease_seconds,
        )

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        self._require_run(run_id)
        if status in _TERMINAL_STATUSES:
            protocol = self._resolve_discovery_protocol()
            if protocol is not None:
                return self._finalize_discovery_status(
                    run_id,
                    status=status,
                    stage=stage,
                    error_message=error_message,
                )
            with self._lifecycle_lock:
                self._require_run(run_id)
                terminal = TerminalResultV1(
                    status=status,
                    stage=stage or f"engine_{status}",
                    summary=self._summary,
                    issues=self._issues,
                    devices=self._devices,
                    points=self._points,
                    topics=self._topics,
                    error_message=error_message,
                )
                outcome = self._repository.finalize_run(
                    run_id, self.lease.owner_token, terminal
                )
                self._terminal_outcome = outcome
                if outcome.conflict:
                    self._ownership_lost.set()
            return {
                "run_id": run_id,
                "status": (
                    terminal.status if outcome.applied else "ownership_lost"
                    if outcome.conflict
                    else status
                ),
                "stage": terminal.stage,
                "progress_percent": 100,
                "result_summary": dict(self._summary),
                "issues": list(self._issues),
                "error_message": error_message,
                "result_sha256": outcome.result_sha256,
            }
        self._begin_mutation(run_id)
        try:
            accepted = self._repository.update_progress(
                run_id,
                self.lease.owner_token,
                stage=stage,
                progress_percent=progress_percent,
                error_message=error_message,
            )
            if not accepted:
                raise OwnershipLostError(run_id)
            return {
                "run_id": run_id,
                "status": status,
                "stage": stage,
                "progress_percent": progress_percent,
                "result_summary": dict(self._summary),
                "issues": list(self._issues),
                "error_message": error_message,
            }
        finally:
            self._end_mutation()

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> dict[str, Any]:
        self._begin_mutation(run_id)
        try:
            if merge:
                self._summary.update(result_summary)
            else:
                self._summary = dict(result_summary)
            accepted = self._repository.update_progress(
                run_id,
                self.lease.owner_token,
                summary=result_summary,
                merge_summary=merge,
            )
            if not accepted:
                raise OwnershipLostError(run_id)
            return {"run_id": run_id, "result_summary": dict(self._summary)}
        finally:
            self._end_mutation()

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> dict[str, Any]:
        self._begin_mutation(run_id)
        try:
            normalized = [
                ValidationIssueRecord.model_validate(issue).model_dump(mode="json")
                for issue in issues
            ]
            protocol = self._resolve_discovery_protocol()
            if protocol is not None:
                self._append_projection_records(
                    protocol,
                    collection="issues",
                    entity_kind="diagnostic",
                    records=normalized,
                )
            self._issues = normalized
            if self._repository.is_cancel_requested(run_id, self.lease.owner_token):
                if not self._repository.update_progress(run_id, self.lease.owner_token):
                    raise OwnershipLostError(run_id)
            return {"run_id": run_id, "issues": list(self._issues)}
        finally:
            self._end_mutation()

    def request_cancel(self, run_id: str) -> Any:
        self._require_run(run_id)
        return self._repository.request_cancel(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        if run_id != self.lease.run_id:
            raise ValueError("owned run store cannot read another run")
        if self.ownership_lost:
            return True
        return self._repository.is_cancel_requested(run_id, self.lease.owner_token)

    def require_active_control(self, *, now: datetime | None = None) -> None:
        """Prove this exact owner may dispatch one more outbound attempt.

        Unlike ``heartbeat()``, this check is read-only and never renews the
        lease. Denials and database errors propagate so callers fail closed.
        """

        self._begin_mutation(self.lease.run_id)
        try:
            self._repository.require_active_control(
                self.lease.run_id,
                self.lease.owner_token,
                self.lease.attempt,
                now=now,
            )
        finally:
            self._end_mutation()

    def replace_devices(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._begin_mutation(run_id)
        try:
            normalized = [dict(json_safe_value(dict(record))) for record in records]
            protocol = self._resolve_discovery_protocol()
            if protocol is not None:
                self._append_projection_records(
                    protocol,
                    collection="devices",
                    entity_kind="device",
                    records=normalized,
                )
            self._devices = normalized
            return len(self._devices)
        finally:
            self._end_mutation()

    def replace_points(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._begin_mutation(run_id)
        try:
            normalized = [dict(json_safe_value(dict(record))) for record in records]
            protocol = self._resolve_discovery_protocol()
            if protocol is not None:
                self._append_projection_records(
                    protocol,
                    collection="points",
                    entity_kind="object",
                    records=normalized,
                )
            self._points = normalized
            return len(self._points)
        finally:
            self._end_mutation()

    def replace_topics(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._begin_mutation(run_id)
        try:
            self._topics = [dict(record) for record in records]
            return len(self._topics)
        finally:
            self._end_mutation()

    def replace_devices_and_points(
        self, run_id: str, records: Sequence[Mapping[str, Any]]
    ) -> int:
        self._begin_mutation(run_id)
        try:
            devices: list[dict[str, Any]] = []
            points: list[dict[str, Any]] = []
            for record in records:
                payload = dict(json_safe_value(dict(record)))
                target = (
                    points
                    if ("point_id" in payload or "device_ref" in payload)
                    else devices
                )
                target.append(payload)
            protocol = self._resolve_discovery_protocol()
            if protocol is not None:
                self._append_projection_records(
                    protocol,
                    collection="devices",
                    entity_kind="device",
                    records=devices,
                )
                self._append_projection_records(
                    protocol,
                    collection="points",
                    entity_kind="object",
                    records=points,
                )
            self._devices = devices
            self._points = points
            return len(devices) + len(points)
        finally:
            self._end_mutation()

    def append_observation(
        self,
        observation: DiscoveryObservationInputV1 | Mapping[str, Any],
    ) -> ObservationAppendOutcomeV1:
        """Append through the executor-owned sink without exposing persistence."""

        self._begin_mutation(self.lease.run_id)
        try:
            return self._repository.append_discovery_observation(
                self.lease.run_id,
                self.lease.owner_token,
                self.lease.attempt,
                observation,
            )
        finally:
            self._end_mutation()

    def _append_projection_records(
        self,
        protocol: str,
        *,
        collection: str,
        entity_kind: str,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        normalized_records = [
            dict(json_safe_value(dict(record))) for record in records
        ]
        with self._lifecycle_lock:
            generation = self._projection_generations.get(collection, 0) + 1
            previous_width = self._projection_widths.get(collection, 0)
            self._projection_generations[collection] = generation
            self._projection_widths[collection] = len(normalized_records)

        for position in range(max(previous_width, len(normalized_records))):
            present = position < len(normalized_records)
            identity = f"projection_v1:{collection}:{position:08d}"
            projected_kind = (
                "host" if protocol == "ip" and entity_kind == "device" else entity_kind
            )
            projection: dict[str, Any] = {
                "collection": collection,
                "position": position,
                "present": present,
            }
            if present:
                projection["record"] = normalized_records[position]
            self._repository.append_discovery_observation(
                self.lease.run_id,
                self.lease.owner_token,
                self.lease.attempt,
                DiscoveryObservationInputV1(
                    protocol=protocol,
                    entity_kind=projected_kind,
                    entity_key=identity,
                    entity_version=generation,
                    event_key=f"{identity}:v{generation}",
                    phase="finalize",
                    outcome=(
                        "removed"
                        if not present
                        else "error"
                        if collection == "issues"
                        else "observed"
                    ),
                    payload_schema_version="1.0",
                    payload={"projection_v1": projection},
                ),
            )

    def _finalize_discovery_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None,
        error_message: str | None,
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            self._require_run(run_id)
            if self._closing or self._terminal_outcome is not None:
                raise RunFinalizingError(run_id)
            if self._active_mutations:
                raise RunFinalizingError(
                    f"{run_id}: provider writes are still active"
                )
            self._closing = True
            summary = dict(self._summary)
            issues = tuple(dict(item) for item in self._issues)
            devices = tuple(dict(item) for item in self._devices)
            points = tuple(dict(item) for item in self._points)
            topics = tuple(dict(item) for item in self._topics)
        terminal = TerminalResultV1(
            status=status,
            stage=stage or f"engine_{status}",
            summary=summary,
            issues=issues,
            devices=devices,
            points=points,
            topics=topics,
            error_message=error_message,
        )
        try:
            outcome = self._repository.finalize_discovery_run(
                run_id,
                self.lease.owner_token,
                self.lease.attempt,
                terminal,
            )
            if (
                outcome.conflict
                and outcome.reason == "run_scope_authority_invalid"
                and status == "failed"
                and terminal.stage == _CONTEXT_FAILURE_STAGE
            ):
                # A corrupt stored context cannot supply the trusted scope that
                # discovery folding requires. Preserve the already-detected
                # context-integrity failure as a fenced terminal seal instead.
                outcome = self._repository.finalize_run(
                    run_id,
                    self.lease.owner_token,
                    terminal,
                )
        except Exception:
            with self._lifecycle_lock:
                self._closing = False
            raise
        with self._lifecycle_lock:
            self._terminal_outcome = outcome
            if outcome.conflict:
                self._ownership_lost.set()
            self._closing = False
        return {
            "run_id": run_id,
            "status": (
                terminal.status
                if outcome.applied or outcome.idempotent
                else "ownership_lost"
                if outcome.conflict
                else status
            ),
            "stage": terminal.stage,
            "progress_percent": 100,
            "result_summary": summary,
            "issues": list(issues),
            "error_message": error_message,
            "result_sha256": outcome.result_sha256,
        }

    def _resolve_discovery_protocol(self) -> str | None:
        with self._lifecycle_lock:
            if self._discovery_protocol_resolved:
                return self._discovery_protocol
        resolver = getattr(self._repository, "get_owned_discovery_protocol", None)
        protocol = (
            resolver(
                self.lease.run_id,
                self.lease.owner_token,
                self.lease.attempt,
            )
            if resolver is not None
            else None
        )
        with self._lifecycle_lock:
            self._discovery_protocol = protocol
            self._discovery_protocol_resolved = True
            return protocol

    def _begin_mutation(self, run_id: str) -> None:
        with self._lifecycle_lock:
            self._require_run(run_id)
            if self._closing or self._terminal_outcome is not None:
                raise RunFinalizingError(run_id)
            self._active_mutations += 1

    def _end_mutation(self) -> None:
        with self._lifecycle_lock:
            self._active_mutations -= 1

    def _require_run(self, run_id: str) -> None:
        if run_id != self.lease.run_id:
            raise ValueError("owned run store cannot write another run")
        if self.ownership_lost:
            raise OwnershipLostError(run_id)
