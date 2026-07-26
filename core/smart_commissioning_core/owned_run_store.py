"""Compatibility RunStore adapter bound to one fenced lifecycle owner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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

    @property
    def terminal_outcome(self) -> FinalizeOutcome | None:
        return self._terminal_outcome

    def heartbeat(self, *, lease_seconds: int = 60) -> bool:
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
            return {
                "run_id": run_id,
                "status": status if not outcome.conflict else "ownership_lost",
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
        self._require_run(run_id)
        return self._repository.is_cancel_requested(run_id, self.lease.owner_token)

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
