"""IP discovery engine: an authorized, throttled TCP-connect host sweep.

What this engine does
---------------------
Given an IP target spec (a CIDR block, or an inclusive ``start``/``end`` range)
plus a small list of TCP ports, it attempts a plain ``asyncio`` TCP connect to
each ``(ip, port)`` pair under the shared :class:`~smart_commissioning_core.engines.base.Throttle`.
Each host's probe list is the resolved base list UNION its register-declared
ports (expected + forbidden, from the ``*_by_address`` maps), so the register's
"Expected services/ports" are genuinely checked — not just used for flagging.
A host is considered *present* if ANY of its scanned ports accepts a connection
(connect-success liveness — we never send ICMP and never send any application
payload). We emit a ``discovered_assets`` row for EVERY scanned host so the
operator sees the whole sweep, not just the responders:

* a *responsive* host gets a ``discovered_assets`` entry in the
  ``DiscoveryAssetObservation`` shape the API's ``DiscoveryResultsResponse``
  reads from ``result_summary["discovered_assets"]`` (``ip_address``,
  ``hostname``, ``observed_ports=[{port,protocol,service}]``, ``match_basis``,
  ``status_detail``), PLUS a structured ``DiscoveredDevice`` row
  (``device_type="ip_host"``) for the DiscoveryRepository, with the open-port
  detail under ``attributes``.
* a *silent* host (no port accepted a connection) gets a ``discovered_assets``
  row too, with ``observed_ports=[]``, ``match_basis="none"``, ``last_seen_at``
  None (we never saw it — no fabricated timestamp) and a ``status_detail`` of
  ``"no response on scanned ports"`` — plus an ``EXPECTED BY REGISTER``
  inconclusive note when the host was named in the register import. A silent
  host emits NO structured record: a host we did not hear from is not a
  discovered device and must never enter the device inventory or reports.

Safety / honesty
----------------
* The real sweep is gated by :func:`safety.require_scan_authorization`.
* Under ``ctx.dry_run`` the engine performs NO socket I/O: it expands the
  ``(ip, port)`` target list and returns it as ``dry_run_plan``.
* Cancellation (``ctx.is_cancelled()``) is honoured *between* per-host batches,
  and ``Throttle.run_throttled`` additionally checks it between port dispatches,
  so a long sweep stops promptly with partial results.

ON-SITE VALIDATION REQUIRED: this module's pure-Python logic (target expansion,
port-result aggregation, asset/record building, throttling, cancellation,
authorization, dry-run) is fully unit-tested against ``127.0.0.1`` plus an
ephemeral loopback listener the test opens (see ``core/tests/test_ip_scan.py``).
What CANNOT be exercised in this environment is a sweep across a REAL building
network: TCP reachability of Modbus and BMS hosts, plus behaviour against
firewalls and rate-limited switch fabrics. Those paths are
listed in the task's ``live_untested`` output and must be validated on site
against the actual VLAN. The first field provider performs no DNS or ARP
enrichment.

The transport is dependency-injected (``connect`` parameter) exactly so the
tests can drive it without real sockets when they want to; the production
default uses ``asyncio.open_connection`` against real addresses.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.discovery_observations import (
    PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    DiscoveryObservationInputV1,
)
from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    Throttle,
    make_cancel_checker,
    run_engine,
)
from smart_commissioning_core.engines.ip import (
    BUILTIN_TCP_PROVIDER,
    IPComparisonReachabilityV1,
    IPHeadlineMetricScopeV1,
    IPHostComparisonInputV1,
    IPHostMetricStateV1,
    IPRegisterAuthorityRowV1,
    IPRegisterAuthorityV1,
    IPRegisterComparisonV1,
    NmapProviderDependencies,
    ProviderDiagnosticV1,
    ProviderIdentityEvidenceV1,
    TCPConnectTransport,
    TCPPortObservationV1,
    TCPProbeRequestV1,
    build_ip_register_authority,
    compare_ip_register,
    probe_tcp_connect,
    provider_capability,
    reduce_ip_headline_metrics,
    require_frozen_ip_provider_execution,
    run_nmap_provider,
)
from smart_commissioning_core.engines.ip.tcp_connect import (
    AsyncioTCPConnectTransport,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.owned_run_store import OwnershipLostError
from smart_commissioning_core.run_context import canonical_sha256

# Default ports for the TCP connect sweep. 1883=MQTT, 502=Modbus, 80/443=web
# mgmt UIs. BACnet/IP (UDP 47808) is intentionally excluded: this engine only
# does TCP connects, so a TCP probe of 47808 can never detect a BACnet device —
# BACnet discovery is the dedicated Who-Is (UDP broadcast) engine.
DEFAULT_PORTS: tuple[int, ...] = (80, 443, 1883, 502)

# Hard ceiling on how many hosts a single sweep may expand to. This is the
# operator policy cap: a request's ``max_hosts`` may LOWER it but can never
# raise it above this value, so a fat CIDR can never explode into millions of
# connects regardless of request parameters.
MAX_HOSTS_CEILING = 4096

# Hard ceiling on ports-per-sweep. A "scan all 60,000+ ports" request expands
# via a range like ``1-65535``; this cap keeps a single sweep from quietly
# turning into a hosts*65k connect storm on a live OT network.
# ponytail: global cap. Raise it deliberately if a wide audit genuinely needs it.
MAX_PORTS_CEILING = 4096

# Best-effort service labels for the observed-port detail (informational only;
# we do not do protocol fingerprinting — a connect success only proves the TCP
# port is open, not which service is behind it).
_SERVICE_HINTS: dict[int, str] = {
    80: "http",
    443: "https",
    502: "modbus",
    1883: "mqtt",
    47808: "bacnet",
}

ENGINE_NAME = "ip_discovery"

# Honest copy for a host that accepted no connection on any scanned port. This
# exact spelling is the single source of truth for the "no response" verdict and
# is mirrored BY NAME in the frontend (discoveryRows.ts NO_RESPONSE_DETAIL) — the
# same cross-language contract as SUMMARY_BACNET_MODE there, pinned by tests on
# both sides. A TCP-connect miss is inconclusive: never "offline", never proof a
# host is absent.
NO_RESPONSE_DETAIL = "no response on scanned ports"
# Marker appended to a silent host's status_detail ONLY when that host was named
# in the register import. Mirrors the BACnet expected-but-silent semantics
# (bacnet_discovery.py ISSUE_EXPECTED_DEVICE_SILENT): register-expected silence
# is amber/inconclusive, unregistered silence is neutral. Mirrored by name in
# discoveryRows.ts (MARKER_EXPECTED_BY_REGISTER).
MARKER_EXPECTED_BY_REGISTER = "EXPECTED BY REGISTER"

# ``reason`` is a viewer-safe observation field.  Keep the status construction
# bound aligned with its public-schema limit so a wide, legitimate scan cannot
# fail only when progressive observations are appended.
IP_STATUS_DETAIL_MAX_CHARS = PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS
IP_STATUS_DETAIL_PORT_SAMPLE_SIZE = 32
# Approved identity providers may return these fields for a reachable host. The
# preview uses the maximum safe UTF-8 hostname plus a canonical MAC so the
# terminal projection stays budgeted even when the register did not contain
# either value. The built-in TCP provider never supplies either field.
IP_PROVIDER_IDENTITY_HOSTNAME_MAX_CHARS = 253
IP_PROVIDER_IDENTITY_MAX_MAC_ADDRESS = "FF:FF:FF:FF:FF:FF"


def max_provider_identity_hostname_v1() -> str:
    """Return the widest valid UTF-8 hostname accepted from a provider."""

    return "\U0001f4a1" * IP_PROVIDER_IDENTITY_HOSTNAME_MAX_CHARS

# A connect probe: open a TCP connection to (host, port) within ``timeout`` and
# return True iff it succeeds. Injectable so tests can avoid real sockets.
ConnectProbe = Callable[[str, int, float], Awaitable[bool]]
ImportLoader = Callable[[str], list[dict[str, Any]]]
IdentityObserver = Callable[[str], Awaitable[ProviderIdentityEvidenceV1 | None]]


def format_ip_status_detail(
    base_detail: str,
    *,
    responsive_ports: Sequence[int] = (),
    forbidden_open_ports: Sequence[int] = (),
    unexpected_open_ports: Sequence[int] = (),
    missing_expected_ports: Sequence[int] = (),
    suffixes: Sequence[str] = (),
) -> str:
    """Format one bounded, deterministic public IP status detail.

    Ordinary plans retain their historic detail verbatim.  When the full port
    lists would exceed the public field ceiling, every populated category keeps
    its count and a fixed ordered sample instead of allowing an unbounded comma
    list to make the whole observation invalid.
    """

    categories = (
        ("FORBIDDEN PORTS OPEN", forbidden_open_ports),
        ("UNEXPECTED PORTS OPEN", unexpected_open_ports),
        ("MISSING EXPECTED PORTS", missing_expected_ports),
    )
    normalized_responsive = tuple(sorted(set(responsive_ports)))
    normalized_categories = tuple(
        (label, tuple(sorted(set(ports))))
        for label, ports in categories
        if ports
    )
    normalized_suffixes = tuple(suffix for suffix in suffixes if suffix)

    def full() -> str:
        detail = (
            "responsive: " + ",".join(map(str, normalized_responsive))
            if normalized_responsive
            else base_detail
        )
        for label, ports in normalized_categories:
            detail += " | " + label + ": " + ",".join(map(str, ports))
        return detail + "".join(normalized_suffixes)

    unbounded = full()
    if len(unbounded) <= IP_STATUS_DETAIL_MAX_CHARS:
        return unbounded

    def sampled(ports: Sequence[int]) -> str:
        values = ",".join(map(str, ports[:IP_STATUS_DETAIL_PORT_SAMPLE_SIZE]))
        return f"{len(ports)} total; sample: {values}"

    detail = (
        "responsive: " + sampled(normalized_responsive)
        if normalized_responsive
        else base_detail
    )
    for label, ports in normalized_categories:
        detail += f" | {label}: {sampled(ports)}"
    detail += "".join(normalized_suffixes)
    # The bounded representation is intentionally much smaller than the
    # ceiling. This guard protects future non-port suffix additions.
    return detail[:IP_STATUS_DETAIL_MAX_CHARS]

_PROVIDER_NAME = BUILTIN_TCP_PROVIDER
_PROVIDER_VERSION = "1.0"
_REACHABLE_OUTCOMES = frozenset({"connected"})
_RETRYABLE_OUTCOMES = frozenset(
    {"timed_out", "network_unreachable", "host_unreachable", "provider_error"}
)


@dataclass(frozen=True, slots=True)
class _IPRuntimePolicy:
    profile: str
    per_target_concurrency: int
    min_target_spacing_s: float
    retries: int
    retry_backoff_s: float
    attempt_ceiling: int


@dataclass(frozen=True, slots=True)
class _PortAttemptEvent:
    observation: TCPPortObservationV1
    entity_version: int
    attempts: int
    elapsed_ms: float
    last_packet_dispatched_at: datetime | None


@dataclass(frozen=True, slots=True)
class _PortProbeResult:
    observation: TCPPortObservationV1
    attempts: int
    last_packet_dispatched_at: datetime | None
    events: tuple[_PortAttemptEvent, ...] = ()
    control_reason: str | None = None


class _DispatchRecordingTransport:
    """Record evidence at the TCP transport boundary, immediately before I/O."""

    def __init__(
        self,
        delegate: TCPConnectTransport,
        on_dispatch: Callable[[], None],
    ) -> None:
        self._delegate = delegate
        self._on_dispatch = on_dispatch

    async def connect(
        self,
        target_ip: str,
        port: int,
        *,
        source_ip: str | None,
    ) -> Any:
        self._on_dispatch()
        return await self._delegate.connect(
            target_ip,
            port,
            source_ip=source_ip,
        )


class _IPControlLost(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("IP dispatch active control was lost")


def _control_reason(error: BaseException, ctx: EngineContext) -> str:
    text = str(getattr(error, "reason", error)).casefold()
    if isinstance(error, OwnershipLostError):
        return "ownership_lost"
    if "deadline" in text:
        return "dispatch_deadline_elapsed"
    if "authorization" in text and "revok" in text:
        return "authorization_revoked"
    if "authorization" in text and ("ended" in text or "expir" in text):
        return "authorization_expired"
    if "grant" in text or "initiating user" in text or "engineer or admin" in text:
        return "grant_revoked"
    if "owner" in text or "attempt" in text or "lease" in text:
        if "cancellation" in text and ctx.is_cancelled():
            return "stop_requested"
        return "ownership_lost"
    if "cancel" in text or "stop" in text:
        return "stop_requested"
    return "control_store_error"


async def _interruptible_sleep(ctx: EngineContext, delay_s: float) -> bool:
    """Wait for policy pacing while polling Stop at a bounded cadence."""

    if ctx.is_cancelled():
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, delay_s)
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return not ctx.is_cancelled()
        await asyncio.sleep(min(remaining, 0.05))
        if ctx.is_cancelled():
            return False


def _cancelled_port_observation(*, host: str, port: int) -> TCPPortObservationV1:
    return TCPPortObservationV1(
        target_ip=host,
        port=port,
        protocol="tcp",
        outcome="cancelled",
        diagnostic=ProviderDiagnosticV1(
            code="cancelled",
            reason="The TCP probe was cancelled.",
        ),
        capability=provider_capability(protocol="tcp", port=port),
        port_hint=_SERVICE_HINTS.get(port),
    )


def _not_attempted_port_result(
    *,
    host: str,
    port: int,
    control_reason: str,
) -> _PortProbeResult:
    observation = _cancelled_port_observation(host=host, port=port)
    event = _PortAttemptEvent(
        observation=observation,
        entity_version=1,
        attempts=0,
        elapsed_ms=0.0,
        last_packet_dispatched_at=None,
    )
    return _PortProbeResult(
        observation=observation,
        attempts=0,
        last_packet_dispatched_at=None,
        events=(event,),
        control_reason=control_reason,
    )


def _make_default_connect(source_ip: str | None) -> ConnectProbe:
    """Build the real connect probe, optionally bound to a source interface.

    When ``source_ip`` is truthy the probe binds every connect to
    ``local_addr=(source_ip, 0)`` (port 0 = OS picks the ephemeral source
    port), forcing egress out that NIC. When it is falsy the probe passes
    ``local_addr=None``, which is byte-for-byte today's OS-default-route
    behaviour (the ``Auto`` path is a no-op).
    """
    local_addr = (source_ip, 0) if source_ip else None

    async def _default_connect(host: str, port: int, timeout: float) -> bool:
        """Real connect probe: True iff a TCP connection to (host, port) succeeds.

        Uses ``asyncio.open_connection`` and immediately closes the connection.
        Never raises: any connection error (refused, timeout, unreachable, OS
        error) is treated as "port closed / host not responding on this port".

        NOTE: only the loopback path of this function is unit-tested; behaviour
        against real remote hosts / firewalls requires on-site validation.
        """
        writer = None
        try:
            connect = asyncio.open_connection(host, port, local_addr=local_addr)
            _reader, writer = await asyncio.wait_for(connect, timeout=timeout)
            return True
        except (OSError, TimeoutError, ValueError):
            return False
        finally:
            if writer is not None:
                try:
                    writer.close()
                    # wait_closed can itself raise on a half-open socket; ignore.
                    await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
                except (OSError, TimeoutError, ValueError):
                    pass

    return _default_connect


def _expand_hosts(parameters: dict[str, Any]) -> list[str]:
    """Expand the target spec into an ordered, de-duplicated list of IP strings.

    Accepts one of three target shapes, in precedence order:

    * ``cidr`` (e.g. ``"10.0.0.0/30"``) — host bits kept for /31 and /32; for
      larger blocks the network/broadcast addresses are dropped (``hosts()``).
    * an inclusive ``start``/``end`` range.
    * an explicit ``addresses`` list of IP strings — this is how an imported IP
      register's *Expected IP address* column is scanned (the route fills it in
      when the operator hasn't given a ``cidr``/range), so "upload register then
      run discovery" sweeps exactly the registered hosts.

    Raises ``ValueError`` on a malformed/empty/oversized spec so ``run_engine``
    records a sanitized failure (the route pre-empts the common "no target"
    case with a clear 400 before the engine runs).
    """
    cidr = parameters.get("cidr")
    start = parameters.get("start") or parameters.get("start_ip")
    end = parameters.get("end") or parameters.get("end_ip")
    addresses = parameters.get("addresses")
    hosts: list[str] = []
    requested_max_hosts = _positive_int(
        parameters.get("max_hosts"), default=MAX_HOSTS_CEILING
    )
    max_hosts = min(requested_max_hosts, MAX_HOSTS_CEILING)

    def _reject_excess_hosts(host_count: int) -> None:
        if host_count > max_hosts:
            raise ValueError(
                f"IP discovery target spec expands to {host_count} hosts, "
                f"exceeding max_hosts={max_hosts}."
            )

    if cidr:
        if not isinstance(cidr, str):
            raise ValueError("cidr must be a string")
        network = ipaddress.ip_network(cidr.strip(), strict=False)
        _reject_excess_hosts(
            network.num_addresses
            if network.num_addresses <= 2
            else network.num_addresses - 2
        )
        iterable = network.hosts() if network.num_addresses > 2 else network
        hosts = [str(ip) for ip in iterable]
    elif start or end:
        if not start or not end:
            raise ValueError("IP discovery 'start'/'end' range requires both a start and an end IP.")
        start_addr = ipaddress.ip_address(str(start).strip())
        end_addr = ipaddress.ip_address(str(end).strip())
        if start_addr.version != end_addr.version:
            raise ValueError("start and end IP must be the same IP version.")
        if int(end_addr) < int(start_addr):
            raise ValueError("end IP must be >= start IP.")
        _reject_excess_hosts(int(end_addr) - int(start_addr) + 1)
        hosts = [
            str(ipaddress.ip_address(value))
            for value in range(int(start_addr), int(end_addr) + 1)
        ]
    elif addresses is not None:
        if not isinstance(addresses, (list, tuple)):
            raise ValueError("addresses must be a list of IP address strings.")
        for value in addresses:
            text = str(value).strip()
            if not text:
                continue
            try:
                hosts.append(str(ipaddress.ip_address(text)))
            except ValueError as error:
                raise ValueError(f"addresses contains an invalid IP: {value!r}") from error
    else:
        raise ValueError(
            "IP discovery requires a target: import an IP register, or provide "
            "'cidr', a 'start'/'end' range, or an 'addresses' list."
        )

    if not hosts:
        raise ValueError("IP discovery target spec expanded to zero hosts.")
    _reject_excess_hosts(len(hosts))
    # De-duplicate while preserving order.
    return list(dict.fromkeys(hosts))


def _parse_port_spec(spec: str) -> list[int]:
    """Expand an "Expected services/ports" string into TCP port numbers.

    Accepts the same comma-separated form the operator types / the IP register
    carries: ``"443/tcp, 1-1024, 8000-8100"``. Bare values are TCP. UDP is
    rejected before a TCP probe list exists, because a TCP connect to UDP/47808
    would fabricate a different transport observation. ``lo-hi`` ranges expand
    inclusively — this is how "scan the other 60,000+ ports" is expressed.
    """
    ports: list[int] = []
    for token in (part.strip() for part in spec.split(",")):
        if not token:
            continue
        number, separator, raw_protocol = token.partition("/")
        protocol = raw_protocol.strip().casefold() if separator else "tcp"
        if protocol != "tcp":
            if protocol == "udp":
                raise ValueError(
                    "Expected services/ports contains UDP, which the TCP provider cannot scan"
                )
            raise ValueError(
                f"Expected services/ports contains an unsupported protocol: {token!r}"
            )
        number = number.strip()
        try:
            if "-" in number:
                low, high = (int(end.strip()) for end in number.split("-", 1))
                ports.extend(range(min(low, high), max(low, high) + 1))
            else:
                ports.append(int(number))
        except ValueError as error:
            raise ValueError(f"Expected services/ports contains an invalid port: {token!r}") from error
    return ports


def _resolve_ports(parameters: dict[str, Any]) -> list[int]:
    raw = parameters.get("ports")
    # The frontend / IP register supplies ports as a "port_specification" string
    # (with optional ranges); fall back to it when an explicit int list is absent
    # so the operator's chosen ports actually reach the sweep.
    if raw is None:
        spec = parameters.get("port_specification")
        if spec:
            raw = _parse_port_spec(str(spec))
    if raw is None:
        return list(DEFAULT_PORTS)
    if not isinstance(raw, (list, tuple)):
        raise ValueError("ports must be a list of integers.")
    ports: list[int] = []
    for value in raw:
        try:
            port = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("ports must be a list of integers.") from error
        if not (0 < port < 65536):
            raise ValueError(f"port out of range: {port}")
        if port not in ports:
            ports.append(port)
    if not ports:
        # A spec that parsed to nothing (e.g. only blanks) falls back to defaults
        # rather than failing the whole run.
        if parameters.get("port_specification"):
            return list(DEFAULT_PORTS)
        raise ValueError("ports list must not be empty.")
    if len(ports) > MAX_PORTS_CEILING:
        raise ValueError(
            f"port list expands to {len(ports)} ports, exceeding MAX_PORTS_CEILING="
            f"{MAX_PORTS_CEILING}; narrow the range."
        )
    return ports


def _resolve_forbidden_ports(parameters: dict[str, Any]) -> set[int]:
    """Ports that must NOT be open (flagged if found). Same spec syntax as ports."""
    raw = parameters.get("forbidden_ports")
    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = _parse_port_spec(raw)
    return {p for value in raw if 0 < (p := int(value)) < 65536}


def _resolve_spec_map(parameters: dict[str, Any], key: str) -> dict[str, set[int]]:
    """Per-host port sets keyed by IP, expanded from a ``{address: spec}`` map the
    route fills from the register. Used for both ``forbidden_ports_by_address``
    (flag if open) and ``expected_ports_by_address`` (flag if open AND not listed).
    A host present here is checked against its OWN set; absent hosts fall back to
    the global forbidden set.
    """
    raw = parameters.get(key)
    if not isinstance(raw, dict):
        return {}
    return {
        str(address): _resolve_forbidden_ports({"forbidden_ports": spec})
        for address, spec in raw.items()
    }


def _host_probe_ports(
    base_ports: Sequence[int], register_ports: set[int]
) -> tuple[list[int], list[int]]:
    """Per-host probe list: the resolved base list UNION the host's
    register-declared ports (its expected + forbidden sets), ceiling-capped.

    The base list keeps its order; register ports not already in it are
    appended in ascending order, so a register-declared service is genuinely
    probed even when the operator's port spec (or the default list) omits it —
    previously those ports only fed the flagging maps and were never connected
    to, so "Expected services/ports: 445" with a blank port field silently
    probed only the defaults. Hosts with no register ports get exactly the
    base list back.

    The union is capped at ``MAX_PORTS_CEILING`` per host. The base list is
    already validated against the same ceiling by :func:`_resolve_ports`, so
    only appended register ports can overflow; the dropped tail is returned so
    the caller reports the truncation honestly instead of silently skipping
    register-declared ports.

    Returns ``(ports_to_probe, dropped_register_ports)``.
    """
    extras = sorted(register_ports.difference(base_ports))
    union = list(base_ports) + extras
    if len(union) <= MAX_PORTS_CEILING:
        return union, []
    return union[:MAX_PORTS_CEILING], union[MAX_PORTS_CEILING:]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _short_hostname(name: str) -> str:
    """Lower-cased short (first-label) form of a hostname, for comparison.

    Reverse DNS usually returns an FQDN (``ahu-l03-017.site.example``) while the
    register's "Expected hostname" carries short names, so mismatch detection
    compares first labels case-insensitively.
    """
    return name.split(".", 1)[0].strip().lower()


def _build_observed_ports(open_ports: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "port": port,
            "protocol": "tcp",
            "service": _SERVICE_HINTS.get(port),
        }
        for port in sorted(open_ports)
    ]


def _ip_contract(parameters: dict[str, Any]) -> dict[str, Any]:
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, dict) or contract.get("job_type") != ENGINE_NAME:
        return {}
    value = contract.get("ip")
    return value if isinstance(value, dict) else {}


def _load_frozen_ip_authority(
    parameters: dict[str, Any],
    import_loader: ImportLoader | None,
) -> IPRegisterAuthorityV1 | None:
    """Load and re-verify only the authority referenced by the frozen plan."""

    ip_contract = _ip_contract(parameters)
    raw_authority = ip_contract.get("authority") if ip_contract else None
    if raw_authority is None:
        return None
    if not isinstance(raw_authority, dict):
        raise ValueError("frozen IP register authority must be an object")
    if raw_authority.get("import_type") not in (None, "ip_register"):
        raise ValueError("frozen IP register authority must use import_type=ip_register")
    import_id = raw_authority.get("import_id")
    digest = raw_authority.get("accepted_rows_sha256")
    accepted_count = raw_authority.get("accepted_count")
    if not isinstance(import_id, str) or not import_id.strip():
        raise ValueError("frozen IP register authority has no import_id")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("frozen IP register authority has an invalid row digest")
    if isinstance(accepted_count, bool) or not isinstance(accepted_count, int):
        raise ValueError("frozen IP register authority has an invalid accepted_count")
    if import_loader is None:
        raise ValueError("frozen IP register authority cannot be loaded")

    rows = [dict(row) for row in import_loader(import_id)]
    if len(rows) != accepted_count:
        raise ValueError("frozen IP register accepted row count does not match")
    if canonical_sha256(rows) != digest:
        raise ValueError("frozen IP register accepted rows failed digest verification")
    return build_ip_register_authority(
        rows,
        import_id=import_id,
        accepted_rows_sha256=digest,
    )


def _matched_authority_row(
    authority: IPRegisterAuthorityV1 | None,
    comparison: IPRegisterComparisonV1,
) -> IPRegisterAuthorityRowV1 | None:
    if authority is None or comparison.matched_device_key is None:
        return None
    return next(
        iter(authority._rows_by_device_key.get(comparison.matched_device_key, ())),
        None,
    )


def _runtime_policy(
    parameters: dict[str, Any],
    *,
    host_count: int,
    port_count: int,
    throttle: Throttle,
) -> _IPRuntimePolicy:
    ip_contract = _ip_contract(parameters)
    raw_policy = ip_contract.get("policy") if ip_contract else None
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    profile = str(policy.get("profile") or ip_contract.get("profile") or "gentle")
    if profile not in {"gentle", "planned_extended"}:
        raise ValueError("frozen IP profile is invalid")
    per_target = _positive_int(
        policy.get("per_target_concurrency"),
        default=throttle.config.max_concurrency,
    )
    per_target = min(per_target, throttle.config.max_concurrency)
    retries = max(0, int(policy.get("retries") or 0))
    spacing_ms = max(0.0, float(policy.get("min_target_spacing_ms") or 0.0))
    backoff_ms = max(0.0, float(policy.get("retry_backoff_ms") or 0.0))
    planned_default = max(1, host_count * port_count * (retries + 1))
    attempt_ceiling = _positive_int(
        policy.get("total_dispatch_attempt_ceiling"),
        default=_positive_int(
            parameters.get("max_dispatch_attempts"),
            default=planned_default,
        ),
    )
    return _IPRuntimePolicy(
        profile=profile,
        per_target_concurrency=per_target,
        min_target_spacing_s=spacing_ms / 1000.0,
        retries=retries,
        retry_backoff_s=backoff_ms / 1000.0,
        attempt_ceiling=attempt_ceiling,
    )


def _observation_provenance(
    parameters: dict[str, Any],
    *,
    profile: str,
    hosts: Sequence[str],
    ports: Sequence[int],
) -> dict[str, Any]:
    contract = parameters.get("scan_contract_v1")
    contract = contract if isinstance(contract, dict) else {}
    packet_digest = contract.get("packet_plan_sha256")
    if not isinstance(packet_digest, str) or re.fullmatch(r"[0-9a-f]{64}", packet_digest) is None:
        packet_digest = canonical_sha256(
            {"addresses": list(hosts), "ports": list(ports)}
        )
    ip_contract = _ip_contract(parameters)
    raw_authority = ip_contract.get("authority") if ip_contract else None
    authority = raw_authority if isinstance(raw_authority, dict) else {}
    import_id = authority.get("import_id")
    rows_digest = authority.get("accepted_rows_sha256")
    source = contract.get("source_interface")
    source = source if isinstance(source, dict) else {}
    return {
        "profile": profile,
        "source_ip": parameters.get("source_ip") or source.get("source_ip"),
        "source_interface": source.get("local_address"),
        "packet_plan_sha256": packet_digest,
        "register_import_id": import_id if isinstance(import_id, str) else None,
        "register_rows_sha256": (
            rows_digest if isinstance(rows_digest, str) else None
        ),
    }


def _reachability_for(outcome: str | None) -> str:
    if outcome is None:
        return "pending"
    return "reachable" if outcome in _REACHABLE_OUTCOMES else "unconfirmed"


def _policy_verdict(
    outcome: str,
    *,
    port: int,
    expected: set[int] | None,
    forbidden: set[int],
) -> str:
    if outcome == "connected":
        if port in forbidden:
            return "forbidden_open"
        if expected is not None and port not in expected:
            return "unexpected_open_review"
        return "pass"
    if outcome == "connection_refused":
        return "expected_closed" if expected is not None and port in expected else "pass"
    if outcome == "cancelled":
        return "not_attempted"
    return "unconfirmed"


def _legacy_probe_observation(
    *,
    host: str,
    port: int,
    connected: bool,
) -> TCPPortObservationV1:
    outcome = "connected" if connected else "timed_out"
    reason = (
        "TCP connection established."
        if connected
        else "The legacy probe did not confirm a TCP connection."
    )
    return TCPPortObservationV1(
        target_ip=host,
        port=port,
        protocol="tcp",
        outcome=outcome,
        diagnostic=ProviderDiagnosticV1(code=outcome, reason=reason),
        capability=provider_capability(protocol="tcp", port=port),
        port_hint=_SERVICE_HINTS.get(port),
    )


def _ip_payload(
    *,
    target: str,
    provenance: dict[str, Any],
    coverage_state: str,
    register_match: str,
    policy_verdict: str,
    attempts: int,
    elapsed_ms: float,
    reachability_state: str = "pending",
    probe: TCPPortObservationV1 | None = None,
    reason: str | None = None,
    transport: str | None = None,
    port: int | None = None,
    control_reason: str | None = None,
    capability_action: str | None = None,
    last_packet_dispatched_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "coverage_state": coverage_state,
        "reachability_state": reachability_state,
        "probe_outcome": probe.outcome if probe is not None else None,
        "register_match": register_match,
        "policy_verdict": policy_verdict,
        "target": target,
        "port": probe.port if probe is not None else port,
        "transport": probe.protocol if probe is not None else transport,
        "provider": probe.provider if probe is not None else _PROVIDER_NAME,
        "provider_version": _PROVIDER_VERSION,
        "provider_contract_version": "1.0",
        "provenance": provenance,
        "reason": reason or (probe.diagnostic.reason if probe is not None else None),
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
        "port_hint": probe.port_hint if probe is not None else None,
        "detected_service": probe.detected_service if probe is not None else None,
        "detected_version": probe.detected_version if probe is not None else None,
        "control_reason": control_reason,
        "capability_action": capability_action,
        "last_packet_dispatched_at": last_packet_dispatched_at,
    }


async def _append_ip_observation(
    ctx: EngineContext,
    *,
    entity_kind: str,
    entity_key: str,
    entity_version: int,
    event_key: str,
    phase: str,
    outcome: str,
    payload: dict[str, Any],
    projection: dict[str, Any] | None = None,
    publish: bool = True,
) -> None:
    if not publish or not ctx.supports_progressive_observations:
        return
    body: dict[str, Any] = {"ip_v1": payload}
    if projection is not None:
        body["projection_v1"] = projection
    await ctx.append_observation(
        DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind=entity_kind,
            entity_key=entity_key,
            entity_version=entity_version,
            event_key=event_key,
            phase=phase,
            outcome=outcome,
            payload_schema_version="1.0",
            payload=body,
            observed_at=datetime.now(UTC),
        )
    )


def process_ip_discovery_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: Any,
    execution_mode: str,
    throttle: Any = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    connect: ConnectProbe | None = None,
    tcp_transport: TCPConnectTransport | None = None,
    import_loader: ImportLoader | None = None,
    identity_observer: IdentityObserver | None = None,
    nmap_dependencies: NmapProviderDependencies | None = None,
) -> Any:
    """Run an IP discovery sweep through the shared engine lifecycle.

    Mirrors the existing ``process_*_run`` processors: builds an
    :class:`EngineContext`, defines the engine coroutine, and hands both to
    :func:`run_engine`. The wiring agent calls this from the worker / inline
    fallback with a real ``run_store`` and a ``persist_records`` backed by
    DiscoveryRepository.replace_devices.

    Args:
        run_id, parameters, run_store, execution_mode, throttle, dry_run:
            standard processor inputs (throttle is a ``ThrottleConfig`` or None).
        persist_records: structured-record persister; defaults to a no-op.
        connect: legacy injectable boolean TCP probe retained for compatibility.
        tcp_transport: typed socket boundary used by the built-in provider.
        import_loader: accepted-row loader for the one frozen IP authority.
        identity_observer: optional approved-provider identity seam. The built-in
            provider leaves this unset and performs no identity traffic.
        nmap_dependencies: protected trust, process, and raw-evidence boundaries
            for the optional operator-managed Nmap provider. Dry-run needs none.

    Returns whatever ``run_store.update_run_status`` returns for the terminal
    status flip (the updated run record).
    """
    from smart_commissioning_core.engines.base import EngineContext as _Ctx
    from smart_commissioning_core.engines.base import ThrottleConfig as _ThrottleConfig

    is_cancelled = make_cancel_checker(run_store, run_id)
    ctx = _Ctx(
        run_id=run_id,
        parameters=dict(parameters or {}),
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle or _ThrottleConfig(),
        dry_run=dry_run,
        _is_cancelled=is_cancelled,
    )

    async def engine(engine_ctx: EngineContext) -> EngineResult:
        if connect is not None and tcp_transport is not None:
            raise ValueError("connect and tcp_transport are mutually exclusive")
        authority = _load_frozen_ip_authority(engine_ctx.parameters, import_loader)
        if (
            _ip_contract(engine_ctx.parameters).get("provider")
            == "operator_managed_nmap"
        ):
            return await run_nmap_provider(
                engine_ctx,
                authority=authority,
                dependencies=nmap_dependencies,
            )
        return await _run_ip_discovery(
            engine_ctx,
            authority=authority,
            identity_observer=identity_observer,
            legacy_probe=connect,
            tcp_transport=tcp_transport,
        )

    # persist_records None -> run_engine's own _noop_persister default.
    if persist_records is None:
        return run_engine(ctx, engine)
    return run_engine(ctx, engine, persist_records=persist_records)


async def _run_ip_discovery(
    ctx: EngineContext,
    *,
    authority: IPRegisterAuthorityV1 | None,
    identity_observer: IdentityObserver | None,
    legacy_probe: ConnectProbe | None,
    tcp_transport: TCPConnectTransport | None,
) -> EngineResult:
    """The engine body: expand targets, sweep (or plan), build results."""
    require_frozen_ip_provider_execution(
        ctx.parameters,
        # Owned production runs expose the progressive sink. Test and
        # historical result-only contexts still read old RunContextV1 payloads,
        # but never receive this live TCP dispatch privilege.
        strict_live=ctx.supports_progressive_observations and ctx.execution_mode != "test",
    )
    hosts = _expand_hosts(ctx.parameters)
    ports = _resolve_ports(ctx.parameters)
    forbidden_ports = _resolve_forbidden_ports(ctx.parameters)
    forbidden_by_address = _resolve_spec_map(ctx.parameters, "forbidden_ports_by_address")
    expected_by_address = _resolve_spec_map(ctx.parameters, "expected_ports_by_address")
    # Registered identity per host (Expected IP address -> Asset ID/name), filled
    # by the route from the IP register so the live "Asset" column resolves a
    # scanned host to its registered asset. A rogue / CIDR host absent from the
    # map stays None (honest blank), never a fabricated identity.
    asset_by_address = ctx.parameters.get("asset_id_by_address")
    if not isinstance(asset_by_address, dict):
        asset_by_address = {}
    # DRY RUN: enumerate the (ip, port) target list, perform NO I/O. The plan
    # uses the same per-host base-list ∪ register-ports union as the real sweep
    # so the preview shows what would genuinely be probed.
    if ctx.dry_run:
        targets = [
            {"ip": host, "port": port}
            for host in hosts
            for port in _host_probe_ports(
                ports,
                set(expected_by_address.get(host) or ()) | forbidden_by_address.get(host, set()),
            )[0]
        ]
        actions = [f"tcp-connect:{port}" for port in sorted({t["port"] for t in targets})]
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=targets,
            actions=actions,
            notes="No packets sent in dry run.",
            extra={"host_count": len(hosts), "port_count": len(ports)},
        )
        return EngineResult(
            result_summary_extra={
                "dry_run_plan": plan,
                "hosts_scanned": 0,
                "hosts_responsive": 0,
            }
        )

    # REAL SWEEP: authorization gates any actual socket I/O.
    require_scan_authorization(ctx.parameters)

    # Source-interface bind pre-check: if a source_ip was chosen, prove it can be
    # bound ONCE here rather than letting every per-host connect fail with an
    # OSError that _default_connect swallows as "port closed" — which would turn a
    # down/invalid NIC into a silent all-negative sweep (the worst failure mode).
    # A bind failure raises ValueError so run_engine records an honest terminal
    # failure instead of a bogus empty result.
    source_ip = ctx.parameters.get("source_ip") or None
    if source_ip:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
                probe_socket.bind((source_ip, 0))
        except OSError as error:
            raise ValueError(
                f"source interface {source_ip} is not available on this host"
            ) from error

    if ctx.supports_progressive_observations or tcp_transport is not None:
        return await _run_progressive_ip_sweep(
            ctx,
            hosts=hosts,
            ports=ports,
            forbidden_ports=forbidden_ports,
            forbidden_by_address=forbidden_by_address,
            expected_by_address=expected_by_address,
            asset_by_address=asset_by_address,
            authority=authority,
            identity_observer=identity_observer,
            legacy_probe=legacy_probe,
            tcp_transport=tcp_transport,
        )

    # Historical in-memory stores still exercise the original result-only
    # adapter. Production owned runs always expose the progressive sink above.
    # WHY: built-in U3 disables DNS/ARP, so only this legacy branch consumes it.
    raw_hostnames = ctx.parameters.get("expected_hostname_by_address")
    expected_hostname_by_address = {
        str(address): text
        for address, value in raw_hostnames.items()
        if (text := str(value).strip())
    } if isinstance(raw_hostnames, dict) else {}
    probe = legacy_probe or _make_default_connect(source_ip)
    throttle = Throttle(ctx.throttle)
    timeout = ctx.throttle.connect_timeout_s

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    project_id = ctx.parameters.get("project_id")
    site_id = ctx.parameters.get("site_id")

    hosts_scanned = 0
    hosts_responsive = 0
    hosts_with_forbidden = 0
    hosts_with_unexpected = 0
    hosts_with_missing_expected = 0
    hosts_with_hostname_mismatch = 0
    # Sweep host-by-host so cancellation can stop between hosts (and the
    # throttle stops between port dispatches within a host). Each host's ports
    # are dispatched as throttled units.
    for host in hosts:
        if ctx.is_cancelled():
            break
        hosts_scanned += 1

        # This host's probe list: the base list ∪ its register-declared ports
        # (expected + forbidden), so a register port outside the operator's
        # spec / defaults is genuinely connected to instead of only feeding the
        # flagging maps. Hosts absent from the register keep exactly the base
        # list; any register ports the per-host ceiling drops are reported in
        # status_detail below, never silently truncated.
        host_expected = expected_by_address.get(host)
        host_ports, dropped_register_ports = _host_probe_ports(
            ports, set(host_expected or ()) | forbidden_by_address.get(host, set())
        )

        def _factory(target_host: str, target_port: int) -> Callable[[], Awaitable[tuple[int, bool]]]:
            async def _probe_one() -> tuple[int, bool]:
                ok = await probe(target_host, target_port, timeout)
                return target_port, bool(ok)

            return _probe_one

        results = await throttle.run_throttled(
            [_factory(host, port) for port in host_ports], ctx
        )
        open_ports = sorted({port for port, ok in results if ok})
        if not open_ports:
            # A host that accepted no connection on any probed port. We still
            # emit a row — the operator asked to see EVERY scanned host, not
            # only the responders — but honestly: "no response on scanned ports"
            # is a TCP-connect miss, never proof the host is absent/offline.
            #
            # Cancellation honesty: if a cancel cut this host's port batch short,
            # fewer probes completed than were dispatched, so a "no response
            # (N probed)" row would claim a full ask that never happened. Skip
            # the row entirely in that case — a half-asked host gets no verdict.
            # The cheap length check is first so the common full-batch path never
            # pays for the (possibly store-backed) cancellation check.
            if len(results) < len(host_ports) and ctx.is_cancelled():
                continue
            detail = f"{NO_RESPONSE_DETAIL} ({len(results)} probed)"
            # Amber ONLY for register-expected silence (asset mapping or an
            # expected-ports declaration), mirroring the BACnet expected-but-
            # silent semantics; an unregistered silent host stays neutral so a
            # wide CIDR override sweep does not drown the table in amber.
            if asset_by_address.get(host) or host in expected_by_address:
                detail += (
                    f" | {MARKER_EXPECTED_BY_REGISTER}: expected from the register "
                    "import but did not answer this scan — inconclusive, not proof "
                    "the host is offline"
                )
            # No reverse-DNS / ARP for a silent host: no observation was made, a
            # blank is the honest value. last_seen_at stays None (never
            # seen -> no fabricated time). We do NOT stamp MISSING EXPECTED PORTS
            # on a fully silent host: that marker renders a red chip and would
            # over-claim a real closed-port finding on a plain TCP miss. Emit
            # into discovered_assets ONLY — no structured_record — so a host we
            # never heard from never enters the device inventory or reports.
            discovered_assets.append(
                {
                    "asset_id": asset_by_address.get(host),
                    "ip_address": host,
                    "mac_address": None,
                    "hostname": None,
                    "observed_ports": [],
                    "match_basis": "none",
                    "status_detail": detail,
                    "last_seen_at": None,
                }
            )
            continue
        hosts_responsive += 1

        hostname = None
        mac = None
        # Per-asset forbidden set wins for this host; else the global union.
        host_forbidden = forbidden_by_address.get(host, forbidden_ports)
        flagged = [port for port in open_ports if port in host_forbidden]
        # Open ports the asset's "Expected services/ports" did not list. Only
        # checked when the host has a registered expected set.
        unexpected = [port for port in open_ports if port not in host_expected] if host_expected is not None else []
        # This compatibility adapter receives only a Boolean. False does not
        # distinguish a refusal from a timeout or routing failure, so it cannot
        # prove that an expected port is closed. The typed provider below emits
        # ``connection_refused`` and owns the missing-service verdict.
        open_set = set(open_ports)
        # Only ports ACTUALLY probed can be verdicted. On a mid-batch Stop,
        # run_throttled returns fewer results than host_ports; verdicting against
        # the planned list would fabricate "missing expected" for never-probed ports
        # and over-claim scanned_ports. On a full batch this equals host_ports, so
        # the happy path is byte-identical (results preserve dispatch order).
        probed_ports = [port for port, _ok in results]
        missing_expected: list[int] = []
        # An explicit PASS: every register-expected port is open (which also
        # means none were cap-dropped) and no forbidden/unexpected verdict
        # fired — a clean host is a recorded decision, not silence.
        expected_ports_ok = bool(host_expected) and host_expected <= open_set and not flagged and not unexpected
        # Reverse-DNS name vs the register's "Expected hostname" — WARNING-ONLY:
        # a blank on EITHER side (no PTR record, site DNS not configured,
        # reverse_dns disabled, host absent from the register) is never a
        # mismatch, because commissioning networks often run without DNS.
        expected_hostname = expected_hostname_by_address.get(host)
        hostname_mismatch = bool(
            hostname
            and expected_hostname
            and _short_hostname(hostname) != _short_hostname(expected_hostname)
        )
        status_detail = "responsive: " + ",".join(str(p) for p in open_ports)
        if flagged:
            status_detail += " | FORBIDDEN PORTS OPEN: " + ",".join(str(p) for p in flagged)
            hosts_with_forbidden += 1
        if unexpected:
            status_detail += " | UNEXPECTED PORTS OPEN: " + ",".join(str(p) for p in unexpected)
            hosts_with_unexpected += 1
        if missing_expected:
            status_detail += " | MISSING EXPECTED PORTS: " + ",".join(str(p) for p in missing_expected)
            hosts_with_missing_expected += 1
        if expected_ports_ok:
            status_detail += f" | EXPECTED PORTS OK: {len(host_expected or ())}/{len(host_expected or ())} open"
        if dropped_register_ports:
            status_detail += " | PROBE LIST CAPPED: register ports not probed: " + ",".join(
                str(p) for p in dropped_register_ports
            )
        if hostname_mismatch:
            status_detail += f" | HOSTNAME MISMATCH: expected {expected_hostname}, got {hostname}"
            hosts_with_hostname_mismatch += 1
        discovered_assets.append(
            {
                "asset_id": asset_by_address.get(host),
                "ip_address": host,
                "mac_address": mac,
                "hostname": hostname,
                "observed_ports": _build_observed_ports(open_ports),
                "match_basis": "ip",
                "status_detail": status_detail,
                "last_seen_at": datetime.now(UTC).isoformat(),
            }
        )
        structured_records.append(
            {
                "project_id": project_id,
                "site_id": site_id,
                "address": host,
                "device_type": "ip_host",
                "name": hostname,
                "attributes": {
                    "open_ports": open_ports,
                    "scanned_ports": probed_ports,
                    "scanned_port_count": len(probed_ports),
                    # The register's declarations for THIS host, persisted next
                    # to the observation so the verdict is auditable. None =
                    # host has no registered expectation (honest blank);
                    # forbidden_ports is the set actually enforced (the host's
                    # own register set, else the global forbidden list).
                    "expected_ports": sorted(host_expected) if host_expected is not None else None,
                    "forbidden_ports": sorted(host_forbidden),
                    "forbidden_open_ports": flagged,
                    "unexpected_open_ports": unexpected,
                    "missing_expected_ports": missing_expected,
                    "register_ports_not_probed": dropped_register_ports,
                    "hostname": hostname,
                    "expected_hostname": expected_hostname,
                    "mac_address": mac,
                },
            }
        )

    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        result_summary_extra={
            "hosts_scanned": hosts_scanned,
            # discovered_assets now includes silent (non-responder) rows, so the
            # responsive count comes from the dedicated counter, not len(assets).
            "hosts_responsive": hosts_responsive,
            "ports_scanned": list(ports),
            "forbidden_ports": sorted(forbidden_ports),
            "hosts_with_forbidden_open": hosts_with_forbidden,
            "hosts_with_unexpected_open": hosts_with_unexpected,
            "hosts_with_missing_expected": hosts_with_missing_expected,
            "hosts_with_hostname_mismatch": hosts_with_hostname_mismatch,
        },
    )


async def _run_progressive_ip_sweep(
    ctx: EngineContext,
    *,
    hosts: list[str],
    ports: list[int],
    forbidden_ports: set[int],
    forbidden_by_address: dict[str, set[int]],
    expected_by_address: dict[str, set[int]],
    asset_by_address: dict[str, Any],
    authority: IPRegisterAuthorityV1 | None,
    identity_observer: IdentityObserver | None,
    legacy_probe: ConnectProbe | None,
    tcp_transport: TCPConnectTransport | None,
) -> EngineResult:
    """Run the U3 typed provider and publish a durable staged trace."""

    throttle = Throttle(ctx.throttle)
    largest_host_plan = max(
        (
            len(
                _host_probe_ports(
                    ports,
                    set(expected_by_address.get(host) or ())
                    | forbidden_by_address.get(host, set()),
                )[0]
            )
            for host in hosts
        ),
        default=len(ports),
    )
    policy = _runtime_policy(
        ctx.parameters,
        host_count=len(hosts),
        port_count=largest_host_plan,
        throttle=throttle,
    )
    provenance = _observation_provenance(
        ctx.parameters,
        profile=policy.profile,
        hosts=hosts,
        ports=ports,
    )
    raw_omissions = ctx.parameters.get("not_attempted_ports_by_address")
    omissions_by_address = raw_omissions if isinstance(raw_omissions, dict) else {}
    source_ip = ctx.parameters.get("source_ip") or None
    project_id = ctx.parameters.get("project_id")
    site_id = ctx.parameters.get("site_id")

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    metric_states: list[IPHostMetricStateV1] = []
    counters = {
        "attempts": 0,
        "responsive": 0,
        "forbidden": 0,
        "unexpected": 0,
        "missing": 0,
    }
    hosts_scanned = 0
    terminal_control_reason: str | None = None
    cutoff_evidence_recorded = False

    async def probe_port(
        *,
        host: str,
        port: int,
        target_gate: asyncio.Semaphore,
        pacing_lock: asyncio.Lock,
        next_dispatch_at: list[float],
    ) -> _PortProbeResult:
        last_result: TCPPortObservationV1 | None = None
        last_packet: datetime | None = None
        attempts = 0
        events: list[_PortAttemptEvent] = []

        def stopped_result(*, entity_version: int) -> _PortProbeResult:
            cancelled = _cancelled_port_observation(host=host, port=port)
            events.append(
                _PortAttemptEvent(
                    observation=cancelled,
                    entity_version=max(1, entity_version),
                    attempts=attempts,
                    elapsed_ms=0.0,
                    last_packet_dispatched_at=last_packet,
                )
            )
            return _PortProbeResult(
                cancelled,
                attempts,
                last_packet,
                tuple(events),
                "stop_requested",
            )

        for attempt in range(1, policy.retries + 2):
            if attempt > 1 and policy.retry_backoff_s:
                if not await _interruptible_sleep(ctx, policy.retry_backoff_s):
                    return stopped_result(entity_version=attempt)
            if ctx.is_cancelled():
                return stopped_result(entity_version=attempt)

            started = asyncio.get_running_loop().time()
            async with target_gate:
                async with throttle.slot():
                    async with pacing_lock:
                        now = asyncio.get_running_loop().time()
                        delay = next_dispatch_at[0] - now
                        if delay > 0:
                            if not await _interruptible_sleep(ctx, delay):
                                return stopped_result(entity_version=attempt)
                        # The throttle gate ran before the pacing wait. Recheck
                        # control after that wait so expiry or Stop cannot use
                        # the gap to dispatch one more packet.
                        ctx.require_dispatch_allowed()
                        if not ctx.is_cancelled():
                            next_dispatch_at[0] = (
                                asyncio.get_running_loop().time()
                                + policy.min_target_spacing_s
                            )
                    if ctx.is_cancelled():
                        return stopped_result(entity_version=attempt)
                    def record_dispatch() -> None:
                        nonlocal attempts, last_packet
                        if counters["attempts"] >= policy.attempt_ceiling:
                            raise ValueError(
                                "frozen IP dispatch attempt ceiling was exhausted"
                            )
                        counters["attempts"] += 1
                        attempts += 1
                        last_packet = datetime.now(UTC)

                    if legacy_probe is not None:
                        record_dispatch()
                        connected = await legacy_probe(
                            host,
                            port,
                            ctx.throttle.connect_timeout_s,
                        )
                        result = _legacy_probe_observation(
                            host=host,
                            port=port,
                            connected=bool(connected),
                        )
                    else:
                        result = await probe_tcp_connect(
                            TCPProbeRequestV1(
                                target_ip=host,
                                port=port,
                                protocol="tcp",
                                source_ip=source_ip,
                                application_deadline_s=ctx.throttle.connect_timeout_s,
                            ),
                            transport=_DispatchRecordingTransport(
                                tcp_transport or AsyncioTCPConnectTransport(),
                                record_dispatch,
                            ),
                            is_cancelled=ctx.is_cancelled,
                        )
            elapsed_ms = max(
                0.0,
                (asyncio.get_running_loop().time() - started) * 1000.0,
            )
            last_result = result
            events.append(
                _PortAttemptEvent(
                    observation=result,
                    entity_version=attempt,
                    attempts=attempts,
                    elapsed_ms=elapsed_ms,
                    last_packet_dispatched_at=last_packet,
                )
            )
            if result.outcome not in _RETRYABLE_OUTCOMES or attempt > policy.retries:
                return _PortProbeResult(
                    result,
                    attempts,
                    last_packet,
                    tuple(events),
                    (
                        "stop_requested"
                        if result.outcome == "cancelled"
                        else None
                    ),
                )

        if last_result is None:  # pragma: no cover - loop always runs once
            raise RuntimeError("TCP probe produced no result")
        return _PortProbeResult(
            last_result,
            attempts,
            last_packet,
            tuple(events),
        )

    for position, host in enumerate(hosts):
        host_cutoff_reason = terminal_control_reason
        if host_cutoff_reason is None and ctx.is_cancelled():
            host_cutoff_reason = "stop_requested"
            terminal_control_reason = host_cutoff_reason
        if (
            host_cutoff_reason is None
            and position
            and policy.min_target_spacing_s
        ):
            if not await _interruptible_sleep(ctx, policy.min_target_spacing_s):
                host_cutoff_reason = "stop_requested"
                terminal_control_reason = host_cutoff_reason

        publish_host_observations = (
            host_cutoff_reason is None or not cutoff_evidence_recorded
        )
        if host_cutoff_reason is not None:
            cutoff_evidence_recorded = True

        host_started = asyncio.get_running_loop().time()
        host_expected = expected_by_address.get(host)
        host_forbidden = forbidden_by_address.get(host, forbidden_ports)
        comparison = compare_ip_register(
            IPHostComparisonInputV1(
                observed_ip=host,
                reachability_state="pending",
            ),
            authority=authority,
        )
        register_match = comparison.register_match
        host_key = f"host:{host}"
        await _append_ip_observation(
            ctx,
            entity_kind="host",
            entity_key=host_key,
            entity_version=1,
            event_key=f"host:{host}:planned",
            phase="planned",
            outcome="planned",
            payload=_ip_payload(
                target=host,
                provenance=provenance,
                coverage_state=(
                    "not_attempted" if host_cutoff_reason else "pending"
                ),
                reachability_state=(
                    "not_applicable" if host_cutoff_reason else "pending"
                ),
                register_match=register_match,
                policy_verdict=(
                    "not_attempted" if host_cutoff_reason else "pending"
                ),
                attempts=0,
                elapsed_ms=0.0,
                reason="The IP target is in the frozen packet plan.",
                control_reason=host_cutoff_reason,
            ),
            publish=publish_host_observations,
        )

        omitted: dict[tuple[str, int], dict[str, Any]] = {}
        raw_host_omissions = omissions_by_address.get(host)
        if isinstance(raw_host_omissions, list):
            for raw in raw_host_omissions:
                if not isinstance(raw, dict):
                    continue
                protocol = str(raw.get("protocol") or "")
                try:
                    omitted_port = int(raw.get("port"))
                except (TypeError, ValueError):
                    continue
                omitted.setdefault((protocol, omitted_port), raw)
        for (protocol, omitted_port), raw in sorted(omitted.items()):
            reason = str(raw.get("reason") or "not_attempted")
            capability = raw.get("capability")
            if isinstance(capability, str):
                reason = f"{reason}: {capability}"
            await _append_ip_observation(
                ctx,
                entity_kind="port",
                entity_key=f"port:{host}:{omitted_port}:{protocol}",
                entity_version=1,
                event_key=f"port:{host}:{omitted_port}:{protocol}:v1",
                phase="planned",
                outcome="not_attempted",
                payload=_ip_payload(
                    target=host,
                    provenance=provenance,
                    coverage_state="not_attempted",
                    reachability_state="not_applicable",
                    register_match=register_match,
                    policy_verdict="not_attempted",
                    attempts=0,
                    elapsed_ms=0.0,
                    port=omitted_port,
                    transport=protocol,
                    reason=reason,
                    capability_action=(
                        "use_bacnet_discovery"
                        if capability == "use_bacnet_discovery"
                        else None
                    ),
                ),
                publish=publish_host_observations,
            )

        await _append_ip_observation(
            ctx,
            entity_kind="host",
            entity_key=host_key,
            entity_version=2,
            event_key=f"host:{host}:reachability",
            phase="reachability",
            outcome="not_attempted" if host_cutoff_reason else "started",
            payload=_ip_payload(
                target=host,
                provenance=provenance,
                coverage_state=(
                    "not_attempted" if host_cutoff_reason else "pending"
                ),
                reachability_state=(
                    "not_applicable" if host_cutoff_reason else "pending"
                ),
                register_match=register_match,
                policy_verdict=(
                    "not_attempted" if host_cutoff_reason else "pending"
                ),
                attempts=0,
                elapsed_ms=0.0,
                reason=(
                    "TCP reachability probing was not dispatched because active "
                    "control was lost."
                    if host_cutoff_reason
                    else "TCP reachability probing started."
                ),
                control_reason=host_cutoff_reason,
            ),
            publish=publish_host_observations,
        )

        host_ports, _legacy_dropped = _host_probe_ports(
            ports,
            set(host_expected or ()) | forbidden_by_address.get(host, set()),
        )
        control_loss: _IPControlLost | None = None
        if host_cutoff_reason is not None:
            port_results = [
                _not_attempted_port_result(
                    host=host,
                    port=port,
                    control_reason=host_cutoff_reason,
                )
                for port in host_ports
            ]
        else:
            target_gate = asyncio.Semaphore(policy.per_target_concurrency)
            pacing_lock = asyncio.Lock()
            next_dispatch_at = [0.0]
            tasks = [
                asyncio.create_task(
                    probe_port(
                        host=host,
                        port=port,
                        target_gate=target_gate,
                        pacing_lock=pacing_lock,
                        next_dispatch_at=next_dispatch_at,
                    )
                )
                for port in host_ports
            ]
            try:
                port_results = list(await asyncio.gather(*tasks))
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            except Exception as error:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if isinstance(error, ValueError):
                    raise
                control_loss = _IPControlLost(_control_reason(error, ctx))
                completed_by_port: dict[int, _PortProbeResult] = {}
                for port, task in zip(host_ports, tasks, strict=True):
                    if task.cancelled():
                        continue
                    task_error = task.exception()
                    if task_error is None:
                        completed = task.result()
                        if completed.observation.outcome == "cancelled":
                            completed = _PortProbeResult(
                                observation=completed.observation,
                                attempts=completed.attempts,
                                last_packet_dispatched_at=(
                                    completed.last_packet_dispatched_at
                                ),
                                events=completed.events,
                                control_reason=control_loss.reason,
                            )
                        completed_by_port[port] = completed
                port_results = [
                    completed_by_port.get(port)
                    or _not_attempted_port_result(
                        host=host,
                        port=port,
                        control_reason=control_loss.reason,
                    )
                    for port in host_ports
                ]

        # The final connect can outlive its pre-dispatch authorization read.
        # Recheck control after every in-flight probe has drained, before we
        # publish comparison/finalization evidence.  Repository finalization
        # repeats this fence atomically, because revocation can still race this
        # read and the terminal commit.
        if control_loss is None:
            try:
                ctx.require_active_control()
            except Exception as error:
                control_loss = _IPControlLost(_control_reason(error, ctx))

        if control_loss is not None:
            terminal_control_reason = control_loss.reason
            await _append_ip_observation(
                ctx,
                entity_kind="diagnostic",
                entity_key=f"diagnostic:{host}:control",
                entity_version=1,
                event_key=f"diagnostic:{host}:control:v1",
                phase="finalize",
                outcome="control_lost",
                payload=_ip_payload(
                    target=host,
                    provenance=provenance,
                    coverage_state=(
                        "cancelled"
                        if control_loss.reason == "stop_requested"
                        else "not_attempted"
                    ),
                    reachability_state="not_applicable",
                    register_match=register_match,
                    policy_verdict="not_attempted",
                    attempts=sum(item.attempts for item in port_results),
                    elapsed_ms=max(
                        0.0,
                        (asyncio.get_running_loop().time() - host_started)
                        * 1000.0,
                    ),
                    reason="Active control was lost before the packet plan completed.",
                    control_reason=control_loss.reason,
                    last_packet_dispatched_at=max(
                        (
                            item.last_packet_dispatched_at
                            for item in port_results
                            if item.last_packet_dispatched_at is not None
                        ),
                        default=None,
                    ),
                ),
                publish=publish_host_observations,
            )
        elif any(
            item.control_reason == "stop_requested" for item in port_results
        ):
            terminal_control_reason = "stop_requested"

        # Persist in frozen port order, not socket-completion order. Cursor
        # order is transport order, so stable insertion also keeps the sealed
        # stream digest deterministic across equivalent concurrent runs.
        for item in port_results:
            for event in item.events:
                # The terminal projection below retains every frozen port and
                # its honest not-attempted result.  Do not make a stopped run
                # await one durable write per untouched port: that turns a
                # 4,096-port Stop into seconds of post-Stop backlog. Completed
                # probe attempts remain durable evidence.
                if terminal_control_reason is not None and event.attempts == 0:
                    continue
                observation = event.observation
                was_dispatched = event.attempts > 0
                verdict = (
                    _policy_verdict(
                        observation.outcome,
                        port=observation.port,
                        expected=host_expected,
                        forbidden=host_forbidden,
                    )
                    if was_dispatched
                    else "not_attempted"
                )
                await _append_ip_observation(
                    ctx,
                    entity_kind="port",
                    entity_key=(
                        f"port:{host}:{observation.port}:{observation.protocol}"
                    ),
                    entity_version=event.entity_version,
                    event_key=(
                        f"port:{host}:{observation.port}:"
                        f"{observation.protocol}:v{event.entity_version}"
                    ),
                    phase="reachability",
                    outcome=(
                        observation.outcome if was_dispatched else "not_attempted"
                    ),
                    payload=_ip_payload(
                        target=host,
                        provenance=provenance,
                        coverage_state=(
                            "not_attempted"
                            if not was_dispatched
                            else "cancelled"
                            if observation.outcome == "cancelled"
                            else "attempted"
                        ),
                        reachability_state=(
                            _reachability_for(observation.outcome)
                            if was_dispatched
                            else "not_applicable"
                        ),
                        register_match=register_match,
                        policy_verdict=verdict,
                        attempts=event.attempts,
                        elapsed_ms=event.elapsed_ms,
                        probe=observation if was_dispatched else None,
                        port=observation.port if not was_dispatched else None,
                        transport=(
                            observation.protocol if not was_dispatched else None
                        ),
                        reason=(
                            "No TCP packet was dispatched because active control "
                            "was lost."
                            if not was_dispatched
                            else None
                        ),
                        control_reason=(
                            item.control_reason
                            if observation.outcome == "cancelled"
                            else None
                        ),
                        last_packet_dispatched_at=(
                            event.last_packet_dispatched_at
                        ),
                    ),
                    publish=publish_host_observations,
                )

        attempted = [item for item in port_results if item.attempts > 0]
        if attempted:
            hosts_scanned += 1
        host_cancelled = any(
            item.observation.outcome == "cancelled"
            and item.control_reason == "stop_requested"
            for item in port_results
        ) or (control_loss is not None and control_loss.reason == "stop_requested")
        if not attempted:
            host_coverage = "not_attempted"
        elif host_cancelled:
            host_coverage = "cancelled"
        else:
            host_coverage = "attempted"
        host_control_reason = (
            control_loss.reason
            if control_loss is not None
            else host_cutoff_reason
            or next(
                (
                    item.control_reason
                    for item in port_results
                    if item.control_reason is not None
                ),
                None,
            )
        )
        open_ports = sorted(
            item.observation.port
            for item in attempted
            if item.observation.outcome == "connected"
        )
        reachable = any(
            item.observation.outcome in _REACHABLE_OUTCOMES for item in attempted
        )
        reachability_state: IPComparisonReachabilityV1 = (
            "reachable"
            if reachable
            else "unconfirmed"
            if attempted
            else "not_applicable"
        )
        identity_evidence: ProviderIdentityEvidenceV1 | None = None
        comparison = compare_ip_register(
            IPHostComparisonInputV1(
                observed_ip=host,
                reachability_state=reachability_state,
            ),
            authority=authority,
        )
        if (
            reachable
            and authority is not None
            and identity_observer is not None
            and comparison.register_match != "expected_match"
        ):
            identity_evidence = await identity_observer(host)
            comparison = compare_ip_register(
                IPHostComparisonInputV1(
                    observed_ip=host,
                    reachability_state=reachability_state,
                    identity_evidence=identity_evidence,
                ),
                authority=authority,
            )
        register_match = comparison.register_match
        matched_row = _matched_authority_row(authority, comparison)
        if reachable:
            counters["responsive"] += 1
        probed_ports = [item.observation.port for item in attempted]
        refused_ports = {
            item.observation.port
            for item in attempted
            if item.observation.outcome == "connection_refused"
        }
        flagged = [port for port in open_ports if port in host_forbidden]
        unexpected = (
            [port for port in open_ports if port not in host_expected]
            if host_expected is not None
            else []
        )
        missing_expected = (
            sorted(port for port in host_expected if port in refused_ports)
            if host_expected is not None
            else []
        )
        if flagged:
            counters["forbidden"] += 1
        if unexpected:
            counters["unexpected"] += 1
        if missing_expected:
            counters["missing"] += 1

        port_observations = []
        for item in port_results:
            observation = item.observation
            was_dispatched = item.attempts > 0
            port_observations.append(
                {
                    "schema_version": "1.0",
                    "coverage_state": (
                        "not_attempted"
                        if not was_dispatched
                        else "cancelled"
                        if observation.outcome == "cancelled"
                        else "attempted"
                    ),
                    "reachability_state": (
                        _reachability_for(observation.outcome)
                        if was_dispatched
                        else "not_applicable"
                    ),
                    "probe_outcome": (
                        observation.outcome if was_dispatched else None
                    ),
                    "register_match": register_match,
                    "policy_verdict": (
                        _policy_verdict(
                            observation.outcome,
                            port=observation.port,
                            expected=host_expected,
                            forbidden=host_forbidden,
                        )
                        if was_dispatched
                        else "not_attempted"
                    ),
                    "target": host,
                    "port": observation.port,
                    "protocol": observation.protocol,
                    "transport": observation.protocol,
                    "provider": observation.provider,
                    "provider_version": _PROVIDER_VERSION,
                    "provider_contract_version": observation.provider_contract_version,
                    "provenance": provenance,
                    "reason": (
                        observation.diagnostic.reason
                        if was_dispatched
                        else "No TCP packet was dispatched because active control "
                        "was lost."
                    ),
                    "attempts": item.attempts,
                    "elapsed_ms": sum(event.elapsed_ms for event in item.events),
                    "port_hint": observation.port_hint,
                    "detected_service": observation.detected_service,
                    "detected_version": observation.detected_version,
                    "control_reason": (
                        item.control_reason
                        if observation.outcome == "cancelled"
                        else None
                    ),
                    "last_packet_dispatched_at": (
                        item.last_packet_dispatched_at.isoformat()
                        if item.last_packet_dispatched_at is not None
                        else None
                    ),
                    "service": observation.port_hint,
                }
            )

        if not attempted:
            host_verdict = "not_attempted"
        elif flagged:
            host_verdict = "forbidden_open"
        elif unexpected:
            host_verdict = "unexpected_open_review"
        elif missing_expected:
            host_verdict = "expected_closed"
        elif not reachable:
            host_verdict = "unconfirmed"
        else:
            host_verdict = "pass"

        if not attempted:
            status_base = "not attempted because active control was lost"
        elif reachable:
            status_base = "reachable: TCP connection refused on probed ports"
        else:
            status_base = f"{NO_RESPONSE_DETAIL} ({len(attempted)} probed)"
            if asset_by_address.get(host) or host in expected_by_address:
                status_base += (
                    f" | {MARKER_EXPECTED_BY_REGISTER}: expected from the register "
                    "import but did not answer this scan - inconclusive, not proof "
                    "the host is offline"
                )
        status_suffixes: list[str] = []
        if comparison.register_match == "wrong_ip_review":
            status_suffixes.append(
                " | WRONG IP REVIEW: expected "
                f"{comparison.expected_ip}, observed {comparison.observed_ip}"
            )
        elif comparison.register_match == "ambiguous_review":
            status_suffixes.append(" | AMBIGUOUS REGISTER IDENTITY REVIEW")
        status_detail = format_ip_status_detail(
            status_base,
            responsive_ports=open_ports if attempted else (),
            forbidden_open_ports=flagged,
            unexpected_open_ports=unexpected,
            missing_expected_ports=missing_expected,
            suffixes=status_suffixes,
        )

        last_packet = max(
            (
                item.last_packet_dispatched_at
                for item in attempted
                if item.last_packet_dispatched_at is not None
            ),
            default=None,
        )
        asset: dict[str, Any] = {
            "schema_version": "1.0",
            "coverage_state": host_coverage,
            "reachability_state": reachability_state,
            "probe_outcome": None,
            "register_match": register_match,
            "policy_verdict": host_verdict,
            "target": host,
            "transport": None,
            "provider": _PROVIDER_NAME,
            "provider_version": _PROVIDER_VERSION,
            "provider_contract_version": "1.0",
            "provenance": provenance,
            "reason": status_detail,
            "attempts": sum(item.attempts for item in port_results),
            "elapsed_ms": max(
                0.0,
                (asyncio.get_running_loop().time() - host_started) * 1000.0,
            ),
            "port_hint": None,
            "detected_service": None,
            "detected_version": None,
            "control_reason": host_control_reason,
            "last_packet_dispatched_at": (
                last_packet.isoformat() if last_packet is not None else None
            ),
            "asset_id": (
                matched_row.asset_id
                if matched_row is not None and matched_row.asset_id is not None
                else asset_by_address.get(host)
            ),
            "ip_address": host,
            "observed_ip": comparison.observed_ip,
            "expected_ip": comparison.expected_ip,
            "expected_ips": list(comparison.expected_ips),
            "register_address": comparison.expected_ip,
            "matched_device_key": comparison.matched_device_key,
            "candidate_device_keys": list(comparison.candidate_device_keys),
            "comparison_reason": comparison.reason_code,
            "mac_address": (
                identity_evidence.mac_address
                if identity_evidence is not None
                else None
            ),
            "hostname": (
                identity_evidence.hostname
                if identity_evidence is not None
                else None
            ),
            "observed_ports": _build_observed_ports(open_ports),
            "port_observations": port_observations,
            "missing_expected_ports": missing_expected,
            "match_basis": "+".join(comparison.match_basis) or "none",
            "status_detail": status_detail,
            "last_seen_at": datetime.now(UTC).isoformat() if reachable else None,
        }
        discovered_assets.append(asset)
        metric_states.append(
            IPHostMetricStateV1(
                target_ip=host,
                entity_version=4,
                lifecycle_state=(
                    "not_attempted"
                    if not attempted
                    else "cancelled"
                    if host_control_reason is not None or host_cancelled
                    else "finalized"
                ),
                probe_outcomes=tuple(
                    item.observation.outcome for item in attempted
                ),
                comparison=comparison,
            )
        )
        await _append_ip_observation(
            ctx,
            entity_kind="host",
            entity_key=host_key,
            entity_version=3,
            event_key=f"host:{host}:comparison",
            phase="comparison",
            outcome="not_attempted" if not attempted else "evaluated",
            payload=_ip_payload(
                target=host,
                provenance=provenance,
                coverage_state=host_coverage,
                reachability_state=reachability_state,
                register_match=register_match,
                policy_verdict=host_verdict,
                attempts=sum(item.attempts for item in port_results),
                elapsed_ms=asset["elapsed_ms"],
                reason=status_detail,
                control_reason=host_control_reason,
                last_packet_dispatched_at=last_packet,
            ),
            publish=publish_host_observations,
        )

        device_attributes = {
            "asset_id": asset["asset_id"],
            "open_ports": open_ports,
            "scanned_ports": probed_ports,
            "scanned_port_count": len(probed_ports),
            "expected_ports": (
                sorted(host_expected) if host_expected is not None else None
            ),
            "forbidden_ports": sorted(host_forbidden),
            "forbidden_open_ports": flagged,
            "unexpected_open_ports": unexpected,
            "missing_expected_ports": missing_expected,
            "register_ports_not_probed": sorted(
                omitted_port for _protocol, omitted_port in omitted
            ),
            "hostname": asset["hostname"],
            "expected_hostname": (
                matched_row.hostname if matched_row is not None else None
            ),
            "mac_address": asset["mac_address"],
            "register_match": register_match,
            "reachable": reachable,
            "position": position,
        }
        if comparison.expected_ip is not None:
            device_attributes["register_address"] = comparison.expected_ip
        if matched_row is not None and matched_row.asset_id is not None:
            device_attributes["register_asset_id"] = matched_row.asset_id
        if matched_row is not None and matched_row.asset_name is not None:
            device_attributes["register_asset_name"] = matched_row.asset_name

        device_record = {
            "project_id": project_id,
            "site_id": site_id,
            "address": host,
            "device_type": "ip_host",
            "name": asset["hostname"],
            "attributes": device_attributes,
        }
        projection = None
        if reachable:
            structured_records.append(device_record)
            projection = {
                "collection": "devices",
                "position": position,
                "present": True,
                "record": device_record,
            }
        await _append_ip_observation(
            ctx,
            entity_kind="host",
            entity_key=host_key,
            entity_version=4,
            event_key=f"host:{host}:finalize",
            phase="finalize",
            outcome=(
                "not_attempted"
                if not attempted
                else "cancelled"
                if host_cancelled
                else "observed"
            ),
            payload=_ip_payload(
                target=host,
                provenance=provenance,
                coverage_state=host_coverage,
                reachability_state=reachability_state,
                register_match=register_match,
                policy_verdict=host_verdict,
                attempts=sum(item.attempts for item in port_results),
                elapsed_ms=asset["elapsed_ms"],
                reason=status_detail,
                control_reason=host_control_reason,
                last_packet_dispatched_at=last_packet,
            ),
            projection=projection,
            publish=publish_host_observations,
        )
        if terminal_control_reason is not None:
            cutoff_evidence_recorded = True

    last_packet_dispatched_at = max(
        (
            asset["last_packet_dispatched_at"]
            for asset in discovered_assets
            if asset.get("last_packet_dispatched_at") is not None
        ),
        default=None,
    )
    terminal_status = None
    terminal_error = None
    if terminal_control_reason is not None:
        terminal_status = (
            "cancelled" if terminal_control_reason == "stop_requested" else "failed"
        )
        if terminal_status == "failed":
            terminal_error = "Discovery stopped because active control was lost."
    headline_metrics = reduce_ip_headline_metrics(
        IPHeadlineMetricScopeV1(
            frozen_targets=tuple(hosts),
            authority=authority,
        ),
        tuple(metric_states),
    )
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=(
            [] if ctx.supports_progressive_observations else structured_records
        ),
        result_summary_extra={
            "hosts_scanned": hosts_scanned,
            "hosts_responsive": counters["responsive"],
            "ports_scanned": list(ports),
            "forbidden_ports": sorted(forbidden_ports),
            "hosts_with_forbidden_open": counters["forbidden"],
            "hosts_with_unexpected_open": counters["unexpected"],
            "hosts_with_missing_expected": counters["missing"],
            "hosts_with_hostname_mismatch": 0,
            "dispatch_attempts": counters["attempts"],
            "not_attempted_ports_by_address": omissions_by_address,
            "provider": _PROVIDER_NAME,
            "provider_version": _PROVIDER_VERSION,
            "control_reason": terminal_control_reason,
            "last_packet_dispatched_at": last_packet_dispatched_at,
            "ip_headline_metrics_v1": headline_metrics.model_dump(mode="json"),
        },
        status_override=terminal_status,
        error_message=terminal_error,
    )
