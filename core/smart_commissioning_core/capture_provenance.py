"""Shared capture provenance predicates used by run and report projections."""

from collections.abc import Mapping

_FAILED_TERMINATION_REASONS = frozenset(
    {
        "capture_unavailable",
        "missing_capture_topics",
        "broker_unreachable",
        "broker_not_configured",
        "dns_resolution_failed",
        "authentication_error",
        "capture_error",
    }
)


def capture_acceptance_eligible(
    status: object,
    summary: Mapping[str, object],
) -> bool | None:
    """Return field-acceptance eligibility without inferring absent capture data."""

    if summary.get("broker_capture_attempted") is not True:
        return None
    blocking_issue_count = summary.get("blocking_issue_count")
    if isinstance(blocking_issue_count, int) and not isinstance(blocking_issue_count, bool):
        if blocking_issue_count > 0:
            return False
    return (
        str(status) == "succeeded"
        and summary.get("window_completed") is True
        and summary.get("termination_reason") == "window_elapsed"
    )


def capture_outcome(status: object, summary: Mapping[str, object]) -> str:
    """Return one shared, honest label for every report surface.

    ``not_applicable`` is used for deterministic fixture/register validation
    where no broker capture was requested. A broker run is complete only when
    its configured window elapsed; a succeeded job with an early stop or
    capture failure remains visibly incomplete.
    """

    termination_reason = str(summary.get("termination_reason") or "").strip()
    if str(status) == "cancelled" or termination_reason == "cancelled":
        return "cancelled"
    if str(status) == "failed" or termination_reason in _FAILED_TERMINATION_REASONS:
        return "failed"
    if summary.get("broker_capture_attempted") is not True:
        return "not_applicable"
    if (
        str(status) == "succeeded"
        and summary.get("window_completed") is True
        and termination_reason == "window_elapsed"
    ):
        return "complete"
    return "incomplete"
