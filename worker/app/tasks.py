"""Dramatiq worker actors: real engine execution on the worker path.

Each discovery/validation actor drives the matching engine processor from
``smart_commissioning_core.engines`` using the shared database-backed run store
(:class:`DbRunStore`) and persists structured discovery records via
:class:`DiscoveryRepository`. Throttle config and the dry-run flag are derived
from the run parameters; the run store is a CancellableRunStore, so the engines
honour ``POST /runs/{id}/cancel`` on this path too.

BROKER ACCESS: every actor resolves broker settings only from its frozen
RunContextV1. At import, the worker registers the read-only secret resolver from
app.mqtt_config_provider. When the backend secrets directory and key file are
mounted into the worker, the same encrypted mutual-TLS references resolve in
both processes. Missing material fails honestly instead of consulting current
configuration or demo defaults.

HONESTY: the real network probes live inside the engines and are unit-tested
against fakes/loopback only. No real BACnet device, building network, or live
MQTT broker exists in this environment; the worker's real-transport behaviour
requires on-site validation.
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import Interrupt, TimeLimitExceeded
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.engines.bacnet_discovery import process_bacnet_discovery_run
from smart_commissioning_core.engines.base import ThrottleConfig, make_cancel_checker
from smart_commissioning_core.engines.comparison import process_mapping_validation_run
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.point_validation import process_bacnet_validation_run
from smart_commissioning_core.execution_context import (
    SecretMaterialUnavailableError,
    resolve_context_parameters,
    verify_stored_context,
)
from smart_commissioning_core.mqtt_config_publish_processor import process_mqtt_config_publish_run
from smart_commissioning_core.owned_run_heartbeat import (
    OWNED_CONTEXT_FAILURE_MESSAGE,
    OWNED_CONTEXT_FAILURE_STAGE,
    OWNED_EXECUTOR_FAILURE_MESSAGE,
    OWNED_EXECUTOR_FAILURE_STAGE,
    OWNED_EXECUTOR_MISSING_TERMINAL_MESSAGE,
    OWNED_EXECUTOR_MISSING_TERMINAL_STAGE,
    OWNED_HEARTBEAT_START_FAILURE_STAGE,
    OwnedRunHeartbeat,
)
from smart_commissioning_core.owned_run_store import OwnedRunStore, OwnershipLostError
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run

from app.config import get_settings
from app.db import get_engine
from app.logging import configure_logging, run_id_context
from app.mqtt_config_provider import (
    register_worker_mqtt_secret_resolver,
    resolve_worker_secret,
)

# Structured JSON logging is installed at worker import (the dramatiq CLI imports
# this module). Every actor binds its run_id via run_id_context so each log line
# carries the run it belongs to.
configure_logging()
logger = logging.getLogger("smart_commissioning.worker")

settings = get_settings()
broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(broker)

# Shared database-backed run store (same DATABASE_URL as the backend). The
# backend owns the schema; the worker only reads/writes run + discovery rows.
_engine = get_engine()
lifecycle_repository = RunLifecycleRepository(_engine)
import_repository = ImportRepository(_engine)
discovery_repository = DiscoveryRepository(_engine)

# Register the secret-reference resolver used by frozen execution contexts.
# Broker settings themselves come only from the persisted context.
register_worker_mqtt_secret_resolver()


# -- conservative worker-side scan throttle defaults ------------------------
# Mirrors the backend Settings defaults (the worker has no API Settings object).
_DEFAULT_SCAN_MAX_CONCURRENCY = 16
_DEFAULT_SCAN_RATE_LIMIT_PER_SEC = 10.0
_DEFAULT_SCAN_CONNECT_TIMEOUT_S = 5.0
# Hard floor for the rate limiter: a request may lower the rate but can never
# disable it (None/unlimited). Mirrors engine_dispatch._MIN_RATE_LIMIT_PER_SEC.
_MIN_SCAN_RATE_LIMIT_PER_SEC = 0.1
_WORKER_HEARTBEAT_SECONDS = settings.heartbeat_seconds
_WORKER_LEASE_SECONDS = settings.lease_seconds


class _ExecutionContextResolutionError(RuntimeError):
    """Keep worker parameter-resolution failures distinct from engine failures."""


def _seal_owned_failure(
    store: OwnedRunStore,
    *,
    stage: str,
    error_message: str,
) -> None:
    """Best-effort fenced failure used by every worker wrapper exit path."""

    if store.ownership_lost or store.terminal_outcome is not None:
        return
    try:
        store.update_run_status(
            store.lease.run_id,
            status="failed",
            stage=stage,
            progress_percent=100,
            error_message=error_message,
        )
    except OwnershipLostError:
        logger.warning(
            "Worker ownership was lost before failure finalization",
            extra={"run_id": store.lease.run_id},
        )
    except Exception as error:
        logger.error(
            "Could not seal owned worker failure",
            extra={
                "run_id": store.lease.run_id,
                "exception_type": type(error).__name__,
            },
        )


def _with_worker_lease(
    function: Callable[[str, RunContextV1, OwnedRunStore], None]
) -> Callable[[str, str], None]:
    """Claim, load frozen context, heartbeat, and guard one delivery."""

    @wraps(function)
    def wrapped(run_id: str, dispatch_id: str) -> None:
        lease = lifecycle_repository.claim_run(
            run_id,
            dispatch_id,
            lease_seconds=_WORKER_LEASE_SECONDS,
        )
        if lease is None:
            logger.info(
                "Skipping duplicate or stale delivery run_id=%s",
                run_id,
                extra={"run_id": run_id, "dispatch_id": dispatch_id},
            )
            return
        owned_store = OwnedRunStore(lifecycle_repository, lease)
        heartbeat: OwnedRunHeartbeat | None = None
        try:
            heartbeat = OwnedRunHeartbeat(
                owned_store,
                lease_seconds=_WORKER_LEASE_SECONDS,
                interval_seconds=_WORKER_HEARTBEAT_SECONDS,
                executor_label="worker",
                thread_name_prefix="run-heartbeat",
            )
            heartbeat.start()
        except Exception as error:
            logger.error(
                "Worker heartbeat could not start",
                extra={"run_id": run_id, "exception_type": type(error).__name__},
            )
            _seal_owned_failure(
                owned_store,
                stage=OWNED_HEARTBEAT_START_FAILURE_STAGE,
                error_message=OWNED_EXECUTOR_FAILURE_MESSAGE,
            )
            if heartbeat is not None:
                heartbeat.stop_and_join()
            return

        try:
            try:
                stored_context = lifecycle_repository.get_context(run_id)
                context = verify_stored_context(stored_context, lease)
            except Exception as error:
                logger.error(
                    "Stored execution context could not be verified",
                    extra={"run_id": run_id, "exception_type": type(error).__name__},
                )
                _seal_owned_failure(
                    owned_store,
                    stage=OWNED_CONTEXT_FAILURE_STAGE,
                    error_message=OWNED_CONTEXT_FAILURE_MESSAGE,
                )
                return
            if heartbeat.ownership_lost or owned_store.ownership_lost:
                return
            try:
                function(run_id, context, owned_store)
            except (
                SecretMaterialUnavailableError,
                _ExecutionContextResolutionError,
            ) as error:
                logger.error(
                    "Frozen execution secret material is unavailable",
                    extra={"run_id": run_id, "exception_type": type(error).__name__},
                )
                _seal_owned_failure(
                    owned_store,
                    stage=OWNED_CONTEXT_FAILURE_STAGE,
                    error_message=OWNED_CONTEXT_FAILURE_MESSAGE,
                )
                return
            except OwnershipLostError:
                logger.warning(
                    "Worker processor observed ownership loss; terminal evidence was not written",
                    extra={"run_id": run_id},
                )
                return
            except BaseException:
                _seal_owned_failure(
                    owned_store,
                    stage=OWNED_EXECUTOR_FAILURE_STAGE,
                    error_message=OWNED_EXECUTOR_FAILURE_MESSAGE,
                )
                raise
            if (
                owned_store.terminal_outcome is None
                and not heartbeat.ownership_lost
                and not owned_store.ownership_lost
            ):
                _seal_owned_failure(
                    owned_store,
                    stage=OWNED_EXECUTOR_MISSING_TERMINAL_STAGE,
                    error_message=OWNED_EXECUTOR_MISSING_TERMINAL_MESSAGE,
                )
        finally:
            heartbeat.stop_and_join()

    return wrapped


def _build_throttle(parameters: dict) -> ThrottleConfig:
    # Request parameters may only NARROW the operator policy, never exceed it:
    # concurrency is clamped to the default ceiling and the rate limiter can
    # never be removed (a non-positive request rate falls back to the default).
    requested_concurrency = _positive_int(parameters.get("scan_max_concurrency"), _DEFAULT_SCAN_MAX_CONCURRENCY)
    concurrency = max(1, min(requested_concurrency, _DEFAULT_SCAN_MAX_CONCURRENCY))
    timeout = _positive_float(parameters.get("scan_connect_timeout_s"), _DEFAULT_SCAN_CONNECT_TIMEOUT_S)
    parsed = _to_float(parameters.get("scan_rate_limit_per_sec"))
    if parsed is None or parsed <= 0:
        rate = _DEFAULT_SCAN_RATE_LIMIT_PER_SEC
    else:
        rate = max(parsed, _MIN_SCAN_RATE_LIMIT_PER_SEC)
    return ThrottleConfig(max_concurrency=concurrency, rate_limit_per_sec=rate, connect_timeout_s=timeout)


def _is_dry_run(parameters: dict) -> bool:
    value = parameters.get("dry_run")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _effective_parameters(
    context: RunContextV1,
    store: OwnedRunStore,
    *,
    channel: str,
) -> dict[str, Any]:
    """Build engine input exclusively from the persisted context."""
    try:
        return resolve_context_parameters(
            context,
            store.lease,
            deployment_id=settings.deployment_id,
            channel=channel,
            secret_resolver=resolve_worker_secret,
        )
    except Exception as error:
        raise _ExecutionContextResolutionError from error


def _import_loader(context: RunContextV1) -> Callable[[str], list[dict[str, Any]]]:
    allowed = {binding.resource_id for binding in (*context.registers, *context.imports)}

    def load(import_id: str) -> list[dict[str, Any]]:
        if import_id not in allowed:
            raise ValueError("requested import is not bound to the execution context")
        return list(import_repository.get_accepted_rows(import_id))

    return load


def _discovery_loader(context: RunContextV1) -> Callable[[str], list[dict[str, Any]]]:
    parameters = context.engine_parameters
    allowed = {
        str(value)
        for key, value in parameters.items()
        if key in {"source_run_id", "discovery_run_id"} and value
    }
    source_run_ids = parameters.get("source_run_ids", [])
    if isinstance(source_run_ids, (list, tuple, set)):
        allowed.update(str(value) for value in source_run_ids if value)

    def load(run_id: str) -> list[dict[str, Any]]:
        if run_id not in allowed:
            raise ValueError("requested discovery run is not bound to the execution context")
        return list(discovery_repository.list_points(run_id))

    return load


def _fail_interrupted_run(
    store: OwnedRunStore, interrupt: BaseException, *, stage: str
) -> None:
    """Record an honest terminal failure when dramatiq interrupts an actor.

    Dramatiq's time-limit/shutdown interrupts derive from BaseException, so they
    bypass the ``except Exception`` failure paths inside the engines/processors;
    without this the run row would stay 'running' forever while the frontend
    polls. Callers re-raise the interrupt afterwards so dramatiq's own message
    accounting stays correct.
    """
    # Limit-agnostic wording: each actor sets its own time_limit (15 min to
    # 49h), so naming a specific duration here would go stale per actor.
    if isinstance(interrupt, TimeLimitExceeded):
        message = "run exceeded the worker time limit"
    else:
        message = f"run interrupted by the worker ({type(interrupt).__name__})"
    try:
        store.update_run_status(
            store.lease.run_id,
            status="failed",
            stage=stage,
            progress_percent=100,
            error_message=message,
        )
    except Exception:
        # Best effort: if the status write itself fails the run row stays
        # 'running', but nothing is faked and the interrupt still propagates.
        logger.exception("Could not record interrupted run as failed")


# Engine actors set max_retries=0: a failed engine run is already recorded as a
# terminal failure by run_engine, so Dramatiq's default ~20x exponential-backoff
# retry would re-run a deterministic failure (or a TimeLimitExceeded on a long
# capture) into a retry storm while the run row stays "running". time_limit is
# generous for discovery so a legitimate long/indefinite capture (bounded by
# max_messages + cancel) is not killed mid-run. The long-capture actors
# (discover_mqtt, validate_udmi_payloads) additionally catch dramatiq's
# Interrupt via _fail_interrupted_run so a time-limit kill leaves a real
# 'failed' run, not a stranded 'running' row.
# discover_ip_range / discover_bacnet catch dramatiq's Interrupt (a BaseException
# that bypasses run_engine's `except Exception`) so a time-limit kill records a real
# 'failed' run promptly; the worker heartbeat is the fallback for process death
# that cannot raise an interrupt. A BACnet scan against large object-lists with
# dead points is not as bounded as once assumed, so it can reach the 1h ceiling.
@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=3_600_000)
@_with_worker_lease
def discover_ip_range(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="ip_discovery")
        logger.info("Starting IP discovery", extra={"actor": "discover_ip_range"})
        try:
            process_ip_discovery_run(
                run_id,
                parameters,
                run_store=store,
                execution_mode="dramatiq_worker",
                throttle=_build_throttle(parameters),
                dry_run=_is_dry_run(parameters),
                persist_records=store.replace_devices,
            )
        except Interrupt as interrupt:
            # Stage mirrors run_engine's failure stage (_STAGE_FAILED).
            _fail_interrupted_run(store, interrupt, stage="engine_failed")
            raise
        logger.info("Finished IP discovery", extra={"actor": "discover_ip_range"})


@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=3_600_000)
@_with_worker_lease
def discover_bacnet(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="bacnet_discovery")
        logger.info("Starting BACnet discovery", extra={"actor": "discover_bacnet"})
        # Non-dry runs default to the UNVALIDATED real bacpypes3 path; simulation
        # is dry-run/test-only. The engine stamps result_summary['backend'] so a
        # preview can never be mistaken for a real scan.
        try:
            process_bacnet_discovery_run(
                run_id,
                parameters,
                run_store=store,
                execution_mode="dramatiq_worker",
                throttle=_build_throttle(parameters),
                dry_run=_is_dry_run(parameters),
                persist_records=store.replace_devices_and_points,
                is_cancelled=make_cancel_checker(store, run_id),
            )
        except Interrupt as interrupt:
            # Stage mirrors run_engine's failure stage (_STAGE_FAILED).
            _fail_interrupted_run(store, interrupt, stage="engine_failed")
            raise
        logger.info("Finished BACnet discovery", extra={"actor": "discover_bacnet"})


@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=176_400_000)
@_with_worker_lease
def discover_mqtt(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="mqtt_discovery")
        logger.info("Starting MQTT discovery", extra={"actor": "discover_mqtt"})
        # live_capture defaults to the real raw-socket subscribe_and_capture.
        # Broker settings and secret references came from the frozen context. If
        # no broker is reachable the engine records 'broker_unreachable'.
        try:
            process_mqtt_discovery_run(
                run_id,
                parameters,
                run_store=store,
                execution_mode="dramatiq_worker",
                throttle=_build_throttle(parameters),
                dry_run=_is_dry_run(parameters),
                persist_records=store.replace_topics,
            )
        except Interrupt as interrupt:
            # Stage mirrors run_engine's failure stage (_STAGE_FAILED).
            _fail_interrupted_run(store, interrupt, stage="engine_failed")
            raise
        logger.info("Finished MQTT discovery", extra={"actor": "discover_mqtt"})


# 49h time limit: the API/UI cap a capture window at 48h (field ask
# 2026-07-14 — real-world UDMI reporting intervals are hours-to-days), and the
# actor limit is that maximum accepted capture plus a 1h margin so a full 48h
# capture is never killed at the wire by its own executor. MUST stay above the
# API's MAX_UDMI_CAPTURE_SECONDS (backend routes/validation.py) and the
# frontend's udmiCaptureOverCap constant. Both long-capture actors sit at
# cap + 1h: discover_mqtt above (its cap is MQTT_MAX_CAPTURE_SECONDS in
# backend routes/discovery.py) and validate_udmi_payloads here. The remaining
# validation actors keep their 15 min ceiling.
@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=176_400_000)
@_with_worker_lease
def validate_udmi_payloads(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="udmi_validation")
        logger.info("Starting UDMI validation", extra={"actor": "validate_udmi_payloads"})
        # live_capture defaults to the real subscribe_and_capture; broker
        # settings came from the frozen context.
        try:
            process_udmi_validation_run(
                run_id,
                parameters,
                run_store=store,
                execution_mode="dramatiq_worker",
            )
        except Interrupt as interrupt:
            # A fully silent broker with no cancel rides the indefinite capture
            # into the actor time limit; the processor's own failure path only
            # catches Exception. Stage mirrors its failure stage.
            _fail_interrupted_run(
                store, interrupt, stage="udmi_fixture_validation_failed"
            )
            raise
        logger.info("Finished UDMI validation", extra={"actor": "validate_udmi_payloads"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
@_with_worker_lease
def publish_mqtt_config(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="mqtt_config_publish")
        logger.info("Starting MQTT config publish", extra={"actor": "publish_mqtt_config"})
        # broker_publisher defaults to the real publish path. A run without
        # use_live_broker stays validate-only (no broker write).
        process_mqtt_config_publish_run(
            run_id,
            parameters,
            run_store=store,
            execution_mode="dramatiq_worker",
        )
        logger.info("Finished MQTT config publish", extra={"actor": "publish_mqtt_config"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
@_with_worker_lease
def validate_bacnet_points(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="bacnet_validation")
        logger.info("Starting BACnet point validation", extra={"actor": "validate_bacnet_points"})
        process_bacnet_validation_run(
            run_id,
            parameters,
            run_store=store,
            execution_mode="dramatiq_worker",
            import_loader=_import_loader(context),
            discovery_loader=_discovery_loader(context),
            is_cancelled=make_cancel_checker(store, run_id),
        )
        logger.info("Finished BACnet point validation", extra={"actor": "validate_bacnet_points"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
@_with_worker_lease
def compare_bacnet_mqtt(
    run_id: str, context: RunContextV1, store: OwnedRunStore
) -> None:
    with run_id_context(run_id):
        parameters = _effective_parameters(context, store, channel="mapping_validation")
        logger.info("Starting BACnet to MQTT mapping comparison", extra={"actor": "compare_bacnet_mqtt"})
        process_mapping_validation_run(
            run_id,
            parameters,
            run_store=store,
            execution_mode="dramatiq_worker",
            import_loader=_import_loader(context),
            discovery_loader=_discovery_loader(context),
            is_cancelled=make_cancel_checker(store, run_id),
        )
        logger.info("Finished BACnet to MQTT mapping comparison", extra={"actor": "compare_bacnet_mqtt"})


@dramatiq.actor(queue_name="maintenance", max_retries=0, time_limit=60_000)
def recover_expired_run_leases() -> None:
    """Periodic maintenance primitive; HTTP GET/SSE paths never call it."""
    recovered = lifecycle_repository.recover_expired_leases()
    if recovered:
        logger.warning(
            "Recovered expired run leases",
            extra={"recovered_count": len(recovered)},
        )


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value, default: float) -> float:
    parsed = _to_float(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
