"""IP discovery engine: an authorized, throttled TCP-connect host sweep.

What this engine does
---------------------
Given an IP target spec (a CIDR block, or an inclusive ``start``/``end`` range)
plus a small list of TCP ports, it attempts a plain ``asyncio`` TCP connect to
each ``(ip, port)`` pair under the shared :class:`~smart_commissioning_core.engines.base.Throttle`.
A host is considered *present* if ANY of its scanned ports accepts a connection
(connect-success liveness — we never send ICMP and never send any application
payload). For each responsive host we emit:

* a ``discovered_assets`` entry in the ``DiscoveryAssetObservation`` shape the
  API's ``DiscoveryResultsResponse`` reads from ``result_summary["discovered_assets"]``
  (``ip_address``, ``hostname``, ``observed_ports=[{port,protocol,service}]``,
  ``match_basis``, ``status_detail``), and
* a structured ``DiscoveredDevice`` row (``device_type="ip_host"``) for the
  DiscoveryRepository, with the open-port detail under ``attributes``.

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
network: discovery of live BACnet (47808) / Modbus (502) / BMS hosts, reverse
DNS against a site resolver, and behaviour against firewalls / rate-limited
switch fabrics. Those paths are listed in the task's ``live_untested`` output
and must be validated on site against the actual VLAN.

The transport is dependency-injected (``connect`` parameter) exactly so the
tests can drive it without real sockets when they want to; the production
default uses ``asyncio.open_connection`` against real addresses.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    Throttle,
    make_cancel_checker,
    run_engine,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)

# Default ports. 47808=BACnet/IP, 1883=MQTT, 502=Modbus, 80/443=web mgmt UIs.
DEFAULT_PORTS: tuple[int, ...] = (80, 443, 47808, 1883, 502)

# Hard ceiling on how many hosts a single sweep may expand to. This is the
# operator policy cap: a request's ``max_hosts`` may LOWER it but can never
# raise it above this value, so a fat CIDR can never explode into millions of
# connects regardless of request parameters.
MAX_HOSTS_CEILING = 4096

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

# A connect probe: open a TCP connection to (host, port) within ``timeout`` and
# return True iff it succeeds. Injectable so tests can avoid real sockets.
ConnectProbe = Callable[[str, int, float], Awaitable[bool]]


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
        connect = asyncio.open_connection(host, port)
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
    if cidr:
        if not isinstance(cidr, str):
            raise ValueError("cidr must be a string")
        network = ipaddress.ip_network(cidr.strip(), strict=False)
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
    # Bound the sweep so a fat CIDR can never explode into millions of connects.
    # MAX_HOSTS_CEILING is a hard operator cap: a request may LOWER max_hosts but
    # can never raise it above the ceiling (a request must not be able to widen
    # the blast radius beyond policy).
    requested_max_hosts = _positive_int(parameters.get("max_hosts"), default=MAX_HOSTS_CEILING)
    max_hosts = min(requested_max_hosts, MAX_HOSTS_CEILING)
    if len(hosts) > max_hosts:
        raise ValueError(
            f"IP discovery target spec expands to {len(hosts)} hosts, "
            f"exceeding max_hosts={max_hosts}."
        )
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            ordered.append(host)
    return ordered


def _resolve_ports(parameters: dict[str, Any]) -> list[int]:
    raw = parameters.get("ports")
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
        raise ValueError("ports list must not be empty.")
    return ports


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _reverse_lookup(ip: str) -> str | None:
    """Best-effort synchronous reverse DNS. Returns None on any failure.

    Only used when the caller explicitly enables ``reverse_dns``. Real reverse
    DNS hits the site resolver and is therefore part of the on-site-validation
    surface; in tests it is monkeypatched / disabled.
    """
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(ip)
        return host or None
    except (OSError, UnicodeError):
        return None


def _build_observed_ports(open_ports: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "port": port,
            "protocol": "tcp",
            "service": _SERVICE_HINTS.get(port),
        }
        for port in sorted(open_ports)
    ]


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
    reverse_lookup: Callable[[str], str | None] = _reverse_lookup,
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
        connect: injectable TCP-connect probe (default: real
            ``asyncio.open_connection``). Tests inject a fake to avoid sockets;
            the real-network path is otherwise untested here.
        reverse_lookup: injectable reverse-DNS function (default: real DNS).

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

    probe = connect or _default_connect

    async def engine(engine_ctx: EngineContext) -> EngineResult:
        return await _run_ip_discovery(engine_ctx, probe=probe, reverse_lookup=reverse_lookup)

    # persist_records None -> run_engine's own _noop_persister default.
    if persist_records is None:
        return run_engine(ctx, engine)
    return run_engine(ctx, engine, persist_records=persist_records)


async def _run_ip_discovery(
    ctx: EngineContext,
    *,
    probe: ConnectProbe,
    reverse_lookup: Callable[[str], str | None],
) -> EngineResult:
    """The engine body: expand targets, sweep (or plan), build results."""
    hosts = _expand_hosts(ctx.parameters)
    ports = _resolve_ports(ctx.parameters)
    do_reverse = bool(ctx.parameters.get("reverse_dns"))

    # DRY RUN: enumerate the (ip, port) target list, perform NO I/O.
    if ctx.dry_run:
        targets = [{"ip": host, "port": port} for host in hosts for port in ports]
        actions = [f"tcp-connect:{port}" for port in ports]
        if do_reverse:
            actions.append("reverse-dns")
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

    throttle = Throttle(ctx.throttle)
    timeout = ctx.throttle.connect_timeout_s

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    project_id = ctx.parameters.get("project_id")
    site_id = ctx.parameters.get("site_id")

    hosts_scanned = 0
    # Sweep host-by-host so cancellation can stop between hosts (and the
    # throttle stops between port dispatches within a host). Each host's ports
    # are dispatched as throttled units.
    for host in hosts:
        if ctx.is_cancelled():
            break
        hosts_scanned += 1

        def _factory(target_host: str, target_port: int) -> Callable[[], Awaitable[tuple[int, bool]]]:
            async def _probe_one() -> tuple[int, bool]:
                ok = await probe(target_host, target_port, timeout)
                return target_port, bool(ok)

            return _probe_one

        results = await throttle.run_throttled(
            [_factory(host, port) for port in ports], ctx
        )
        open_ports = sorted({port for port, ok in results if ok})
        if not open_ports:
            continue

        # Reverse DNS is a synchronous, potentially blocking call (it hits the
        # site resolver); run it off the event loop so it does not stall the
        # async sweep.
        hostname = await asyncio.to_thread(reverse_lookup, host) if do_reverse else None
        discovered_assets.append(
            {
                "asset_id": None,
                "ip_address": host,
                "hostname": hostname,
                "observed_ports": _build_observed_ports(open_ports),
                "match_basis": "ip",
                "status_detail": "responsive: "
                + ",".join(str(p) for p in open_ports),
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
                    "scanned_ports": list(ports),
                    "hostname": hostname,
                },
            }
        )

    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        result_summary_extra={
            "hosts_scanned": hosts_scanned,
            "hosts_responsive": len(discovered_assets),
            "ports_scanned": list(ports),
        },
    )
