from typing import Any, Protocol

from smart_commissioning_core.records import ValidationIssueRecord


class RunStore(Protocol):
    """Minimal run persistence API required by the shared run processors.

    The API service satisfies this with its RunService; the worker satisfies it
    with its file-backed run store. Implementations may return whatever run
    representation they use internally (pydantic model, plain dict, ...) — the
    processors only pass the final return value back to the caller.
    """

    def update_run_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> Any: ...

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> Any: ...

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> Any: ...


class CancellableRunStore(RunStore, Protocol):
    """A RunStore that also supports cooperative cancellation.

    The engine framework (smart_commissioning_core.engines) uses
    ``is_cancel_requested`` to build an EngineContext cancellation checker, and
    the API exposes ``request_cancel`` to flip the flag. Implementations back
    these with the Run.cancel_requested column (see DbRunStore).
    """

    def request_cancel(self, run_id: str) -> Any: ...

    def is_cancel_requested(self, run_id: str) -> bool: ...
