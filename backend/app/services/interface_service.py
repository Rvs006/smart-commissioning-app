"""Enumerate the host's usable network interfaces for the Source Interface picker.

Backs ``GET /api/v1/system/interfaces``. Returns only real egress NICs
(``name / ipv4 / prefix_length / cidr / is_up``) so the Configuration page can
offer a dropdown of source interfaces; loopback (``127.0.0.0/8``) and link-local
APIPA (``169.254.0.0/16``) are excluded because they are never a valid scan
egress. Deliberately omits MAC / gateway / DNS / driver strings (section 5.3 of
docs/proposals/nic-interface-selection.md): none are needed to pick a NIC and
each widens the host-fingerprint surface exposed over the API.

``psutil`` is the only cross-platform way to get the real prefix length (which
BACnet binding requires) and up/down status without locale-fragile parsing, so
it is a deliberate exception to the "stdlib before deps" convention. The import
is guarded so a host without ``psutil`` degrades to an empty list (the UI falls
back to ``Auto`` + free-text) rather than 500-ing the endpoint.
"""

from __future__ import annotations

import ipaddress
import socket

from app.schemas.system import SystemInterface

try:
    import psutil
except ImportError:  # ponytail: psutil optional -> degrade to Auto-only (empty list), never 500.
    psutil = None


# APIPA / link-local IPv4 auto-configuration range; excluded like loopback.
_APIPA_NETWORK = ipaddress.ip_network("169.254.0.0/16")


def list_usable_interfaces() -> list[SystemInterface]:
    """Return the host's usable IPv4 NICs, is_up first then by name.

    Keeps only ``AF_INET`` addresses and drops loopback and APIPA. Derives the
    prefix length from the interface netmask via ``ipaddress``. Returns ``[]``
    when ``psutil`` is unavailable so the endpoint degrades gracefully to
    ``Auto``-only.
    """
    if psutil is None:
        return []

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
                )
            )

    interfaces.sort(key=lambda interface: (not interface.is_up, interface.name))
    return interfaces
