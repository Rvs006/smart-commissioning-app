"""Shared engine framework: context, throttling, results, and the run wrapper.

This is the stable contract every concrete discovery/validation engine builds
on. It is pure-asyncio + standard library + the existing
``smart_commissioning_core`` contracts; importing it never touches a network or
hardware dependency.

Lifecycle (mirrors the existing ``process_*_run`` processors so frontend
polling keeps working):

    1. ``run_engine`` sets the run ``running`` (stage/progress).
    2. The engine callable receives an :class:`EngineContext` and returns an
       :class:`EngineResult`.
    3. On success the wrapper merges ``discovered_assets`` + ``result_summary_extra``
       into ``result_summary``, replaces issues, persists structured records via
       the injected persister, and sets a terminal status:
       ``succeeded`` (default), or ``cancelled`` if the context was cancelled
       mid-run / the engine returned ``status_override="cancelled"``.
    4. On exception the wrapper sets ``failed`` with a SANITIZED error message
       (raw exception text is never surfaced — it may contain credentials).

Concurrency safety: :class:`Throttle` bounds in-flight work with an
``asyncio.Semaphore`` AND spaces dispatches with a simple async token-bucket so
an engine cannot hammer a live BMS/OT network. :func:`Throttle.run_throttled`
checks ``ctx.is_cancelled()`` between dispatches and returns partial results
when cancellation is requested.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_store import RunStore

T = TypeVar("T")

# A callable returning whether cooperative cancellation has been requested.
CancelChecker = Callable[[], bool]

# Persists structured discovery records to the database. Receives the run id and
# the engine's ``structured_records`` list (plain dicts). Injected by the wiring
# agent (typically backed by DiscoveryRepository); defaults to a no-op so the
# framework is usable/testable without a database.
StructuredRecordPersister = Callable[[str, "Sequence[dict[str, Any]]"], None]


def _noop_persister(_run_id: str, _records: "Sequence[dict[str, Any]]") -> None:
    """Default structured-record persister: does nothing."""


def _never_cancelled() -> bool:
    return False


def make_cancel_checker(run_store: object, run_id: str) -> CancelChecker:
    """Build a cooperative-cancellation checker from a (possibly) cancellable store.

    Uses ``is_cancel_requested`` when the store advertises it
    (CancellableRunStore); otherwise cancellation is never requested. Never
    raises — a missing method or any store error reads as not-cancelled, so
    cancellation can never crash a run. Shared by every engine + the API/worker
    dispatch so the four identical copies don't drift.
    """
    checker = getattr(run_store, "is_cancel_requested", None)
    if not callable(checker):
        return _never_cancelled

    def _check() -> bool:
        try:
            return bool(checker(run_id))
        except Exception:
            return False

    return _check


@dataclass(slots=True)
class ThrottleConfig:
    """Conservative concurrency/rate limits for active-scan engines.

    Defaults are intentionally gentle so an engine pointed at a real
    building-automation network does not overwhelm controllers or a broker.

    Attributes:
        max_concurrency: maximum number of unit-tasks in flight at once.
        rate_limit_per_sec: max dispatch rate (tasks started per second).
            ``None`` disables rate limiting (concurrency bound still applies).
        connect_timeout_s: advisory per-connection timeout engines should pass
            to their transport. The framework does not open connections itself;
            it carries this so every engine uses a consistent default.
    """

    max_concurrency: int = 16
    rate_limit_per_sec: float | None = 10.0
    connect_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.rate_limit_per_sec is not None and self.rate_limit_per_sec <= 0:
            raise ValueError("rate_limit_per_sec must be > 0 or None")
        if self.connect_timeout_s <= 0:
            raise ValueError("connect_timeout_s must be > 0")


@dataclass(slots=True)
class EngineContext:
    """Everything an engine needs to run, with safety config injected.

    Attributes:
        run_id: the run this engine execution belongs to.
        parameters: the run's parameters dict (carries scan authorization, the
            target spec, etc. — see ``engines.safety``).
        run_store: the shared :class:`RunStore` used to update status/summary/issues.
        execution_mode: free-form label mirrored into ``result_summary``
            (e.g. ``"inline_local_fallback"``, ``"dramatiq_redis"``).
        throttle: concurrency/rate-limit config (see :class:`ThrottleConfig`).
        dry_run: when True, active engines enumerate a plan and perform NO I/O.
        _is_cancelled: cancellation-check callable; use :meth:`is_cancelled`.
    """

    run_id: str
    parameters: dict[str, Any]
    run_store: RunStore
    execution_mode: str
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    dry_run: bool = False
    _is_cancelled: CancelChecker = field(default=_never_cancelled, repr=False)

    def is_cancelled(self) -> bool:
        """Return True if cooperative cancellation has been requested.

        Engines should call this between units of work and stop early when it
        returns True. Never raises — a misbehaving checker is treated as
        "not cancelled" so cancellation logic can never crash an engine.
        """
        try:
            return bool(self._is_cancelled())
        except Exception:
            return False


@dataclass(slots=True)
class EngineResult:
    """The structured outcome an engine returns to :func:`run_engine`.

    Attributes:
        discovered_assets: assets in the shape ``DiscoveryResultsResponse``
            reads from ``result_summary["discovered_assets"]`` (list of dicts).
        structured_records: rows for DB persistence (plain dicts) handed to the
            injected structured-record persister. Shape is engine-defined; the
            discovery engines use the DiscoveryRepository row shapes.
        issues: validation issues persisted via ``run_store.replace_issues``.
        result_summary_extra: extra keys merged into ``result_summary`` (e.g.
            ``{"dry_run_plan": {...}}``, counts, scan window metadata).
        status_override: force a terminal status. ``None`` means the wrapper
            decides (``succeeded``, or ``cancelled`` if the context was
            cancelled). May be set to ``"cancelled"``, ``"failed"`` or
            ``"succeeded"`` to override.
        error_message: an operator-facing message stored on the run record when
            the engine returns a self-diagnosed failure (``status_override=
            "failed"``). Unlike a raised exception — which the wrapper replaces
            with a sanitized generic message to avoid leaking credentials — this
            is a message the engine has vetted as safe, so the UI can show it.
    """

    discovered_assets: list[dict[str, Any]] = field(default_factory=list)
    structured_records: list[dict[str, Any]] = field(default_factory=list)
    issues: list[ValidationIssueRecord | dict[str, Any]] = field(default_factory=list)
    result_summary_extra: dict[str, Any] = field(default_factory=dict)
    status_override: str | None = None
    error_message: str | None = None


# An engine is any (sync or async) callable taking an EngineContext and
# returning an EngineResult.
EngineCallable = Callable[[EngineContext], "EngineResult | Awaitable[EngineResult]"]


class _AsyncRateLimiter:
    """Minimal async token bucket: at most ``rate`` dispatches per second.

    Capacity 1 (strict spacing). ``acquire`` sleeps until the next token is
    available. Single-event-loop use only (no cross-thread locking needed).
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._min_interval = 1.0 / rate_per_sec
        self._next_allowed: float | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_allowed is None or now >= self._next_allowed:
            self._next_allowed = now + self._min_interval
            return
        wait = self._next_allowed - now
        self._next_allowed += self._min_interval
        await asyncio.sleep(wait)


class Throttle:
    """Bounds concurrency (semaphore) and dispatch rate (token bucket).

    Use as an async context manager around a single unit of work::

        async with throttle.slot():
            await contact_one_target(...)

    or drive a batch with :meth:`run_throttled`, which additionally honours
    cooperative cancellation between dispatches.
    """

    def __init__(self, config: ThrottleConfig) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._rate_limiter = (
            _AsyncRateLimiter(config.rate_limit_per_sec)
            if config.rate_limit_per_sec is not None
            else None
        )

    @property
    def config(self) -> ThrottleConfig:
        return self._config

    def slot(self) -> "_ThrottleSlot":
        """Return an async context manager that holds one concurrency slot.

        Rate limiting is applied on ENTRY (before the body runs) so dispatches
        are spaced regardless of how long each body takes.
        """
        return _ThrottleSlot(self._semaphore, self._rate_limiter)

    async def run_throttled(
        self,
        coro_factories: Iterable[Callable[[], Awaitable[T]]],
        ctx: EngineContext,
    ) -> list[T]:
        """Run unit-tasks under the throttle, stopping early on cancellation.

        ``coro_factories`` is an iterable of zero-arg callables each returning a
        fresh awaitable (factories, not coroutines, so cancelled units are
        never instantiated — avoiding un-awaited-coroutine warnings).

        Behaviour:
            * Checks ``ctx.is_cancelled()`` BEFORE dispatching each unit; stops
              dispatching as soon as cancellation is requested.
            * Acquires the concurrency slot IN the dispatch loop, so the loop
              yields to the event loop whenever the bound is reached. That lets
              already-running units progress (and observe/raise cancellation)
              before the next cancellation check — so early-stop works whether
              cancellation comes from outside or from the units themselves.
            * Awaits already-dispatched units and returns their results
              (partial results on cancellation), preserving dispatch order.

        Returns the list of results from units that actually ran.
        """
        tasks: list[asyncio.Task[T]] = []

        async def _guarded(factory: Callable[[], Awaitable[T]]) -> T:
            try:
                if self._rate_limiter is not None:
                    await self._rate_limiter.acquire()
                return await factory()
            finally:
                self._semaphore.release()

        for factory in coro_factories:
            if ctx.is_cancelled():
                break
            # Acquire here (not inside the task) so this loop blocks — and yields
            # to the event loop — once max_concurrency units are in flight. The
            # task is responsible for releasing the slot (see _guarded).
            await self._semaphore.acquire()
            if ctx.is_cancelled():
                # Cancellation observed while waiting for a slot: release and stop.
                self._semaphore.release()
                break
            tasks.append(asyncio.ensure_future(_guarded(factory)))

        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))


class _ThrottleSlot:
    """Async context manager holding one concurrency slot + rate token."""

    __slots__ = ("_semaphore", "_rate_limiter")

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        rate_limiter: _AsyncRateLimiter | None,
    ) -> None:
        self._semaphore = semaphore
        self._rate_limiter = rate_limiter

    async def __aenter__(self) -> "_ThrottleSlot":
        await self._semaphore.acquire()
        try:
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire()
        except BaseException:
            self._semaphore.release()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._semaphore.release()


# -- run wrapper ------------------------------------------------------------

# Sanitized message surfaced to the API/frontend on engine failure. The raw
# exception is intentionally NOT included: parameters/transport errors can echo
# back credentials, hostnames, or tokens. The engine should attach any safe,
# user-facing detail to result_summary_extra itself.
_SANITIZED_FAILURE_MESSAGE = (
    "Engine execution failed. See server logs for details "
    "(error detail withheld to avoid leaking credentials)."
)

# Stage labels mirror the existing processors' "<thing>_complete/_failed" style.
_STAGE_RUNNING = "engine_running"
_STAGE_SUCCEEDED = "engine_complete"
_STAGE_CANCELLED = "engine_cancelled"
_STAGE_FAILED = "engine_failed"


def _terminal_status(result: EngineResult, ctx: EngineContext) -> str:
    """Resolve the terminal status for a successful engine return."""
    if result.status_override in {"cancelled", "failed", "succeeded"}:
        return result.status_override
    if ctx.is_cancelled():
        return "cancelled"
    return "succeeded"


def _stage_for(status: str) -> str:
    return {
        "succeeded": _STAGE_SUCCEEDED,
        "cancelled": _STAGE_CANCELLED,
        "failed": _STAGE_FAILED,
    }.get(status, _STAGE_SUCCEEDED)


def _apply_success(
    ctx: EngineContext,
    result: EngineResult,
    persist_records: StructuredRecordPersister,
) -> Any:
    """Persist a successful engine result and set the terminal status.

    Order mirrors the existing processors: result_summary first (so polling
    sees data before the terminal flip), then issues, then structured records,
    then the status flip last.
    """
    summary: dict[str, Any] = {
        "discovered_assets": list(result.discovered_assets),
        "execution_mode": ctx.execution_mode,
        "dry_run": ctx.dry_run,
        **result.result_summary_extra,
    }
    ctx.run_store.update_result_summary(ctx.run_id, summary)
    ctx.run_store.replace_issues(ctx.run_id, list(result.issues))

    if result.structured_records:
        persist_records(ctx.run_id, list(result.structured_records))

    status = _terminal_status(result, ctx)
    return ctx.run_store.update_run_status(
        ctx.run_id,
        status=status,
        stage=_stage_for(status),
        progress_percent=100,
        error_message=result.error_message,
    )


def _apply_failure(ctx: EngineContext) -> Any:
    """Set the run failed with a sanitized message (no raw exception text)."""
    return ctx.run_store.update_run_status(
        ctx.run_id,
        status="failed",
        stage=_STAGE_FAILED,
        progress_percent=100,
        error_message=_SANITIZED_FAILURE_MESSAGE,
    )


async def run_engine_async(
    ctx: EngineContext,
    engine: EngineCallable,
    *,
    persist_records: StructuredRecordPersister = _noop_persister,
) -> Any:
    """Async run wrapper. ``engine`` may be sync or async.

    Sets ``running``, invokes the engine, persists results, and sets the
    terminal status (``succeeded``/``cancelled``/``failed``). Returns whatever
    ``run_store.update_run_status`` returns for the terminal flip (the updated
    run record), matching the existing processors.

    On ANY exception from the engine, the run is set ``failed`` with a sanitized
    message and the exception is swallowed (the run record carries the failure),
    mirroring ``process_udmi_validation_run`` / ``process_mqtt_config_publish_run``.
    """
    ctx.run_store.update_run_status(
        ctx.run_id,
        status="running",
        stage=_STAGE_RUNNING,
        progress_percent=15,
    )
    try:
        outcome = engine(ctx)
        if inspect.isawaitable(outcome):
            result = await outcome
        else:
            result = outcome
        if not isinstance(result, EngineResult):
            raise TypeError(
                "engine callable must return an EngineResult, "
                f"got {type(result).__name__}"
            )
        return _apply_success(ctx, result, persist_records)
    except Exception:
        return _apply_failure(ctx)


def run_engine(
    ctx: EngineContext,
    engine: EngineCallable,
    *,
    persist_records: StructuredRecordPersister = _noop_persister,
) -> Any:
    """Synchronous entrypoint wrapping :func:`run_engine_async`.

    Convenience for callers (the worker / inline-fallback path) that are not
    already inside an event loop. Raises ``RuntimeError`` if called from within
    a running loop — use :func:`run_engine_async` directly there.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_engine_async(ctx, engine, persist_records=persist_records))
    raise RuntimeError(
        "run_engine() was called from within a running event loop; "
        "await run_engine_async(...) instead."
    )
