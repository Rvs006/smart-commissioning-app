"""Enumerate the host's usable network interfaces for the Source Interface picker.

Backs ``GET /api/v1/system/interfaces``. Returns the host's IPv4 NICs
(``name / ipv4 / prefix_length / cidr / is_up / adapter_type / subnet_mask /
gateway / dns_servers``) so the Configuration page can offer a dropdown of
source interfaces plus a read-only details panel; loopback (``127.0.0.0/8``)
and link-local APIPA (``169.254.0.0/16``) are excluded because they are never
a valid scan egress. VIRTUAL adapters (Hyper-V vEthernet, WSL, Docker,
VPN/TAP/TUN, VMware) are LISTED, classified ``virtual`` and ranked last — not
hidden: on a Hyper-V vSwitch or NIC-team host the machine's only routable IPv4
rides a Virtual-flagged adapter, and hiding it left the dropdown Auto-only with
no way to bind the real egress NIC (field regression, 2026-07-14). The
frontend labels them so they are never picked by accident, and the wired-first
auto-default still ignores them. Gateway and DNS are exposed per the
product-owner decision (2026-07-03 meeting) REVERSING the section-5.3 omission
in docs/proposals/nic-interface-selection.md — engineers need them to confirm
the tool reads the NIC correctly; MAC / driver / InterfaceDescription strings
stay deliberately omitted. This module only READS network facts: Windows owns
IP settings and the app never writes adapter configuration.

Classification + per-adapter gateway + DNS come from ONE short-lived, cached
PowerShell subprocess on Windows (Get-NetAdapter / Get-NetRoute /
Get-DnsClientServerAddress piped through ConvertTo-Json — locale-safe JSON, a
fixed literal command, hard timeout) and from sysfs / ``/proc/net/route`` /
``/etc/resolv.conf`` on Linux. Every lookup degrades to
``adapter_type="unknown"`` / ``gateway=None`` / ``dns_servers=[]`` rather than
failing: enumeration must never 500 and never fabricate values.

``psutil`` is the only cross-platform way to get the real prefix length (which
BACnet binding requires) and up/down status without locale-fragile parsing, so
it is a deliberate exception to the "stdlib before deps" convention. The import
is guarded so a host without ``psutil`` degrades to an empty list (the UI falls
back to ``Auto`` + free-text) rather than 500-ing the endpoint.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from app.schemas.system import AdapterType, SystemInterface

try:
    import psutil
except ImportError:  # ponytail: psutil optional -> degrade to Auto-only (empty list), never 500.
    psutil = None


# APIPA / link-local IPv4 auto-configuration range; excluded like loopback.
_APIPA_NETWORK = ipaddress.ip_network("169.254.0.0/16")

_IS_WINDOWS = sys.platform == "win32"

_logger = logging.getLogger(__name__)

# Hard ceiling for the single PowerShell net-facts call; a blocked/hung
# PowerShell (ThreatLocker hosts) costs at most this once per TTL window.
# 20s, not 5s: Get-NetAdapter/Get-NetRoute/Get-DnsClientServerAddress cold-load
# the CIM/WMI subsystem inside the frozen exe's CREATE_NO_WINDOW subprocess and
# were measured at ~9.5s wall time on real hardware. At 5s the call ALWAYS timed
# out, so every adapter silently degraded to unknown/None/[] (no wired-first
# default, no Wi-Fi tag, no gateway/DNS, virtual adapters unfiltered) in the
# shipped portable exe — invisible because the timeout was swallowed to None.
_POWERSHELL_TIMEOUT_S = 20.0
# Facts cache TTL: fresh enough for a details panel, long enough that repeated
# dropdown refreshes never fork one subprocess per request. Kept >= the timeout
# so a slow-but-eventually-successful call is cached, not re-forked every window.
_NET_FACTS_TTL_S = 30.0

# Dropdown ordering (requirement: Ethernet first, then USB-Ethernet, then
# unknown, Wi-Fi and finally virtual, both with frontend warning tags).
_TYPE_RANK: dict[str, int] = {
    "ethernet": 0,
    "usb_ethernet": 1,
    "unknown": 2,
    "wifi": 3,
    "virtual": 4,
}

# Absolute path to Windows PowerShell. An unqualified "powershell" would let
# CreateProcess search the application directory and CWD before System32
# (CWE-427 binary planting) — and the portable profile runs from a
# user-writable folder. SystemRoot is always set on Windows; the literal
# fallback only matters for non-Windows imports, where this is never executed.
_POWERSHELL_EXE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)

# Fixed literal command — NEVER interpolates user input. ('' + $_.X) coerces
# CIM enums to their invariant display strings ("802.3", "Native 802.11") so
# the JSON transport is locale-safe; single quotes only so it embeds cleanly
# in this double-quoted Python string. [Console]::OutputEncoding forces the
# redirected stdout bytes to UTF-8 — without it a hidden console writes the
# OEM codepage (cp437/cp850), which mojibakes/explodes non-ASCII adapter names
# when Python decodes; the matching decode is subprocess.run(encoding="utf-8").
_WINDOWS_NET_FACTS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
    "@{ adapters=@(Get-NetAdapter | ForEach-Object { @{ name=('' + $_.Name); ifIndex=[int]$_.ifIndex; "
    "virtual=[bool]$_.Virtual; media=('' + $_.PhysicalMediaType); pnp=('' + $_.PnPDeviceID); "
    "desc=('' + $_.InterfaceDescription) } }); "
    "routes=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
    "| ForEach-Object { @{ ifIndex=[int]$_.ifIndex; nextHop=('' + $_.NextHop); metric=[int]$_.RouteMetric } }); "
    "dns=@(Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue "
    "| ForEach-Object { @{ ifIndex=[int]$_.InterfaceIndex; "
    "servers=@($_.ServerAddresses | ForEach-Object { '' + $_ }) } }) "
    "} | ConvertTo-Json -Depth 5 -Compress"
)

# Windows virtual-adapter fingerprints in InterfaceDescription (belt-and-braces
# on top of Get-NetAdapter's Virtual flag, which misses some VPN/TAP drivers).
_VIRTUAL_DESC_RE = re.compile(
    r"virtual|vethernet|hyper-v|vmware|virtualbox|tap-|tun|wintun|wireguard|loopback|npcap",
    re.IGNORECASE,
)

# Dispatch-time guard messages (exact API contract text — the frontend surfaces
# these verbatim via the generic HTTP 400 detail path).
_SOURCE_IP_NOT_PRESENT_MSG = (
    "Source Interface {ip} is not present on this host. Reconnect the adapter, "
    "or set Source Interface to 'Auto (OS default route)' on the Configuration page."
)
_SOURCE_IP_DOWN_MSG = (
    "Source Interface {ip} ({name}) is down. Bring the adapter up, "
    "or set Source Interface to 'Auto (OS default route)' on the Configuration page."
)


@dataclass(frozen=True)
class _NetFacts:
    """OS network facts keyed by adapter name (the psutil <-> OS join key).

    ``adapter`` holds the raw Windows classification inputs (virtual flag,
    media, pnp, desc) — used ONLY server-side, never returned over the API.
    """

    adapter: dict[str, dict[str, Any]] = field(default_factory=dict)
    gateway: dict[str, str] = field(default_factory=dict)
    dns: dict[str, list[str]] = field(default_factory=dict)

    EMPTY: ClassVar[_NetFacts]


_NetFacts.EMPTY = _NetFacts()

_net_facts_lock = threading.Lock()
_net_facts_cache: tuple[float, _NetFacts] | None = None


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _run_powershell_net_facts() -> str | None:
    """Run the fixed net-facts script once; None on ANY failure.

    ponytail: enumeration must degrade, not 500 — a missing/denied/slow
    PowerShell (FileNotFoundError is an OSError subclass) yields None and the
    caller serves psutil-only data with unknown/None/[] details.
    """
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                _POWERSHELL_EXE,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _WINDOWS_NET_FACTS_SCRIPT,
            ],
            capture_output=True,
            # UTF-8 to match the script's [Console]::OutputEncoding; without an
            # explicit encoding Python decodes with the ANSI codepage and a
            # non-ASCII adapter name mojibakes (or raises in the reader thread).
            # errors="replace" keeps a stray undecodable byte from killing the
            # whole facts payload.
            encoding="utf-8",
            errors="replace",
            timeout=_POWERSHELL_TIMEOUT_S,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        # Was silently swallowed to None, which shipped a broken NIC picker with
        # no trace. Log it so the field cause is diagnosable next time.
        _logger.warning(
            "NIC net-facts PowerShell timed out after %.1fs; adapters will "
            "degrade to Auto-only (unknown type, no gateway/DNS). Raise "
            "_POWERSHELL_TIMEOUT_S if this recurs.",
            _POWERSHELL_TIMEOUT_S,
        )
        return None
    except OSError as exc:
        _logger.warning("NIC net-facts PowerShell could not be run: %s", exc)
        return None
    if completed.returncode != 0:
        _logger.warning(
            "NIC net-facts PowerShell exited %s after %.1fs: %s",
            completed.returncode,
            time.monotonic() - started,
            (completed.stderr or "").strip()[:500],
        )
        return None
    return completed.stdout


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """Normalize a ConvertTo-Json collection: PowerShell 5.1 can serialize a
    single-element pipeline as one object instead of a one-item array
    (belt-and-braces on top of the script's @() wrapping)."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _parse_windows_net_facts(raw: str) -> _NetFacts:
    """PURE parser for the PowerShell JSON payload; EMPTY on any malformed input."""
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return _NetFacts.EMPTY

        adapter_by_name: dict[str, dict[str, Any]] = {}
        name_by_index: dict[int, str] = {}
        for adapter in _as_dict_list(payload.get("adapters")):
            name = str(adapter.get("name") or "").strip()
            if not name:
                continue
            adapter_by_name[name] = adapter
            if adapter.get("ifIndex") is not None:
                name_by_index[int(adapter["ifIndex"])] = name

        # Per interface, keep the lowest-metric default route's NextHop.
        best_default_route: dict[int, tuple[int, str]] = {}
        for route in _as_dict_list(payload.get("routes")):
            if route.get("ifIndex") is None:
                continue
            next_hop = str(route.get("nextHop") or "").strip()
            # 0.0.0.0 = an on-link default route: there is no gateway to show.
            if not _is_ipv4(next_hop) or next_hop == "0.0.0.0":
                continue
            index = int(route["ifIndex"])
            metric = int(route.get("metric") or 0)
            best = best_default_route.get(index)
            if best is None or metric < best[0]:
                best_default_route[index] = (metric, next_hop)
        gateway_by_name = {
            name_by_index[index]: next_hop
            for index, (_, next_hop) in best_default_route.items()
            if index in name_by_index
        }

        dns_by_name: dict[str, list[str]] = {}
        for entry in _as_dict_list(payload.get("dns")):
            if entry.get("ifIndex") is None:
                continue
            name = name_by_index.get(int(entry["ifIndex"]))
            if name is None:
                continue
            servers = entry.get("servers")
            if isinstance(servers, str):  # single-element quirk again
                servers = [servers]
            if not isinstance(servers, list):
                continue
            addresses = [str(server) for server in servers if _is_ipv4(str(server))]
            if addresses:
                dns_by_name[name] = addresses

        return _NetFacts(adapter=adapter_by_name, gateway=gateway_by_name, dns=dns_by_name)
    except (KeyError, ValueError, TypeError, AttributeError):
        return _NetFacts.EMPTY


def _classify_windows(adapter: dict[str, Any]) -> AdapterType:
    """PURE best-effort classification from Get-NetAdapter facts.

    PhysicalMediaType enum display names ("802.3", "Native 802.11") and
    PnPDeviceID prefixes ("USB\\...") are invariant across Windows locales.
    An adapter psutil knows but PowerShell didn't report (empty dict) is
    honestly "unknown", never guessed.
    """
    desc = str(adapter.get("desc") or "")
    media = str(adapter.get("media") or "")
    pnp = str(adapter.get("pnp") or "")
    if bool(adapter.get("virtual")) or _VIRTUAL_DESC_RE.search(desc):
        return "virtual"
    if "802.11" in media or "Wireless" in media:
        return "wifi"
    if pnp.upper().startswith("USB"):
        return "usb_ethernet"
    if "802.3" in media:
        return "ethernet"
    return "unknown"


def _classify_linux(name: str) -> AdapterType:
    """Best-effort sysfs classification (docker0/veth*/br-*/tun/wg live under
    /sys/devices/virtual/net; USB NICs resolve through a .../usb.../ device path)."""
    try:
        if Path(f"/sys/devices/virtual/net/{name}").exists():
            return "virtual"
        if Path(f"/sys/class/net/{name}/wireless").exists():
            return "wifi"
        if not Path(f"/sys/class/net/{name}").exists():
            return "unknown"
        if "usb" in os.path.realpath(f"/sys/class/net/{name}"):
            return "usb_ethernet"
        return "ethernet"
    except OSError:
        return "unknown"


def _linux_gateway_by_iface() -> dict[str, str]:
    """Per-interface IPv4 default gateway from /proc/net/route, best effort."""
    gateways: dict[str, str] = {}
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            columns = line.split()
            if len(columns) < 4:
                continue
            name, destination, gateway_hex, flags = columns[0], columns[1], columns[2], columns[3]
            # Default route (0.0.0.0/0) with RTF_GATEWAY (0x2) set; the hex
            # fields are little-endian.
            if destination != "00000000" or not int(flags, 16) & 0x2:
                continue
            if name not in gateways:  # first (highest-priority) default route wins
                gateways[name] = socket.inet_ntoa(struct.pack("<L", int(gateway_hex, 16)))
    except (OSError, ValueError, struct.error):
        return {}
    return gateways


def _linux_dns_servers() -> list[str]:
    """IPv4 nameservers from /etc/resolv.conf, in file order, best effort."""
    servers: list[str] = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver" and _is_ipv4(parts[1]):
                servers.append(parts[1])
    except OSError:
        return []
    return servers


def _collect_net_facts() -> _NetFacts:
    if _IS_WINDOWS:
        raw = _run_powershell_net_facts()
        return _parse_windows_net_facts(raw) if raw is not None else _NetFacts.EMPTY
    # Linux: no subprocess at all. resolv.conf is a GLOBAL resolver list, not
    # per-adapter, so it is applied to every enumerated interface (documented
    # best effort per the 2026-07-03 meeting). Inside a container host NICs are
    # invisible, so a sparse/empty result here is the honest expected outcome.
    dns_servers = _linux_dns_servers()
    names = list(psutil.net_if_addrs()) if psutil is not None else []
    dns = {name: list(dns_servers) for name in names} if dns_servers else {}
    return _NetFacts(adapter={}, gateway=_linux_gateway_by_iface(), dns=dns)


def _get_net_facts() -> _NetFacts:
    """Net facts behind a short TTL cache. Failures are cached too, so a
    blocked PowerShell (ThreatLocker) costs at most one ~5s attempt per 10s
    window, not one per request. The lock is held across collection on purpose:
    concurrent requests serialize instead of forking a subprocess each."""
    global _net_facts_cache
    with _net_facts_lock:
        now = time.monotonic()
        if _net_facts_cache is not None and now - _net_facts_cache[0] < _NET_FACTS_TTL_S:
            return _net_facts_cache[1]
        facts = _collect_net_facts()
        _net_facts_cache = (time.monotonic(), facts)
        return facts


def list_usable_interfaces() -> list[SystemInterface]:
    """Return the host's usable IPv4 NICs with classification + read-only details.

    Keeps only ``AF_INET`` addresses and drops loopback and APIPA. Virtual
    adapters are listed (classified ``virtual``, ranked last) rather than
    dropped: on Hyper-V vSwitch / NIC-team hosts they carry the machine's only
    routable IPv4, so hiding them made the real egress NIC unselectable.
    Derives the prefix length from the interface netmask via ``ipaddress``.
    Sorted up-first, then Ethernet < USB-Ethernet < unknown < Wi-Fi < virtual,
    then by name. Returns ``[]`` when ``psutil`` is unavailable so the endpoint
    degrades gracefully to ``Auto``-only.
    """
    if psutil is None:
        return []

    facts = _get_net_facts()
    stats = psutil.net_if_stats()
    interfaces: list[SystemInterface] = []
    for name, addresses in psutil.net_if_addrs().items():
        adapter_type: AdapterType = (
            _classify_windows(facts.adapter.get(name, {})) if _IS_WINDOWS else _classify_linux(name)
        )
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            ipv4 = address.address
            try:
                parsed = ipaddress.ip_address(ipv4)
            except ValueError:
                continue
            if parsed.is_loopback or parsed in _APIPA_NETWORK:
                continue
            try:
                prefix_length = ipaddress.ip_network(f"0.0.0.0/{address.netmask}").prefixlen
            except (ValueError, TypeError):
                continue
            is_up = bool(stats[name].isup) if name in stats else False
            interfaces.append(
                SystemInterface(
                    name=name,
                    ipv4=ipv4,
                    prefix_length=prefix_length,
                    cidr=f"{ipv4}/{prefix_length}",
                    is_up=is_up,
                    adapter_type=adapter_type,
                    subnet_mask=str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_length}").netmask),
                    gateway=facts.gateway.get(name),
                    dns_servers=list(facts.dns.get(name, [])),
                )
            )

    interfaces.sort(
        key=lambda interface: (not interface.is_up, _TYPE_RANK[interface.adapter_type], interface.name)
    )
    return interfaces


def ensure_source_ip_available(source_ip: str) -> None:
    """Dispatch-time guard: the configured source IP must still be assigned and up.

    Raises ``ValueError`` with an actionable, operator-facing message when the
    IP is not assigned to any adapter ("not present") or its adapter is down;
    returns None silently when available. NEVER silently substitutes another
    NIC — failing the run creation with a clear error is the honest behavior.

    Without ``psutil`` the fallback is a throwaway UDP-socket bind probe (bind
    binds only, nothing is sent), which can prove presence but not up/down —
    the engine-level checks remain the backstop there.
    """
    if psutil is None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((source_ip, 0))
        except OSError as error:
            raise ValueError(_SOURCE_IP_NOT_PRESENT_MSG.format(ip=source_ip)) from error
        finally:
            probe.close()
        return

    stats = psutil.net_if_stats()
    down_adapter: str | None = None
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family != socket.AF_INET or address.address != source_ip:
                continue
            stat = stats.get(name)
            if stat is not None and stat.isup:
                return
            # Missing stats counts as down — consistent with the is_up=False
            # the interfaces list would show for the same adapter.
            if down_adapter is None:
                down_adapter = name
    if down_adapter is not None:
        raise ValueError(_SOURCE_IP_DOWN_MSG.format(ip=source_ip, name=down_adapter))
    raise ValueError(_SOURCE_IP_NOT_PRESENT_MSG.format(ip=source_ip))
