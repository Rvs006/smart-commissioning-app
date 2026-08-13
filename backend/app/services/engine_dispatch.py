"""Shared engine dispatch helpers for discovery + validation routes/worker.

This module centralises the logic that wires the new engine processors
(``smart_commissioning_core.engines``) into both the API inline-fallback path
and the dramatiq worker actors, so the two never drift:

* :func:`build_throttle` derives a conservative :class:`ThrottleConfig` from a
  run's ``parameters`` layered over the service settings defaults.
* :func:`is_dry_run` reads the dry-run flag from ``parameters``.
* The ``make_*_persister`` factories build structured-record persisters backed
  by :class:`DiscoveryRepository` that route each engine's records to the right
  table (devices / points / topics). The BACnet engine emits a MIXED list of
  device + point rows; :func:`make_device_point_persister` splits them.
* :func:`run_inline_discovery` / :func:`run_inline_validation` drive a chosen
  engine processor with a real run store + persister, used by both the route
  inline path and the worker.

HONESTY: none of this opens a network connection. The discovery engines own the
real I/O and are gated by ``safety.require_scan_authorization``; this module
only assembles their inputs and persists their output. The MQTT/BACnet real
transports remain on-site-validation surface (see each engine's docstring).
"""

import ipaddress
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.bacnet_discovery import resolve_bacnet_backend_name

# make_cancel_checker re-exported from core (single impl in engines.base) so the
# API routes keep importing it from here.
from smart_commissioning_core.engines.base import (  # noqa: F401
    ThrottleConfig,
    make_cancel_checker,
    resolve_throttle_config,
)
from smart_commissioning_core.engines.ip import require_frozen_ip_provider_execution
from smart_commissioning_core.source_interface import (
    SOURCE_INTERFACE_IDENTITY_KEY,
    FrozenSourceInterfaceV1,
)

from app.services import interface_service

logger = logging.getLogger(__name__)

def build_throttle(
    parameters: dict[str, Any],
    *,
    max_concurrency: int,
    rate_limit_per_sec: float,
    connect_timeout_s: float,
) -> ThrottleConfig:
    """Build the shared policy-bounded throttle for inline API execution.

    Request parameters ``scan_max_concurrency`` / ``scan_rate_limit_per_sec`` /
    ``scan_connect_timeout_s`` may only NARROW the operator's policy, never
    exceed it. A frozen ``scan_contract_v1.effective_throttle`` takes
    precedence so inline execution uses the same sealed values as the worker.
    """
    contract = parameters.get("scan_contract_v1")
    ip_contract = contract.get("ip") if isinstance(contract, Mapping) else None
    if not (
        isinstance(ip_contract, Mapping)
        and ip_contract.get("provider") == "operator_managed_nmap"
    ):
        require_frozen_ip_provider_execution(parameters)
    return resolve_throttle_config(
        parameters,
        max_concurrency=max_concurrency,
        rate_limit_per_sec=rate_limit_per_sec,
        connect_timeout_s=connect_timeout_s,
    )

def is_dry_run(parameters: dict[str, Any]) -> bool:
    """Return True if the run requests a side-effect-free dry-run preview."""
    value = parameters.get("dry_run")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


# Parameter key the BACnet engine reads to select its transport backend.
BACNET_BACKEND_KEY = "bacnet_backend"


def resolve_bacnet_backend(parameters: dict[str, Any]) -> None:
    """Default an AUTHORIZED, non-dry-run BACnet scan to the real bacpypes3 backend.

    Honesty: a real scan must ATTEMPT real discovery. Simulated is accepted only
    for dry-run previews; unknown selectors and live simulated requests fail
    closed. If bacpypes3 is unavailable, the engine records a failed run.
    """
    dry_run = is_dry_run(parameters)
    selector = resolve_bacnet_backend_name(parameters, dry_run=dry_run)
    if not dry_run or BACNET_BACKEND_KEY in parameters:
        parameters[BACNET_BACKEND_KEY] = selector


def resolve_ip_enrichment(parameters: dict[str, Any]) -> None:
    """Disable active or ambient host enrichment for the first IP field release.

    The built-in provider may use frozen register data and scan-protocol evidence.
    It must not emit DNS traffic or read/launch ARP helpers, even when a legacy
    caller asks for reverse DNS.
    """
    parameters["reverse_dns"] = False


# The literal dropdown value meaning "resolve and freeze the OS default route".
# Compared case-insensitively; empty / absent is treated the same way.
_AUTO_SOURCE_INTERFACE = "Auto (OS default route)"


def resolve_source_interface(
    parameters: dict[str, Any],
    source_interface: str | None,
    *,
    executor_scope: str,
) -> None:
    """Resolve and freeze one concrete source NIC into ``parameters``.

    Explicit run-level ``source_ip`` / ``local_address`` still win over saved
    configuration.  Empty or Auto now resolves the current deterministic
    lowest-metric default route rather than leaving the operating system free
    to choose a different egress later.  A bare explicit IP takes the current
    NIC prefix from inventory; a configured prefix must match that inventory.
    """

    raw_source = str(parameters.get("source_ip") or "").strip()
    raw_local = str(parameters.get("local_address") or "").strip()
    selection: str
    requested_source: str | None = None
    requested_prefix: int | None = None

    if raw_source or raw_local:
        selection = "explicit"
        parsed_source: ipaddress.IPv4Address | None = None
        parsed_local: ipaddress.IPv4Interface | None = None
        if raw_source:
            try:
                candidate_source = ipaddress.ip_address(raw_source)
            except ValueError as error:
                raise ValueError("source_ip must be a valid IPv4 address") from error
            if not isinstance(candidate_source, ipaddress.IPv4Address):
                raise ValueError("source_ip must be a valid IPv4 address")
            parsed_source = candidate_source
        if raw_local:
            if "/" not in raw_local:
                raise ValueError("local_address must preserve an IPv4 prefix")
            try:
                candidate_local = ipaddress.ip_interface(raw_local)
            except ValueError as error:
                raise ValueError("local_address must be a valid IPv4 interface") from error
            if not isinstance(candidate_local, ipaddress.IPv4Interface):
                raise ValueError("local_address must be a valid IPv4 interface")
            parsed_local = candidate_local
        if parsed_source is not None and parsed_local is not None and parsed_source != parsed_local.ip:
            raise ValueError("source_ip and local_address must identify the same interface")
        requested_source = str(parsed_source or parsed_local.ip)  # type: ignore[union-attr]
        requested_prefix = parsed_local.network.prefixlen if parsed_local is not None else None
    else:
        configured = (source_interface or "").strip()
        if not configured or configured.casefold() == _AUTO_SOURCE_INTERFACE.casefold():
            selection = "default_route"
        else:
            selection = "explicit"
            try:
                if "/" in configured:
                    candidate_interface = ipaddress.ip_interface(configured)
                    if not isinstance(candidate_interface, ipaddress.IPv4Interface):
                        raise ValueError("Source Interface must be an IPv4 address")
                    requested_source = str(candidate_interface.ip)
                    requested_prefix = candidate_interface.network.prefixlen
                else:
                    candidate_address = ipaddress.ip_address(configured)
                    if not isinstance(candidate_address, ipaddress.IPv4Address):
                        raise ValueError("Source Interface must be an IPv4 address")
                    requested_source = str(candidate_address)
            except ValueError as error:
                raise ValueError("Source Interface must be a valid IPv4 address or interface") from error

    if selection == "default_route":
        frozen = interface_service.resolve_source_interface_identity(
            selection="default_route",
            executor_scope=executor_scope,
        )
    else:
        frozen = interface_service.resolve_source_interface_identity(
            selection="explicit",
            executor_scope=executor_scope,
            source_ip=requested_source,
            prefix_length=requested_prefix,
        )
    identity = FrozenSourceInterfaceV1.model_validate(frozen)
    parameters["source_ip"] = identity.source_ip
    parameters["local_address"] = identity.local_address
    parameters[SOURCE_INTERFACE_IDENTITY_KEY] = identity.model_dump(mode="json")


# -- structured-record persisters ------------------------------------------


def _is_point_record(record: dict[str, Any]) -> bool:
    """A point record carries point identity; a device record carries device_type."""
    return "point_id" in record or "device_ref" in record


def make_device_persister(repository: DiscoveryRepository) -> Callable[[str, Sequence[dict[str, Any]]], None]:
    """Persister for IP discovery: every record is a DiscoveredDevice row."""

    def persist(run_id: str, records: Sequence[dict[str, Any]]) -> None:
        repository.replace_devices(run_id, [dict(record) for record in records])

    return persist


def make_topic_persister(repository: DiscoveryRepository) -> Callable[[str, Sequence[dict[str, Any]]], None]:
    """Persister for MQTT discovery: every record is a DiscoveredTopic row."""

    def persist(run_id: str, records: Sequence[dict[str, Any]]) -> None:
        repository.replace_topics(run_id, [dict(record) for record in records])

    return persist


def make_device_point_persister(
    repository: DiscoveryRepository,
) -> Callable[[str, Sequence[dict[str, Any]]], None]:
    """Persister for BACnet discovery: split the mixed device/point record list.

    The BACnet engine emits device rows first then point rows in a single
    ``structured_records`` list (see ``bacnet_discovery``). We route device rows
    to ``replace_devices`` and point rows to ``replace_points`` so each lands in
    its proper table; both are idempotent rewrites for the run.
    """

    def persist(run_id: str, records: Sequence[dict[str, Any]]) -> None:
        devices: list[dict[str, Any]] = []
        points: list[dict[str, Any]] = []
        for record in records:
            target = points if _is_point_record(record) else devices
            target.append(dict(record))
        try:
            repository.replace_devices(run_id, devices)
            repository.replace_points(run_id, points)
        except Exception:
            # SESSION HYGIENE: each replace_* runs in its OWN
            # ``sessionmaker.begin()`` transaction, which rolls back on a mid-flush
            # raise and returns the pooled connection clean — so the engine
            # framework's subsequent terminal-"failed" status write (a fresh
            # session on the same engine) still succeeds and the run is never
            # fossilized at "running". Log a breadcrumb naming the run, then
            # re-raise the ORIGINAL error so that failure path runs.
            logger.warning(
                "Persisting BACnet discovery records failed for run %s "
                "(%d device(s), %d point(s) not saved).",
                run_id,
                len(devices),
                len(points),
            )
            raise

    return persist


# -- validation loaders -----------------------------------------------------


def make_import_loader(repository: ImportRepository) -> Callable[[str], list[dict[str, Any]]]:
    """Build an import_loader for the validation/comparison engines.

    Returns the accepted_rows for an import batch, or an empty list when the
    import id is unknown (a missing import must not crash the engine — it
    surfaces as a missing-register comparison instead).
    """

    def load(import_id: str) -> list[dict[str, Any]]:
        try:
            return list(repository.get_accepted_rows(import_id))
        except FileNotFoundError:
            return []

    return load


def make_discovery_loader(repository: DiscoveryRepository) -> Callable[[str], list[dict[str, Any]]]:
    """Build a discovery_loader returning a discovery run's DiscoveredPoint rows.

    The validation/comparison engines read observed BACnet point values from a
    discovery run's points; this loader backs that with
    ``DiscoveryRepository.list_points`` (empty list for an unknown run).
    """

    def load(run_id: str) -> list[dict[str, Any]]:
        return list(repository.list_points(run_id))

    return load
