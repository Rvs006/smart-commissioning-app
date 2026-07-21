"""BACnet/IP device discovery engine, behind a swappable backend abstraction.

Discovery runs THREE INDEPENDENT WHO-IS LANES, then reads each device it heard:

    1. **Local broadcast** on UDP 47808. The path that works today, and it stays
       byte-identical when nothing new is configured (the zero-regression pin).
       Reaches the laptop's own subnet.
    2. **Directed unicast** to the addresses in the ``bacnet_register`` import —
       FALLBACK-ONLY, sent only to devices still silent after lanes 1 and 3. It
       crosses subnets by ordinary IP routing with NO BBMD involved, so it is
       the lane that still works when a site's BBMD refuses to cooperate.
    3. **Foreign-device broadcast** through a BBMD on UDP 47809, gated strictly
       on ``bacnet_mode == 'foreign_device'``. Reaches other subnets, including
       routed MS/TP devices that lane 2 structurally cannot see.

    Then, per device: read its ``object-list`` and the ``present-value`` of each
    readable point, back through the lane that heard it.

The lanes are deliberately redundant: each reaches devices the others cannot, so
no single unknown about a site's topology blanks a commissioning day. They are
also deliberately NON-SUBSTITUTING — a lane that fails is reported as failed and
never quietly replaced by another, because "we scanned something else instead"
reported as success is the bug this design exists to remove.

Every run stamps ``result_summary["bacnet_diagnostics"]`` (interface, port, bind
outcome, mode, BBMD registration outcome, Who-Is counters, bacpypes3 version), a
per-lane breakdown, and the expected-but-silent register rows — on SUCCESS and
on every self-diagnosed failure alike. The bar is that a failed scan can be
diagnosed from the run record alone, without a live debugging session.

The engine drives a :class:`BacnetDiscoveryBackend` (an async Protocol) under
the shared :class:`~smart_commissioning_core.engines.base.Throttle`, honours
cooperative cancellation, and emits ``discovered_assets`` + DiscoveredDevice /
DiscoveredPoint records in the DiscoveryRepository row shapes.

HONESTY / TESTABILITY (read this before trusting any "it works" claim):

    * :class:`SimulatedBacnetBackend` is a deterministic in-memory fixture used
      only for dry-run previews and explicitly injected tests. Results from it
      are labelled ``result_summary["backend"] == "simulated"``.
    * :class:`Bacpypes3Backend` is the real BACnet/IP path. It has NEVER been
      integration-tested in this environment (there is no BACnet device or
      building network here). It REQUIRES on-site validation against real
      controllers before it can be trusted. Its ``bacpypes3`` import is lazy and
      guarded so importing this module never requires ``bacpypes3`` to be
      installed.

WHY THE SILENT PARTS ARE THE DANGEROUS PARTS (read before editing the backend):

    bacpypes3 does NOT raise when it cannot bind its UDP socket —
    ``IPv4DatagramServer`` schedules ``retrying_create_datagram_endpoint`` and
    swallows ``OSError`` in an infinite 1s retry. A contended UDP 47808 (a
    BACnet browser left open, or this process's own leaked socket from a
    previous scan) therefore looks EXACTLY like "no devices answered". The same
    is true of a BBMD that refuses or ignores a foreign-device registration:
    ``BIPForeign`` drops every broadcast and Who-Is returns ``[]``.

    Two mechanisms in this module exist solely to convert those silences into
    named, actionable failures, and neither is optional:

        1. :func:`preflight_bind` — a plain stdlib bind/close on the exact
           (ip, port) BEFORE any Application is constructed. This is the ONLY
           way a port conflict is detectable at all.
        2. :meth:`Bacpypes3Backend._ensure_registered` — a bounded wait on the
           BBMD registration status before the first Who-Is on a foreign-mode
           app, hard-failing on NAK/timeout rather than scanning into a void.

    :func:`build_transport_plan` is deliberately PURE (no bacpypes3, no socket)
    so the construction decisions above are assertable in CI, where the
    ``[bacnet]`` extra is never installed and no BACnet hardware exists.

This module imports cleanly with only the standard library + the engine
framework; ``bacpypes3`` is an OPTIONAL extra (``pip install
smart-commissioning-core[bacnet]``).
"""

from __future__ import annotations

import asyncio
import errno
import importlib.metadata
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from smart_commissioning_core.engines.bacnet_params import (
    BACNET_INSTANCE_MAX,
    BACNET_INSTANCE_MIN,
    DEFAULT_BBMD_PORT,
    MODE_BROADCAST,
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_MODE,
    PARAM_BBMD_ADDRESS,
    PARAM_BBMD_PORT,
    BacnetTarget,
    bacnet_mode,
    bbmd_address,
    bbmd_port,
    fd_ttl,
    is_foreign_device_mode,
    read_targets,
)
from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    Throttle,
    ThrottleConfig,
    run_engine,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_store import RunStore

logger = logging.getLogger(__name__)

# Stable engine identifier used in dry-run plans / summaries.
ENGINE_NAME = "bacnet_discovery"

# Backend selector values (parameters["bacnet_backend"] / config).
BACKEND_SIMULATED = "simulated"
BACKEND_BACPYPES3 = "bacpypes3"

# -- local transport constants ----------------------------------------------
#
# NOT part of the route <-> engine seam (the route never sends these), so they
# live here rather than in bacnet_params: nothing outside this module writes a
# local bind port.

#: BACnet/IP's assigned UDP port (0xBAC0). Devices send their I-Am here, so the
#: local-broadcast lane must own it.
DEFAULT_LOCAL_UDP_PORT = 47808

#: The foreign-device lane binds its OWN port so it can coexist with the
#: local-broadcast app (one bacnetIPMode per network-port, one stack per UDP
#: port — bacpypes3's own multiple-stacks sample runs 47808 + 47809 on one IP).
#: It also sidesteps a third-party BACnet browser squatting 47808.
FD_LOCAL_UDP_PORT = 47809

#: Identity this scanner presents on the wire. 4194303 is the BACnet
#: unconfigured-device wildcard — a safe transient identity for a scanner that
#: must not collide with a real device instance.
SCANNER_DEVICE_INSTANCE = 4194303
SCANNER_DEVICE_NAME = "SmartCommissioningScanner"
SCANNER_VENDOR_ID = 999
SCANNER_NETWORK_PORT_INSTANCE = 1
SCANNER_NETWORK_PORT_NAME = "NetworkPort-1"

#: Hard ceiling on how many register targets the directed lane may probe in one
#: run. Mirrors ip_scan's MAX_HOSTS_CEILING and carries the same operator-policy
#: rule: a request's ``max_targets`` may LOWER this, never raise it, so an
#: oversized register can never quietly become a packet storm on a live OT
#: network. Monday's ~60-device lab sits at ~6% of this.
MAX_BACNET_UNICAST_TARGETS_CEILING = 1024

# -- lane / provenance labels -----------------------------------------------
#
# Stamped into discovered_assets and the run's lanes breakdown, and asserted on
# by the tests. Import them BY NAME (same rule as the bacnet_params keys).

#: Heard via a Who-Is BROADCAST — lane 1 (local) or lane 3 (through the BBMD).
#: Byte-identical to the label every release before v0.1.12 stamped: the
#: local-broadcast lane must keep describing its devices exactly as it always
#: has, or the zero-regression pin is broken by the reporting layer.
MATCH_BASIS_WHO_IS = "bacnet_who_is"
#: Heard ONLY via a directed (unicast) Who-Is to a register address — lane 2.
MATCH_BASIS_WHO_IS_DIRECTED = "bacnet_who_is_directed"

LANE_BROADCAST = "broadcast"
LANE_DIRECTED = "directed"
LANE_FOREIGN_DEVICE = "foreign_device"

# -- issue types ------------------------------------------------------------

#: A register row no lane heard from. AMBER, never a failure, and never
#: "device absent": BACnet-135 lets a device answer a directed Who-Is with a
#: local-broadcast I-Am that this host cannot hear from off-subnet, and routed
#: MS/TP devices are invisible to the directed lane BY DESIGN. Silence here is
#: INCONCLUSIVE. Claiming otherwise would put "device offline" next to ~60 lab
#: devices that are merely unreachable by one lane.
ISSUE_EXPECTED_DEVICE_SILENT = "bacnet_expected_device_silent"
#: A device answered at a register address carrying a DIFFERENT device instance.
ISSUE_REGISTER_INSTANCE_MISMATCH = "bacnet_register_instance_mismatch"
#: Two different addresses announced the SAME device instance (instances are
#: spec-unique network-wide, so this is a real misconfiguration worth naming).
ISSUE_INSTANCE_COLLISION = "bacnet_instance_collision"
#: A device was heard (its I-Am is real) but its object-list read failed, so its
#: points could not be enumerated. Reported discovered-but-unenumerated — never
#: dropped, never faked as fully read.
ISSUE_OBJECT_LIST_UNREADABLE = "bacnet_object_list_unreadable"
#: A device's point reads failed back-to-back, so the scan stopped asking for its
#: remaining points. The points not attempted are ABSENT (never recorded as
#: failures — absent != failed).
ISSUE_POINT_READS_ABORTED = "bacnet_point_reads_aborted"

#: After this many CONSECUTIVE per-point read failures on one device, stop reading
#: its remaining points. A device that answered Who-Is but refuses reads (wrong
#: lane, ACL, went quiet) otherwise burns ~12s (bacpypes3 apduTimeout x retries)
#: per dead point — a 500-object device would take ~100 min back-to-back. Reset to
#: 0 on any successful read, so a device whose first few object types happen to be
#: unreadable is not abandoned.
#: ponytail: fixed threshold; make it a run parameter if a real device legitimately
#: leads with >5 unreadable points.
_MAX_CONSECUTIVE_POINT_READ_FAILURES = 5

#: bacpypes3's ErrorRejectAbortNack (Error/Reject/Abort PDUs) subclasses
#: BaseException, NOT Exception (verified against pinned bacpypes3==0.0.106,
#: apdu.py: ``class ErrorRejectAbortNack(BaseException)``), so a bare
#: ``except Exception`` around a ReadProperty MISSES a real APDU error and lets one
#: unreadable object/device fail the entire run. The per-device read catches below
#: therefore catch BaseException but ALWAYS re-raise true control-flow signals.
_READ_PROPAGATE = (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)
#: The object-list read additionally treats RuntimeError as a vetted transport-dead
#: failure that must fail the run (matching the engine's vetted/unvetted split).
_OBJECT_LIST_PROPAGATE = (RuntimeError, *_READ_PROPAGATE)


def _is_worker_interrupt(exc: BaseException) -> bool:
    """True for a dramatiq worker Interrupt (TimeLimitExceeded / Shutdown).

    core cannot import dramatiq, so match by top-level module. dramatiq's Interrupt
    subclasses BaseException, and the TimeLimit middleware delivers TimeLimitExceeded
    via PyThreadState_SetAsyncExc EXACTLY ONCE (it nulls the deadline first). If the
    per-point / object-list ``except BaseException`` guards swallowed it, the worker
    time limit would be silently disarmed and the run stranded — so it is re-raised
    to reach worker.app.tasks' own ``except Interrupt`` handling.
    """
    return type(exc).__module__.split(".", 1)[0] == "dramatiq"

#: Stage stamped on the mid-run progress writes below. Mirrors base._STAGE_RUNNING
#: ("engine_running"), which the initial 15% write already set — kept in step so a
#: progress write never changes the stage the monitor is showing.
_STAGE_RUNNING_ENGINE = "engine_running"

# -- bind pre-flight outcomes -----------------------------------------------

#: The port is free (or was, a moment ago — see preflight_bind's race note).
BIND_OK = "ok"
#: Another process holds the port. The actionable, common case.
BIND_PORT_IN_USE = "udp_port_in_use"
#: Any other bind failure (interface down, IP not on this host, ...).
BIND_FAILED = "interface_bind_failed"

#: Errno values that mean "somebody else has this port". Deliberately carries
#: BOTH the symbolic POSIX constants AND the literal Windows WSA codes: on
#: Windows a socket OSError may surface the WSA code via ``errno``, via
#: ``winerror``, or both, and errno.EADDRINUSE does NOT equal 10048 there.
#: Checking the union is correct under every mapping without platform
#: branching. VERIFIED BY DESIGN, NEVER BY EXECUTION — no Python runs on the
#: dev machine; the Windows leg is first exercised on the field laptop.
_PORT_IN_USE_CODES = frozenset({errno.EADDRINUSE, 10048, errno.EACCES, 10013})

# -- foreign-device registration outcomes -----------------------------------
#
# Import these BY NAME (like the bacnet_params keys) rather than re-spelling
# them: they are stamped into the run record's diagnostics and asserted on.

#: bbmdRegistrationStatus == 0: the BBMD acknowledged. Only value safe to scan on.
FD_REGISTRATION_REGISTERED = "registered"
#: bbmdRegistrationStatus > 0: the BBMD NAK'd; the value IS the BVLL result code.
FD_REGISTRATION_REFUSED = "refused"
#: bbmdRegistrationStatus is -1 (in process) / -2 (unregistered): still waiting.
FD_REGISTRATION_PENDING = "pending"
#: Still pending when the wait window expired — no answer from the BBMD.
FD_REGISTRATION_TIMEOUT = "timeout"
#: The status could not be read at all (bacpypes3 internals not as expected).
FD_REGISTRATION_UNKNOWN = "unknown"

#: How long to wait for the BBMD to acknowledge before failing the run.
#: bacpypes3 re-attempts registration every min(5, ttl) seconds, so this window
#: spans at least two attempts — a single dropped packet does not become a false
#: "BBMD did not answer".
FD_REGISTRATION_WAIT_S = 10.0
FD_REGISTRATION_POLL_INTERVAL_S = 0.25

#: Single copy of the no-Source-Interface sentence. Both the engine's early
#: guard and the transport-plan builder raise it, and the operator must get the
#: same instruction from either — two drifting copies of a fix-it message is how
#: an operator ends up following the wrong one.
_NO_SOURCE_INTERFACE_MESSAGE = (
    "No Source Interface selected for a live BACnet scan. Open the "
    "Configuration page, set Source Interface to your wired network "
    "adapter, and Save, then run the scan again — a real BACnet Who-Is "
    "must bind to a specific local network interface."
)


def _json_safe_value(value: Any) -> Any:
    """Coerce a backend-supplied value into a JSON-serializable primitive.

    The live bacpypes3 backend returns LIBRARY objects, not plain primitives: a
    binary-input present-value is an enumerated object, a vendor id is an
    ``Unsigned``. Left raw, they reach a JSON repository column / result_summary
    and raise on serialization AFTER the scan already completed on the wire —
    the 100%-reproducible field crash this guard removes (simulated/dry-run data
    is plain primitives, which is why only real runs died).

    Primitives (``None`` / ``bool`` / ``int`` / ``float`` / ``str``) pass
    through UNCHANGED; anything else is coerced with ``str()``. For a bacpypes3
    enumerated value that yields the honest observed token (e.g. ``"active"``).
    No numeric re-encoding is ever invented — an unrecognised value is reported
    as the string the library itself renders, never a fabricated number.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@runtime_checkable
class BacnetDiscoveryBackend(Protocol):
    """Async transport abstraction for BACnet discovery.

    All three methods are ``async`` — the engine drives them under the shared
    :class:`Throttle` (an asyncio construct), so a consistent async contract
    keeps the call sites uniform. A synchronous transport (e.g. a blocking
    library) should wrap its blocking calls in ``asyncio.to_thread`` inside
    these coroutines.

    The dict shapes are intentionally loose so a backend may carry extra
    vendor-specific keys; the engine reads a documented core subset (see
    :func:`_device_asset` / :func:`_point_record`) and stores the rest under the
    record ``attributes``.
    """

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        """Broadcast Who-Is over ``[low_limit, high_limit]`` and return devices.

        Each returned dict SHOULD carry at least:
            ``device_instance`` (int), ``address`` (str). Optional:
            ``vendor`` / ``vendor_id``, ``model``, ``name``, and any extra keys
            (stored under the device record's ``attributes``).

        ``address`` optionally targets a unicast/directed Who-Is instead of a
        full broadcast.
        """
        ...

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the readable objects (points) for ``device``.

        ``device`` is one of the dicts returned by :meth:`who_is`. Each returned
        object dict SHOULD carry at least:
            ``object_identifier`` (str, e.g. ``"analog-input,3"``). Optional:
            ``object_name``, ``object_type``, ``units``, and extra keys.
        """
        ...

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        """Read and return the ``present-value`` of ``obj`` on ``device``.

        ``device`` / ``obj`` are dicts returned by :meth:`who_is` /
        :meth:`read_object_list`. The return value is the raw decoded value
        (number, bool, str, ...) and is stored JSON-wrapped on the point record.
        """
        ...


# -- simulated backend ------------------------------------------------------


# Deterministic fixture: a handful of fake devices, each with a fixed set of
# objects and present-values. Stable across runs so tests can assert on it.
_DEFAULT_SIM_DEVICES: tuple[dict[str, Any], ...] = (
    {
        "device_instance": 1001,
        "address": "10.10.0.11:47808",
        "name": "AHU-1 Controller",
        "vendor": "Acme Controls",
        "vendor_id": 999,
        "model": "ACME-VAV-200",
        "objects": [
            {
                "object_identifier": "analog-input,1",
                "object_name": "SupplyAirTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 18.6,
            },
            {
                "object_identifier": "analog-input,2",
                "object_name": "ReturnAirTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 22.1,
            },
            {
                "object_identifier": "binary-output,1",
                "object_name": "SupplyFanCmd",
                "object_type": "binary-output",
                "units": None,
                "present_value": "active",
            },
        ],
    },
    {
        "device_instance": 1002,
        "address": "10.10.0.12:47808",
        "name": "VAV-3rd-Floor-01",
        "vendor": "Acme Controls",
        "vendor_id": 999,
        "model": "ACME-VAV-100",
        "objects": [
            {
                "object_identifier": "analog-value,10",
                "object_name": "ZoneTempSetpoint",
                "object_type": "analog-value",
                "units": "degreesCelsius",
                "present_value": 21.0,
            },
            {
                "object_identifier": "analog-input,5",
                "object_name": "ZoneTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 21.4,
            },
        ],
    },
    {
        "device_instance": 2050,
        "address": "10.10.0.30:47808",
        "name": "Chiller-Plant-Ctrl",
        "vendor": "Globex BMS",
        "vendor_id": 555,
        "model": "GLX-CH-9",
        "objects": [
            {
                "object_identifier": "analog-input,7",
                "object_name": "ChilledWaterSupplyTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 6.7,
            },
        ],
    },
)


class SimulatedBacnetBackend:
    """Deterministic in-memory BACnet backend for offline tests/demos.

    Performs NO network I/O. ``who_is`` filters the fixture by the requested
    instance range; ``read_object_list`` / ``read_present_value`` read from the
    fixture. This is the DEFAULT backend, so the engine produces sample
    ``discovered_assets`` with zero hardware — callers MUST treat the output as
    simulated (the engine stamps ``result_summary["backend"] == "simulated"``).

    A custom fixture may be supplied for richer test scenarios; it must follow
    the ``_DEFAULT_SIM_DEVICES`` shape.
    """

    backend_name = BACKEND_SIMULATED

    def __init__(self, devices: Sequence[Mapping[str, Any]] | None = None) -> None:
        source = devices if devices is not None else _DEFAULT_SIM_DEVICES
        # Deep-ish copy so callers/tests cannot mutate the shared fixture.
        self._devices: list[dict[str, Any]] = [dict(device) for device in source]

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,  # noqa: ARG002 - part of the Protocol; unused by the sim
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for device in self._devices:
            instance = device.get("device_instance")
            if not isinstance(instance, int):
                continue
            if low_limit <= instance <= high_limit:
                # Return device metadata WITHOUT the embedded objects list; the
                # engine fetches objects via read_object_list, mirroring how a
                # real backend works (Who-Is returns device info, not points).
                matched.append({k: v for k, v in device.items() if k != "objects"})
        return matched

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        fixture = self._find(device)
        objects = fixture.get("objects") if fixture else None
        if not isinstance(objects, list):
            return []
        # Strip present_value here — it is fetched separately via
        # read_present_value, matching the real ReadProperty two-step.
        return [{k: v for k, v in dict(obj).items() if k != "present_value"} for obj in objects]

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        fixture = self._find(device)
        if not fixture:
            return None
        target_id = obj.get("object_identifier")
        for candidate in fixture.get("objects", []):
            if candidate.get("object_identifier") == target_id:
                return candidate.get("present_value")
        return None

    def _find(self, device: Mapping[str, Any]) -> dict[str, Any] | None:
        instance = device.get("device_instance")
        for fixture in self._devices:
            if fixture.get("device_instance") == instance:
                return fixture
        return None


# -- transport plan (PURE — no bacpypes3, no sockets, no I/O) ---------------
#
# Everything in this section is plain data and plain functions. That is the
# point: the construction decisions that reach the lab (which IP/port we bind,
# broadcast vs foreign mode, the exact fdBBMDAddress string) are the highest-
# risk part of the fix, and they must be assertable in CI — which has neither
# bacpypes3 nor a BACnet network. The impure shell below (_ensure_app) does
# nothing but hand these values to bacpypes3.


@dataclass(frozen=True)
class BacnetTransportPlan:
    """Resolved, JSON-safe description of ONE bacpypes3 Application to build.

    One plan == one Application == one UDP port == one ``bacnetIPMode``. A
    foreign-mode app sets ``no_broadcast`` internally and so cannot also do
    local-subnet discovery; that is why an FD run builds TWO apps (local
    broadcast on 47808, foreign on 47809) rather than one, and why this is a
    per-app plan rather than a per-run one.

    Attributes:
        mode: ``MODE_BROADCAST`` or ``MODE_FOREIGN_DEVICE`` (bacnet_params).
        local_address: the string handed verbatim to ``NetworkPortObject`` —
            e.g. ``"192.168.1.10/24"`` or ``"192.168.1.10/24:47809"``.
        bind_ip: the bare local IP, for the stdlib bind pre-flight.
        udp_port: the resolved local UDP port, for the same pre-flight.
        fd_bbmd_address: ``"ip:port"`` for ``HostNPort``, ALWAYS with an
            explicit port (never a bare IP — see :func:`build_transport_plan`).
            ``None`` in broadcast mode.
        fd_ttl: ``fdSubscriptionLifetime`` seconds. ``None`` in broadcast mode.
    """

    mode: str
    local_address: str
    bind_ip: str
    udp_port: int
    device_instance: int = SCANNER_DEVICE_INSTANCE
    device_name: str = SCANNER_DEVICE_NAME
    vendor_identifier: int = SCANNER_VENDOR_ID
    network_port_instance: int = SCANNER_NETWORK_PORT_INSTANCE
    network_port_name: str = SCANNER_NETWORK_PORT_NAME
    fd_bbmd_address: str | None = None
    fd_ttl: int | None = None

    @property
    def is_foreign_device(self) -> bool:
        """True when this app must register with a BBMD before any Who-Is."""
        return self.mode == MODE_FOREIGN_DEVICE

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the run record's diagnostics / the dry-run plan.

        Every value is operator-configured settings or a constant identity — no
        credentials, no raw exception text (see base.py's sanitization posture).
        """
        plan: dict[str, Any] = {
            "mode": self.mode,
            "local_address": self.local_address,
            "bind_ip": self.bind_ip,
            "udp_port": self.udp_port,
            "device_instance": self.device_instance,
        }
        if self.fd_bbmd_address is not None:
            plan["fd_bbmd_address"] = self.fd_bbmd_address
            plan["fd_ttl"] = self.fd_ttl
        return plan


def split_local_address(address: str) -> tuple[str, str | None, int | None]:
    """Split ``"ip[/prefix][:port]"`` into ``(ip, prefix, port)``.

    Accepts every shape this codebase actually produces or documents:
    ``"192.168.1.10"``, ``"192.168.1.10/24"`` (what ``engine_dispatch`` injects
    from the configured Source Interface), ``"192.168.1.10/24:47808"``, and
    ``"192.168.1.10:47808"``.

    Lenient by design: it never raises, and anything it cannot read comes back
    as ``None`` rather than a guess. The caller decides what a missing piece
    means — inventing a port or an IP here would be exactly the kind of silent
    substitution this release exists to remove. Bare IPv6 is not special-cased
    (BACnet/IP here is v4); a value with several colons keeps its colons and
    reports no port rather than mangling itself.
    """
    text = str(address or "").strip()
    if not text:
        return "", None, None
    port: int | None = None
    # Only treat a trailing ":<digits>" as a port when there is exactly one
    # colon, so an IPv6-ish string is left intact instead of being truncated.
    if text.count(":") == 1:
        head, _, tail = text.rpartition(":")
        if head and tail.isdigit():
            parsed = int(tail)
            if 0 < parsed <= 65535:
                text, port = head, parsed
    ip, sep, prefix = text.partition("/")
    return ip.strip(), (prefix.strip() or None) if sep else None, port


def format_local_address(ip: str, prefix: str | None, port: int | None) -> str:
    """Rebuild the ``"ip[/prefix][:port]"`` form ``NetworkPortObject`` parses."""
    text = ip if prefix is None else f"{ip}/{prefix}"
    return text if port is None else f"{text}:{port}"


def build_transport_plan(
    parameters: Mapping[str, Any],
    *,
    local_address: str | None = None,
    mode: str | None = None,
    udp_port: int | None = None,
) -> BacnetTransportPlan:
    """Resolve run parameters into ONE app's construction plan. PURE.

    Args:
        parameters: the run parameters (read via the ``bacnet_params``
            contract — never by re-spelled string literals).
        local_address: overrides ``parameters["local_address"]`` (the Source
            Interface injected by ``engine_dispatch``).
        mode: overrides the run's ``bacnet_mode``. Required for the two-app
            layout: lane 1 must be built with ``MODE_BROADCAST`` even on a run
            whose mode is ``MODE_FOREIGN_DEVICE``.
        udp_port: overrides the local bind port (lane 3 passes
            :data:`FD_LOCAL_UDP_PORT`).

    Raises:
        ValueError: with an OPERATOR-ACTIONABLE message — no Source Interface,
            or foreign mode with no BBMD address. Foreign mode without a BBMD
            address must never quietly become a broadcast scan: that would
            report a clean local scan for a run the operator explicitly asked to
            send through a BBMD, which is the original bug wearing a new hat.
    """
    resolved_mode = bacnet_mode(parameters) if mode is None else str(mode).strip().casefold()
    if resolved_mode not in {MODE_BROADCAST, MODE_FOREIGN_DEVICE}:
        # An unrecognised override is a caller bug, but it must not degrade to
        # broadcast: the whole point of this release is that a transport the
        # operator did not ask for is never silently substituted.
        raise ValueError(
            f"Unsupported BACnet transport mode {mode!r}. Use {MODE_BROADCAST!r} or {MODE_FOREIGN_DEVICE!r}."
        )
    raw_address = local_address if local_address is not None else parameters.get("local_address")
    ip, prefix, embedded_port = split_local_address(str(raw_address or ""))
    if not ip:
        raise ValueError(_NO_SOURCE_INTERFACE_MESSAGE)

    resolved_port = udp_port if udp_port is not None else (embedded_port or DEFAULT_LOCAL_UDP_PORT)
    # ZERO-REGRESSION PIN: when no port override is needed, hand bacpypes3 the
    # operator's address string BYTE-IDENTICALLY to today rather than helpfully
    # re-rendering it with ":47808". Today's working local-broadcast path must
    # not be the place we discover that NetworkPortObject parses a port suffix
    # differently than we assume; that risk is confined to the new FD lane,
    # which genuinely cannot express port 47809 any other way.
    if udp_port is None or udp_port == embedded_port:
        plan_address = str(raw_address).strip()
    else:
        plan_address = format_local_address(ip, prefix, resolved_port)

    if resolved_mode != MODE_FOREIGN_DEVICE:
        return BacnetTransportPlan(
            mode=resolved_mode,
            local_address=plan_address,
            bind_ip=ip,
            udp_port=resolved_port,
        )

    bbmd_host_raw = bbmd_address(parameters)
    if not bbmd_host_raw:
        raise ValueError(
            "Foreign-device registration is enabled but no BBMD Address is set. "
            "Open the Configuration page, enter the BBMD's IP address, and Save, "
            "then run the scan again."
        )
    # The contract says bbmd_address is a bare IP, and ConfigurationService
    # validates it as one — but a snapshot predating that validator can hold
    # "10.0.0.5:47808". Parse the host back out so the explicit-port form below
    # can never become the un-routable "10.0.0.5:47808:47808".
    bbmd_host, _, bbmd_embedded_port = split_local_address(bbmd_host_raw)
    if parameters.get(PARAM_BBMD_PORT) is not None:
        resolved_bbmd_port = bbmd_port(parameters)
    else:
        resolved_bbmd_port = bbmd_embedded_port or DEFAULT_BBMD_PORT

    return BacnetTransportPlan(
        mode=MODE_FOREIGN_DEVICE,
        local_address=plan_address,
        bind_ip=ip,
        udp_port=resolved_port,
        # ALWAYS explicit "ip:port". HostNPort accepts a bare IP and applies its
        # own default, but that default was never verbatim-verified at the pin —
        # and a wrong BBMD port is a 09:00 Monday failure that looks exactly
        # like a firewall. Passing the port removes the question entirely.
        fd_bbmd_address=f"{bbmd_host}:{resolved_bbmd_port}",
        fd_ttl=fd_ttl(parameters),
    )


# -- bind pre-flight (the only way a port conflict is visible at all) --------


def classify_bind_error(error: OSError) -> str:
    """Map a bind ``OSError`` to :data:`BIND_PORT_IN_USE` / :data:`BIND_FAILED`. PURE.

    Reads BOTH ``errno`` and ``winerror`` because Windows may report a socket
    failure through either; ``getattr(..., None)`` keeps it portable (there is
    no ``winerror`` on POSIX).

    DEFENCE IN DEPTH ONLY. This does not catch the real 47808 conflict on its
    own — bacpypes3 never lets that OSError out (see the module docstring). It
    classifies the pre-flight's OWN bind failure, and is a pure function so the
    Windows codes can be tested on Linux CI, which is the only place they can be
    tested before the field.
    """
    codes = {getattr(error, "errno", None), getattr(error, "winerror", None)}
    if codes & _PORT_IN_USE_CODES:
        return BIND_PORT_IN_USE
    return BIND_FAILED


def bind_error_message(kind: str, *, ip: str, port: int, error_code: int | None = None) -> str:
    """Operator-facing message for a failed bind pre-flight. PURE.

    Credential-free by construction: it names only the operator-configured
    interface/port and a numeric errno — never raw exception text (base.py:319).
    """
    if kind == BIND_PORT_IN_USE:
        return (
            f"UDP port {port} on {ip} is already in use by another program — "
            "usually another BACnet tool (for example a BACnet browser) still "
            "running on this machine. Close it and run the scan again."
        )
    detail = f" (error {error_code})" if error_code is not None else ""
    return (
        f"Cannot bind UDP port {port} on {ip} for a live BACnet scan{detail}. "
        "Check that Source Interface on the Configuration page matches a network "
        "adapter that is up on this machine."
    )


class BacnetBindError(RuntimeError):
    """A failed bind pre-flight, carrying the machine-readable classification.

    A plain RuntimeError would still reach the operator (``_engine`` turns any
    RuntimeError into a self-diagnosed failed run carrying the message), but the
    run RECORD would only hold prose. ``kind`` is what lets
    ``bacnet_diagnostics["bind"]["reason"]`` say ``"udp_port_in_use"`` — the
    difference between an evening post-mortem grepping artifacts and one
    re-reading English.

    Subclasses RuntimeError deliberately: every existing ``except RuntimeError``
    on this path keeps working unchanged.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def preflight_bind(ip: str, port: int) -> None:
    """Prove ``(ip, port)`` is bindable, or raise an actionable BacnetBindError.

    MANDATORY before constructing any bacpypes3 Application. bacpypes3 catches
    its own bind ``OSError`` and retries every second FOREVER, so without this
    check a contended port produces a scan that finds nothing and says nothing —
    indistinguishable from a quiet network. This plain stdlib bind is the entire
    mechanism behind the "UDP 47808 in use" message.

    No ``SO_REUSEADDR``: we want to fail exactly where bacpypes3 would fail.

    KNOWN LIMITATION (kept in the runbook, not papered over here): this proves
    the port was free a moment ago, not that it stays free. Another process can
    take it between this close and bacpypes3's bind, and a holder using
    SO_REUSEADDR can let this probe succeed while still stealing datagrams. It
    catches the common exclusive-bind case — which is what a BACnet browser
    does — and is not a guarantee.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, port))
    except OSError as error:
        kind = classify_bind_error(error)
        raise BacnetBindError(
            bind_error_message(kind, ip=ip, port=port, error_code=getattr(error, "errno", None)),
            kind=kind,
        ) from error
    finally:
        sock.close()


# -- foreign-device registration status (PURE) ------------------------------


def classify_fd_registration_status(status: Any) -> str:
    """Map ``BIPForeign.bbmdRegistrationStatus`` to an outcome constant. PURE.

    VERIFIED verbatim against bacpypes3 ``ipv4/service.py``: the attribute is
    initialised ``self.bbmdRegistrationStatus = -2`` with the comment
    ``# -2=unregistered, -1=in process, 0=OK, >0 error``, and the Result-LPDU
    handler assigns ``lpdu.bvlciResultCode`` into it — so a positive value IS
    the BVLL result code the BBMD sent back.

    ``bool`` is rejected before ``int`` (``True`` would otherwise read as a
    positive result code and manufacture a "refused" out of a type error).
    """
    if isinstance(status, bool) or not isinstance(status, int):
        return FD_REGISTRATION_UNKNOWN
    if status == 0:
        return FD_REGISTRATION_REGISTERED
    if status > 0:
        return FD_REGISTRATION_REFUSED
    return FD_REGISTRATION_PENDING


def fd_registration_error_message(
    outcome: str,
    status: Any,
    *,
    bbmd_address: str | None,
    waited_s: float,
) -> str:
    """Operator-facing message for a foreign-device registration that failed. PURE.

    Names the BBMD the operator typed and, on a refusal, the exact BVLL result
    code — the difference between "BACnet didn't work" and a sentence the site's
    BBMD administrator can act on. Credential-free: config values and numbers.
    """
    target = bbmd_address or "the configured address"
    if outcome == FD_REGISTRATION_REFUSED:
        return (
            f"The BBMD at {target} refused foreign-device registration "
            f"(BVLL result code {status}). Ask the BBMD administrator to permit "
            "foreign-device registrations from this machine's IP address, and to "
            "check the BBMD's foreign-device table has a free entry."
        )
    if outcome == FD_REGISTRATION_TIMEOUT:
        return (
            f"No response from the BBMD at {target} — it did not acknowledge "
            f"foreign-device registration within {waited_s:g}s. Check the BBMD "
            "address and UDP port on the Configuration page, and that UDP traffic "
            "is routed and permitted between this machine and the BBMD."
        )
    return (
        "Could not read the foreign-device registration status for the BBMD at "
        f"{target} after {waited_s:g}s. The installed bacpypes3 does not expose "
        "the expected foreign-device internals; this build expects "
        "bacpypes3==0.0.106. The scan was stopped rather than reporting results "
        "from an unverified BBMD registration."
    )


# -- real backend (UNVALIDATED — requires on-site validation) ---------------


class Bacpypes3Backend:
    """Real BACnet/IP backend using ``bacpypes3`` (Who-Is + ReadProperty).

    !!! NEVER INTEGRATION-TESTED — REQUIRES ON-SITE VALIDATION !!!

    There is no BACnet device or building network in the development/CI
    environment, so this class has NOT been exercised against real hardware.
    Construction, foreign-device registration and directed Who-Is were verified
    VERBATIM AGAINST bacpypes3 SOURCE at the pinned ``bacpypes3==0.0.106`` (and
    cross-checked at v0.0.97/v0.0.99); those call sites are tagged
    ``# VERIFIED against bacpypes3 source``. Verified-against-source is NOT
    verified-against-hardware — the whole path still MUST be validated on-site.

    What is verified, and what is not, stated plainly because the difference is
    what someone will be debugging in a live lab:

        VERIFIED verbatim: ``Application.who_is(low_limit, high_limit, address,
        timeout)`` accepts an address (the old "signature does not accept an
        address" comment here was simply WRONG, at every version in range);
        ``from_object_list``'s foreign branch reads ``bacnetIPMode`` /
        ``fdBBMDAddress`` / ``fdSubscriptionLifetime`` off the NetworkPortObject
        and calls ``link_layer.register(...)`` itself; ``link_layers`` is keyed
        by the network-port object's own ``objectIdentifier``;
        ``BIPForeign.bbmdRegistrationStatus`` carries -2/-1/0/>0.

        NOT verified (flagged for ``test_bacpypes3_contract.py``, which asserts
        this surface against the real package): the ``DeviceObject`` /
        ``NetworkPortObject`` constructor kwargs used below, ``HostNPort``'s
        import module, and whether ``NetworkPortObject`` parses a ``":port"``
        suffix. The last one is why the broadcast lane still passes the
        operator's address string through untouched.

    Construction uses ``from_object_list`` (the maintainer's own ``from_args``
    path), NOT ``from_json``: the JSON form needs kebab-case keys and a
    ``sequence_to_json(HostNPort(...))`` value that this project has never run.

    The ``bacpypes3`` import is performed lazily in :meth:`_ensure_app` (NOT at
    module import), guarded so a missing dependency raises a clear
    :class:`RuntimeError` with an install hint instead of an ImportError at an
    unexpected place. Importing this module never requires ``bacpypes3``.

    One backend == one Application == one UDP port == one mode. Build a second
    instance for the foreign-device lane; do not try to make one app do both.
    """

    backend_name = BACKEND_BACPYPES3

    def __init__(
        self,
        *,
        local_address: str | None = None,
        timeout_s: float = 5.0,
        object_list_property: str = "object-list",
        parameters: Mapping[str, Any] | None = None,
        mode: str | None = None,
        udp_port: int | None = None,
    ) -> None:
        """Configure the real backend.

        Args:
            local_address: the local BACnet/IP interface (e.g.
                ``"192.168.1.10/24"`` or ``"192.168.1.10/24:47808"``). Required
                by bacpypes3 to bind a socket; passed through to the Application.
            timeout_s: per-request timeout in seconds for Who-Is / ReadProperty.
            object_list_property: property id read for the device object list
                (overridable for non-standard devices).
            parameters: the run parameters, read through the ``bacnet_params``
                contract for the BBMD address / port / TTL. Omitted (or without
                those keys) means a plain local-broadcast app — the behaviour
                that works today.
            mode: force ``MODE_BROADCAST`` / ``MODE_FOREIGN_DEVICE`` instead of
                reading the run's ``bacnet_mode``. The broadcast lane of a
                foreign-device run is built with an explicit ``MODE_BROADCAST``.
            udp_port: force the local bind port (the FD lane passes
                :data:`FD_LOCAL_UDP_PORT`).

        Nothing here touches bacpypes3 or a socket: the plan is resolved on
        first use, so constructing a backend can never fail and the existing
        "no Source Interface" guard still fires where it always did.
        """
        self._local_address = local_address
        self._timeout_s = timeout_s
        self._object_list_property = object_list_property
        self._parameters: Mapping[str, Any] = parameters or {}
        self._mode = mode
        self._udp_port = udp_port
        self._app: Any = None  # lazily-created bacpypes3 Application
        self._plan: BacnetTransportPlan | None = None
        self._network_port_object: Any = None
        self._registration_verified = False
        #: Last bind pre-flight, as a JSON-safe record: ``{"attempted", "ok",
        #: "ip", "port", "reason"?}``. Populated on BOTH outcomes, before the
        #: failure is raised, so the orchestration layer can stamp it into the
        #: run's diagnostics from an ``except`` — a run that died on a contended
        #: 47808 must still name the port and the interface from artifacts alone.
        self.bind: dict[str, Any] | None = None
        #: Last foreign-device registration attempt, as a JSON-safe record.
        #: Populated even when registration FAILS, so the orchestration layer can
        #: stamp it into the run's diagnostics from an ``except``/``finally`` —
        #: a failed run must still explain itself from artifacts alone.
        self.fd_registration: dict[str, Any] | None = None

    @property
    def transport_plan(self) -> BacnetTransportPlan:
        """The resolved plan for this app (cached). Raises RuntimeError if unusable.

        :func:`build_transport_plan` signals an unusable configuration with
        ``ValueError`` (it is pure and has no opinion about engines); the
        messages are already operator-vetted, so they are re-raised as
        ``RuntimeError`` — the type the engine converts into a self-diagnosed
        failed run carrying the message. A ``ValueError`` would instead escape to
        base.py's blanket except and be replaced by the generic sanitized string,
        losing the very sentence that makes the failure fixable.
        """
        if self._plan is None:
            try:
                self._plan = build_transport_plan(
                    self._parameters,
                    local_address=self._local_address,
                    mode=self._mode,
                    udp_port=self._udp_port,
                )
            except ValueError as error:
                raise RuntimeError(str(error)) from error
        return self._plan

    def _ensure_app(self) -> Any:
        """Lazily import bacpypes3, pre-flight the bind, and build the Application.

        REQUIRES ON-SITE VALIDATION. Raises a clear RuntimeError (not a bare
        ImportError) when bacpypes3 is not installed, so selecting this backend
        without the optional dependency fails with an actionable message.

        ORDER IS LOAD-BEARING: the install guard fires before anything else (so a
        missing extra always reports as a missing extra), then the address guard,
        then the bind pre-flight, and only then is an Application constructed. A
        conflicted port must never reach construction — bacpypes3 would swallow
        the bind failure and start an immortal retry task behind a scan that
        silently finds nothing.
        """
        if self._app is not None:
            return self._app
        try:
            # VERIFIED against bacpypes3 source: Application lives at
            # bacpypes3.app.Application and is the high-level entry point
            # (from_args / from_json / from_object_list).
            from bacpypes3.app import Application  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "The 'bacpypes3' backend was selected but bacpypes3 is not installed. "
                "Install the optional BACnet extra: pip install "
                "'smart-commissioning-core[bacnet]' (or: pip install bacpypes3)."
            ) from exc

        if not self._local_address:
            raise RuntimeError(
                "Bacpypes3Backend requires a local BACnet/IP address "
                "(local_address=...), e.g. '192.168.1.10/24'."
            )
        plan = self.transport_plan

        # MANDATORY, and the reason this whole method is ordered the way it is.
        # See preflight_bind: bacpypes3 retries a failed bind forever without
        # ever raising, so this is the only point at which a port conflict can
        # be seen. Raises an actionable BacnetBindError naming the port.
        #
        # The record is written on BOTH legs BEFORE the raise propagates, so the
        # engine's diagnostics can report which (ip, port) was refused and why
        # even though this method never returns.
        bind_record: dict[str, Any] = {
            "attempted": True,
            "ok": False,
            "ip": plan.bind_ip,
            "port": plan.udp_port,
        }
        self.bind = bind_record
        try:
            preflight_bind(plan.bind_ip, plan.udp_port)
        except BacnetBindError as error:
            bind_record["reason"] = error.kind
            raise
        bind_record["ok"] = True

        try:
            from bacpypes3.local.device import DeviceObject  # type: ignore[import-not-found]
            from bacpypes3.local.networkport import (  # type: ignore[import-not-found]
                NetworkPortObject,
            )
        except ImportError as exc:  # pragma: no cover - requires a mismatched bacpypes3
            # bacpypes3 IS installed but is not the shape this code was written
            # against. Reporting "not installed" here would be a lie that costs
            # somebody an afternoon; name the real problem and the pinned version.
            raise RuntimeError(
                "bacpypes3 is installed but does not expose the expected BACnet object "
                f"API ({exc}). This build was written against bacpypes3==0.0.106; "
                "reinstall the pinned version: pip install "
                "'smart-commissioning-core[bacnet]'."
            ) from exc

        # VERIFIED against bacpypes3 source (app.py from_args): the maintainer's
        # own path builds a DeviceObject + a NetworkPortObject and hands them to
        # Application.from_object_list. NOT VERBATIM-VERIFIED (see the class
        # docstring, and test_bacpypes3_contract.py): the exact constructor
        # kwargs below.
        device_object = DeviceObject(
            objectIdentifier=("device", plan.device_instance),
            objectName=plan.device_name,
            vendorIdentifier=plan.vendor_identifier,
        )
        # VERIFIED against bacpypes3 source (local/networkport.py):
        # NetworkPortObject.__init__(self, addr=None, *args, **kwargs) parses the
        # address string and sets macAddress/networkType/ipAddress/ipSubnetMask/
        # bacnetIPUDPPort from it.
        network_port_object = NetworkPortObject(
            plan.local_address,
            objectIdentifier=("network-port", plan.network_port_instance),
            objectName=plan.network_port_name,
        )

        if plan.is_foreign_device:
            try:
                from bacpypes3.basetypes import (  # type: ignore[import-not-found]
                    HostNPort,
                    IPMode,
                )
            except ImportError as exc:  # pragma: no cover - requires a mismatched bacpypes3
                raise RuntimeError(
                    "bacpypes3 is installed but does not expose the foreign-device "
                    f"types needed to register with a BBMD ({exc}). This build was "
                    "written against bacpypes3==0.0.106; reinstall the pinned version: "
                    "pip install 'smart-commissioning-core[bacnet]'."
                ) from exc
            # VERIFIED against bacpypes3 source (app.py from_args), verbatim:
            #   network_port_object.bacnetIPMode = IPMode.foreign
            #   network_port_object.fdBBMDAddress = HostNPort(args.foreign)
            #   network_port_object.fdSubscriptionLifetime = args.ttl
            # Registration is then DECLARATIVE: from_object_list's foreign branch
            # builds a ForeignLinkLayer_ipv4 and calls
            #   link_layer.register(obj.fdBBMDAddress.address,
            #                       obj.fdSubscriptionLifetime)
            # itself. We never call register() — we only WAIT for its outcome
            # (see _ensure_registered), which is the part bacpypes3 leaves silent.
            network_port_object.bacnetIPMode = IPMode.foreign
            network_port_object.fdBBMDAddress = HostNPort(plan.fd_bbmd_address)
            network_port_object.fdSubscriptionLifetime = plan.fd_ttl

        self._network_port_object = network_port_object
        self._app = Application.from_object_list([device_object, network_port_object])
        return self._app

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        """REQUIRES ON-SITE VALIDATION. Who-Is (broadcast or directed) -> I-Am list.

        ``address`` sends a DIRECTED (unicast) Who-Is to one device instead of
        broadcasting.

        Treat silence from a directed Who-Is as INCONCLUSIVE, never as
        device-absent: BACnet-135 lets a device answer with a local-broadcast
        I-Am on its own subnet, which this host cannot hear unless it is
        registered with that subnet's BBMD.
        """
        app = self._ensure_app()
        # Bounded wait on the BBMD's answer before the first Who-Is of a
        # foreign-mode app. No-op for a broadcast app. Never skip it: an
        # unregistered BIPForeign silently DROPS every broadcast, so scanning
        # first and asking later is how a refused registration turns into
        # "succeeded, 0 devices".
        await self._ensure_registered()
        # VERIFIED against bacpypes3 source (service/device.py), verbatim:
        #   def who_is(self, low_limit=None, high_limit=None,
        #              address: Optional[Address] = None,
        #              timeout: Optional[int] = WHO_IS_TIMEOUT) -> asyncio.Future
        # It RETURNS a list of IAmRequest APDUs (awaited, not delivered via an
        # indication callback), and `address` defaults to a global broadcast.
        #
        # The comment that used to sit here — "the documented who_is() signature
        # does not accept an address" — was FALSE at every version in the pinned
        # range, and it is why directed discovery was never wired up even though
        # the engine has always plumbed an address this far.
        if address is None or not str(address).strip():
            # Byte-identical to the broadcast call this backend has always made.
            i_ams = await app.who_is(low_limit=low_limit, high_limit=high_limit, timeout=self._timeout_s)
        else:
            i_ams = await app.who_is(
                low_limit=low_limit,
                high_limit=high_limit,
                address=self._pdu_address(address),
                timeout=self._timeout_s,
            )
        devices: list[dict[str, Any]] = []
        for i_am in i_ams or []:
            # VERIFIED against bacpypes3 (context7 /joelbender/bacpypes3): the
            # documented Who-Is example reads inst = i_am.iAmDeviceIdentifier[1]
            # (iAmDeviceIdentifier is the ObjectIdentifier ("device", instance))
            # and addr = i_am.pduSource (the responder's source address).
            try:
                instance = int(i_am.iAmDeviceIdentifier[1])
            except (AttributeError, IndexError, TypeError, ValueError):  # pragma: no cover - hardware shapes
                continue
            devices.append(
                {
                    "device_instance": instance,
                    "address": str(getattr(i_am, "pduSource", "")),
                    # BEST-EFFORT (still needs on-site validation): the I-Am APDU
                    # carries a vendor id (BACnet I-Am = deviceIdentifier,
                    # maxAPDULengthAccepted, segmentationSupported, vendorID), but
                    # context7's bacpypes3 docs only demonstrate
                    # iAmDeviceIdentifier/pduSource, not the exact vendor attribute
                    # name. getattr(..., None) degrades to None rather than raising
                    # if the casing differs on real hardware.
                    #
                    # Normalized at this boundary: vendorID is a bacpypes3
                    # ``Unsigned`` on real hardware, not a plain int, and it flows
                    # straight into the discovered_assets summary and the device
                    # repository row. Coercing it here keeps every downstream
                    # consumer holding a JSON-safe value (see _json_safe_value).
                    "vendor_id": _json_safe_value(getattr(i_am, "vendorID", None)),
                }
            )
        return devices

    def _pdu_address(self, address: Any) -> Any:
        """Convert a target address string into a bacpypes3 ``Address``.

        Raises ``ValueError`` (not RuntimeError) on an unusable address, so the
        directed lane can drop ONE bad register row and keep probing the rest,
        while a RuntimeError from the transport still stops the whole run. The
        address is operator-supplied register data, so echoing it is safe and is
        the only way the operator can find the offending row.
        """
        # VERIFIED against bacpypes3 source: Address lives at bacpypes3.pdu and
        # is what who_is(address=...) expects. Imported function-locally like
        # every other bacpypes3 name in this module.
        from bacpypes3.pdu import Address  # type: ignore[import-not-found]

        try:
            return Address(str(address).strip())
        except Exception as exc:  # noqa: BLE001 - any parse failure is one bad target
            raise ValueError(
                f"'{address}' is not a usable BACnet address for a directed Who-Is."
            ) from exc

    def _foreign_link_layer(self) -> Any:
        """Return the ``BIPForeign`` link layer carrying ``bbmdRegistrationStatus``.

        VERIFIED against bacpypes3 source (app.py from_object_list):
        ``self.link_layers[obj.objectIdentifier] = link_layer``. Looking it up
        with the network-port object's OWN ``objectIdentifier`` — the identical
        instance we passed in — sidesteps having to guess how an
        ``ObjectIdentifier`` built from a string compares/hashes.

        Falls back to any link layer exposing the status attribute, and returns
        ``None`` rather than raising if bacpypes3's internals have moved: the
        caller turns that into an honest "could not read registration status"
        failure, never an assumed success.
        """
        link_layers = getattr(self._app, "link_layers", None)
        if not link_layers:
            return None
        object_id = getattr(self._network_port_object, "objectIdentifier", None)
        if object_id is not None:
            try:
                layer = link_layers.get(object_id)
            except Exception:  # noqa: BLE001 - unexpected mapping shape; fall through
                layer = None
            if layer is not None:
                return layer
        try:
            candidates = list(link_layers.values())
        except Exception:  # noqa: BLE001 - not a mapping after all
            return None
        for candidate in candidates:
            if hasattr(candidate, "bbmdRegistrationStatus"):
                return candidate
        return None

    async def _ensure_registered(self) -> None:
        """Gate the first Who-Is of a foreign-mode app on the BBMD's answer.

        No-op for a broadcast app, and runs at most once per app. Called from
        :meth:`who_is` rather than left to the caller ON PURPOSE: a lane that
        forgot to wait would scan through an unregistered BIPForeign, find
        nothing, and report a clean empty scan — the exact failure this release
        exists to eliminate. Making it unskippable is worth more than making it
        explicit.
        """
        if self._registration_verified or not self.transport_plan.is_foreign_device:
            return
        await self._wait_for_fd_registration()
        self._registration_verified = True

    async def _wait_for_fd_registration(self) -> dict[str, Any]:
        """Poll ``bbmdRegistrationStatus`` until it resolves; raise if it is not OK.

        Returns the JSON-safe registration record on success. On refusal /
        timeout / unreadable status it records the SAME shape on
        :attr:`fd_registration` and then raises a RuntimeError naming the BBMD —
        the record survives the raise so a failed run can still explain itself.

        HARD FAIL, NEVER FALL BACK. Quietly continuing on local broadcast after a
        BBMD refuses us would report a clean scan of the wrong network.
        """
        plan = self.transport_plan
        link_layer = self._foreign_link_layer()
        loop = asyncio.get_running_loop()
        started = loop.time()
        status: Any = None
        outcome = FD_REGISTRATION_UNKNOWN
        while True:
            status = getattr(link_layer, "bbmdRegistrationStatus", None)
            outcome = classify_fd_registration_status(status)
            if outcome != FD_REGISTRATION_PENDING:
                break
            if loop.time() - started >= FD_REGISTRATION_WAIT_S:
                # Never acknowledged within the window: -1/-2 at the deadline is
                # a BBMD that is not answering, not one that said no.
                outcome = FD_REGISTRATION_TIMEOUT
                break
            await asyncio.sleep(FD_REGISTRATION_POLL_INTERVAL_S)

        waited_s = round(loop.time() - started, 3)
        record: dict[str, Any] = {
            "mode": MODE_FOREIGN_DEVICE,
            # Named fd_bbmd_address, NOT bbmd_address, on purpose: this is the
            # resolved "ip:port" we handed to HostNPort, whereas the run's
            # parameters[PARAM_BBMD_ADDRESS] is a bare IP. Reusing the parameter's
            # name for a different shape is how someone reading the two side by
            # side at 09:00 on a lab morning concludes the config was mangled.
            "fd_bbmd_address": plan.fd_bbmd_address,
            "fd_ttl": plan.fd_ttl,
            "local_udp_port": plan.udp_port,
            "outcome": outcome,
            # The BVLL result code when refused; -1/-2 otherwise. Recorded raw so
            # an evening post-mortem can read what the BBMD actually said.
            "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
            "waited_s": waited_s,
        }
        self.fd_registration = record
        if outcome == FD_REGISTRATION_REGISTERED:
            return record
        raise RuntimeError(
            fd_registration_error_message(
                outcome,
                record["status"],
                bbmd_address=plan.fd_bbmd_address,
                waited_s=waited_s,
            )
        )

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        """REQUIRES ON-SITE VALIDATION. ReadProperty device object-list."""
        app = self._ensure_app()
        address = str(device.get("address") or "")
        instance = device.get("device_instance")
        device_object = f"device,{instance}"
        # VERIFIED against bacpypes3 (context7 /joelbender/bacpypes3): the call
        # shape is await app.read_property(address, object_id, property_id) with
        # string shorthand for all three (e.g. "device,1001", "object-list").
        # Reading the whole "object-list" returns the full array (the docs read
        # an entire "priority-array" the same way).
        # KNOWN LIMITATION (on-site validation / live_untested): on large devices
        # the whole-array read can exceed the APDU size. The documented array
        # indexing supports a chunked fallback — read the length with
        #   read_property(address, "device,<n>", "object-list", instance=0)
        # (the shell form is object-list[0]) then each element by index
        # (object-list[i], instance=i). Wire that fallback up against real
        # hardware if the single read aborts.
        raw_objects = await app.read_property(address, device_object, self._object_list_property)
        objects: list[dict[str, Any]] = []
        for raw in raw_objects or []:
            # VERIFIED against bacpypes3 (context7 /joelbender/bacpypes3): each
            # object-list entry is an ObjectIdentifier whose str() yields the
            # "object-type,instance" shorthand (the same shorthand read_property
            # accepts as an object_id, e.g. "analog-input,3"). Skip the device
            # object itself.
            object_identifier = str(raw)
            if object_identifier.startswith("device,"):
                continue
            objects.append(
                {
                    "object_identifier": object_identifier,
                    "object_type": object_identifier.split(",", 1)[0],
                }
            )
        return objects

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        """REQUIRES ON-SITE VALIDATION. ReadProperty present-value of an object."""
        app = self._ensure_app()
        address = str(device.get("address") or "")
        object_identifier = str(obj.get("object_identifier") or "")
        # VERIFIED against bacpypes3 (context7 /joelbender/bacpypes3): present-
        # value is read with await app.read_property(address, "<type>,<inst>",
        # "present-value") — exactly the documented example. On failure
        # read_property raises bacpypes3.apdu.ErrorRejectAbortNack (e.g. an
        # object with no present-value such as structured-view); the engine's
        # per-point `except Exception` records the read error and keeps scanning,
        # so one bad object does not abort the device.
        return await app.read_property(address, object_identifier, "present-value")

    def close(self) -> None:
        """Tear down the Application and RELEASE THE UDP SOCKET. Not optional.

        Until v0.1.12 this method had ZERO call sites. The portable exe runs
        engines inline in one long-lived process, so the first live scan's bound
        UDP 47808 socket leaked for the life of the app and the SECOND scan of
        the session conflicted with itself — and, because bacpypes3 swallows bind
        failures in an immortal retry loop, that conflict was invisible: every
        scan after the first silently returned 0 devices. Nobody ever reported it
        as a bug, because it does not look like one.

        The bind pre-flight makes this worse if it is not called: a leaked socket
        would produce a scan that confidently blames "another BACnet tool" and
        names a port that this very process is holding. So close()-in-a-finally
        is a correctness prerequisite for the pre-flight's message being TRUE,
        not merely tidy resource handling.

        Idempotent and safe to call on a half-built app (the engine calls it in a
        finally, including on the failure paths where no Application was ever
        constructed).
        """
        app = self._app
        self._app = None
        self._network_port_object = None
        self._registration_verified = False
        # fd_registration and bind are deliberately NOT cleared: they are the
        # diagnostic record of what the BBMD said and which port was bound, and
        # the caller reads them AFTER the finally that calls this.
        if app is not None:
            close = getattr(app, "close", None)
            if callable(close):
                # VERIFIED against bacpypes3 (context7 /joelbender/bacpypes3):
                # Application.close() is synchronous — the documented examples
                # call app.close() (not awaited) in a finally block for clean
                # shutdown.
                close()


# -- backend selection ------------------------------------------------------


def resolve_bacnet_backend_name(
    parameters: Mapping[str, Any],
    *,
    dry_run: bool,
) -> str:
    """Return the allowed backend name, failing closed on unsafe selectors."""
    default = BACKEND_SIMULATED if dry_run else BACKEND_BACPYPES3
    raw_selector = parameters.get("bacnet_backend")
    selector = (
        default
        if raw_selector is None or (isinstance(raw_selector, str) and not raw_selector.strip())
        else str(raw_selector).strip().casefold()
    )
    if selector == BACKEND_SIMULATED and not dry_run:
        raise ValueError("The simulated BACnet backend is only available for dry runs.")
    if selector in {BACKEND_BACPYPES3, BACKEND_SIMULATED}:
        return selector
    raise ValueError(
        "Unsupported BACnet backend. Use 'bacpypes3' for live scans or "
        "'simulated' for dry runs."
    )


def _select_backend(
    parameters: Mapping[str, Any],
    backend: BacnetDiscoveryBackend | None,
    *,
    dry_run: bool,
) -> BacnetDiscoveryBackend:
    """Resolve the backend to use for a run.

    Precedence: an explicitly injected ``backend`` wins (used by tests/wiring).
    Otherwise ``parameters["bacnet_backend"]`` selects ``"simulated"`` or
    ``"bacpypes3"``. Dry runs default to simulated; real runs default to
    bacpypes3 so an omitted selector can never return fixture data.
    """
    # Resolve the mode even when it is not used below, so an UNRECOGNISED
    # bacnet_mode raises here (-> _engine's vetted failed run) instead of being
    # ignored. Lane 1 is built with an explicit MODE_BROADCAST override, which
    # would otherwise never read parameters[PARAM_BACNET_MODE] at all — and a
    # transport setting that is silently ignored is the exact bug this release
    # exists to fix. Do not "simplify" this call away.
    bacnet_mode(parameters)
    if backend is not None:
        return backend
    selector = resolve_bacnet_backend_name(parameters, dry_run=dry_run)
    if selector == BACKEND_SIMULATED:
        return SimulatedBacnetBackend()
    if selector == BACKEND_BACPYPES3:
        # Construct here so an unavailable bacpypes3 raises the clear RuntimeError
        # (from _ensure_app) only when the real backend is actually used.
        #
        # ZERO-REGRESSION PIN: lane 1 is ALWAYS a plain local-broadcast app on
        # the default port, even on a foreign-device run. It is the path that
        # works today and it must stay byte-identical to today.
        #
        # This is also why an FD run needs TWO apps rather than one: foreign mode
        # sets no_broadcast internally, so a single FD app would LOSE own-subnet
        # discovery — the devices most likely to be sitting next to the laptop.
        # Lane 3 is built separately by _select_fd_backend.
        return Bacpypes3Backend(
            local_address=parameters.get("local_address"),
            timeout_s=_timeout_s(parameters),
            parameters=parameters,
            mode=MODE_BROADCAST,
        )
    raise AssertionError(f"unhandled BACnet backend: {selector}")


def _select_fd_backend(
    parameters: Mapping[str, Any],
    backend: BacnetDiscoveryBackend | None,
    fd_backend: BacnetDiscoveryBackend | None,
    *,
    dry_run: bool,
) -> tuple[BacnetDiscoveryBackend | None, str | None]:
    """Resolve lane 3's foreign-device app. Returns ``(backend, skip_reason)``.

    Exactly one of the two is None. A skip_reason is never silent — the caller
    stamps it into the run's ``lanes`` breakdown, because "the lane you enabled
    did not run" must be readable from the artifact, not inferred from a device
    count.

    Lane 3 is gated STRICTLY on ``bacnet_mode == 'foreign_device'`` via
    :func:`is_foreign_device_mode` (the single copy of that comparison). Nothing
    else enables it — notably NOT the presence of a bbmd_address, which every
    default install carries from the seeded config and which would otherwise
    make every site register against a BBMD nobody asked for.
    """
    if not is_foreign_device_mode(parameters):
        return None, None
    if fd_backend is not None:
        return fd_backend, None
    if dry_run:
        # A dry run emits nothing, so it builds no app. The dry-run plan still
        # echoes the resolved foreign-device transport (see _dry_run_result) —
        # that echo IS Monday's config gate.
        return None, "dry_run"
    if backend is not None:
        # An injected backend is ONE transport supplied by a test or by wiring;
        # there is no second app to construct and constructing a real one behind
        # an injected fake would put live packets on the network from a test.
        # Recorded, never silent.
        return None, "backend_injected"
    return (
        Bacpypes3Backend(
            local_address=parameters.get("local_address"),
            timeout_s=_timeout_s(parameters),
            parameters=parameters,
            mode=MODE_FOREIGN_DEVICE,
            # Its OWN port: lane 1 must keep 47808 to hear I-Am replies, and one
            # bacnetIPMode per UDP port is the constraint that forces two apps.
            # 47809 also sidesteps a third-party browser squatting 47808.
            udp_port=FD_LOCAL_UDP_PORT,
        ),
        None,
    )


def _timeout_s(parameters: Mapping[str, Any]) -> float:
    """Per-request Who-Is timeout, in seconds.

    Falls back through ``scan_connect_timeout_s`` — the key the API/route actually
    accepts and clamps into ThrottleConfig.connect_timeout_s — because the BACnet
    backend reads this directly (only ip_scan reads ctx.throttle.connect_timeout_s),
    so before this fallback the operator's timeout knob never reached a BACnet scan.
    The legacy ``connect_timeout_s`` keeps precedence; the default is unchanged at
    5.0 so a bare run is byte-identical.

    Tolerant like the dispatch seam's ``_positive_float`` (engine_dispatch.build_throttle
    / worker._build_throttle sanitize this SAME key): a non-positive or unparseable
    value in either key falls back to the default instead of raising — a form-derived
    ``"5s"`` once failed the whole run — or reaching ``who_is`` as a negative timeout.
    """
    for key in ("connect_timeout_s", "scan_connect_timeout_s"):
        try:
            value = float(parameters.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 5.0


def _safe_progress_write(
    ctx: EngineContext,
    *,
    progress_percent: int,
    summary: dict[str, Any] | None = None,
) -> None:
    """Best-effort mid-run progress write. Never raises.

    A live scan that is working on the wire must never be failed by a progress
    write to a poisoned/contended store session (mirrors base._safe_update_run_status).
    The bar moves via ``update_run_status(progress_percent=...)``; the X-of-Y detail
    rides ``update_result_summary(merge=True)`` under a ``progress`` key, so it never
    clobbers other summary keys and the terminal summary write overwrites cleanly.
    """
    try:
        ctx.run_store.update_run_status(
            ctx.run_id,
            status="running",
            stage=_STAGE_RUNNING_ENGINE,
            progress_percent=progress_percent,
        )
        if summary is not None:
            ctx.run_store.update_result_summary(ctx.run_id, summary)
    except Exception:  # noqa: BLE001 - a progress write must never fail a live scan
        logger.exception("progress write failed for run %s; scan continues", ctx.run_id)


def _close_backend(backend: BacnetDiscoveryBackend) -> None:
    """Release a backend's transport. Never raises.

    A failure while closing must not turn a completed scan into a sanitized
    "engine execution failed" — the devices were really found. Logged rather
    than silently dropped, so a leak is still traceable.
    """
    close = getattr(backend, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - a close failure must not fail a good run
        logger.exception("failed to close the %s BACnet backend", _backend_name(backend))


def _backend_name(backend: BacnetDiscoveryBackend) -> str:
    return getattr(backend, "backend_name", backend.__class__.__name__)


def _instance_range(parameters: Mapping[str, Any]) -> tuple[int, int]:
    """Return the (low, high) device-instance Who-Is window from parameters."""
    low = parameters.get("device_instance_low")
    high = parameters.get("device_instance_high")
    low_int = int(low) if isinstance(low, (int, float, str)) and str(low).strip() != "" else BACNET_INSTANCE_MIN
    high_int = int(high) if isinstance(high, (int, float, str)) and str(high).strip() != "" else BACNET_INSTANCE_MAX
    if low_int > high_int:
        low_int, high_int = high_int, low_int
    return max(low_int, BACNET_INSTANCE_MIN), min(high_int, BACNET_INSTANCE_MAX)


# -- record building --------------------------------------------------------


def _bacnet_issue(
    issues: Sequence[Any],
    *,
    asset_id: str | None,
    issue_type: str,
    severity: Literal["low", "medium", "high", "critical"],
    description: str,
    match_basis: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    suggested_action: str | None = None,
) -> ValidationIssueRecord:
    """Build a discovery issue with a run-unique id.

    Mirrors comparison_common._issue: every optional field is spelled out rather
    than taken as ``**fields``, so a typoed name is a TypeError instead of a key
    pydantic silently drops.

    ``severity`` is "medium" for everything this engine raises, deliberately.
    These are AMBER findings — a device that did not answer one lane, or a
    register row that disagrees with a controller. None of them is proof of a
    fault, and escalating them would train an operator to ignore the colour.
    """
    return ValidationIssueRecord(
        issue_id=f"bacnet-{len(issues) + 1:04d}",
        asset_id=asset_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
        match_basis=match_basis,
        expected_value=expected_value,
        observed_value=observed_value,
        suggested_action=suggested_action,
    )


def _device_asset(
    device: Mapping[str, Any],
    backend_name: str,
    *,
    match_basis: str = MATCH_BASIS_WHO_IS,
    lane: str = LANE_BROADCAST,
) -> dict[str, Any]:
    """Map a backend device dict to a discovered_assets entry.

    The asset_id is a stable per-device key (the BACnet device instance), which
    the DiscoveredPoint rows reference via ``device_ref``.

    ``match_basis`` / ``lane`` record HOW the device was heard. They default to
    the broadcast values so a plain local scan produces exactly the entry it
    always has.
    """
    instance = device.get("device_instance")
    return {
        "asset_id": f"bacnet-device-{instance}",
        "device_instance": instance,
        "address": device.get("address"),
        "name": device.get("name"),
        "vendor": device.get("vendor"),
        "vendor_id": device.get("vendor_id"),
        "model": device.get("model"),
        "match_basis": match_basis,
        "lane": lane,
        "backend": backend_name,
    }


def _device_record(
    device: Mapping[str, Any],
    asset_id: str,
    *,
    target: BacnetTarget | None = None,
) -> dict[str, Any]:
    """Map a backend device dict to a DiscoveredDevice repository row.

    When ``target`` is the register row this device matched, the register's
    identity is merged into ``attributes`` — the BACnet analogue of the IP
    route's ``_resolve_asset_ids``. It is kept under ``register_*`` names and
    NEVER overwrites an observed field: what the register CLAIMS and what the
    device ANNOUNCED are different facts, and a report that silently replaces
    the second with the first is how a mislabelled panel survives commissioning.
    """
    attributes: dict[str, Any] = {
        "asset_id": asset_id,
        "device_instance": device.get("device_instance"),
        "vendor_id": device.get("vendor_id"),
    }
    if target is not None:
        attributes["register_asset_id"] = target.asset_id
        attributes["register_asset_name"] = target.asset_name
        attributes["register_address"] = target.address
        attributes["expected_network"] = target.network
    return {
        "address": device.get("address"),
        "device_type": "bacnet_device",
        "name": device.get("name"),
        "vendor": device.get("vendor"),
        "model": device.get("model"),
        "attributes": attributes,
    }


def _point_record(
    device: Mapping[str, Any],
    obj: Mapping[str, Any],
    present_value: Any,
    *,
    device_ref: str,
    read_error: str | None = None,
) -> dict[str, Any]:
    """Map a backend object + present-value to a DiscoveredPoint repository row.

    ``observed_value`` is a JSON object (the repository column is JSON), so the
    raw present-value is wrapped under a ``"value"`` key; ``read_error`` records
    a per-point read failure without aborting the device.

    The present-value is normalized (see :func:`_json_safe_value`) because the
    live backend hands back a raw bacpypes3 object here — an enumerated
    present-value serializes to nothing JSON knows, so wrapping it raw is what
    poisoned the repository write on every real run.
    """
    attributes: dict[str, Any] = {
        "object_type": obj.get("object_type"),
        "device_instance": device.get("device_instance"),
    }
    if read_error is not None:
        attributes["read_error"] = read_error
    return {
        "device_ref": device_ref,
        "point_id": obj.get("object_identifier"),
        "point_name": obj.get("object_name") or obj.get("object_identifier"),
        "observed_value": {} if read_error is not None else {"value": _json_safe_value(present_value)},
        "units": obj.get("units"),
        "attributes": attributes,
    }


# -- targets, hints and diagnostics (PURE) ----------------------------------


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_unicast_targets(parameters: Mapping[str, Any]) -> list[BacnetTarget]:
    """Read the register targets for the directed lane, enforcing the ceiling. PURE.

    Mirrors ip_scan's max_hosts rule exactly: a request's ``max_targets`` may
    LOWER the ceiling but can never raise it above
    :data:`MAX_BACNET_UNICAST_TARGETS_CEILING`.

    Raises:
        RuntimeError: over the ceiling, with an actionable message. RuntimeError
            rather than ValueError ON PURPOSE — that is the type ``_engine``
            converts into a self-diagnosed failed run carrying this exact
            sentence. A ValueError would escape to base.py's blanket except and
            be replaced by the generic sanitized string, which tells the operator
            nothing about their register.
    """
    targets = read_targets(parameters)
    requested = _positive_int(
        parameters.get("max_targets"), default=MAX_BACNET_UNICAST_TARGETS_CEILING
    )
    max_targets = min(requested, MAX_BACNET_UNICAST_TARGETS_CEILING)
    if len(targets) > max_targets:
        raise RuntimeError(
            f"The BACnet register expands to {len(targets)} directed Who-Is targets, "
            f"exceeding max_targets={max_targets}. Narrow the register import, or "
            f"lower max_targets, before running the scan."
        )
    return targets


def build_empty_scan_hint(
    *,
    mode: str,
    interface: Any,
    instance_low: int,
    instance_high: int,
    timeout_s: float,
    fd_bbmd_address: str | None = None,
    unanswered_directed: int = 0,
) -> str:
    """One authored sentence explaining a live scan that heard nothing. PURE.

    Finding nothing is a VALID result, so this never changes the run's status —
    it explains it. The wording is chosen from what actually happened rather
    than listing every possibility, because an operator on a lab floor acts on
    the first instruction they read.

    Credential-free: operator-configured settings and counts only (base.py:319).
    """
    window = f"instances {instance_low}–{instance_high}"
    if mode == MODE_FOREIGN_DEVICE:
        base = (
            f"Registered with the BBMD at {fd_bbmd_address or 'the configured address'}, "
            f"but no devices answered the Who-Is ({window}) within {timeout_s:g}s"
        )
        advice = (
            "Check the device-instance range, and ask the BBMD administrator whether its "
            "broadcast distribution table covers the subnets these devices are on."
        )
    else:
        base = (
            f"No devices answered the local broadcast Who-Is ({window}) on "
            f"{interface or 'the configured interface'} within {timeout_s:g}s"
        )
        advice = (
            "Devices on another subnet behind a BBMD cannot hear a local broadcast — set "
            "Foreign Device to Enabled and enter the BBMD Address on the Configuration "
            "page, then scan again."
        )
    if unanswered_directed:
        plural = "es" if unanswered_directed != 1 else ""
        base += (
            f", and {unanswered_directed} directed Who-Is to register "
            f"address{plural} from the import also went unanswered"
        )
    return f"{base}. {advice}"


def _bacpypes3_version() -> str | None:
    """Installed bacpypes3 version, or None. Never raises.

    Recorded on every live run: "CI was green but the lab was red" is most often
    a version that is not the pinned one, and that question must be answerable
    from the artifact without asking the operator to run pip.
    """
    try:
        return importlib.metadata.version("bacpypes3")
    except Exception:  # noqa: BLE001 - diagnostics must never break a scan
        return None


def _new_summary_record(
    ctx: EngineContext,
    backend: BacnetDiscoveryBackend,
    *,
    mode: str,
) -> dict[str, Any]:
    """Seed the result_summary extras for a live run.

    Created BEFORE the scan and mutated in place so that EVERY outcome — success,
    clean empty, and each hard failure — carries the same diagnostics block. That
    is the whole design bar for v0.1.12: a failed Monday scan must be
    reconstructible from GET /discovery/runs/{id} alone, with no live debugging
    session, because there will not be one.
    """
    low, high = _instance_range(ctx.parameters)
    backend_name = _backend_name(backend)
    return {
        "backend": backend_name,
        # Top-level transport stamp, deliberately duplicating
        # bacnet_diagnostics.mode below. The results page reads these two keys
        # at the TOP level to name the transport on the run badge, and it cannot
        # import bacnet_params (TypeScript), so the contract crosses that seam by
        # spelling alone. Nesting them only under bacnet_diagnostics made the
        # badge silently render "Live bacpypes3 scan." with no transport named —
        # a configured foreign-device registration honoured by the engine and
        # then invisible on the one surface built to prove it. Which is the bug
        # this release exists to fix, wearing a different hat. Keep these keys
        # and frontend/src/features/workflow/discoveryRows.ts in step.
        PARAM_BACNET_MODE: mode,
        # FD runs only: broadcast runs have no BBMD, and stamping an empty
        # string would read as "recorded, and it was blank".
        **(
            {PARAM_BBMD_ADDRESS: bbmd_address(ctx.parameters)}
            if mode == MODE_FOREIGN_DEVICE
            else {}
        ),
        "device_instance_low": low,
        "device_instance_high": high,
        "device_count": 0,
        "point_count": 0,
        "bacnet_diagnostics": {
            # The operator-configured Source Interface, verbatim as they typed
            # it. bind.ip carries the resolved bare IP; keeping both means a
            # mis-parsed "/24" suffix is visible rather than inferred.
            "interface": ctx.parameters.get("local_address"),
            "udp_port": None,
            "bind": {"attempted": False, "ok": False},
            "mode": mode,
            "fd_registration": None,
            "who_is": {
                "instance_low": low,
                "instance_high": high,
                "timeout_s": _timeout_s(ctx.parameters),
                "broadcast_sent": 0,
                "unicast_targets": 0,
                "unicast_sent": 0,
                "i_am_count": 0,
            },
            "bacpypes3_version": (
                _bacpypes3_version() if backend_name == BACKEND_BACPYPES3 else None
            ),
            # Set only once the lanes complete without a transport failure. An
            # empty scan is reported as a CLEAN empty only when this is True.
            "transport_verified": False,
        },
        "lanes": {},
        "expected_device_count": 0,
        "expected_responding_count": 0,
        "unicast_fallback_attempted": False,
        "expected_not_responding": [],
    }


def _stamp_transport(
    record: dict[str, Any],
    backend: BacnetDiscoveryBackend,
    fd_backend: BacnetDiscoveryBackend | None,
) -> None:
    """Copy the backends' bind / FD-registration records into the diagnostics.

    Called from a ``finally``, so it runs on the success path AND on the hard
    failures — which are the runs that need it most. Never raises: a diagnostics
    problem must not become the operator's error message.
    """
    diagnostics = record.get("bacnet_diagnostics")
    if not isinstance(diagnostics, dict):
        return
    bind = getattr(backend, "bind", None)
    if isinstance(bind, dict):
        diagnostics["bind"] = dict(bind)
        diagnostics["udp_port"] = bind.get("port")
    if fd_backend is not None:
        fd_registration = getattr(fd_backend, "fd_registration", None)
        if isinstance(fd_registration, dict):
            diagnostics["fd_registration"] = dict(fd_registration)
        fd_bind = getattr(fd_backend, "bind", None)
        if isinstance(fd_bind, dict):
            # The FD lane binds its own port (47809); record it separately so a
            # conflict there is not mistaken for one on 47808.
            diagnostics["fd_bind"] = dict(fd_bind)


def _fd_bbmd_address(fd_backend: BacnetDiscoveryBackend | None) -> str | None:
    """The resolved ``ip:port`` the FD lane registered against, if any. Never raises."""
    if fd_backend is None:
        return None
    try:
        plan = getattr(fd_backend, "transport_plan", None)
    except RuntimeError:  # pragma: no cover - unresolvable plan already failed the run
        return None
    return getattr(plan, "fd_bbmd_address", None)


# -- the engine -------------------------------------------------------------


async def _run_bacnet_discovery(
    ctx: EngineContext,
    backend: BacnetDiscoveryBackend,
    *,
    fd_backend: BacnetDiscoveryBackend | None = None,
    fd_skip_reason: str | None = None,
    record: dict[str, Any] | None = None,
) -> EngineResult:
    """Async engine body: three Who-Is lanes, then throttled per-device reads.

    THE LANES, and why they are independent (COORDINATION decision 3). Each one
    reaches devices the others cannot, so no single field unknown blanks the day:

        1. LOCAL BROADCAST on 47808. Unchanged from today when nothing new is
           configured — the zero-regression pin. Reaches the laptop's own subnet.
        2. DIRECTED unicast to register addresses, FALLBACK-ONLY: sent only to
           targets still silent after lanes 1 and 3. Crosses subnets by ordinary
           IP routing with NO BBMD, so it is the path that still works when the
           lab's BBMD refuses to cooperate.
        3. FOREIGN-DEVICE broadcast through the BBMD on 47809. Reaches other
           subnets, including routed MS/TP devices the directed lane cannot see.

    Lane 2 runs LAST on purpose: lane 3 is also a broadcast, so any device it
    hears needs no unicast probe. Ordering it after both broadcast lanes is what
    keeps the fallback to the genuinely-silent minority — ~60 single UDP frames
    worst case on Monday's lab, spaced by the throttle.

    A lane never silently substitutes for another. Lane 3 hard-fails the run on
    a BBMD NAK/timeout (the RuntimeError from ``_ensure_registered`` propagates
    to ``_engine``) rather than quietly continuing on local broadcast — quietly
    continuing would report a clean scan of the wrong network, which is the
    original bug wearing a new hat.
    """
    parameters = ctx.parameters
    low, high = _instance_range(parameters)
    address = parameters.get("address")
    backend_name = _backend_name(backend)
    mode = bacnet_mode(parameters)
    if record is None:  # pragma: no cover - callers pass one; keeps the helper usable alone
        record = _new_summary_record(ctx, backend, mode=mode)
    diagnostics: dict[str, Any] = record["bacnet_diagnostics"]
    who_is_counters: dict[str, Any] = diagnostics["who_is"]
    lanes: dict[str, Any] = record["lanes"]

    # Enforced BEFORE any packet: an oversized register is a configuration
    # mistake, and telling the operator after spraying 5,000 frames at an OT
    # network would be the wrong order.
    targets = resolve_unicast_targets(parameters)

    throttle = Throttle(ctx.throttle)

    async def _who_is(
        source: BacnetDiscoveryBackend,
        target_address: str | None,
    ) -> list[dict[str, Any]]:
        async with throttle.slot():
            return await source.who_is(low, high, target_address)

    # merged: device_instance -> the entry that first heard it. Insertion order
    # is preserved, and BROADCAST WINS: a device heard on two lanes is ONE
    # device, recorded with the provenance of the lane that heard it first.
    merged: dict[int, dict[str, Any]] = {}
    issues: list[Any] = []

    def _absorb(
        devices: Sequence[Mapping[str, Any]],
        *,
        match_basis: str,
        lane: str,
        source: BacnetDiscoveryBackend,
    ) -> int:
        """Merge one lane's I-Ams into `merged`. Returns how many were new.

        ``source`` is the backend that heard them, kept so the per-device reads
        go back out the same transport.
        """
        new_count = 0
        for device in devices:
            instance = device.get("device_instance")
            if not isinstance(instance, int) or isinstance(instance, bool):
                # A device we cannot key cannot be merged, deduped, or matched to
                # a register row. Dropping it is honest; inventing an instance
                # would put a fabricated identity in a commissioning report.
                logger.warning("BACnet %s lane returned a device with no usable instance", lane)
                continue
            existing = merged.get(instance)
            if existing is None:
                merged[instance] = {
                    "device": dict(device),
                    "match_basis": match_basis,
                    "lane": lane,
                    "source": source,
                }
                new_count += 1
                continue
            # Same instance from a different address: instances are spec-unique
            # network-wide, so this is a real misconfiguration. First-seen wins
            # the record; the collision is NAMED rather than silently dropped.
            previous_address = str(existing["device"].get("address") or "")
            current_address = str(device.get("address") or "")
            if current_address and previous_address and current_address != previous_address:
                issues.append(
                    _bacnet_issue(
                        issues,
                        asset_id=f"bacnet-device-{instance}",
                        issue_type=ISSUE_INSTANCE_COLLISION,
                        severity="medium",
                        description=(
                            f"BACnet device instance {instance} answered from two different "
                            f"addresses ({previous_address} and {current_address}). Device "
                            "instances must be unique on a BACnet internetwork; the first "
                            "responder was recorded."
                        ),
                        match_basis=match_basis,
                        suggested_action=(
                            "Check the two controllers' Device Object instance numbers and "
                            "give one of them a unique instance."
                        ),
                    )
                )
        return new_count

    # -- Lane 1: local broadcast (or the legacy single directed address) ------
    #
    # parameters["address"] is the pre-existing single-target override and is
    # passed through EXACTLY as before. Nothing about this call changes when no
    # new configuration is present.
    broadcast_devices = await _who_is(backend, address)
    if address:
        who_is_counters["unicast_sent"] += 1
    else:
        who_is_counters["broadcast_sent"] += 1
    who_is_counters["i_am_count"] += len(broadcast_devices)
    lanes[LANE_BROADCAST] = {
        "ran": True,
        "device_count": _absorb(
            broadcast_devices,
            match_basis=MATCH_BASIS_WHO_IS,
            lane=LANE_BROADCAST,
            source=backend,
        ),
        "i_am_count": len(broadcast_devices),
        "directed_address": address or None,
    }

    # -- Lane 3: foreign-device broadcast through the BBMD -------------------
    #
    # Runs before lane 2 because it is a BROADCAST: whatever it hears needs no
    # unicast probe. A refused/silent BBMD raises out of the backend's
    # registration gate and fails the whole run, naming the BBMD.
    if fd_backend is not None:
        fd_devices = await _who_is(fd_backend, None)
        who_is_counters["broadcast_sent"] += 1
        who_is_counters["i_am_count"] += len(fd_devices)
        lanes[LANE_FOREIGN_DEVICE] = {
            "ran": True,
            "device_count": _absorb(
                fd_devices,
                match_basis=MATCH_BASIS_WHO_IS,
                lane=LANE_FOREIGN_DEVICE,
                source=fd_backend,
            ),
            "i_am_count": len(fd_devices),
            "udp_port": FD_LOCAL_UDP_PORT,
            "bbmd_address": _fd_bbmd_address(fd_backend),
        }
    else:
        # "Did not run" is always RECORDED with a reason. An operator who enabled
        # foreign-device registration and got a lane that quietly never ran would
        # be back where v0.1.12 started.
        lanes[LANE_FOREIGN_DEVICE] = {
            "ran": False,
            "reason": fd_skip_reason or ("not_configured" if mode != MODE_FOREIGN_DEVICE else "unavailable"),
        }

    # -- Lane 2: directed unicast, fallback-only -----------------------------
    #
    # Only targets whose expected instance sits INSIDE the operator's Who-Is
    # window are in scope: a device whose instance is outside [low, high] cannot
    # answer this scan by definition, so probing it would burn a timeout and then
    # report a false "expected but did not answer" row. On Monday's narrow-window
    # gate (one known device, instance [N,N]) that mistake alone would have
    # produced ~59 bogus amber rows against a healthy lab.
    in_scope = [t for t in targets if low <= t.device_instance <= high]
    out_of_scope = len(targets) - len(in_scope)
    record["expected_device_count"] = len(in_scope)
    who_is_counters["unicast_targets"] = len(in_scope)

    silent_targets = [t for t in in_scope if t.device_instance not in merged]
    # One probe per unique ADDRESS, not per row. bacpypes3's WhoIsFuture sets
    # only_one=True whenever an address is given, so it completes on the FIRST
    # matching I-Am — a second probe to the same address provably cannot surface
    # a second device. Deduping costs nothing in reach and keeps the frame count
    # down on an OT network.
    probe_addresses = list(dict.fromkeys(t.address for t in silent_targets))

    directed_new = 0
    directed_i_ams = 0
    answered_addresses: set[str] = set()
    # Addresses a directed probe was ACTUALLY sent to (dispatched and not skipped
    # as unusable). Distinct from probe_addresses (the PLANNED list): a skipped or
    # never-dispatched target must not later be reported as "a directed Who-Is was
    # sent to it and went unanswered".
    sent_addresses: set[str] = set()
    if probe_addresses:
        record["unicast_fallback_attempted"] = True

        def _make_probe(probe_address: str) -> Callable[[], Awaitable[dict[str, Any]]]:
            async def _unit() -> dict[str, Any]:
                # Call the backend DIRECTLY, not through _who_is: this unit is
                # already dispatched under throttle.run_throttled, which holds one
                # semaphore permit AND applies the rate limiter for it. _who_is would
                # acquire a SECOND permit from the same throttle, and because
                # asyncio.Semaphore.acquire's fast path is synchronous, the dispatch
                # loop drains every permit before any probe body runs — so with
                # >= max_concurrency silent addresses every unit blocks on the nested
                # acquire with the semaphore at 0 and nothing releasable: a
                # deterministic deadlock (the 2026-07-21 field hang). Calling the
                # backend directly de-nests it and also drops the double rate token.
                try:
                    found = await backend.who_is(low, high, probe_address)
                except ValueError:
                    # ONE unusable register address (see _pdu_address) must not
                    # kill the lane for the other 59 targets. A RuntimeError from
                    # the transport still stops the run — that is the distinction
                    # the two exception types carry here.
                    logger.warning(
                        "skipping directed BACnet Who-Is: %r is not a usable address",
                        probe_address,
                    )
                    return {"address": probe_address, "devices": [], "skipped": True}
                return {"address": probe_address, "devices": found, "skipped": False}

            return _unit

        probe_results = await throttle.run_throttled(
            [_make_probe(item) for item in probe_addresses], ctx
        )
        for probe in probe_results:
            probe_address = probe["address"]
            found = probe["devices"]
            if not probe["skipped"]:
                who_is_counters["unicast_sent"] += 1
                sent_addresses.add(probe_address)
            if found:
                answered_addresses.add(probe_address)
            directed_i_ams += len(found)
            directed_new += _absorb(
                found,
                match_basis=MATCH_BASIS_WHO_IS_DIRECTED,
                lane=LANE_DIRECTED,
                source=backend,
            )
            # A device answered where the register expected a DIFFERENT instance.
            # We heard a real device: record it under the instance it ANNOUNCED
            # (never the one we hoped for) and name the discrepancy.
            expected_here = {
                t.device_instance for t in silent_targets if t.address == probe_address
            }
            for device in found:
                instance = device.get("device_instance")
                if not isinstance(instance, int) or isinstance(instance, bool):
                    continue
                if expected_here and instance not in expected_here:
                    expected_text = ", ".join(str(item) for item in sorted(expected_here))
                    issues.append(
                        _bacnet_issue(
                            issues,
                            asset_id=f"bacnet-device-{instance}",
                            issue_type=ISSUE_REGISTER_INSTANCE_MISMATCH,
                            severity="medium",
                            description=(
                                f"The register expects device instance {expected_text} at "
                                f"{probe_address}, but instance {instance} answered there."
                            ),
                            match_basis=MATCH_BASIS_WHO_IS_DIRECTED,
                            expected_value=expected_text,
                            observed_value=str(instance),
                            suggested_action=(
                                "Confirm the BACnet device instance for this address in the "
                                "register import, or check the controller's Device Object."
                            ),
                        )
                    )
        who_is_counters["i_am_count"] += directed_i_ams

    lanes[LANE_DIRECTED] = {
        "ran": bool(probe_addresses),
        "target_count": len(in_scope),
        "out_of_instance_range_count": out_of_scope,
        "probe_count": len(probe_addresses),
        "device_count": directed_new,
        "i_am_count": directed_i_ams,
    }

    # The Who-Is lanes are done; the long per-device read phase is next. Move the
    # bar off the initial 15% so the monitor shows the scan is past discovery — the
    # field run sat at 15% for 16+ minutes with no way to tell hung from working.
    _safe_progress_write(ctx, progress_percent=25)

    targets_by_instance = {t.device_instance: t for t in in_scope}

    discovered_assets: list[dict[str, Any]] = []
    device_records: list[dict[str, Any]] = []
    point_records: list[dict[str, Any]] = []

    total_devices = len(merged)
    # Per-device progress, written from inside each completing unit (run_throttled
    # gathers the batch, so there is no other seam). Best-effort + throttled to at
    # most one store write per ~2s, but always one on the final device so a
    # finished phase 2 lands an honest count. Single event loop, so the counter
    # increments need no lock.
    _progress = {"done": 0, "points": 0, "last": asyncio.get_running_loop().time()}

    def _note_device_progress(points_added: int) -> None:
        _progress["done"] += 1
        _progress["points"] += points_added
        done = _progress["done"]
        now = asyncio.get_running_loop().time()
        if done < total_devices and now - _progress["last"] < 2.0:
            return
        _progress["last"] = now
        # Cap at 95: the monitor treats 100 as done-adjacent, and only the
        # terminal write may reach it.
        percent = min(95, 25 + int(70 * done / total_devices)) if total_devices else 25
        _safe_progress_write(
            ctx,
            progress_percent=percent,
            summary={
                "progress": {
                    "devices_total": total_devices,
                    "devices_done": done,
                    "points_read": _progress["points"],
                }
            },
        )

    # Phase 2: per device, read the object list then each present-value. Each
    # device is a throttled unit.
    #
    # Reads go back through the LANE THAT HEARD THE DEVICE. A device that only
    # the foreign-device lane could hear (a routed MS/TP device, say) is not
    # necessarily reachable from the local-broadcast app, and reading it through
    # the wrong app would turn a device we genuinely found into a wall of
    # read errors.
    def _make_device_unit(
        device: Mapping[str, Any],
        source: BacnetDiscoveryBackend,
    ) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def _unit() -> dict[str, Any]:
            asset_id = f"bacnet-device-{device.get('device_instance')}"
            points: list[dict[str, Any]] = []
            result: dict[str, Any] = {"device": device, "asset_id": asset_id, "points": points}
            try:
                # Stop must bite mid-device. With <= max_concurrency devices every
                # unit dispatches immediately, so run_throttled's between-dispatch
                # cancel check never fires again; without a check here a device with
                # N dead points holds Stop hostage for up to N x ~12s. Return the
                # points read so far HONESTLY — points not attempted are absent, not
                # recorded as failures.
                if ctx.is_cancelled():
                    result["heard_only"] = True
                    return result
                # One device's failed object-list read must not vaporize the whole
                # run (and discard every already-scanned device with it). RuntimeError
                # stays the vetted transport-dead failure and still fails the run; any
                # other failure means this device was heard but could not be
                # enumerated — reported discovered-but-unenumerated, never dropped.
                # BaseException, not Exception: bacpypes3's ErrorRejectAbortNack
                # (the APDU-size abort this predicts on large devices) subclasses
                # BaseException, so `except Exception` would MISS it and let one
                # device fail the whole run — the critical field bug this guards.
                try:
                    objects = await source.read_object_list(device)
                except _OBJECT_LIST_PROPAGATE:
                    raise
                except BaseException as exc:  # noqa: BLE001 - device heard, object-list unreadable
                    if _is_worker_interrupt(exc):
                        raise
                    result["object_list_error"] = True
                    return result
                consecutive_failures = 0
                for index, obj in enumerate(objects):
                    if ctx.is_cancelled():
                        # Stop bit mid-enumeration. Mark WHY this device has fewer
                        # points than it has objects, so a Stop-truncated row is
                        # never mistaken for a device that genuinely has this few
                        # readable points — every other bail path (heard_only,
                        # object_list_error, reads_aborted) marks itself; this one
                        # must too. Points not attempted are ABSENT, never faked as
                        # failures.
                        result["reads_truncated"] = {
                            "points_not_attempted": len(objects) - len(points)
                        }
                        break
                    try:
                        value = await source.read_present_value(device, obj)
                        points.append(_point_record(device, obj, value, device_ref=asset_id))
                        consecutive_failures = 0
                    except _READ_PROPAGATE:
                        raise
                    except BaseException as exc:  # noqa: BLE001 - APDU errors subclass BaseException; record + keep scanning
                        if _is_worker_interrupt(exc):
                            raise
                        points.append(
                            _point_record(
                                device,
                                obj,
                                None,
                                device_ref=asset_id,
                                read_error="present_value_read_failed",
                            )
                        )
                        consecutive_failures += 1
                        if consecutive_failures >= _MAX_CONSECUTIVE_POINT_READ_FAILURES:
                            # Stop asking a device that answers Who-Is but refuses
                            # reads. The remaining points are ABSENT, never labelled
                            # as failures, and the device is marked so a truncated
                            # scan never looks fully read.
                            result["reads_aborted"] = {
                                "after_consecutive_failures": consecutive_failures,
                                "points_not_attempted": len(objects) - (index + 1),
                            }
                            break
                return result
            finally:
                _note_device_progress(len(points))

        return _unit

    device_units = [
        _make_device_unit(entry["device"], entry["source"]) for entry in merged.values()
    ]
    per_device_results = await throttle.run_throttled(device_units, ctx)

    cancelled = ctx.is_cancelled()
    # Index enriched results by instance so heard-but-unenriched devices — heard on
    # a real Who-Is but whose read unit never ran because Stop halted dispatch — are
    # NOT silently discarded. They answered; they must still appear, marked, never
    # dropped (else expected_responding_count contradicts device_count on a Stop).
    enriched: dict[int, dict[str, Any]] = {}
    for entry in per_device_results:
        inst = entry["device"].get("device_instance")
        if isinstance(inst, int) and not isinstance(inst, bool):
            enriched[inst] = entry

    for instance, provenance in merged.items():
        device = provenance["device"]
        asset_id = f"bacnet-device-{instance}"
        target = targets_by_instance.get(instance)
        asset = _device_asset(
            device,
            backend_name,
            match_basis=provenance.get("match_basis", MATCH_BASIS_WHO_IS),
            lane=provenance.get("lane", LANE_BROADCAST),
        )
        record_row = _device_record(device, asset_id, target=target)
        entry = enriched.get(instance)
        if entry is None:
            # Heard, but its enrichment unit never ran (Stop halted dispatch first).
            asset["point_count"] = 0
            asset["heard_not_enriched"] = True
            record_row["attributes"]["heard_not_enriched"] = True
        else:
            asset["point_count"] = len(entry["points"])
            point_records.extend(entry["points"])
            if entry.get("heard_only"):
                asset["heard_not_enriched"] = True
                record_row["attributes"]["heard_not_enriched"] = True
            if entry.get("object_list_error"):
                record_row["attributes"]["object_list_read_failed"] = True
                issues.append(
                    _bacnet_issue(
                        issues,
                        asset_id=asset_id,
                        issue_type=ISSUE_OBJECT_LIST_UNREADABLE,
                        severity="medium",
                        description=(
                            f"BACnet device instance {instance} ({device.get('address')}) "
                            "answered Who-Is but its object-list could not be read, so its "
                            "points were not enumerated. The device was discovered and is "
                            "reported as discovered-but-unenumerated — not offline, and not "
                            "fully scanned."
                        ),
                        suggested_action=(
                            "The object-list read may exceed the device's APDU size, or the "
                            "device may not support the request. Re-scan this device on its "
                            "own or check its BACnet configuration."
                        ),
                    )
                )
            aborted = entry.get("reads_aborted")
            if aborted:
                record_row["attributes"]["point_reads_aborted"] = aborted
                issues.append(
                    _bacnet_issue(
                        issues,
                        asset_id=asset_id,
                        issue_type=ISSUE_POINT_READS_ABORTED,
                        severity="medium",
                        description=(
                            f"BACnet device instance {instance} ({device.get('address')}) "
                            f"returned {aborted['after_consecutive_failures']} consecutive "
                            "point-read failures, so the scan stopped reading its remaining "
                            f"{aborted['points_not_attempted']} point(s). The points not "
                            "attempted are absent from the results, never recorded as failures."
                        ),
                        suggested_action=(
                            "Confirm the device is reachable for ReadProperty on the lane "
                            "that heard it (check ACLs and routing), then re-scan it if needed."
                        ),
                    )
                )
            truncated = entry.get("reads_truncated")
            if truncated:
                # Operator-initiated Stop cut this device's point reads. Record it
                # per-device (no issue: a Stop is not a device fault — the run-level
                # 'cancelled'/'partial' already says the scan was stopped) so the
                # row is reconstructible as "cut, not fully read" from the artifact.
                record_row["attributes"]["point_reads_truncated"] = truncated
        if target is not None:
            # Register identity travels on the asset too, so the results table can
            # show the operator's own asset name next to what answered.
            asset["register_asset_id"] = target.asset_id
            asset["register_asset_name"] = target.asset_name
        discovered_assets.append(asset)
        device_records.append(record_row)

    structured_records = device_records + point_records

    # -- expected-but-silent: amber, never a failure, never "device absent" ---
    responding = [t for t in in_scope if t.device_instance in merged]
    not_responding = [t for t in in_scope if t.device_instance not in merged]
    record["expected_responding_count"] = len(responding)
    record["expected_not_responding"] = [
        {
            "asset_id": t.asset_id,
            "asset_name": t.asset_name,
            "device_instance": t.device_instance,
            "address": t.address,
            "directed_probe_sent": t.address in sent_addresses,
        }
        for t in not_responding
    ]
    for t in not_responding:
        probed = t.address in sent_addresses
        answered = t.address in answered_addresses
        if answered:
            # Something DID answer at this address — just not this instance.
            # Both rows are kept on purpose: the mismatch issue says "instance M
            # lives here", this one says "the instance you expected did not
            # answer". They are different facts and the operator needs both to
            # decide whether the register or the controller is wrong.
            detail = (
                f"another device answered at {t.address}, but instance "
                f"{t.device_instance} did not"
            )
        elif probed:
            detail = f"no answer to a directed Who-Is sent to {t.address}"
        else:
            detail = f"no directed Who-Is was sent to {t.address}"
        issues.append(
            _bacnet_issue(
                issues,
                asset_id=t.asset_id or f"bacnet-device-{t.device_instance}",
                issue_type=ISSUE_EXPECTED_DEVICE_SILENT,
                severity="medium",
                description=(
                    f"{t.asset_name or 'The registered device'} (BACnet instance "
                    f"{t.device_instance}, {t.address}) was expected from the register "
                    f"import but did not answer this scan — {detail}. This is "
                    "INCONCLUSIVE, not proof the device is offline: BACnet permits a "
                    "device to answer a directed Who-Is with a broadcast this host "
                    "cannot hear from another subnet, and devices behind a BACnet "
                    "router are only reachable through a BBMD."
                ),
                match_basis=MATCH_BASIS_WHO_IS_DIRECTED if probed else None,
                expected_value=str(t.device_instance),
                suggested_action=(
                    "Confirm the device is powered and on the network, and check its "
                    "address and instance in the register import. If it sits on another "
                    "subnet, enable Foreign Device registration so the scan can reach it "
                    "through the BBMD."
                ),
            )
        )

    record["device_count"] = len(discovered_assets)
    record["point_count"] = len(point_records)
    if cancelled:
        record["partial"] = True

    # The lanes completed without a transport failure. Everything that could have
    # made the transport a lie — a contended UDP port, a BBMD that refused or
    # ignored us — raises before this line and fails the run. Reaching here is
    # what earns the right to call an empty scan a CLEAN empty rather than an
    # unexplained silence.
    diagnostics["transport_verified"] = True

    # Finding nothing is a valid result, so the status stays succeeded — but it
    # never again goes out unexplained. Only for the live backend: a simulated
    # run's emptiness says nothing about a network.
    if backend_name == BACKEND_BACPYPES3 and not discovered_assets and not cancelled:
        record["empty_scan_hint"] = build_empty_scan_hint(
            mode=mode,
            interface=parameters.get("local_address"),
            instance_low=low,
            instance_high=high,
            timeout_s=_timeout_s(parameters),
            fd_bbmd_address=_fd_bbmd_address(fd_backend),
            # Probes actually SENT that got no reply — not the planned count, which
            # would over-count addresses skipped as unusable or never dispatched.
            unanswered_directed=len(sent_addresses) - len(answered_addresses),
        )

    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        issues=issues,
        result_summary_extra=record,
        status_override="cancelled" if cancelled else None,
    )


def _plan_transport(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve, without I/O, the transport a LIVE run would use. Never raises.

    This is Monday's pre-flight gate: the operator runs a dry run and reads back
    "foreign_device via <BBMD ip>:47808" before a single packet leaves the
    laptop. A config that would fail live must therefore fail VISIBLY here — but
    as an echoed message, not as a failed dry run: a preview that refuses to
    render the problem is a preview that hides it.
    """
    transport: dict[str, Any] = {"mode": bacnet_mode(parameters), "lanes": []}
    lane_specs: list[tuple[str, str, int | None]] = [
        (LANE_BROADCAST, MODE_BROADCAST, None),
    ]
    if is_foreign_device_mode(parameters):
        lane_specs.append((LANE_FOREIGN_DEVICE, MODE_FOREIGN_DEVICE, FD_LOCAL_UDP_PORT))
    for lane, lane_mode, udp_port in lane_specs:
        try:
            plan = build_transport_plan(parameters, mode=lane_mode, udp_port=udp_port)
        except ValueError as error:
            transport["lanes"].append({"lane": lane, "error": str(error)})
            continue
        transport["lanes"].append({"lane": lane, **plan.as_dict()})
    return transport


def _dry_run_result(ctx: EngineContext, backend: BacnetDiscoveryBackend) -> EngineResult:
    """Build the dry-run plan WITHOUT contacting the network (no Who-Is)."""
    parameters = ctx.parameters
    low, high = _instance_range(parameters)
    address = parameters.get("address")
    transport = _plan_transport(parameters)
    extra: dict[str, Any] = {
        "backend": _backend_name(backend),
        "interface": address,
        "transport": transport,
    }
    # Echo the directed lane's target list so the operator can confirm the
    # register import actually reached the engine BEFORE the live scan — "0
    # targets" here is the cheapest possible catch for an import that never
    # landed, and it costs nothing to look at.
    try:
        targets = resolve_unicast_targets(parameters)
    except RuntimeError as error:
        extra["unicast_target_error"] = str(error)
    else:
        in_scope = [t for t in targets if low <= t.device_instance <= high]
        extra["unicast_target_count"] = len(in_scope)
        extra["unicast_targets"] = [t.as_dict() for t in in_scope]
        if len(targets) != len(in_scope):
            extra["unicast_targets_out_of_instance_range"] = len(targets) - len(in_scope)

    actions = ["bacnet-who-is-broadcast", "read-property:object-list", "read-property:present-value"]
    if extra.get("unicast_target_count"):
        actions.insert(1, "bacnet-who-is-directed")
    mode_note = (
        f"foreign-device registration via BBMD {_transport_bbmd(transport)}"
        if transport["mode"] == MODE_FOREIGN_DEVICE
        else "local broadcast only"
    )
    plan = build_dry_run_plan(
        engine=ENGINE_NAME,
        targets=[{"device_instance_low": low, "device_instance_high": high, "address": address}],
        actions=actions,
        notes=(
            "Dry run: no Who-Is broadcast emitted. Would scan the device-instance "
            f"range [{low}, {high}] via the '{_backend_name(backend)}' backend "
            f"using {mode_note}."
        ),
        extra=extra,
    )
    return EngineResult(result_summary_extra={"backend": _backend_name(backend), "dry_run_plan": plan})


def _transport_bbmd(transport: Mapping[str, Any]) -> str:
    """The BBMD 'ip:port' from a planned transport, for the dry-run note."""
    for lane in transport.get("lanes", []):
        address = lane.get("fd_bbmd_address")
        if address:
            return str(address)
    return "(not configured)"


def make_bacnet_discovery_engine(
    backend: BacnetDiscoveryBackend | None = None,
    fd_backend: BacnetDiscoveryBackend | None = None,
) -> Callable[[EngineContext], Awaitable[EngineResult]]:
    """Return an async engine callable bound to a backend (for ``run_engine``).

    The returned callable enforces scan authorization, performs a dry-run plan
    when ``ctx.dry_run`` is set (NO broadcast), and otherwise drives the backend
    Who-Is + per-device reads under the throttle. The backend is resolved per
    call from ``ctx.parameters`` unless one is injected here.

    Args:
        backend: lane 1/2's transport (the local-broadcast app). Injected by
            tests and wiring; otherwise resolved from ``ctx.parameters``.
        fd_backend: lane 3's foreign-device app. Injectable SOLELY so the
            foreign-device orchestration is testable: CI has no BBMD, so without
            a seam here the BBMD-refusal path — the single most likely way
            Monday goes wrong — could not be exercised before Monday. A live run
            builds its own (see :func:`_select_fd_backend`) and ignores this.
    """

    async def _engine(ctx: EngineContext) -> EngineResult:
        # A dry run is side-effect free (it emits NO Who-Is broadcast), so a
        # preview is allowed without authorization — matching the IP/MQTT engines
        # and the safety module's documented dry-run convention. A real scan
        # (BACnet Who-Is is a broadcast that can disrupt fragile field buses)
        # still requires explicit authorization, gated AFTER the dry-run branch.
        try:
            chosen = _select_backend(ctx.parameters, backend, dry_run=ctx.dry_run)
            fd_chosen, fd_skip_reason = _select_fd_backend(
                ctx.parameters, backend, fd_backend, dry_run=ctx.dry_run
            )
        except ValueError as error:
            return EngineResult(
                status_override="failed",
                error_message=str(error),
                result_summary_extra={"device_count": 0, "point_count": 0},
            )
        if ctx.dry_run:
            return _dry_run_result(ctx, chosen)
        require_scan_authorization(ctx.parameters)
        # A real bacpypes3 Who-Is must bind a specific local interface. When no
        # Source Interface is configured (local_address unset), the scan cannot
        # run — fail with an ACTIONABLE error_message (which the UI surfaces via
        # the run's error_message) instead of a raw bind error that the framework
        # would sanitize to a generic "engine execution failed". Deliberately do
        # NOT stamp result_summary.backend here: no socket was bound and no scan
        # ran, so a "Live bacpypes3 scan" provenance label would be a false
        # positive — the run is simply a failure with a clear, safe reason.
        if _backend_name(chosen) == BACKEND_BACPYPES3 and not str(
            ctx.parameters.get("local_address") or ""
        ).strip():
            return EngineResult(
                status_override="failed",
                error_message=_NO_SOURCE_INTERFACE_MESSAGE,
                result_summary_extra={"device_count": 0, "point_count": 0},
            )
        # Two things this try/finally guarantees, both of which were missing and
        # both of which would have cost a live lab day on their own:
        #
        # 1. close() ACTUALLY RUNS. It had zero call sites, so the first scan's
        #    UDP socket leaked in the long-lived exe process and every later scan
        #    silently found nothing. See Bacpypes3Backend.close.
        # 2. A vetted RuntimeError REACHES THE OPERATOR. Every actionable message
        #    this backend raises — "UDP 47808 in use", "the BBMD refused
        #    registration (result code N)", "bacpypes3 is not installed" — used to
        #    propagate into base.py's blanket except and be replaced by the
        #    generic "Engine execution failed". The messages existed; nobody ever
        #    saw one. status_override="failed" is the engine self-diagnosing, the
        #    same path the no-Source-Interface branch above already uses, and it
        #    preserves result_summary/issues (a raised exception discards them).
        #
        # Only RuntimeError is treated as vetted. Anything else still goes to the
        # framework's sanitizer, because only these messages have been checked for
        # credential leakage.
        #
        # The summary record is created HERE, before the scan, and the same dict
        # object is carried into the engine body, returned inside the
        # EngineResult, and stamped in the finally below. One object, so no exit
        # path can lose the diagnostics: a run that dies on a contended 47808 or
        # a BBMD refusal still records the interface, the port, the bind reason
        # and what the BBMD said. That is the v0.1.12 bar — a failed Monday scan
        # must be diagnosable from the run artifacts ALONE.
        record = _new_summary_record(ctx, chosen, mode=bacnet_mode(ctx.parameters))
        try:
            result = await _run_bacnet_discovery(
                ctx,
                chosen,
                fd_backend=fd_chosen,
                fd_skip_reason=fd_skip_reason,
                record=record,
            )
        except RuntimeError as error:
            result = EngineResult(
                status_override="failed",
                error_message=str(error),
                result_summary_extra=record,
            )
        finally:
            # Runs on every path — success, clean empty, and every hard failure.
            # Stamped before close() so the backends' bind / fd_registration
            # records are read while they are still there to read.
            _stamp_transport(record, chosen, fd_chosen)
            _close_backend(chosen)
            if fd_chosen is not None:
                _close_backend(fd_chosen)
        return result

    return _engine


def process_bacnet_discovery_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: RunStore,
    execution_mode: str,
    backend: BacnetDiscoveryBackend | None = None,
    fd_backend: BacnetDiscoveryBackend | None = None,
    throttle: ThrottleConfig | None = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Synchronous processor entrypoint mirroring the other ``process_*_run`` jobs.

    Builds the :class:`EngineContext`, selects the backend (bacpypes3 for real
    runs; simulated only for dry runs), and drives the engine via
    :func:`run_engine`. Authorization is enforced inside the engine; on an
    unauthorized run the framework records a sanitized ``failed`` status.

    Args:
        run_id / parameters / run_store / execution_mode: standard run context.
        backend: optional explicit backend (else selected from parameters).
            Inject :class:`SimulatedBacnetBackend` for offline tests; real runs
            default to the real, UNVALIDATED bacpypes3 path.
        fd_backend: optional explicit foreign-device (lane 3) backend, for
            offline tests of the BBMD paths. A live foreign-device run builds
            its own second Application and ignores this.
        throttle: optional :class:`ThrottleConfig` (defaults applied otherwise).
        dry_run: when True, returns the planned Who-Is window WITHOUT broadcasting.
        persist_records: optional structured-record persister (e.g. backed by
            DiscoveryRepository); defaults to the framework no-op.
        is_cancelled: optional cooperative-cancellation checker.

    Returns the terminal run record from the run store.
    """
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "parameters": parameters,
        "run_store": run_store,
        "execution_mode": execution_mode,
        "throttle": throttle or ThrottleConfig(),
        "dry_run": dry_run,
    }
    if is_cancelled is not None:
        kwargs["_is_cancelled"] = is_cancelled
    ctx = EngineContext(**kwargs)
    engine = make_bacnet_discovery_engine(backend, fd_backend)
    if persist_records is not None:
        return run_engine(ctx, engine, persist_records=persist_records)
    return run_engine(ctx, engine)
