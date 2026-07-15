"""Shared BACnet run-parameter contract — the route <-> engine seam.

This module is the SINGLE SOURCE OF TRUTH for the BACnet transport/targeting
keys carried in a discovery run's ``parameters`` dict. It exists because that
dict crosses a process boundary: the API route (and the worker's Dramatiq
message) WRITES it, and the engine READS it. A key spelled ``"bbmd_addr"`` on
one side and ``"bbmd_address"`` on the other would pass the route suite and the
engine suite independently and fail only against a real BBMD on the lab
network, where nobody can debug it.

THE RULE THAT MAKES THAT IMPOSSIBLE: every producer and consumer of these
parameters — ``backend/app/api/routes/discovery.py``,
``backend/app/services/configuration_service.py``,
``engines/bacnet_discovery.py``, and BOTH test suites — imports these names and
uses them BY NAME. Never re-spell a key as a string literal at a call site. A
literal is invisible to the type checker and to every test; an import is not.

    from smart_commissioning_core.engines.bacnet_params import (
        PARAM_BBMD_ADDRESS, MODE_FOREIGN_DEVICE, read_targets,
    )
    parameters.setdefault(PARAM_BBMD_ADDRESS, "10.0.0.5")   # yes
    parameters.setdefault("bbmd_address", "10.0.0.5")       # NO

Placement: ``engines/`` alongside ``safety.py``, which is the same shape (pure,
stdlib-only, contract documented verbatim for the wiring agent). The backend
already imports ``smart_commissioning_core.engines.*`` directly
(``discovery.py`` imports both ``engines.bacnet_discovery`` and
``engines.safety``), so this adds NO new dependency direction.

DEPENDENCIES: standard library only. No ``bacpypes3`` import, no I/O, no
network, no logging side effects — so this module (and any test importing it)
is importable in CI, where the ``[bacnet]`` extra is never installed.


THE CONTRACT
============

Transport keys (flat JSON-safe scalars — they must survive the Dramatiq message
round-trip to the worker unchanged):

    bacnet_mode    str   "broadcast" (default) | "foreign_device"
    bbmd_address   str   bare IP of the BBMD, e.g. "10.0.0.5"
    bbmd_port      int   BBMD UDP port; soft default 47808
    fd_ttl         int   foreign-device subscription lifetime, seconds; soft default 300

Targeting key:

    bacnet_targets  list[dict]  RICH rows, NOT a flat list of addresses

Each ``bacnet_targets`` row:

    address          str   REQUIRED  bare IP from the register's "IP address"
    device_instance  int   REQUIRED  the register's "BACnet device instance"
    asset_id         str   optional  register identity, for reporting
    asset_name       str   optional  register identity, for reporting
    network          int   optional  the register's "BACnet network"

``address`` and ``device_instance`` are both required because the rows must
express **"expected but did not answer"** — an amber, per-device outcome that a
flat address list structurally cannot represent. Reporting which of the ~60
expected devices stayed silent is a required output of the run, not a nicety.


RULES THAT ARE EASY TO GET WRONG
================================

* **An absent register is LEGITIMATE, never a 400.** BACnet broadcast-only
  discovery is a valid scan. When no ``bacnet_register`` import exists,
  ``bacnet_targets`` is simply absent and the run proceeds on the broadcast
  (and, if configured, foreign-device) lanes. This differs deliberately from
  the IP route's ``_ensure_ip_targets``, which 400s because an IP scan with no
  targets has nothing to do at all. Do not copy that behaviour here.

* **Malformed / partial rows are SKIPPED, never fatal.** A legacy import
  predating the numeric validators must not turn a scan into a 500.
  :func:`parse_targets` drops any row it cannot read (see its docstring for the
  exact rules) and returns the rest.

* **Skipping is silent AT THIS LAYER, by design.** ``import_service`` already
  validates and rejects bad register rows at import time (``"IP address"``
  through ``ipaddress.ip_address``, ``"BACnet device instance"`` all-digits),
  so a row reaching here malformed is a legacy artefact, not operator input
  awaiting feedback. This is a parser, not a reporter: it has no run store, no
  logger, and no issue list. That is not a licence to hide a real scan result —
  the honest reporting of what answered, what stayed silent, and why is the
  ENGINE's job, built from the targets this module returns.

* **Absent ``bacnet_mode`` means broadcast** — the zero-regression default. With
  nothing new configured, discovery behaves exactly as it does today. But an
  *unrecognised* mode RAISES (see :func:`bacnet_mode`) rather than quietly
  degrading to broadcast: a silently-ignored transport config is the very bug
  v0.1.12 exists to fix, and it must not reappear as a default-case fallthrough.

* **This module does not validate reachability or IP format.**
  :func:`bbmd_address` returns a non-empty string, not a proven-routable host;
  ``ConfigurationService`` owns ``ipaddress`` validation of the BBMD Address
  (ValueError -> HTTP 400 with a fix-your-config message) at the point where a
  human can still act on it. Likewise a ``bacnet_targets`` address is
  structurally non-empty but not guaranteed to parse as a ``bacpypes3.pdu``
  ``Address`` — the engine must handle a per-target conversion failure without
  killing the lane.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BACNET_INSTANCE_MAX",
    "BACNET_INSTANCE_MIN",
    "BacnetTarget",
    "DEFAULT_BBMD_PORT",
    "DEFAULT_FD_TTL",
    "FD_TTL_MAX",
    "FD_TTL_MIN",
    "MODE_BROADCAST",
    "MODE_FOREIGN_DEVICE",
    "PARAM_BACNET_MODE",
    "PARAM_BACNET_TARGETS",
    "PARAM_BBMD_ADDRESS",
    "PARAM_BBMD_PORT",
    "PARAM_FD_TTL",
    "TARGET_ADDRESS",
    "TARGET_ASSET_ID",
    "TARGET_ASSET_NAME",
    "TARGET_DEVICE_INSTANCE",
    "TARGET_NETWORK",
    "UDP_PORT_MAX",
    "UDP_PORT_MIN",
    "bacnet_mode",
    "bbmd_address",
    "bbmd_port",
    "fd_ttl",
    "is_foreign_device_mode",
    "parse_targets",
    "read_targets",
]

# -- parameter keys ---------------------------------------------------------
#
# Import and use these BY NAME on both sides of the seam. See the module
# docstring: a re-spelled literal is exactly the failure this module prevents.

PARAM_BACNET_MODE = "bacnet_mode"
PARAM_BBMD_ADDRESS = "bbmd_address"
PARAM_BBMD_PORT = "bbmd_port"
PARAM_FD_TTL = "fd_ttl"
PARAM_BACNET_TARGETS = "bacnet_targets"

# -- bacnet_mode values -----------------------------------------------------

#: Local broadcast only. The default, and the behaviour that works today.
MODE_BROADCAST = "broadcast"

#: Register with a BBMD as a foreign device, so discovery reaches devices on
#: other subnets. Requires a usable ``bbmd_address``.
MODE_FOREIGN_DEVICE = "foreign_device"

_KNOWN_MODES = frozenset({MODE_BROADCAST, MODE_FOREIGN_DEVICE})

# -- bacnet_targets row keys ------------------------------------------------
#
# The persisted row shape is part of the seam too: route tests assert on it and
# the engine reads it back. Spell these by name as well.

TARGET_ADDRESS = "address"
TARGET_DEVICE_INSTANCE = "device_instance"
TARGET_ASSET_ID = "asset_id"
TARGET_ASSET_NAME = "asset_name"
TARGET_NETWORK = "network"

# -- defaults and bounds ----------------------------------------------------

#: BACnet/IP's IANA-assigned default port (0xBAC0), used when the stored "BBMD
#: UDP Port" is missing or unusable. Soft-defaulted rather than fatal: an old
#: snapshot holding junk here is not worth blocking a lab scan over.
DEFAULT_BBMD_PORT = 47808

#: Foreign-device subscription lifetime in seconds, used when the stored "TTL"
#: is missing or unusable. Soft-defaulted for the same reason as the port.
DEFAULT_FD_TTL = 300

UDP_PORT_MIN = 1
UDP_PORT_MAX = 65535

#: bacpypes3 carries ``fdSubscriptionLifetime`` as a BACnet Unsigned16, so a
#: TTL outside this range cannot be represented on the wire at all.
FD_TTL_MIN = 1
FD_TTL_MAX = 65535

#: BACnet device instances span 0..4194303 (22-bit). A row outside this range
#: is malformed and is dropped by :func:`parse_targets`.
BACNET_INSTANCE_MIN = 0
BACNET_INSTANCE_MAX = 4194303


# -- targets ----------------------------------------------------------------


@dataclass(frozen=True)
class BacnetTarget:
    """One expected device from the ``bacnet_register`` import.

    Frozen so a parsed target cannot be mutated into disagreeing with the
    persisted row it came from.
    """

    address: str
    device_instance: int
    asset_id: str | None = None
    asset_name: str | None = None
    network: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        """Dedup identity: ``(address, device_instance)``.

        Note this is NOT the device-merge identity. Merging discovered devices
        keys on ``device_instance`` alone (spec-unique network-wide); this key
        only de-duplicates register ROWS, where the same instance legitimately
        appears under two addresses in a misconfigured register and both are
        worth probing.
        """
        return (self.address, self.device_instance)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe row for ``parameters[PARAM_BACNET_TARGETS]``.

        Optional fields are omitted when unset rather than written as ``None``,
        so the persisted parameters stay minimal and a round-trip through
        :func:`parse_targets` is stable.
        """
        row: dict[str, Any] = {
            TARGET_ADDRESS: self.address,
            TARGET_DEVICE_INSTANCE: self.device_instance,
        }
        if self.asset_id is not None:
            row[TARGET_ASSET_ID] = self.asset_id
        if self.asset_name is not None:
            row[TARGET_ASSET_NAME] = self.asset_name
        if self.network is not None:
            row[TARGET_NETWORK] = self.network
        return row


def parse_targets(rows: Any) -> list[BacnetTarget]:
    """Normalise arbitrary register/parameter rows into targets.

    Does not raise on any JSON-shaped input (``None`` / scalars / list / dict,
    nested arbitrarily) — which is every input it can actually receive, since
    both callers read JSON: the route from the import repository's JSON rows,
    the engine from run parameters that survived the Dramatiq round-trip. That
    guarantee is what lets the route call this without a try/except and still
    never turn a legacy register row into a 500.

    Used by BOTH sides of the seam, which is what keeps them in agreement:

        route:   parameters[PARAM_BACNET_TARGETS] = [t.as_dict() for t in parse_targets(register_rows)]
        engine:  targets = read_targets(parameters)

    The function is idempotent — parsing already-normalised ``as_dict()`` rows
    returns the same targets — so the route's write and the engine's read cannot
    drift apart.

    Rules (a dropped row is never fatal; see the module docstring):

    * ``rows`` that is None, a str/bytes, a Mapping, or any non-iterable -> ``[]``.
    * A row that is not a Mapping is dropped.
    * A blank/missing ``address`` is dropped — there is nothing to probe.
    * A ``device_instance`` that is missing, non-numeric, or outside
      0..4194303 is dropped: without a usable instance the row cannot be
      matched against a discovered device, so it could never be reported as
      "expected but did not answer" and would be dead weight.
    * ``asset_id`` / ``asset_name`` / ``network`` are optional; unusable values
      become ``None`` and do NOT drop the row (a target with no asset name is
      still perfectly probeable).
    * Deduplicated on ``(address, device_instance)``, keeping first-seen order —
      which is register order, so the operator's list reads in the order they
      wrote it.
    """
    if rows is None or isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        return []
    targets: list[BacnetTarget] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        target = _parse_target_row(row)
        if target is None or target.key in seen:
            continue
        seen.add(target.key)
        targets.append(target)
    return targets


def read_targets(parameters: Mapping[str, Any]) -> list[BacnetTarget]:
    """Read ``parameters[PARAM_BACNET_TARGETS]`` as targets.

    An absent key returns ``[]`` — a broadcast-only run, which is legitimate.
    """
    return parse_targets(parameters.get(PARAM_BACNET_TARGETS))


def _parse_target_row(row: Any) -> BacnetTarget | None:
    if not isinstance(row, Mapping):
        return None
    address = _optional_text(row.get(TARGET_ADDRESS))
    if address is None:
        return None
    instance = _optional_int(row.get(TARGET_DEVICE_INSTANCE))
    if instance is None or not BACNET_INSTANCE_MIN <= instance <= BACNET_INSTANCE_MAX:
        return None
    return BacnetTarget(
        address=address,
        device_instance=instance,
        asset_id=_optional_text(row.get(TARGET_ASSET_ID)),
        asset_name=_optional_text(row.get(TARGET_ASSET_NAME)),
        network=_optional_int(row.get(TARGET_NETWORK)),
    )


# -- transport --------------------------------------------------------------


def bacnet_mode(parameters: Mapping[str, Any]) -> str:
    """Return the resolved transport mode: :data:`MODE_BROADCAST` or :data:`MODE_FOREIGN_DEVICE`.

    Absent/blank -> :data:`MODE_BROADCAST`, the zero-regression default.

    An UNRECOGNISED mode raises :class:`ValueError` — it is never silently
    downgraded to broadcast. Callers should surface that honestly: the route
    maps it to a 400 (the operator can still fix the request), and the engine
    fails the run with the message rather than scanning on a transport the
    operator did not ask for. Silently ignoring a transport setting is the
    original bug.
    """
    raw = parameters.get(PARAM_BACNET_MODE)
    if raw is None:
        return MODE_BROADCAST
    text = str(raw).strip().casefold()
    if not text:
        return MODE_BROADCAST
    if text in _KNOWN_MODES:
        return text
    raise ValueError(
        f"Unsupported {PARAM_BACNET_MODE} {raw!r}. Use {MODE_FOREIGN_DEVICE!r} to register with a BBMD, "
        f"or omit it for {MODE_BROADCAST!r} (local broadcast only)."
    )


def is_foreign_device_mode(parameters: Mapping[str, Any]) -> bool:
    """True when the run asked for foreign-device registration via a BBMD.

    THE gate for the foreign-device lane. Use this rather than comparing the
    mode inline, so the comparison exists in exactly one place. Raises
    :class:`ValueError` on an unrecognised mode (see :func:`bacnet_mode`).
    """
    return bacnet_mode(parameters) == MODE_FOREIGN_DEVICE


def bbmd_address(parameters: Mapping[str, Any]) -> str | None:
    """Return the BBMD address as a non-empty string, or None if absent/blank.

    NOT validated as an IP here — ``ConfigurationService`` owns that check,
    where a bad value becomes a 400 the operator can act on. A non-None return
    means "the operator supplied something", not "this host is reachable".

    A caller in :data:`MODE_FOREIGN_DEVICE` that gets None here must FAIL the
    run with an actionable message naming the missing setting. It must never
    fall back to broadcast: that would report a clean local-broadcast scan for a
    run the operator explicitly asked to send through a BBMD.
    """
    return _optional_text(parameters.get(PARAM_BBMD_ADDRESS))


def bbmd_port(parameters: Mapping[str, Any]) -> int:
    """BBMD UDP port, soft-defaulting to :data:`DEFAULT_BBMD_PORT` (47808).

    Missing, non-numeric, or out of 1..65535 -> the default. Soft rather than
    fatal by decision: an old config snapshot holding junk here should not block
    a scan.
    """
    return _bounded_int(parameters.get(PARAM_BBMD_PORT), default=DEFAULT_BBMD_PORT, low=UDP_PORT_MIN, high=UDP_PORT_MAX)


def fd_ttl(parameters: Mapping[str, Any]) -> int:
    """Foreign-device subscription lifetime in seconds, soft-defaulting to 300.

    Missing, non-numeric, or outside :data:`FD_TTL_MIN`..:data:`FD_TTL_MAX` ->
    :data:`DEFAULT_FD_TTL`. The upper bound is not arbitrary: the value travels
    as a BACnet Unsigned16, so a larger number has no wire representation.
    """
    return _bounded_int(parameters.get(PARAM_FD_TTL), default=DEFAULT_FD_TTL, low=FD_TTL_MIN, high=FD_TTL_MAX)


# -- small pure readers -----------------------------------------------------


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    """Int from an int/float/str, or None. Bools are NOT ints here.

    Config and CSV values arrive as strings ("300"), so str is accepted. ``True``
    is rejected because a bool sneaking into a device instance would silently
    become instance 1 — a real device address invented out of a type error.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    parsed = _optional_int(value)
    if parsed is None or not low <= parsed <= high:
        return default
    return parsed
