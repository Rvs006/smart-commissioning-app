"""Freeze and revalidate the concrete source NIC used by active discovery.

The API resolves an explicit source address or the current lowest-metric
default route into a stable interface identity before sealing a preview.  The
executor repeats the same read-only inventory immediately before an engine can
open its transport, compares the stable identity, and performs a bind-only UDP
socket proof.  No connect or send operation exists in this module.

Windows inventory uses the in-process IP Helper API.  Linux uses
``socket.if_nameindex``, read-only ``ioctl`` calls, and ``/proc/net/route``.
Unsupported or unavailable inventory fails closed.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
import sys
import uuid
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.run_context import canonical_sha256

try:  # Linux only; importing the package must remain portable on Windows.
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows imports.
    fcntl = None


SOURCE_INTERFACE_IDENTITY_KEY = "source_interface_identity_v1"

SourceInterfaceSelection = Literal["default_route", "explicit"]

_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")
_APIPA = ipaddress.ip_network("169.254.0.0/16")

_WINDOWS_AF_INET = 2
_WINDOWS_ERROR_BUFFER_OVERFLOW = 111
_WINDOWS_ERROR_INSUFFICIENT_BUFFER = 122
_WINDOWS_INITIAL_BUFFER_BYTES = 15_000
_WINDOWS_MAX_BUFFER_BYTES = 16 * 1024 * 1024
_WINDOWS_MAX_LINKED_ENTRIES = 4_096
_WINDOWS_GAA_FLAGS = 0x0002 | 0x0004 | 0x0008  # Skip anycast, multicast, DNS servers.
_WINDOWS_IF_OPER_STATUS_UP = 1
_WINDOWS_IP_DAD_STATE_PREFERRED = 4


class _SocketAddress(ctypes.Structure):
    _fields_ = [("sockaddr", ctypes.c_void_p), ("length", ctypes.c_int)]


class _SockaddrIn(ctypes.Structure):
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("port", ctypes.c_ushort),
        ("address", ctypes.c_ubyte * 4),
        ("zero", ctypes.c_ubyte * 8),
    ]


class _AdapterUnicastAddress(ctypes.Structure):
    pass


_AdapterUnicastAddressPointer = ctypes.POINTER(_AdapterUnicastAddress)
_AdapterUnicastAddress._fields_ = [
    ("alignment", ctypes.c_uint64),
    ("next", _AdapterUnicastAddressPointer),
    ("address", _SocketAddress),
    ("prefix_origin", ctypes.c_int),
    ("suffix_origin", ctypes.c_int),
    ("dad_state", ctypes.c_int),
    ("valid_lifetime", ctypes.c_uint32),
    ("preferred_lifetime", ctypes.c_uint32),
    ("lease_lifetime", ctypes.c_uint32),
    ("on_link_prefix_length", ctypes.c_ubyte),
]


class _AdapterAddresses(ctypes.Structure):
    pass


_AdapterAddressesPointer = ctypes.POINTER(_AdapterAddresses)
_AdapterAddresses._fields_ = [
    ("alignment", ctypes.c_uint64),
    ("next", _AdapterAddressesPointer),
    ("adapter_name", ctypes.c_char_p),
    ("first_unicast_address", _AdapterUnicastAddressPointer),
    ("first_anycast_address", ctypes.c_void_p),
    ("first_multicast_address", ctypes.c_void_p),
    ("first_dns_server_address", ctypes.c_void_p),
    ("dns_suffix", ctypes.c_wchar_p),
    ("description", ctypes.c_wchar_p),
    ("friendly_name", ctypes.c_wchar_p),
    ("physical_address", ctypes.c_ubyte * 8),
    ("physical_address_length", ctypes.c_uint32),
    ("flags", ctypes.c_uint32),
    ("mtu", ctypes.c_uint32),
    ("if_type", ctypes.c_uint32),
    ("oper_status", ctypes.c_int),
    ("ipv6_if_index", ctypes.c_uint32),
    ("zone_indices", ctypes.c_uint32 * 16),
    ("first_prefix", ctypes.c_void_p),
    ("transmit_link_speed", ctypes.c_uint64),
    ("receive_link_speed", ctypes.c_uint64),
    ("first_wins_server_address", ctypes.c_void_p),
    ("first_gateway_address", ctypes.c_void_p),
    ("ipv4_metric", ctypes.c_uint32),
    ("ipv6_metric", ctypes.c_uint32),
    ("luid", ctypes.c_uint64),
]


class _IpForwardRow(ctypes.Structure):
    _fields_ = [
        ("destination", ctypes.c_uint32),
        ("mask", ctypes.c_uint32),
        ("policy", ctypes.c_uint32),
        ("next_hop", ctypes.c_uint32),
        ("interface_index", ctypes.c_uint32),
        ("route_type", ctypes.c_uint32),
        ("protocol", ctypes.c_uint32),
        ("age", ctypes.c_uint32),
        ("next_hop_as", ctypes.c_uint32),
        ("metric_1", ctypes.c_uint32),
        ("metric_2", ctypes.c_uint32),
        ("metric_3", ctypes.c_uint32),
        ("metric_4", ctypes.c_uint32),
        ("metric_5", ctypes.c_uint32),
    ]


def _canonical_ipv4(value: object, *, label: str) -> str:
    try:
        parsed = ipaddress.ip_address(str(value))
    except ValueError as error:
        raise ValueError(f"{label} must be a valid IPv4 address") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError(f"{label} must be a valid IPv4 address")
    return str(parsed)


class SourceInterfaceCandidateV1(BaseModel):
    """One current IPv4 address attached to one OS interface identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    interface_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9:-]+$")
    interface_name: str = Field(min_length=1, max_length=255)
    source_ip: str
    prefix_length: int = Field(ge=0, le=32)
    is_up: bool
    default_route_metric: int | None = Field(default=None, ge=0, le=4_294_967_295)

    @field_validator("source_ip")
    @classmethod
    def _validate_source_ip(cls, value: str) -> str:
        return _canonical_ipv4(value, label="source_ip")


class FrozenSourceInterfaceV1(BaseModel):
    """Canonical source-interface identity sealed into ``scan_contract_v1``."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    selection: SourceInterfaceSelection
    executor_scope: str = Field(min_length=1, max_length=255)
    interface_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9:-]+$")
    interface_name: str = Field(min_length=1, max_length=255)
    source_ip: str
    prefix_length: int = Field(ge=0, le=32)
    local_address: str
    default_route_metric: int | None = Field(default=None, ge=0, le=4_294_967_295)

    @field_validator("source_ip")
    @classmethod
    def _validate_source_ip(cls, value: str) -> str:
        return _canonical_ipv4(value, label="source_ip")

    @model_validator(mode="after")
    def _validate_local_address(self) -> FrozenSourceInterfaceV1:
        if "/" not in self.local_address:
            raise ValueError("local_address must preserve an IPv4 prefix")
        try:
            local = ipaddress.ip_interface(self.local_address)
        except ValueError as error:
            raise ValueError("local_address must be a valid IPv4 interface") from error
        if not isinstance(local, ipaddress.IPv4Interface):
            raise ValueError("local_address must be a valid IPv4 interface")
        if str(local.ip) != self.source_ip or local.network.prefixlen != self.prefix_length:
            raise ValueError(
                "source_ip, prefix_length, and local_address must identify the same interface"
            )
        if self.selection == "default_route" and self.default_route_metric is None:
            raise ValueError("default-route identity must preserve its route metric")
        return self


def source_interface_resource_key(
    identity: FrozenSourceInterfaceV1 | Mapping[str, object],
) -> str:
    """Return the KTD21 slot key for one executor-scoped OS interface."""

    frozen = FrozenSourceInterfaceV1.model_validate(identity)
    digest = canonical_sha256(
        {
            "executor_scope": frozen.executor_scope,
            "interface_id": frozen.interface_id,
        }
    )
    return f"nic:v1:{digest}"


def select_source_interface(
    candidates: Sequence[SourceInterfaceCandidateV1],
    *,
    selection: SourceInterfaceSelection,
    executor_scope: str,
    source_ip: str | None = None,
    prefix_length: int | None = None,
) -> FrozenSourceInterfaceV1:
    """Select a deterministic usable candidate and return its frozen identity."""

    scope = str(executor_scope).strip()
    if not scope or len(scope) > 255:
        raise ValueError("executor_scope must contain between 1 and 255 characters")
    usable = [candidate for candidate in candidates if candidate.is_up]
    if selection == "default_route":
        routed = [candidate for candidate in usable if candidate.default_route_metric is not None]
        if not routed:
            raise ValueError(
                "No usable IPv4 default route is available on this executor. "
                "Bring the intended adapter up or select a concrete Source Interface."
            )
        selected = min(
            routed,
            key=lambda item: (
                int(item.default_route_metric or 0),
                item.interface_id,
                item.interface_name.casefold(),
                int(ipaddress.IPv4Address(item.source_ip)),
                item.prefix_length,
            ),
        )
    else:
        if source_ip is None:
            raise ValueError("explicit source-interface selection requires source_ip")
        requested_ip = _canonical_ipv4(source_ip, label="source_ip")
        address_matches = [candidate for candidate in usable if candidate.source_ip == requested_ip]
        if not address_matches:
            raise ValueError(
                f"Source Interface {requested_ip} is not present and up on this executor. "
                "Reconnect the adapter or select a current network adapter."
            )
        if prefix_length is not None:
            if isinstance(prefix_length, bool) or not 0 <= int(prefix_length) <= 32:
                raise ValueError("source-interface prefix length must be between 0 and 32")
            prefix = int(prefix_length)
            prefix_matches = [
                candidate for candidate in address_matches if candidate.prefix_length == prefix
            ]
            if not prefix_matches:
                actual = min(candidate.prefix_length for candidate in address_matches)
                raise ValueError(
                    f"Source Interface {requested_ip} prefix /{prefix} does not match "
                    f"the current executor prefix /{actual}."
                )
            address_matches = prefix_matches
        selected = min(
            address_matches,
            key=lambda item: (
                item.interface_id,
                item.interface_name.casefold(),
                item.prefix_length,
            ),
        )

    return FrozenSourceInterfaceV1(
        selection=selection,
        executor_scope=scope,
        interface_id=selected.interface_id,
        interface_name=selected.interface_name,
        source_ip=selected.source_ip,
        prefix_length=selected.prefix_length,
        local_address=f"{selected.source_ip}/{selected.prefix_length}",
        default_route_metric=selected.default_route_metric,
    )


def resolve_source_interface_identity(
    *,
    selection: SourceInterfaceSelection,
    executor_scope: str,
    source_ip: str | None = None,
    prefix_length: int | None = None,
) -> FrozenSourceInterfaceV1:
    """Resolve a frozen identity from a fresh, read-only OS inventory."""

    return select_source_interface(
        enumerate_source_interfaces(),
        selection=selection,
        executor_scope=executor_scope,
        source_ip=source_ip,
        prefix_length=prefix_length,
    )


def guard_frozen_source_interface(
    parameters: Mapping[str, object],
    *,
    expected_executor_scope: str,
) -> None:
    """Fail before transport creation if the frozen source identity has drifted."""

    raw_identity = parameters.get(SOURCE_INTERFACE_IDENTITY_KEY)
    if not isinstance(raw_identity, Mapping):
        raise ValueError(
            "The sealed scan is missing a concrete source-interface identity. "
            "Create a new preview on the intended executor."
        )
    frozen = FrozenSourceInterfaceV1.model_validate(dict(raw_identity))
    if frozen.executor_scope != str(expected_executor_scope).strip():
        raise ValueError(
            "The sealed source-interface executor identity does not match this executor. "
            "Create a new preview for this deployment before starting the scan."
        )
    parameter_source = parameters.get("source_ip")
    parameter_local = parameters.get("local_address")
    if str(parameter_source or "").strip() != frozen.source_ip:
        raise ValueError("sealed source_ip does not match source_interface_identity_v1")
    if str(parameter_local or "").strip() != frozen.local_address:
        raise ValueError("sealed local_address does not match source_interface_identity_v1")

    current = enumerate_source_interfaces()
    same_stable_interface = [
        candidate for candidate in current if candidate.interface_id == frozen.interface_id
    ]
    if not same_stable_interface:
        raise ValueError(
            f"Source Interface {frozen.source_ip} ({frozen.interface_name}) is no longer present "
            "on this executor. Create a new preview after restoring or re-selecting the adapter."
        )
    exact = [
        candidate
        for candidate in same_stable_interface
        if candidate.is_up
        and candidate.interface_name == frozen.interface_name
        and candidate.source_ip == frozen.source_ip
        and candidate.prefix_length == frozen.prefix_length
    ]
    if not exact:
        raise ValueError(
            f"Source Interface {frozen.source_ip} identity drifted after preview "
            "(stable ID, name, IPv4 address, or prefix changed). Create a new preview."
        )

    if frozen.selection == "default_route":
        try:
            selected_now = select_source_interface(
                current,
                selection="default_route",
                executor_scope=frozen.executor_scope,
            )
        except ValueError as error:
            raise ValueError(
                "The executor default route changed or disappeared after preview. "
                "Create a new preview before starting the scan."
            ) from error
        if (
            selected_now.interface_id,
            selected_now.interface_name,
            selected_now.source_ip,
            selected_now.prefix_length,
            selected_now.default_route_metric,
        ) != (
            frozen.interface_id,
            frozen.interface_name,
            frozen.source_ip,
            frozen.prefix_length,
            frozen.default_route_metric,
        ):
            raise ValueError(
                "The executor default route changed after preview. "
                "Create a new preview before starting the scan."
            )

    _prove_source_ip_bindable(frozen.source_ip, frozen.interface_name)


def _prove_source_ip_bindable(source_ip: str, interface_name: str) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((source_ip, 0))
    except OSError as error:
        raise ValueError(
            f"Source Interface {source_ip} ({interface_name}) cannot be bound on this executor. "
            "Create a new preview after restoring or re-selecting the adapter."
        ) from error
    finally:
        probe.close()


def enumerate_source_interfaces() -> list[SourceInterfaceCandidateV1]:
    """Return a fresh OS inventory; failures are empty and therefore fail closed."""

    if _IS_WINDOWS:
        return _enumerate_windows_source_interfaces()
    if _IS_LINUX:
        return _enumerate_linux_source_interfaces()
    return []


def _windows_ip_helper_buffer(
    function: object,
    *,
    arguments_before_buffer: tuple[object, ...],
    resize_error: int,
    arguments_after_size: tuple[object, ...] = (),
) -> ctypes.Array[ctypes.c_char] | None:
    """Call one bounded IP Helper buffer API without process or network I/O."""

    buffer_size = ctypes.c_uint32(_WINDOWS_INITIAL_BUFFER_BYTES)
    for _ in range(3):
        capacity = int(buffer_size.value)
        if capacity <= 0 or capacity > _WINDOWS_MAX_BUFFER_BYTES:
            return None
        buffer = ctypes.create_string_buffer(capacity)
        result = function(
            *arguments_before_buffer,
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(buffer_size),
            *arguments_after_size,
        )
        if result == 0:
            return buffer
        if (
            result != resize_error
            or int(buffer_size.value) <= capacity
            or int(buffer_size.value) > _WINDOWS_MAX_BUFFER_BYTES
        ):
            return None
    return None


def _windows_default_route_metrics(ip_helper: object) -> dict[int, int]:
    get_routes = ip_helper.GetIpForwardTable
    get_routes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    get_routes.restype = ctypes.c_uint32
    buffer = _windows_ip_helper_buffer(
        get_routes,
        arguments_before_buffer=(),
        resize_error=_WINDOWS_ERROR_INSUFFICIENT_BUFFER,
        arguments_after_size=(1,),
    )
    if buffer is None or len(buffer) < ctypes.sizeof(ctypes.c_uint32):
        return {}

    count = int(ctypes.c_uint32.from_buffer(buffer).value)
    row_offset = ctypes.sizeof(ctypes.c_uint32)
    required_bytes = row_offset + count * ctypes.sizeof(_IpForwardRow)
    if count > _WINDOWS_MAX_LINKED_ENTRIES or required_bytes > len(buffer):
        return {}
    rows_type = _IpForwardRow * count
    rows = rows_type.from_buffer(buffer, row_offset)
    metrics: dict[int, int] = {}
    for row in rows:
        metric = int(row.metric_1)
        if row.destination != 0 or row.mask != 0 or metric == 0xFFFFFFFF:
            continue
        index = int(row.interface_index)
        metrics[index] = min(metrics.get(index, metric), metric)
    return metrics


def _windows_adapter_guid(adapter_name: bytes | None) -> str | None:
    if not adapter_name:
        return None
    try:
        return str(uuid.UUID(adapter_name.decode("ascii").strip().strip("{}")))
    except (UnicodeError, ValueError):
        return None


def _windows_unicast_ipv4(
    address: _AdapterUnicastAddress,
) -> tuple[str, int] | None:
    socket_address = address.address
    if (
        address.dad_state != _WINDOWS_IP_DAD_STATE_PREFERRED
        or not socket_address.sockaddr
        or socket_address.length < ctypes.sizeof(_SockaddrIn)
    ):
        return None
    sockaddr = ctypes.cast(
        socket_address.sockaddr,
        ctypes.POINTER(_SockaddrIn),
    ).contents
    prefix_length = int(address.on_link_prefix_length)
    if sockaddr.family != _WINDOWS_AF_INET or not 0 <= prefix_length <= 32:
        return None
    source_ip = _usable_ipv4(ipaddress.IPv4Address(bytes(sockaddr.address)))
    if source_ip is None:
        return None
    return source_ip, prefix_length


def _enumerate_windows_source_interfaces() -> list[SourceInterfaceCandidateV1]:
    try:
        ip_helper = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
        metrics = _windows_default_route_metrics(ip_helper)
        get_adapters = ip_helper.GetAdaptersAddresses
        get_adapters.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_adapters.restype = ctypes.c_uint32
        buffer = _windows_ip_helper_buffer(
            get_adapters,
            arguments_before_buffer=(
                _WINDOWS_AF_INET,
                _WINDOWS_GAA_FLAGS,
                None,
            ),
            resize_error=_WINDOWS_ERROR_BUFFER_OVERFLOW,
        )
        if buffer is None:
            return []

        candidates: list[SourceInterfaceCandidateV1] = []
        seen: set[tuple[str, str, int]] = set()
        adapter_pointer = ctypes.cast(buffer, _AdapterAddressesPointer)
        for _ in range(_WINDOWS_MAX_LINKED_ENTRIES):
            if not adapter_pointer:
                break
            adapter = adapter_pointer.contents
            structure_length = int(adapter.alignment & 0xFFFFFFFF)
            interface_index = int(adapter.alignment >> 32)
            guid = _windows_adapter_guid(adapter.adapter_name)
            required_length = _AdapterAddresses.luid.offset + ctypes.sizeof(ctypes.c_uint64)
            if structure_length >= required_length and interface_index and guid:
                interface_name = str(
                    adapter.friendly_name or adapter.description or guid
                ).strip()[:255]
                unicast_pointer = adapter.first_unicast_address
                for _ in range(_WINDOWS_MAX_LINKED_ENTRIES):
                    if not unicast_pointer:
                        break
                    unicast = unicast_pointer.contents
                    resolved = _windows_unicast_ipv4(unicast)
                    if resolved is not None and interface_name:
                        source_ip, prefix_length = resolved
                        key = (guid, source_ip, prefix_length)
                        if key not in seen:
                            seen.add(key)
                            candidates.append(
                                SourceInterfaceCandidateV1(
                                    interface_id=f"windows-guid:{guid}",
                                    interface_name=interface_name,
                                    source_ip=source_ip,
                                    prefix_length=prefix_length,
                                    is_up=(
                                        adapter.oper_status == _WINDOWS_IF_OPER_STATUS_UP
                                    ),
                                    default_route_metric=metrics.get(interface_index),
                                )
                            )
                    unicast_pointer = unicast.next
            adapter_pointer = adapter.next
        return sorted(candidates, key=_candidate_order)
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        return []


def _usable_ipv4(value: object) -> str | None:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if address.is_loopback or address in _APIPA:
        return None
    return str(address)


def _linux_default_route_metrics() -> dict[str, int]:
    metrics: dict[str, int] = {}
    try:
        with open("/proc/net/route", encoding="ascii") as route_file:
            lines = route_file.read().splitlines()[1:]
    except OSError:
        return metrics
    for line in lines:
        columns = line.split()
        if len(columns) < 7 or columns[1] != "00000000":
            continue
        try:
            flags = int(columns[3], 16)
            metric = int(columns[6])
        except ValueError:
            continue
        if not flags & 0x1 or metric < 0:  # RTF_UP
            continue
        name = columns[0]
        metrics[name] = min(metrics.get(name, metric), metric)
    return metrics


def _linux_ioctl(probe: socket.socket, name: str, request: int) -> bytes:
    if fcntl is None:
        raise OSError("fcntl is unavailable")
    encoded = name.encode("utf-8")[:15]
    return fcntl.ioctl(probe.fileno(), request, struct.pack("256s", encoded))


def _enumerate_linux_source_interfaces() -> list[SourceInterfaceCandidateV1]:
    metrics = _linux_default_route_metrics()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    candidates: list[SourceInterfaceCandidateV1] = []
    try:
        for index, name in socket.if_nameindex():
            try:
                flags_payload = _linux_ioctl(probe, name, 0x8913)  # SIOCGIFFLAGS
                address_payload = _linux_ioctl(probe, name, 0x8915)  # SIOCGIFADDR
                netmask_payload = _linux_ioctl(probe, name, 0x891B)  # SIOCGIFNETMASK
                flags = struct.unpack("H", flags_payload[16:18])[0]
                source_ip = _usable_ipv4(socket.inet_ntoa(address_payload[20:24]))
                if source_ip is None:
                    continue
                netmask = socket.inet_ntoa(netmask_payload[20:24])
                prefix = ipaddress.ip_network(f"0.0.0.0/{netmask}").prefixlen
                candidates.append(
                    SourceInterfaceCandidateV1(
                        interface_id=f"linux-ifindex:{index}",
                        interface_name=name,
                        source_ip=source_ip,
                        prefix_length=prefix,
                        is_up=bool(flags & 0x1),  # IFF_UP
                        default_route_metric=metrics.get(name),
                    )
                )
            except (OSError, ValueError, struct.error):
                continue
    finally:
        probe.close()
    return sorted(candidates, key=_candidate_order)


def _candidate_order(item: SourceInterfaceCandidateV1) -> tuple[str, str, int, int]:
    return (
        item.interface_id,
        item.interface_name.casefold(),
        int(ipaddress.IPv4Address(item.source_ip)),
        item.prefix_length,
    )
