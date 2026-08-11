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
    4. On exception the wrapper logs the traceback and sets ``failed`` with a
       SANITIZED error message (raw exception text is never surfaced — it may
       contain credentials). An engine that can diagnose itself should NOT use
       this path: returning ``EngineResult(status_override="failed",
       error_message=...)`` keeps its own vetted message, plus result_summary
       and issues, all of which this path discards.

Concurrency safety: :class:`Throttle` bounds in-flight work with an
``asyncio.Semaphore`` AND spaces dispatches with a simple async token-bucket so
an engine cannot hammer a live BMS/OT network. :func:`Throttle.run_throttled`
checks ``ctx.is_cancelled()`` between dispatches and returns partial results
when cancellation is requested.
"""

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_store import RunStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_THROTTLE_CONCURRENCY = 2_147_483_647
_EFFECTIVE_THROTTLE_FIELDS = frozenset({"max_concurrency", "rate_limit_per_sec", "connect_timeout_s"})
_MISSING = object()


class _SealedRuntimeDeadlineExceeded(RuntimeError):
    """Raised internally when a frozen discovery runtime ceiling expires."""


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
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or not 1 <= self.max_concurrency <= _MAX_THROTTLE_CONCURRENCY
        ):
            raise ValueError("max_concurrency must be a supported positive integer")
        if self.rate_limit_per_sec is not None and not _is_finite_positive_number(self.rate_limit_per_sec):
            raise ValueError("rate_limit_per_sec must be a finite positive number or None")
        if not _is_finite_positive_number(self.connect_timeout_s):
            raise ValueError("connect_timeout_s must be a finite positive number")


def resolve_throttle_config(
    parameters: Mapping[str, Any],
    *,
    max_concurrency: Any,
    rate_limit_per_sec: Any,
    connect_timeout_s: Any,
) -> ThrottleConfig:
    """Resolve one policy-bounded throttle for API and worker execution.

    Historical request fields may narrow the current policy. Wider values are
    clamped to its ceilings, malformed values fail closed, and only the legacy
    zero rate keeps its established meaning of "use the policy default".

    A frozen ``scan_contract_v1.effective_throttle`` has already been resolved
    before the execution context was sealed. When present, it is authoritative:
    all three fields must be positive finite values within the current policy,
    and the exact frozen values are returned without consulting the raw request
    fields. A frozen value above current policy is rejected instead of clamped,
    because silently changing sealed execution input would break replay parity.
    """
    policy_concurrency = _policy_positive_int(
        max_concurrency,
        label="max_concurrency",
    )
    policy_rate = _policy_positive_float(
        rate_limit_per_sec,
        label="rate_limit_per_sec",
    )
    policy_timeout = _policy_positive_float(
        connect_timeout_s,
        label="connect_timeout_s",
    )

    frozen = _frozen_effective_throttle(parameters)
    if frozen is not None:
        return _validate_frozen_throttle(
            frozen,
            max_concurrency=policy_concurrency,
            rate_limit_per_sec=policy_rate,
            connect_timeout_s=policy_timeout,
        )

    requested_concurrency = _request_positive_int(
        parameters.get("scan_max_concurrency"),
        default=policy_concurrency,
        label="scan_max_concurrency",
    )
    requested_rate = _request_rate(
        parameters.get("scan_rate_limit_per_sec"),
        default=policy_rate,
        label="scan_rate_limit_per_sec",
    )
    requested_timeout = _request_positive_float(
        parameters.get("scan_connect_timeout_s"),
        default=policy_timeout,
        label="scan_connect_timeout_s",
    )
    return ThrottleConfig(
        max_concurrency=min(requested_concurrency, policy_concurrency),
        rate_limit_per_sec=min(requested_rate, policy_rate),
        connect_timeout_s=min(requested_timeout, policy_timeout),
    )


def _frozen_effective_throttle(
    parameters: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    contract = parameters.get("scan_contract_v1", _MISSING)
    if contract is _MISSING:
        return None
    if not isinstance(contract, Mapping):
        raise ValueError("scan_contract_v1 must be a mapping")

    effective = contract.get("effective_throttle", _MISSING)
    if effective is _MISSING:
        return None
    if not isinstance(effective, Mapping):
        raise ValueError("scan_contract_v1.effective_throttle must be a mapping")

    fields = set(effective)
    missing = _EFFECTIVE_THROTTLE_FIELDS - fields
    unexpected = fields - _EFFECTIVE_THROTTLE_FIELDS
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected " + ", ".join(sorted(map(str, unexpected))))
        raise ValueError("scan_contract_v1.effective_throttle has invalid fields: " + "; ".join(details))
    return effective


def _validate_frozen_throttle(
    frozen: Mapping[str, Any],
    *,
    max_concurrency: int,
    rate_limit_per_sec: float,
    connect_timeout_s: float,
) -> ThrottleConfig:
    concurrency = _strict_positive_int(
        frozen["max_concurrency"],
        label="scan_contract_v1.effective_throttle.max_concurrency",
    )
    rate = _strict_positive_float(
        frozen["rate_limit_per_sec"],
        label="scan_contract_v1.effective_throttle.rate_limit_per_sec",
    )
    timeout = _strict_positive_float(
        frozen["connect_timeout_s"],
        label="scan_contract_v1.effective_throttle.connect_timeout_s",
    )

    ceilings = (
        ("max_concurrency", concurrency, max_concurrency),
        ("rate_limit_per_sec", rate, rate_limit_per_sec),
        ("connect_timeout_s", timeout, connect_timeout_s),
    )
    for field_name, value, ceiling in ceilings:
        if value > ceiling:
            raise ValueError(f"scan_contract_v1.effective_throttle.{field_name} exceeds the policy ceiling {ceiling}")

    return ThrottleConfig(
        max_concurrency=concurrency,
        rate_limit_per_sec=rate,
        connect_timeout_s=timeout,
    )


def _policy_positive_int(value: Any, *, label: str) -> int:
    parsed = _parse_bounded_int(value, label=label)
    if parsed < 1:
        raise ValueError(f"{label} must be greater than zero")
    return parsed


def _request_positive_int(value: Any, *, default: int, label: str) -> int:
    if value is None or value == "":
        return default
    parsed = _parse_bounded_int(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return parsed


def _strict_positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a positive integer")
    if not 1 <= value <= _MAX_THROTTLE_CONCURRENCY:
        raise ValueError(f"{label} must be a supported positive integer")
    return value


def _parse_bounded_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(f"{label} must be a finite integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be an integer") from error
    if abs(parsed) > _MAX_THROTTLE_CONCURRENCY:
        raise ValueError(f"{label} is outside the supported integer range")
    return parsed


def _policy_positive_float(value: Any, *, label: str) -> float:
    parsed = _parse_finite_float(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return parsed


def _request_positive_float(value: Any, *, default: float, label: str) -> float:
    if value is None or value == "":
        return default
    parsed = _parse_finite_float(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return parsed


def _request_rate(value: Any, *, default: float, label: str) -> float:
    if value is None or value == "":
        return default
    parsed = _parse_finite_float(value, label=label)
    if parsed < 0:
        raise ValueError(f"{label} must not be negative")
    return default if parsed == 0 else parsed


def _strict_positive_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        parsed = float(value)
    except OverflowError as error:
        raise ValueError(f"{label} must be a finite positive number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return parsed


def _parse_finite_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be a finite number")
    return parsed


def _is_finite_positive_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class _ScanRuntimeLimits:
    dispatch_seconds: float
    run_seconds: float


def _scan_runtime_limits(parameters: Mapping[str, Any]) -> _ScanRuntimeLimits | None:
    """Read the authoritative runtime ceilings from a frozen scan contract.

    Contexts created before ``scan_contract_v1`` and non-scan jobs have no
    runtime contract, so they retain the historical unbounded wrapper behavior.
    Once the versioned contract is present, its nested protocol policy is
    authoritative and malformed deadline data fails closed.
    """

    contract = parameters.get("scan_contract_v1", _MISSING)
    if contract is _MISSING:
        return None
    if not isinstance(contract, Mapping):
        raise ValueError("scan_contract_v1 must be a mapping")
    if contract.get("scan_contract_version") != "1.0":
        raise ValueError("scan_contract_v1 has an unsupported version")

    job_type = contract.get("job_type")
    protocol_key = {
        "ip_discovery": "ip",
        "bacnet_discovery": "bacnet",
    }.get(job_type)
    if protocol_key is None:
        raise ValueError("scan_contract_v1 has an unsupported scan job type")

    protocol = contract.get(protocol_key)
    if not isinstance(protocol, Mapping):
        raise ValueError(f"scan_contract_v1.{protocol_key} must be a mapping")
    policy = protocol.get("policy")
    if not isinstance(policy, Mapping):
        raise ValueError(f"scan_contract_v1.{protocol_key}.policy must be a mapping")

    dispatch_seconds = _strict_positive_float(
        policy.get("dispatch_phase_seconds"),
        label=f"scan_contract_v1.{protocol_key}.policy.dispatch_phase_seconds",
    )
    run_seconds = _strict_positive_float(
        policy.get("run_deadline_seconds"),
        label=f"scan_contract_v1.{protocol_key}.policy.run_deadline_seconds",
    )
    if dispatch_seconds > run_seconds:
        raise ValueError("scan dispatch deadline cannot exceed its run deadline")
    return _ScanRuntimeLimits(
        dispatch_seconds=dispatch_seconds,
        run_seconds=run_seconds,
    )


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
    _dispatch_deadline_monotonic: float | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _run_deadline_monotonic: float | None = field(
        default=None,
        init=False,
        repr=False,
    )

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

    def require_active_control(self) -> None:
        """Fail closed when an owned live run can no longer send traffic.

        Legacy/in-memory stores have no active-control method and remain usable
        for network-free tests. Production claimed runs use ``OwnedRunStore``,
        whose check validates the database owner, attempt, lease, cancellation,
        authorization window/revocation, and initiating user's current scope.
        Errors intentionally propagate; a control-store failure must never read
        as permission to send another packet.
        """

        if self.dry_run:
            return
        checker = getattr(self.run_store, "require_active_control", None)
        if callable(checker):
            checker()

    def require_dispatch_allowed(self) -> None:
        """Apply the final live-control and monotonic deadline dispatch gate."""

        self.require_active_control()
        deadline = self._dispatch_deadline_monotonic
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise _SealedRuntimeDeadlineExceeded("sealed scan dispatch deadline elapsed")

    def require_run_completion_allowed(self) -> None:
        """Reject an engine result produced after its frozen total deadline."""

        deadline = self._run_deadline_monotonic
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise _SealedRuntimeDeadlineExceeded("sealed scan run deadline elapsed")

    @property
    def has_cancel_path(self) -> bool:
        """True when a real cooperative-cancel checker is wired for this run.

        ``make_cancel_checker`` returns the ``_never_cancelled`` sentinel when the
        run store advertises no ``is_cancel_requested``, so identity against that
        sentinel is the precise test for "the operator can stop this run". A
        capture engine uses it to decide whether an indefinite (blank) window is
        safe to honour: with a cancel path the run can always be stopped; without
        one it must bound itself so a never-publishing broker cannot hang forever.
        """
        return self._is_cancelled is not _never_cancelled


_ACTIVE_ENGINE_CONTEXT: ContextVar[EngineContext | None] = ContextVar("active_engine_context", default=None)


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
            _AsyncRateLimiter(config.rate_limit_per_sec) if config.rate_limit_per_sec is not None else None
        )

    @property
    def config(self) -> ThrottleConfig:
        return self._config

    def slot(self) -> "_ThrottleSlot":
        """Return an async context manager that holds one concurrency slot.

        Rate limiting is applied on ENTRY (before the body runs) so dispatches
        are spaced regardless of how long each body takes. When called inside
        :func:`run_engine_async`, owned-run active control and the sealed
        dispatch deadline form the final gate before the body starts.
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
            * Checks ``ctx.require_dispatch_allowed()`` after any rate-limit
              wait and immediately before scheduling each unit. Active-control
              denial, a store read error, or deadline expiry cancels safely
              dispatched siblings and raises.
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
                return await factory()
            finally:
                self._semaphore.release()

        async def _cancel_dispatched() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
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
                try:
                    if self._rate_limiter is not None:
                        await self._rate_limiter.acquire()
                    if ctx.is_cancelled():
                        self._semaphore.release()
                        break
                    # This is the final synchronous gate before the unit factory is
                    # scheduled. It is deliberately after the rate-limit wait so an
                    # approval revoked during that wait cannot dispatch afterward.
                    ctx.require_dispatch_allowed()
                except BaseException:
                    self._semaphore.release()
                    raise
                tasks.append(asyncio.ensure_future(_guarded(factory)))
        except BaseException:
            # The total runtime timeout can cancel this coroutine while it waits
            # for a concurrency slot. Reap every unit already scheduled so no
            # detached task survives the terminal deadline failure.
            await _cancel_dispatched()
            raise

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
            context = _ACTIVE_ENGINE_CONTEXT.get()
            if context is not None:
                context.require_dispatch_allowed()
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
    "Engine execution failed. See server logs for details (error detail withheld to avoid leaking credentials)."
)

_RUNTIME_DEADLINE_FAILURE_MESSAGE = "Discovery stopped because its sealed runtime deadline elapsed."

# DISTINCT from the engine-failure text above: the engine ran to completion and
# its results are real, but writing them to the store failed (a value that will
# not serialize into a JSON column, a poisoned session). The operator must not
# re-run a scan that already worked, so the message points at saving — not the
# scan — as the thing to look at. Credential-free by construction (no raw
# exception text): the traceback goes to the server logs via logger.exception.
_PERSIST_FAILURE_MESSAGE = "Discovery finished but its results could not be saved. See server logs for details."

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


def _safe_update_run_status(
    ctx: EngineContext,
    *,
    status: str,
    stage: str,
    error_message: str | None,
) -> Any:
    """Write a terminal status, swallowing and logging any run-store error.

    THE LAST LINE OF DEFENCE against a fossilized ``running`` run. The terminal
    write can itself raise — a poisoned / pending-rollback SQLAlchemy session
    after a failed flush is the field case this exists for — and if that
    exception escaped :func:`run_engine_async` the POST would 500 and the run
    would stay ``running`` forever, which is exactly the bug this guard removes.
    On failure it logs the traceback and returns ``None``; it never raises.
    """
    try:
        return ctx.run_store.update_run_status(
            ctx.run_id,
            status=status,
            stage=stage,
            progress_percent=100,
            error_message=error_message,
        )
    except Exception:
        logger.exception(
            "terminal '%s' status write raised for run %s; swallowing so the run is not left 'running'",
            status,
            ctx.run_id,
        )
        return None


def _apply_failure(ctx: EngineContext, *, error_message: str = _SANITIZED_FAILURE_MESSAGE) -> Any:
    """Set the run failed with a safe message; the write can never escape.

    ``error_message`` defaults to the sanitized generic text (a raw exception may
    echo credentials); the persistence path passes the distinct
    :data:`_PERSIST_FAILURE_MESSAGE` so "the scan worked but saving it did not"
    is never misreported as an engine crash. Routes the write through
    :func:`_safe_update_run_status`, so even a failing terminal write is logged
    rather than raised.
    """
    return _safe_update_run_status(ctx, status="failed", stage=_STAGE_FAILED, error_message=error_message)


def _apply_cancelled(ctx: EngineContext) -> Any:
    """Terminal write when an :class:`asyncio.CancelledError` unwound the run.

    ``cancelled`` only when a cancel was actually requested (the operator asked
    for it); otherwise the CancelledError came from elsewhere and ``failed`` is
    the honest terminal state. Never leaves the run ``running``, never raises.
    """
    if ctx.is_cancelled():
        return _safe_update_run_status(ctx, status="cancelled", stage=_STAGE_CANCELLED, error_message=None)
    return _safe_update_run_status(ctx, status="failed", stage=_STAGE_FAILED, error_message=_SANITIZED_FAILURE_MESSAGE)


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

    A terminal status is written on EVERY path, so a run can never fossilize at
    ``running`` — the field failure this wrapper exists to make impossible:

        * Engine raises ``Exception``          -> ``failed``, sanitized message.
        * Engine raises ``CancelledError``     -> ``cancelled`` if a cancel was
          requested, else ``failed``; swallowed (the run record carries it).
        * Engine raises ``KeyboardInterrupt`` /
          ``SystemExit``                       -> best-effort ``failed``, then
          RE-RAISED (interpreter shutdown must win). ``GeneratorExit`` is not
          caught at all.
        * Engine succeeds but PERSISTING its results fails                  ->
          ``failed`` with the distinct :data:`_PERSIST_FAILURE_MESSAGE`, so a
          scan that really worked is not misreported as an engine crash.
        * Even the terminal status write failing is caught and logged, never
          raised (see :func:`_safe_update_run_status`).
    """
    ctx.run_store.update_run_status(
        ctx.run_id,
        status="running",
        stage=_STAGE_RUNNING,
        progress_percent=15,
    )
    active_context_token = _ACTIVE_ENGINE_CONTEXT.set(ctx)
    try:
        runtime_limits = _scan_runtime_limits(ctx.parameters)
        if runtime_limits is not None:
            started_at = asyncio.get_running_loop().time()
            ctx._dispatch_deadline_monotonic = started_at + runtime_limits.dispatch_seconds
            ctx._run_deadline_monotonic = started_at + runtime_limits.run_seconds

        timeout = asyncio.timeout_at(ctx._run_deadline_monotonic)
        try:
            async with timeout:
                outcome = engine(ctx)
                if inspect.isawaitable(outcome):
                    result = await outcome
                else:
                    result = outcome
                if not isinstance(result, EngineResult):
                    raise TypeError(f"engine callable must return an EngineResult, got {type(result).__name__}")
                # A synchronous engine can hold the event loop beyond the
                # timeout callback. Check the same monotonic deadline before
                # accepting its late result, even when cancellation could not
                # pre-empt the call itself.
                ctx.require_run_completion_allowed()
        except TimeoutError:
            if timeout.expired():
                raise _SealedRuntimeDeadlineExceeded("sealed scan run deadline elapsed") from None
            raise
    except _SealedRuntimeDeadlineExceeded:
        logger.warning("sealed runtime deadline elapsed for run %s", ctx.run_id)
        return _apply_failure(
            ctx,
            error_message=_RUNTIME_DEADLINE_FAILURE_MESSAGE,
        )
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so the `except Exception` below would
        # MISS it and the run would fossilize at 'running' — the secondary
        # mechanism this wrapper must defend against. Swallow it like a failure
        # (the run record carries the terminal state) and never re-raise.
        logger.warning("engine cancelled for run %s", ctx.run_id)
        return _apply_cancelled(ctx)
    except (KeyboardInterrupt, SystemExit):
        # Process-level interruption (Ctrl-C, sys.exit): record a best-effort
        # terminal 'failed' so the run is not left 'running', then RE-RAISE —
        # swallowing these would defeat interpreter shutdown. GeneratorExit is
        # deliberately NOT caught: it must propagate to close the frame.
        logger.warning("engine interrupted for run %s; recording 'failed' and re-raising", ctx.run_id)
        _apply_failure(ctx)
        raise
    except Exception:
        # The operator-facing message says "See server logs for details". Until
        # now nothing in this module logged anything, so that sentence was a
        # lie — the traceback was destroyed here and the run record carried only
        # the generic text. The sanitized message stays exactly as it was (raw
        # exception text can echo credentials back to the UI); the traceback now
        # goes where the message has always claimed it goes. An engine with a
        # safe, specific diagnosis should not rely on this path at all — it
        # should return EngineResult(status_override="failed",
        # error_message=...), which the operator actually sees.
        logger.exception("engine execution failed for run %s", ctx.run_id)
        return _apply_failure(ctx)
    finally:
        ctx._dispatch_deadline_monotonic = None
        ctx._run_deadline_monotonic = None
        _ACTIVE_ENGINE_CONTEXT.reset(active_context_token)

    # The engine SUCCEEDED and its results are real. Persisting them is a SEPARATE
    # failure domain: a save that fails (a non-JSON-serializable value reaching a
    # JSON column — the live-scan crash this release fixes — or a poisoned
    # session) must still land a terminal 'failed' run, with a message that says
    # the scan itself worked, never a run stuck 'running'. _apply_success writes
    # result_summary + issues BEFORE the structured records, so a persist failure
    # does not silently discard the summary the operator can still use.
    try:
        return _apply_success(ctx, result, persist_records)
    except asyncio.CancelledError:
        logger.warning("result persistence cancelled for run %s", ctx.run_id)
        return _apply_cancelled(ctx)
    except (KeyboardInterrupt, SystemExit):
        logger.warning(
            "result persistence interrupted for run %s; recording 'failed' and re-raising",
            ctx.run_id,
        )
        _apply_failure(ctx, error_message=_PERSIST_FAILURE_MESSAGE)
        raise
    except Exception:
        logger.exception("failed to persist engine results for run %s", ctx.run_id)
        return _apply_failure(ctx, error_message=_PERSIST_FAILURE_MESSAGE)


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
    raise RuntimeError("run_engine() was called from within a running event loop; await run_engine_async(...) instead.")
