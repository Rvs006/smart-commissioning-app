"""Small helpers for seeding lifecycle-v2 runs in report API tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def finish_run(
    service,
    run_id: str,
    *,
    status: str = "succeeded",
    stage: str = "done",
    error_message: str | None = None,
    summary: Mapping[str, Any] | None = None,
    issues: Sequence[Mapping[str, Any]] | None = None,
    devices: Sequence[Mapping[str, Any]] | None = None,
    points: Sequence[Mapping[str, Any]] | None = None,
    topics: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Claim, buffer, and seal a test run through the production finalizer."""
    owned = service.claim_owned_run(run_id)
    if owned is None:
        raise AssertionError(f"test run {run_id} could not be claimed")
    if summary is not None:
        owned.update_result_summary(run_id, dict(summary), merge=False)
    if issues is not None:
        owned.replace_issues(run_id, list(issues))
    if devices is not None:
        owned.replace_devices(run_id, devices)
    if points is not None:
        owned.replace_points(run_id, points)
    if topics is not None:
        owned.replace_topics(run_id, topics)
    result = owned.update_run_status(
        run_id,
        status=status,
        stage=stage,
        progress_percent=100,
        error_message=error_message,
    )
    if result.get("status") == "ownership_lost":
        raise AssertionError(f"test run {run_id} lost ownership before finalization")
