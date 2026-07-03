"""Enumerate the host's usable network interfaces for the Source Interface picker.

Backs ``GET /api/v1/system/interfaces``. Returns real egress NICs
(``name / ipv4 / prefix_length / subnet_mask / cidr / gateway / is_up``) so the
Configuration page can offer a dropdown of source interfaces and, on selection,
confirm the chosen adapter's IPv4 / subnet mask / gateway (the operator visually
verifies they picked the OT/Ethernet NIC, not Wi-Fi). Loopback (``127.0.0.0/8``)
and link-local APIPA (``169.254.0.0/16``) are excluded because they are never a
valid scan egress. MAC / DNS / driver strings are still deliberately omitted
(section 5.3 of docs/proposals/nic-interface-selection.md): none are needed to
pick a NIC and each widens the host-fingerprint surface exposed over the API.

``gateway`` is a deliberate, operator-requested reversal of section 5.3's
original gateway omission: showing the selected NIC's default gateway helps the
engineer confirm the adapter is on the expected OT segment. ``psutil`` does not
expose gateways, so it comes from a guarded Windows routing-table lookup
(``Get-CimInstance Win32_NetworkAdapterConfiguration`` via a subprocess, mirroring
the guarded ``icacls`` pattern in ``app.core.runtime``). The lookup is Windows-only
and best-effort: on any failure — non-Windows host, missing/blocked PowerShell,
timeout, unparseable output — every ``gateway`` degrades to ``None`` and the rest
of the enumeration is unaffected. No new PyPI dependency is added for it.

``psutil`` is the only cross-platform way to get the real prefix length (which
BACnet binding requires) and up/down status without locale-fragile parsing, so
it is a deliberate exception to the "stdlib before deps" convention. The import
is guarded so a host without ``psutil`` degrades to an empty list (the UI falls
back to ``Auto`` + free-text) rather than 500-ing the endpoint.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import sys

from app.schemas.system import SystemInterface

try:
    import psutil
except ImportError:  # ponytail: psutil optional -> degrade to Auto-only (empty list), never 500.
    psutil = None


# APIPA / link-local IPv4 auto-configuration range; excluded like loopback.
_APIPA_NETWORK = ipaddress.ip_network("169.254.0.0/16")

# One PowerShell CIM query returns every IP-enabled adapter's IPv4 addresses and
# its default gateway(s); we map gateway-by-IP so the caller never has to reconcile
# psutil's friendly adapter names against WMI's driver descriptions. -Compress keeps
# the payload small; -Depth covers the nested string arrays.
_GATEWAY_PS_SCRIPT = (
    "Get-CimInstance -ClassName Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' "
    "| Select-Object IPAddress, DefaultIPGateway | ConvertTo-Json -Depth 4 -Compress"
)
_GATEWAY_TIMEOUT_SECONDS = 6


def _as_str_list(value: object) -> list[str]:
    """Normalise a CIM field that may be absent / a bare string / a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _first_ipv4(values: object) -> str | None:
    """Return the first IPv4 address in a normalised CIM string field, else None."""
    for candidate in _as_str_list(values):
        try:
            if isinstance(ipaddress.ip_address(candidate), ipaddress.IPv4Address):
                return candidate
        except ValueError:
            continue
    return None


def _default_gateways_by_ip() -> dict[str, str]:
    """Best-effort map of local IPv4 -> default gateway from the Windows routing table.

    Returns ``{}`` on any non-Windows host or on any failure (PowerShell missing or
    blocked, non-zero exit, timeout, unparseable JSON) so a gateway lookup can never
    break or slow-fail the interface enumeration. psutil does not expose gateways,
    so this guarded ``Get-CimInstance`` subprocess is the source (see module docstring).
    """
    if sys.platform != "win32":
        return {}
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _GATEWAY_PS_SCRIPT],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GATEWAY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {}
    try:
        parsed = json.loads(completed.stdout)
    except ValueError:  # JSONDecodeError is a ValueError subclass.
        return {}

    # ConvertTo-Json emits a bare object for a single adapter, a list for many.
    records = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []
    gateways: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        gateway = _first_ipv4(record.get("DefaultIPGateway"))
        if not gateway:
            continue
        for ipv4 in _as_str_list(record.get("IPAddress")):
            try:
                if isinstance(ipaddress.ip_address(ipv4), ipaddress.IPv4Address):
                    gateways.setdefault(ipv4, gateway)
            except ValueError:
                continue
    return gateways


def list_usable_interfaces() -> list[SystemInterface]:
    """Return the host's usable IPv4 NICs, is_up first then by name.

    Keeps only ``AF_INET`` addresses and drops loopback and APIPA. Derives the
    prefix length and dotted subnet mask from the interface netmask via
    ``ipaddress`` and attaches the default gateway when the guarded Windows lookup
    can resolve one (``None`` otherwise). Returns ``[]`` when ``psutil`` is
    unavailable so the endpoint degrades gracefully to ``Auto``-only.
    """
    if psutil is None:
        return []

    gateways = _default_gateways_by_ip()
    stats = psutil.net_if_stats()
    interfaces: list[SystemInterface] = []
    for name, addresses in psutil.net_if_addrs().items():
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
                network = ipaddress.ip_network(f"0.0.0.0/{address.netmask}")
            except (ValueError, TypeError):
                continue
            prefix_length = network.prefixlen
            is_up = bool(stats[name].isup) if name in stats else False
            interfaces.append(
                SystemInterface(
                    name=name,
                    ipv4=ipv4,
                    prefix_length=prefix_length,
                    subnet_mask=str(network.netmask),
                    cidr=f"{ipv4}/{prefix_length}",
                    gateway=gateways.get(ipv4),
                    is_up=is_up,
                )
            )

    interfaces.sort(key=lambda interface: (not interface.is_up, interface.name))
    return interfaces
