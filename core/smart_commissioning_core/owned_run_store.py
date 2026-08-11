"""Compatibility RunStore adapter bound to one fenced lifecycle owner."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_lifecycle import FinalizeOutcome, RunLeaseV1, TerminalResultV1

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class OwnershipLostError(RuntimeError):
    """Raised when an active engine tries to write after losing its lease."""


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

    def mark_ownership_lost(self) -> None:
        """Fence this in-memory executor after a heartbeat confirms loss."""

        with self._lifecycle_lock:
            self._ownership_lost.set()

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

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> dict[str, Any]:
        self._require_run(run_id)
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

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> dict[str, Any]:
        self._require_run(run_id)
        self._issues = [
            ValidationIssueRecord.model_validate(issue).model_dump(mode="json")
            for issue in issues
        ]
        if self._repository.is_cancel_requested(run_id, self.lease.owner_token):
            if not self._repository.update_progress(run_id, self.lease.owner_token):
                raise OwnershipLostError(run_id)
        return {"run_id": run_id, "issues": list(self._issues)}

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

        with self._lifecycle_lock:
            self._require_run(self.lease.run_id)
            self._repository.require_active_control(
                self.lease.run_id,
                self.lease.owner_token,
                self.lease.attempt,
                now=now,
            )

    def replace_devices(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._require_run(run_id)
        self._devices = [dict(record) for record in records]
        return len(self._devices)

    def replace_points(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._require_run(run_id)
        self._points = [dict(record) for record in records]
        return len(self._points)

    def replace_topics(self, run_id: str, records: Sequence[Mapping[str, Any]]) -> int:
        self._require_run(run_id)
        self._topics = [dict(record) for record in records]
        return len(self._topics)

    def replace_devices_and_points(
        self, run_id: str, records: Sequence[Mapping[str, Any]]
    ) -> int:
        self._require_run(run_id)
        devices: list[dict[str, Any]] = []
        points: list[dict[str, Any]] = []
        for record in records:
            payload = dict(record)
            target = points if ("point_id" in payload or "device_ref" in payload) else devices
            target.append(payload)
        self._devices = devices
        self._points = points
        return len(devices) + len(points)

    def _require_run(self, run_id: str) -> None:
        if run_id != self.lease.run_id:
            raise ValueError("owned run store cannot write another run")
        if self.ownership_lost:
            raise OwnershipLostError(run_id)
