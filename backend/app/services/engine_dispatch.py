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
from collections.abc import Callable, Sequence
from typing import Any

from smart_commissioning_core.db.repositories import DiscoveryRepository, ImportRepository
from smart_commissioning_core.engines.bacnet_discovery import resolve_bacnet_backend_name

# make_cancel_checker re-exported from core (single impl in engines.base) so the
# API routes keep importing it from here.
from smart_commissioning_core.engines.base import (  # noqa: F401
    ThrottleConfig,
    make_cancel_checker,
)

logger = logging.getLogger(__name__)

# Hard floor for the active-scan rate limiter. A request may lower the rate but
# can never disable it (set it to None / unlimited): the limiter always stays a
# positive bound so a request cannot remove the operator's safety throttle.
_MIN_RATE_LIMIT_PER_SEC = 0.1


def build_throttle(
    parameters: dict[str, Any],
    *,
    max_concurrency: int,
    rate_limit_per_sec: float,
    connect_timeout_s: float,
) -> ThrottleConfig:
    """Build a ThrottleConfig from request parameters over service defaults.

    Request parameters ``scan_max_concurrency`` / ``scan_rate_limit_per_sec`` /
    ``scan_connect_timeout_s`` may only NARROW the operator's policy, never
    exceed it (a request cannot widen the blast radius of a scan against a live
    building network):

    * ``max_concurrency`` is clamped to ``min(request, settings default)``.
    * the rate limiter can never be disabled by a request: a non-positive /
      missing / unparseable ``scan_rate_limit_per_sec`` falls back to the
      operator default, and any positive request rate is enforced as a floor of
      a small positive value so the bound always stays active (a request can
      lower the rate but cannot remove the limit).
    """
    requested_concurrency = _positive_int(parameters.get("scan_max_concurrency"), default=max_concurrency)
    concurrency = max(1, min(requested_concurrency, max_concurrency))
    timeout = _positive_float(parameters.get("scan_connect_timeout_s"), default=connect_timeout_s)

    # The operator default is the rate used when a request omits / disables the
    # override (rate <= 0 => "use the default"); never None (unlimited).
    default_rate = rate_limit_per_sec if rate_limit_per_sec > 0 else _MIN_RATE_LIMIT_PER_SEC
    raw_rate = parameters.get("scan_rate_limit_per_sec")
    parsed_rate = _to_float(raw_rate)
    if parsed_rate is None or parsed_rate <= 0:
        rate = default_rate
    else:
        # A request may lower the rate, but the limiter must stay a positive
        # bound — clamp to a small positive floor so it can never be removed.
        rate = max(parsed_rate, _MIN_RATE_LIMIT_PER_SEC)

    return ThrottleConfig(
        max_concurrency=concurrency,
        rate_limit_per_sec=rate,
        connect_timeout_s=timeout,
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
    """Default an authorized, non-dry-run IP sweep to resolve hostnames (reverse DNS).

    Best-effort: the engine's ``_reverse_lookup`` returns None when no PTR exists, so a
    blank hostname is honest, never fabricated. Dry runs are left untouched (their
    plan only advertises 'reverse-dns' if the operator opted in). ``setdefault`` so an
    explicit ``reverse_dns=false`` operator override wins.

    MAC enrichment needs no parameter here: the engine reads the OS ARP cache as an
    unconditional best-effort per responsive host (gated only by host-responsive +
    non-dry-run), degrading to a blank MAC when there is no ARP entry.
    """
    if is_dry_run(parameters):
        return
    parameters.setdefault("reverse_dns", True)


# The literal dropdown value meaning "use the OS default route" (bind nothing).
# Compared case-insensitively; empty / absent is treated the same way.
_AUTO_SOURCE_INTERFACE = "Auto (OS default route)"


def resolve_source_interface(parameters: dict[str, Any], source_interface: str | None) -> None:
    """Inject source_ip (+ local_address for BACnet) into run parameters, in place.

    ``source_interface`` is the configured device."Source Interface" value:

    * falsy / ``"Auto (OS default route)"`` (case-insensitive) -> no-op (OS
      default route; nothing is bound, the backward-compatible path).
    * ``"192.168.1.10/24"`` or bare ``"192.168.1.10"`` ->
      ``parameters["source_ip"] = "192.168.1.10"`` (bare IP, used by the IP sweep
      and MQTT) and ``parameters["local_address"] = "192.168.1.10/24"`` (ip/prefix,
      consumed by BACnet). A bare IP defaults BACnet to ``/32``.

    An operator-supplied ``parameters["source_ip"]`` / ``["local_address"]`` wins
    (``setdefault`` never clobbers an explicit run-level override). Raises
    ``ValueError`` on a malformed value so the route can return a clean 400.
    """
    value = (source_interface or "").strip()
    if not value or value.casefold() == _AUTO_SOURCE_INTERFACE.casefold():
        return
    interface = ipaddress.ip_interface(value)
    parameters.setdefault("source_ip", str(interface.ip))
    parameters.setdefault("local_address", interface.with_prefixlen)


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


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: Any, *, default: float) -> float:
    parsed = _to_float(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
