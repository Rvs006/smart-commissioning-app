"""Server-owned, injection-free Nmap profile and argument contracts.

The models contain no executable, data, output, or operator-supplied file path.
Those paths belong to the separately confirmed installation and executor-owned
temporary artifact layer.  Targets are already-expanded literal IPv4 values
from the sealed preview and are passed to Nmap only through a private ``-iL``
file created by the executor.
"""

from __future__ import annotations

import ipaddress
import math
import re
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.run_context import canonical_sha256

_WINDOWS_GUID_INTERFACE = re.compile(
    r"^windows-guid:(?P<guid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


class NmapProfileName(StrEnum):
    TCP_CONNECT_INVENTORY = "tcp_connect_inventory"
    HOST_DISCOVERY = "host_discovery"
    TCP_SYN_INVENTORY = "tcp_syn_inventory"
    SELECTED_UDP = "selected_udp"
    SERVICE_VERSION_INVENTORY = "service_version_inventory"
    OS_INVENTORY = "os_inventory"
    TRACEROUTE_INVENTORY = "traceroute_inventory"
    REVIEWED_SCRIPT_INVENTORY = "reviewed_script_inventory"


class NmapReviewedScriptV1(BaseModel):
    """One first-party script whose installed bytes are covered by policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NmapScanPlanV1(BaseModel):
    """Complete provider work selected by a checked-in named profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    provider: Literal["operator_managed_nmap"] = "operator_managed_nmap"
    provider_contract_version: Literal["1.0"] = "1.0"
    profile: NmapProfileName
    profile_version: Literal["1.0"] = "1.0"
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[str, ...] = Field(min_length=1, max_length=4096)
    tcp_ports: tuple[int, ...] = Field(default=(), max_length=4096)
    udp_ports: tuple[int, ...] = Field(default=(), max_length=4096)
    source_ip: str
    interface_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9:-]+$")
    interface_name: str = Field(min_length=1, max_length=255)
    nmap_device_name: str | None = Field(default=None, max_length=255)
    profile_arguments: tuple[str, ...]
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = ()
    raw_capability_required: bool
    max_rate_per_second: float = Field(gt=0, le=10)
    max_parallelism: int = Field(ge=1, le=16)
    max_hostgroup: int = Field(ge=1, le=64)
    scan_delay_ms: int = Field(ge=1, le=10_000)
    retries: int = Field(ge=0, le=1)
    host_timeout_seconds: float = Field(gt=0, le=60)
    parent_deadline_seconds: float = Field(gt=0, le=3000)
    output_max_bytes: int = Field(ge=1024, le=67_108_864)
    planned_attempts: int = Field(ge=1, le=14_000)

    @field_validator("source_ip")
    @classmethod
    def _validate_source_ip(cls, value: str) -> str:
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("source_ip must be a numeric IPv4 address") from error
        if not isinstance(parsed, ipaddress.IPv4Address):
            raise ValueError("source_ip must be a numeric IPv4 address")
        return str(parsed)

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        parsed: list[ipaddress.IPv4Address] = []
        for item in value:
            try:
                candidate = ipaddress.ip_address(item)
            except ValueError as error:
                raise ValueError("Nmap targets must be literal IPv4 addresses") from error
            if not isinstance(candidate, ipaddress.IPv4Address):
                raise ValueError("Nmap targets must be literal IPv4 addresses")
            parsed.append(candidate)
        canonical = tuple(str(item) for item in sorted(set(parsed), key=int))
        if canonical != value:
            raise ValueError("Nmap targets must be unique and numerically sorted")
        return canonical

    @field_validator("tcp_ports", "udp_ports")
    @classmethod
    def _validate_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(port, bool) or port < 1 or port > 65535 for port in value):
            raise ValueError("Nmap ports must be integers from 1 through 65535")
        canonical = tuple(sorted(set(value)))
        if canonical != value:
            raise ValueError("Nmap ports must be unique and sorted")
        return canonical

    @field_validator("max_rate_per_second", "host_timeout_seconds", "parent_deadline_seconds")
    @classmethod
    def _finite_numbers(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Nmap limits must be finite")
        return value

    @field_validator("interface_name")
    @classmethod
    def _safe_interface_name(cls, value: str) -> str:
        if (
            value.startswith("-")
            or any(ord(character) < 32 for character in value)
            or "\x00" in value
            or "/" in value
            or "\\" in value
        ):
            raise ValueError("interface_name is invalid")
        return value

    @model_validator(mode="after")
    def _validate_protocol_shape(self) -> NmapScanPlanV1:
        if self.profile is NmapProfileName.HOST_DISCOVERY:
            if self.tcp_ports or self.udp_ports:
                raise ValueError("host_discovery does not accept a port list")
        elif self.profile is NmapProfileName.SELECTED_UDP:
            if self.tcp_ports or not self.udp_ports:
                raise ValueError("selected_udp accepts only explicit UDP ports")
        elif self.udp_ports or not self.tcp_ports:
            raise ValueError("this Nmap profile accepts only explicit TCP ports")
        if self.profile is NmapProfileName.REVIEWED_SCRIPT_INVENTORY:
            if not self.reviewed_scripts:
                raise ValueError("reviewed_script_inventory requires reviewed script identities")
        elif self.reviewed_scripts:
            raise ValueError("reviewed scripts are valid only for reviewed_script_inventory")
        if tuple(sorted(self.reviewed_scripts, key=lambda item: item.name)) != self.reviewed_scripts:
            raise ValueError("reviewed scripts must be unique and sorted")
        if len({item.name for item in self.reviewed_scripts}) != len(self.reviewed_scripts):
            raise ValueError("reviewed scripts must be unique and sorted")
        policy = _PROFILES[self.profile]
        expected_device = nmap_device_name_for_interface_id(self.interface_id)
        if policy.raw_required:
            if expected_device is None or self.nmap_device_name != expected_device:
                raise ValueError(
                    "raw Nmap profiles require the canonical Windows Npcap device identity"
                )
        elif self.nmap_device_name is not None:
            raise ValueError("connect-only Nmap profiles cannot select an Npcap device")
        immutable_values = (
            (self.profile_arguments, policy.arguments),
            (self.raw_capability_required, policy.raw_required),
            (self.max_parallelism, policy.max_parallelism),
            (self.max_hostgroup, policy.max_hostgroup),
            (self.scan_delay_ms, policy.scan_delay_ms),
        )
        if any(actual != expected for actual, expected in immutable_values):
            raise ValueError("Nmap plan does not match its checked-in profile")
        if self.profile_fingerprint != nmap_profile_fingerprint(self.profile):
            raise ValueError("Nmap profile fingerprint is invalid")
        selected_ports = self.udp_ports if self.profile is NmapProfileName.SELECTED_UDP else self.tcp_ports
        expected_attempts = len(self.targets) * (
            policy.probe_units_per_target + len(selected_ports) * policy.probe_units_per_port
        ) * (self.retries + 1)
        if self.planned_attempts != expected_attempts:
            raise ValueError("Nmap planned attempt count is invalid")
        return self


class _ProfilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    arguments: tuple[str, ...]
    raw_required: bool
    max_hosts: int
    max_ports: int
    max_attempts: int
    max_rate: float
    max_retries: int
    max_host_timeout_seconds: float
    max_parent_deadline_seconds: float
    max_output_bytes: int
    max_parallelism: int
    max_hostgroup: int
    scan_delay_ms: int
    probe_units_per_target: int
    probe_units_per_port: int


_PROFILES: dict[NmapProfileName, _ProfilePolicy] = {
    NmapProfileName.TCP_CONNECT_INVENTORY: _ProfilePolicy(
        arguments=("-sT", "-Pn", "--disable-arp-ping"),
        raw_required=False,
        max_hosts=256,
        max_ports=64,
        max_attempts=6000,
        max_rate=8,
        max_retries=0,
        max_host_timeout_seconds=3,
        max_parent_deadline_seconds=2700,
        max_output_bytes=16 * 1024 * 1024,
        max_parallelism=4,
        max_hostgroup=16,
        scan_delay_ms=125,
        probe_units_per_target=0,
        probe_units_per_port=1,
    ),
    NmapProfileName.HOST_DISCOVERY: _ProfilePolicy(
        arguments=("-sn", "-PE", "-PS443", "--disable-arp-ping"),
        raw_required=True,
        max_hosts=256,
        max_ports=0,
        max_attempts=256,
        max_rate=8,
        max_retries=0,
        max_host_timeout_seconds=3,
        max_parent_deadline_seconds=900,
        max_output_bytes=8 * 1024 * 1024,
        max_parallelism=4,
        max_hostgroup=16,
        scan_delay_ms=125,
        probe_units_per_target=2,
        probe_units_per_port=0,
    ),
    NmapProfileName.TCP_SYN_INVENTORY: _ProfilePolicy(
        arguments=("-sS", "-Pn", "--disable-arp-ping"),
        raw_required=True,
        max_hosts=256,
        max_ports=64,
        max_attempts=6000,
        max_rate=8,
        max_retries=0,
        max_host_timeout_seconds=3,
        max_parent_deadline_seconds=2700,
        max_output_bytes=16 * 1024 * 1024,
        max_parallelism=4,
        max_hostgroup=16,
        scan_delay_ms=125,
        probe_units_per_target=0,
        probe_units_per_port=1,
    ),
    NmapProfileName.SELECTED_UDP: _ProfilePolicy(
        arguments=("-sU", "-Pn", "--disable-arp-ping"),
        raw_required=True,
        max_hosts=64,
        max_ports=16,
        max_attempts=1024,
        max_rate=4,
        max_retries=0,
        max_host_timeout_seconds=5,
        max_parent_deadline_seconds=2700,
        max_output_bytes=16 * 1024 * 1024,
        max_parallelism=1,
        max_hostgroup=8,
        scan_delay_ms=250,
        probe_units_per_target=0,
        probe_units_per_port=1,
    ),
    NmapProfileName.SERVICE_VERSION_INVENTORY: _ProfilePolicy(
        arguments=("-sT", "-Pn", "-sV", "--version-light", "--disable-arp-ping"),
        raw_required=False,
        max_hosts=64,
        max_ports=32,
        max_attempts=2048,
        max_rate=4,
        max_retries=0,
        max_host_timeout_seconds=10,
        max_parent_deadline_seconds=2700,
        max_output_bytes=32 * 1024 * 1024,
        max_parallelism=2,
        max_hostgroup=8,
        scan_delay_ms=250,
        probe_units_per_target=0,
        probe_units_per_port=16,
    ),
    NmapProfileName.OS_INVENTORY: _ProfilePolicy(
        arguments=("-sS", "-Pn", "-O", "--osscan-limit", "--disable-arp-ping"),
        raw_required=True,
        max_hosts=32,
        max_ports=16,
        max_attempts=512,
        max_rate=4,
        max_retries=0,
        max_host_timeout_seconds=15,
        max_parent_deadline_seconds=2700,
        max_output_bytes=32 * 1024 * 1024,
        max_parallelism=1,
        max_hostgroup=4,
        scan_delay_ms=500,
        probe_units_per_target=16,
        probe_units_per_port=1,
    ),
    NmapProfileName.TRACEROUTE_INVENTORY: _ProfilePolicy(
        arguments=("-sS", "-Pn", "--traceroute", "--disable-arp-ping"),
        raw_required=True,
        max_hosts=32,
        max_ports=8,
        max_attempts=256,
        max_rate=2,
        max_retries=0,
        max_host_timeout_seconds=15,
        max_parent_deadline_seconds=1800,
        max_output_bytes=16 * 1024 * 1024,
        max_parallelism=1,
        max_hostgroup=4,
        scan_delay_ms=500,
        probe_units_per_target=32,
        probe_units_per_port=1,
    ),
    NmapProfileName.REVIEWED_SCRIPT_INVENTORY: _ProfilePolicy(
        arguments=("-sT", "-Pn", "-sV", "--version-light", "--disable-arp-ping"),
        raw_required=False,
        max_hosts=16,
        max_ports=8,
        max_attempts=128,
        max_rate=2,
        max_retries=0,
        max_host_timeout_seconds=15,
        max_parent_deadline_seconds=1800,
        max_output_bytes=32 * 1024 * 1024,
        max_parallelism=1,
        max_hostgroup=4,
        scan_delay_ms=500,
        probe_units_per_target=0,
        probe_units_per_port=32,
    ),
}


def nmap_profile_fingerprint(profile: NmapProfileName | str) -> str:
    """Hash the exact checked-in profile policy selected by the server."""

    selected = NmapProfileName(profile)
    policy = _PROFILES[selected]
    return canonical_sha256(
        {
            "profile": selected.value,
            "profile_version": "1.0",
            **policy.model_dump(mode="json"),
        }
    )


def nmap_device_name_for_interface_id(interface_id: str) -> str | None:
    """Return Nmap's canonical Npcap device name for one Windows adapter GUID."""

    match = _WINDOWS_GUID_INTERFACE.fullmatch(str(interface_id).strip())
    if match is None:
        return None
    return rf"\Device\NPF_{{{match.group('guid').upper()}}}"


def build_nmap_scan_plan(
    *,
    profile: NmapProfileName | str,
    targets: list[str] | tuple[str, ...],
    tcp_ports: list[int] | tuple[int, ...] = (),
    udp_ports: list[int] | tuple[int, ...] = (),
    source_ip: str,
    interface_id: str,
    interface_name: str,
    packet_plan_sha256: str,
    max_rate_per_second: float | None = None,
    retries: int | None = None,
    host_timeout_seconds: float | None = None,
    parent_deadline_seconds: float | None = None,
    output_max_bytes: int | None = None,
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = (),
) -> NmapScanPlanV1:
    """Build one server-clamped plan; callers cannot supply raw Nmap flags."""

    selected = NmapProfileName(profile)
    policy = _PROFILES[selected]
    nmap_device_name = (
        nmap_device_name_for_interface_id(interface_id) if policy.raw_required else None
    )
    if policy.raw_required and nmap_device_name is None:
        raise ValueError("raw Nmap profiles require a Windows adapter GUID")
    effective_rate = policy.max_rate if max_rate_per_second is None else max_rate_per_second
    effective_retries = policy.max_retries if retries is None else retries
    effective_host_timeout = (
        policy.max_host_timeout_seconds
        if host_timeout_seconds is None
        else host_timeout_seconds
    )
    effective_parent_deadline = (
        policy.max_parent_deadline_seconds
        if parent_deadline_seconds is None
        else parent_deadline_seconds
    )
    effective_output_max = policy.max_output_bytes if output_max_bytes is None else output_max_bytes
    parsed_targets: list[ipaddress.IPv4Address] = []
    for item in targets:
        if not isinstance(item, str):
            raise ValueError("Nmap targets must be literal IPv4 addresses")
        try:
            parsed = ipaddress.ip_address(item)
        except ValueError as error:
            raise ValueError("Nmap targets must be literal IPv4 addresses") from error
        if not isinstance(parsed, ipaddress.IPv4Address):
            raise ValueError("Nmap targets must be literal IPv4 addresses")
        parsed_targets.append(parsed)
    canonical_targets = tuple(str(item) for item in sorted(set(parsed_targets), key=int))
    canonical_tcp_ports = _canonical_ports(tcp_ports)
    canonical_udp_ports = _canonical_ports(udp_ports)
    if len(canonical_targets) > policy.max_hosts:
        raise ValueError(f"{selected.value} permits at most {policy.max_hosts} targets")
    selected_ports = canonical_udp_ports if selected is NmapProfileName.SELECTED_UDP else canonical_tcp_ports
    if len(selected_ports) > policy.max_ports:
        raise ValueError(f"{selected.value} permits at most {policy.max_ports} ports")
    unit_count = policy.probe_units_per_target + (
        len(selected_ports) * policy.probe_units_per_port
    )
    planned_attempts = len(canonical_targets) * unit_count * (int(effective_retries) + 1)
    if planned_attempts > policy.max_attempts:
        raise ValueError(f"{selected.value} exceeds its {policy.max_attempts}-attempt ceiling")
    _require_narrowed("max_rate_per_second", effective_rate, policy.max_rate)
    _require_narrowed("retries", effective_retries, policy.max_retries)
    _require_narrowed(
        "host_timeout_seconds",
        effective_host_timeout,
        policy.max_host_timeout_seconds,
    )
    _require_narrowed(
        "parent_deadline_seconds",
        effective_parent_deadline,
        policy.max_parent_deadline_seconds,
    )
    _require_narrowed("output_max_bytes", effective_output_max, policy.max_output_bytes)
    return NmapScanPlanV1(
        profile=selected,
        profile_fingerprint=nmap_profile_fingerprint(selected),
        packet_plan_sha256=packet_plan_sha256,
        targets=canonical_targets,
        tcp_ports=canonical_tcp_ports,
        udp_ports=canonical_udp_ports,
        source_ip=source_ip,
        interface_id=interface_id,
        interface_name=interface_name,
        nmap_device_name=nmap_device_name,
        profile_arguments=policy.arguments,
        reviewed_scripts=reviewed_scripts,
        raw_capability_required=policy.raw_required,
        max_rate_per_second=effective_rate,
        max_parallelism=policy.max_parallelism,
        max_hostgroup=policy.max_hostgroup,
        scan_delay_ms=policy.scan_delay_ms,
        retries=effective_retries,
        host_timeout_seconds=effective_host_timeout,
        parent_deadline_seconds=effective_parent_deadline,
        output_max_bytes=effective_output_max,
        planned_attempts=planned_attempts,
    )


def _canonical_ports(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    ports: list[int] = []
    for value in values:
        if type(value) is not int or value < 1 or value > 65535:
            raise ValueError("Nmap ports must be integers from 1 through 65535")
        ports.append(value)
    return tuple(sorted(set(ports)))


def _require_narrowed(label: str, value: float | int, maximum: float | int) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    if value <= 0 and label != "retries":
        raise ValueError(f"{label} must be positive")
    if value < 0 or value > maximum:
        raise ValueError(f"{label} cannot exceed the {maximum} profile ceiling")


def render_nmap_target_list(plan: NmapScanPlanV1) -> bytes:
    """Return the exact private ``-iL`` bytes for a sealed plan."""

    return ("\n".join(plan.targets) + "\n").encode("ascii")


def build_nmap_subprocess_arguments(
    plan: NmapScanPlanV1,
    *,
    data_directory: str,
    target_list_path: str,
) -> tuple[str, ...]:
    """Build the child argument vector from typed values only."""

    data_path = _validated_executor_path(data_directory, label="data_directory")
    target_path = _validated_executor_path(target_list_path, label="target_list_path")
    arguments: list[str] = [
        "--noninteractive",
        "--no-stylesheet",
        "-n",
        "-oX",
        "-",
        "--datadir",
        data_path,
        "-iL",
        target_path,
        "-S",
        plan.source_ip,
        "--max-rate",
        format(plan.max_rate_per_second, ".15g"),
        "--max-parallelism",
        str(plan.max_parallelism),
        "--max-hostgroup",
        str(plan.max_hostgroup),
        "--scan-delay",
        f"{plan.scan_delay_ms}ms",
        "--max-retries",
        str(plan.retries),
        "--host-timeout",
        f"{max(1, math.ceil(plan.host_timeout_seconds * 1000))}ms",
        *plan.profile_arguments,
    ]
    if plan.raw_capability_required:
        if plan.nmap_device_name is None:
            raise ValueError("raw Nmap plan is missing its canonical Npcap device identity")
        arguments.extend(("-e", plan.nmap_device_name))
    port_tokens: list[str] = []
    if plan.tcp_ports:
        port_tokens.append("T:" + ",".join(str(port) for port in plan.tcp_ports))
    if plan.udp_ports:
        port_tokens.append("U:" + ",".join(str(port) for port in plan.udp_ports))
    if port_tokens:
        arguments.extend(("-p", ",".join(port_tokens)))
    if plan.reviewed_scripts:
        arguments.append("--script=" + ",".join(script.name for script in plan.reviewed_scripts))
    return tuple(arguments)


def _validated_executor_path(value: str, *, label: str) -> str:
    stripped = str(value).strip()
    if not stripped or "\x00" in stripped or "\r" in stripped or "\n" in stripped:
        raise ValueError(f"{label} must be an absolute executor-owned path")
    if not PureWindowsPath(stripped).is_absolute():
        raise ValueError(f"{label} must be an absolute executor-owned path")
    return stripped
