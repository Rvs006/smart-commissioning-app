"""Dramatiq worker actors: real engine execution on the worker path.

Each discovery/validation actor drives the matching engine processor from
``smart_commissioning_core.engines`` using the shared database-backed run store
(:class:`DbRunStore`) and persists structured discovery records via
:class:`DiscoveryRepository`. Throttle config and the dry-run flag are derived
from the run parameters; the run store is a CancellableRunStore, so the engines
honour ``POST /runs/{id}/cancel`` on this path too.

BROKER ACCESS (Phase 2 carry-forward): the worker registers an MQTT
configuration-values provider at import (see app.mqtt_config_provider) so the
MQTT discovery / live UDMI capture / config-publish actors can resolve a broker
host from stored configuration OR from run parameters. Certificate (mutual-TLS)
material is NOT resolved on the worker — see that module's docstring; that path
stays on-site-validation surface.

HONESTY: the real network probes live inside the engines and are unit-tested
against fakes/loopback only. No real BACnet device, building network, or live
MQTT broker exists in this environment; the worker's real-transport behaviour
requires on-site validation.
"""

import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.bacnet_discovery import process_bacnet_discovery_run
from smart_commissioning_core.engines.base import ThrottleConfig, make_cancel_checker
from smart_commissioning_core.engines.comparison import process_mapping_validation_run
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.point_validation import process_bacnet_validation_run
from smart_commissioning_core.mqtt_config_publish_processor import process_mqtt_config_publish_run
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run

from app.config import get_settings
from app.db import get_engine
from app.logging import configure_logging, run_id_context
from app.mqtt_config_provider import register_worker_mqtt_configuration_provider

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
run_store = DbRunStore(_engine)
discovery_repository = DiscoveryRepository(_engine)
import_repository = ImportRepository(_engine)

# Give the worker broker connection defaults from stored configuration so MQTT
# engines can connect on the worker path (run parameters still take precedence).
register_worker_mqtt_configuration_provider()


# -- conservative worker-side scan throttle defaults ------------------------
# Mirrors the backend Settings defaults (the worker has no API Settings object).
_DEFAULT_SCAN_MAX_CONCURRENCY = 16
_DEFAULT_SCAN_RATE_LIMIT_PER_SEC = 10.0
_DEFAULT_SCAN_CONNECT_TIMEOUT_S = 5.0
# Hard floor for the rate limiter: a request may lower the rate but can never
# disable it (None/unlimited). Mirrors engine_dispatch._MIN_RATE_LIMIT_PER_SEC.
_MIN_SCAN_RATE_LIMIT_PER_SEC = 0.1


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


def _persist_devices(run_id: str, records) -> None:
    discovery_repository.replace_devices(run_id, [dict(r) for r in records])


def _persist_topics(run_id: str, records) -> None:
    discovery_repository.replace_topics(run_id, [dict(r) for r in records])


def _persist_devices_and_points(run_id: str, records) -> None:
    devices, points = [], []
    for record in records:
        target = points if ("point_id" in record or "device_ref" in record) else devices
        target.append(dict(record))
    discovery_repository.replace_devices(run_id, devices)
    discovery_repository.replace_points(run_id, points)


def _import_loader(import_id: str):
    try:
        return list(import_repository.get_accepted_rows(import_id))
    except FileNotFoundError:
        return []


def _discovery_loader(run_id: str):
    return list(discovery_repository.list_points(run_id))


# Engine actors set max_retries=0: a failed engine run is already recorded as a
# terminal failure by run_engine, so Dramatiq's default ~20x exponential-backoff
# retry would re-run a deterministic failure (or a TimeLimitExceeded on a long
# capture) into a retry storm while the run row stays "running". time_limit is
# generous for discovery so a legitimate long/indefinite capture (bounded by
# max_messages + cancel) is not killed mid-run.
@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=3_600_000)
def discover_ip_range(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting IP discovery", extra={"actor": "discover_ip_range"})
        process_ip_discovery_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
            throttle=_build_throttle(parameters),
            dry_run=_is_dry_run(parameters),
            persist_records=_persist_devices,
        )
        logger.info("Finished IP discovery", extra={"actor": "discover_ip_range"})


@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=3_600_000)
def discover_bacnet(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting BACnet discovery", extra={"actor": "discover_bacnet"})
        # Default backend is the OFFLINE SimulatedBacnetBackend unless parameters
        # select bacnet_backend='bacpypes3' (the UNVALIDATED real path). The engine
        # stamps result_summary['backend'] so simulated data is never mistaken for
        # a real scan.
        process_bacnet_discovery_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
            throttle=_build_throttle(parameters),
            dry_run=_is_dry_run(parameters),
            persist_records=_persist_devices_and_points,
            is_cancelled=make_cancel_checker(run_store, run_id),
        )
        logger.info("Finished BACnet discovery", extra={"actor": "discover_bacnet"})


@dramatiq.actor(queue_name="discovery", max_retries=0, time_limit=3_600_000)
def discover_mqtt(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting MQTT discovery", extra={"actor": "discover_mqtt"})
        # live_capture defaults to the real raw-socket subscribe_and_capture. With
        # the worker MQTT configuration provider registered above, the broker host
        # resolves from stored configuration or run parameters. If no broker is
        # reachable the engine records a credential-free 'broker_unreachable' status
        # rather than faking success.
        process_mqtt_discovery_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
            throttle=_build_throttle(parameters),
            dry_run=_is_dry_run(parameters),
            persist_records=_persist_topics,
        )
        logger.info("Finished MQTT discovery", extra={"actor": "discover_mqtt"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
def validate_udmi_payloads(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting UDMI validation", extra={"actor": "validate_udmi_payloads"})
        # live_capture defaults to the real subscribe_and_capture; broker host now
        # resolves via the worker MQTT configuration provider / run parameters.
        process_udmi_validation_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
        )
        logger.info("Finished UDMI validation", extra={"actor": "validate_udmi_payloads"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
def publish_mqtt_config(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting MQTT config publish", extra={"actor": "publish_mqtt_config"})
        # broker_publisher defaults to the real publish path; broker host resolves
        # via the worker MQTT configuration provider / run parameters. A run without
        # use_live_broker stays validate-only (no broker write).
        process_mqtt_config_publish_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
        )
        logger.info("Finished MQTT config publish", extra={"actor": "publish_mqtt_config"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
def validate_bacnet_points(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting BACnet point validation", extra={"actor": "validate_bacnet_points"})
        process_bacnet_validation_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
            import_loader=_import_loader,
            discovery_loader=_discovery_loader,
            is_cancelled=make_cancel_checker(run_store, run_id),
        )
        logger.info("Finished BACnet point validation", extra={"actor": "validate_bacnet_points"})


@dramatiq.actor(queue_name="validation", max_retries=0, time_limit=900_000)
def compare_bacnet_mqtt(run_id: str, parameters: dict) -> None:
    with run_id_context(run_id):
        logger.info("Starting BACnet to MQTT mapping comparison", extra={"actor": "compare_bacnet_mqtt"})
        process_mapping_validation_run(
            run_id,
            parameters,
            run_store=run_store,
            execution_mode="dramatiq_worker",
            import_loader=_import_loader,
            discovery_loader=_discovery_loader,
            is_cancelled=make_cancel_checker(run_store, run_id),
        )
        logger.info("Finished BACnet to MQTT mapping comparison", extra={"actor": "compare_bacnet_mqtt"})


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
