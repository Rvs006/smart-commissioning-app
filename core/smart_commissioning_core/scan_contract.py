"""Versioned, deterministic discovery request contracts.

This module contains only normalization and validation.  It performs no network
I/O and is shared by the API, worker, and engines so a sealed preview and its
linked live run can rebuild the same packet-plan inputs.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.run_context import canonical_sha256

MAX_IPV4_HOSTS = 4096
MAX_PROTOCOL_PORTS = 4096
_MAX_PREVIEW_SAMPLE = 32

TargetKind = Literal["cidr", "range", "address"]
PortProtocol = Literal["tcp", "udp"]
IPRegisterPortSource = Literal["forbidden", "expected"]
IPNotAttemptedReason = Literal["profile_port_cap", "unsupported_protocol"]
IPNotAttemptedCapability = Literal["use_bacnet_discovery"]
IPProvider = Literal["builtin_tcp_connect", "operator_managed_nmap"]
IPProfile = Literal["gentle", "planned_extended"]
DiscoveryPolicyProfile = Literal["gentle", "planned_extended", "bacnet_conservative"]
WorkEstimateBasis = Literal["exact_cartesian", "bounded_response_driven"]
DiscoveryAuthorityType = Literal["ip_register", "bacnet_register", "bacnet_points"]
AuthoritySourceDigestKind = Literal["original_file", "legacy_unavailable"]
BacnetLane = Literal["local_broadcast", "foreign_device", "directed_unicast"]
BacnetProperty = Literal[
    "object_name",
    "present_value",
    "units",
    "status_flags",
    "reliability",
    "out_of_service",
    "description",
]

_LANE_ORDER: tuple[BacnetLane, ...] = (
    "local_broadcast",
    "foreign_device",
    "directed_unicast",
)
_PROPERTY_ORDER: tuple[BacnetProperty, ...] = (
    "object_name",
    "present_value",
    "units",
    "status_flags",
    "reliability",
    "out_of_service",
    "description",
)
_BASE_READ_SET: tuple[BacnetProperty, ...] = _PROPERTY_ORDER[:-1]

_LIMITED_BROADCAST = int(ipaddress.IPv4Address("255.255.255.255"))
_MULTICAST_START = int(ipaddress.IPv4Address("224.0.0.0"))
_MULTICAST_END = int(ipaddress.IPv4Address("239.255.255.255"))
_RESERVED_START = int(ipaddress.IPv4Address("240.0.0.0"))
_PORT_TOKEN_RE = re.compile(
    r"^(?P<low>[0-9]{1,5})(?:\s*-\s*(?P<high>[0-9]{1,5}))?"
    r"(?:\s*/\s*(?P<protocol>tcp|udp))?$",
    re.IGNORECASE,
)


class IPv4TargetExpressionV1(BaseModel):
    """One explicit IPv4 address, CIDR, or inclusive address range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TargetKind
    cidr: str | None = None
    start: str | None = None
    end: str | None = None
    address: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> IPv4TargetExpressionV1:
        populated = {
            name
            for name in ("cidr", "start", "end", "address")
            if getattr(self, name) not in (None, "")
        }
        required = {
            "cidr": {"cidr"},
            "range": {"start", "end"},
            "address": {"address"},
        }[self.kind]
        if populated != required:
            raise ValueError(
                f"{self.kind} target requires exactly {', '.join(sorted(required))}"
            )

        if self.kind == "cidr":
            network = _ipv4_network(self.cidr or "")
            start, end = _network_host_bounds(network)
            _reject_disallowed_interval(start, end)
            object.__setattr__(self, "cidr", network.with_prefixlen)
            return self

        if self.kind == "address":
            address = _ipv4_address(self.address or "")
            _reject_disallowed_interval(int(address), int(address))
            object.__setattr__(self, "address", str(address))
            return self

        start = _ipv4_address(self.start or "")
        end = _ipv4_address(self.end or "")
        if int(end) < int(start):
            raise ValueError("end IPv4 address must be greater than or equal to start")
        _reject_disallowed_interval(int(start), int(end))
        object.__setattr__(self, "start", str(start))
        object.__setattr__(self, "end", str(end))
        return self

    def interval(self, *, exclusion: bool = False) -> tuple[int, int]:
        if self.kind == "address":
            value = int(_ipv4_address(self.address or ""))
            return value, value
        if self.kind == "range":
            return int(_ipv4_address(self.start or "")), int(_ipv4_address(self.end or ""))
        network = _ipv4_network(self.cidr or "")
        if exclusion:
            return int(network.network_address), int(network.broadcast_address)
        return _network_host_bounds(network)


class ProtocolPortV1(BaseModel):
    """One transport-aware port. Protocol is never inferred away."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    port: int = Field(ge=1, le=65535)
    protocol: PortProtocol = "tcp"


class IPNotAttemptedPortV1(BaseModel):
    """One frozen register port that the selected IP provider will not dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    port: int = Field(ge=1, le=65535)
    protocol: PortProtocol
    source: IPRegisterPortSource
    reason: IPNotAttemptedReason
    capability: IPNotAttemptedCapability | None = None

    @model_validator(mode="after")
    def validate_reason_and_capability(self) -> IPNotAttemptedPortV1:
        if self.protocol == "tcp":
            if self.reason != "profile_port_cap":
                raise ValueError("TCP omissions require reason=profile_port_cap")
            if self.capability is not None:
                raise ValueError("TCP omissions cannot carry a UDP capability")
            return self
        if self.reason != "unsupported_protocol":
            raise ValueError("UDP omissions require reason=unsupported_protocol")
        if self.port == 47808 and self.capability != "use_bacnet_discovery":
            raise ValueError(
                "UDP/47808 omissions require capability=use_bacnet_discovery"
            )
        if self.port != 47808 and self.capability is not None:
            raise ValueError("only UDP/47808 can use the BACnet discovery capability")
        return self


class IPObservationBudgetV1(BaseModel):
    """Frozen upper bound for the U3 progressive IP observation stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    estimator: Literal["ip_u3_v1"] = "ip_u3_v1"
    planned_observation_rows: int = Field(ge=1)
    planned_observation_payload_bytes: int = Field(ge=1)
    row_ceiling: int = Field(ge=1)
    canonical_payload_byte_ceiling: int = Field(ge=1)
    host_state_rows: int = Field(ge=4)
    # Host-only profiles (for example Nmap host discovery) perform packet work
    # without producing a normalized port observation.
    port_attempt_rows: int = Field(ge=0)
    not_attempted_port_rows: int = Field(ge=0)
    control_diagnostic_rows: Literal[1] = 1

    @model_validator(mode="after")
    def validate_row_breakdown(self) -> IPObservationBudgetV1:
        expected = (
            self.host_state_rows
            + self.port_attempt_rows
            + self.not_attempted_port_rows
            + self.control_diagnostic_rows
        )
        if self.planned_observation_rows != expected:
            raise ValueError("planned observation rows do not match their frozen breakdown")
        if self.planned_observation_rows > self.row_ceiling:
            raise ValueError("planned observation rows exceed the frozen row ceiling")
        if self.planned_observation_payload_bytes > self.canonical_payload_byte_ceiling:
            raise ValueError(
                "planned observation payload bytes exceed the frozen byte ceiling"
            )
        return self


class EffectiveThrottleV1(BaseModel):
    """Finite execution throttle frozen into the packet-plan digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrency: int = Field(ge=1, le=2_147_483_647)
    rate_limit_per_sec: float = Field(gt=0, allow_inf_nan=False)
    connect_timeout_s: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("max_concurrency", mode="before")
    @classmethod
    def validate_concurrency_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_concurrency must be a positive integer")
        return value

    @field_validator("rate_limit_per_sec", "connect_timeout_s", mode="before")
    @classmethod
    def validate_finite_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("throttle values must be finite positive numbers")
        return value


class DiscoveryPolicyV1(BaseModel):
    """Checked-in profile limits after any request-level narrowing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["1.0"] = "1.0"
    profile: DiscoveryPolicyProfile
    max_targets: int = Field(ge=1, le=MAX_IPV4_HOSTS)
    max_protocol_ports_per_target: int | None = Field(
        default=None,
        ge=1,
        le=MAX_PROTOCOL_PORTS,
    )
    total_dispatch_attempt_ceiling: int = Field(ge=1)
    profile_max_concurrency: int = Field(ge=1)
    per_target_concurrency: int = Field(ge=1)
    profile_max_rate_limit_per_sec: float = Field(gt=0, allow_inf_nan=False)
    min_target_spacing_ms: float = Field(gt=0, allow_inf_nan=False)
    retries: int = Field(ge=0, le=16)
    retry_backoff_ms: float = Field(ge=0, allow_inf_nan=False)
    dispatch_phase_seconds: int = Field(ge=1)
    cleanup_margin_seconds: int = Field(ge=0)
    run_deadline_seconds: int = Field(ge=1)
    risk_acknowledgement_required: bool = False
    risk_acknowledged: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> DiscoveryPolicyV1:
        if self.profile_max_concurrency < self.per_target_concurrency:
            raise ValueError(
                "profile_max_concurrency must cover per_target_concurrency"
            )
        if self.dispatch_phase_seconds + self.cleanup_margin_seconds > self.run_deadline_seconds:
            raise ValueError(
                "dispatch phase and cleanup margin exceed the run deadline"
            )
        if self.risk_acknowledgement_required and not self.risk_acknowledged:
            raise ValueError("profile requires an explicit risk acknowledgement")
        return self


class DispatchTargetSampleV1(BaseModel):
    """Bounded target projection for a sealed preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, max_length=512)
    base_dispatch_units: int = Field(ge=1)
    planned_dispatch_attempts: int = Field(ge=1)


class DiscoveryWorkEstimateV1(BaseModel):
    """Deterministic work count and timeout-bounded duration projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    estimate_basis: WorkEstimateBasis
    target_count: int = Field(ge=1)
    known_initial_dispatch_units: int = Field(ge=1)
    known_initial_dispatch_attempts: int = Field(ge=1)
    register_added_dispatch_units: int = Field(ge=0)
    maximum_base_dispatch_units_per_target: int = Field(ge=1)
    planned_dispatch_attempts: int = Field(ge=1)
    derived_executable_attempt_ceiling: int = Field(ge=1)
    dispatch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_samples: tuple[DispatchTargetSampleV1, ...] = Field(
        min_length=1,
        max_length=_MAX_PREVIEW_SAMPLE,
    )
    optimistic_dispatch_seconds: float = Field(ge=0, allow_inf_nan=False)
    conservative_dispatch_seconds: float = Field(gt=0, allow_inf_nan=False)
    cleanup_margin_seconds: int = Field(ge=0)
    optimistic_run_seconds: float = Field(ge=0, allow_inf_nan=False)
    conservative_run_seconds: float = Field(gt=0, allow_inf_nan=False)
    required_authorization_window_seconds: int = Field(ge=1)
    executor_limit_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_estimate(self) -> DiscoveryWorkEstimateV1:
        if self.known_initial_dispatch_attempts < self.known_initial_dispatch_units:
            raise ValueError("known dispatch attempts cannot be below dispatch units")
        if self.planned_dispatch_attempts < self.known_initial_dispatch_attempts:
            raise ValueError("planned dispatch attempts cannot be below known work")
        if self.planned_dispatch_attempts > self.derived_executable_attempt_ceiling:
            raise ValueError("planned work exceeds its derived executable ceiling")
        if self.optimistic_dispatch_seconds > self.conservative_dispatch_seconds:
            raise ValueError("optimistic duration cannot exceed conservative duration")
        if self.optimistic_run_seconds != round(
            self.optimistic_dispatch_seconds + self.cleanup_margin_seconds,
            3,
        ):
            raise ValueError("optimistic run duration does not include cleanup margin")
        if self.conservative_run_seconds != round(
            self.conservative_dispatch_seconds + self.cleanup_margin_seconds,
            3,
        ):
            raise ValueError("conservative run duration does not include cleanup margin")
        return self


class IPv4RangeV1(BaseModel):
    """One immutable, inclusive numeric range in a normalized target plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: str
    end: str
    count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> IPv4RangeV1:
        start = _ipv4_address(self.start)
        end = _ipv4_address(self.end)
        expected = int(end) - int(start) + 1
        if expected < 1:
            raise ValueError("normalized IPv4 range is reversed")
        if self.count != expected:
            raise ValueError("normalized IPv4 range count does not match its bounds")
        object.__setattr__(self, "start", str(start))
        object.__setattr__(self, "end", str(end))
        return self


class ImportAuthorityReferenceV1(BaseModel):
    """Bounded identity for one immutable imported authority record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    import_id: str = Field(min_length=1, max_length=64)
    import_type: DiscoveryAuthorityType
    source_filename: str = Field(min_length=1, max_length=512)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_digest_kind: AuthoritySourceDigestKind = "original_file"
    accepted_rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    authority_schema_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_source_digest(self) -> ImportAuthorityReferenceV1:
        if self.source_digest_kind == "original_file" and self.source_sha256 is None:
            raise ValueError("original_file authority requires source_sha256")
        if self.source_digest_kind == "legacy_unavailable" and self.source_sha256 is not None:
            raise ValueError("legacy_unavailable authority cannot claim a source_sha256")
        return self


class BacnetExpectedDeviceV1(BaseModel):
    """One authority-backed BACnet device target with stable network identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    internetwork_id: str = Field(min_length=1, max_length=255)
    device_instance: int = Field(ge=0, le=4_194_302)
    source_address: str
    network: int | None = Field(default=None, ge=0, le=65_534)
    asset_id: str | None = Field(default=None, max_length=512)
    asset_name: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_identity(self) -> BacnetExpectedDeviceV1:
        internetwork_id = self.internetwork_id.strip()
        if not internetwork_id:
            raise ValueError("internetwork_id must not be blank")
        address = _ipv4_address(self.source_address)
        _reject_disallowed_interval(int(address), int(address))
        object.__setattr__(self, "internetwork_id", internetwork_id)
        object.__setattr__(self, "source_address", str(address))
        for field_name in ("asset_id", "asset_name"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, value.strip() or None)
        return self


class NormalizedIPv4TargetsV1(BaseModel):
    """Bounded preview projection plus the exact expanded addresses for execution.

    ``expanded_addresses`` is deliberately excluded from serialization.  The
    frozen contract stores normalized expressions, grouped ranges, count, and
    digest; execution rebuilds the short (at most 4,096-host) tuple and verifies
    the digest instead of duplicating it in preview and live context JSON.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_expressions: tuple[IPv4TargetExpressionV1, ...]
    exclusions: tuple[IPv4TargetExpressionV1, ...] = ()
    target_count: int = Field(ge=1, le=MAX_IPV4_HOSTS)
    excluded_count: int = Field(ge=0)
    expanded_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grouped_ranges: tuple[IPv4RangeV1, ...]
    sample_addresses: tuple[str, ...]

    @property
    def expanded_addresses(self) -> tuple[str, ...]:
        return tuple(
            str(ipaddress.IPv4Address(value))
            for item in self.grouped_ranges
            for value in range(
                int(ipaddress.IPv4Address(item.start)),
                int(ipaddress.IPv4Address(item.end)) + 1,
            )
        )


class IPScanParametersV1(BaseModel):
    """Operator-supplied IP discovery fields before policy-effective freezing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_contract_version: Literal["1.0"] = "1.0"
    target_expressions: tuple[IPv4TargetExpressionV1, ...] = Field(min_length=1)
    exclusions: tuple[IPv4TargetExpressionV1, ...] = ()
    provider: IPProvider = "builtin_tcp_connect"
    profile: IPProfile = "gentle"
    ports: tuple[ProtocolPortV1, ...] = (
        ProtocolPortV1(port=80),
        ProtocolPortV1(port=443),
        ProtocolPortV1(port=1883),
        ProtocolPortV1(port=502),
    )
    use_register_addresses: bool = False

    @model_validator(mode="after")
    def validate_provider_capability(self) -> IPScanParametersV1:
        if self.provider == "operator_managed_nmap" and not self.ports:
            return self
        normalized = normalize_protocol_ports(self.ports, max_ports=MAX_PROTOCOL_PORTS)
        if self.provider == "builtin_tcp_connect" and any(
            item.protocol == "udp" for item in normalized
        ):
            raise ValueError(
                "builtin_tcp_connect does not support UDP probes; use the BACnet "
                "discovery workflow for BACnet/IP UDP/47808"
            )
        object.__setattr__(self, "ports", normalized)
        return self


class BacnetScanParametersV1(BaseModel):
    """Frozen BACnet transport lanes and property authorization boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scan_contract_version: Literal["1.0"] = "1.0"
    lanes: tuple[BacnetLane, ...] = ("local_broadcast", "directed_unicast")
    local_address: str | None = None
    bacnet_port: int = Field(default=47808, ge=1, le=65535)
    bbmd_address: str | None = None
    bbmd_port: int = Field(default=47808, ge=1, le=65535)
    bbmd_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    instance_low: int = Field(default=0, ge=0, le=4_194_302)
    instance_high: int = Field(default=4_194_302, ge=0, le=4_194_302)
    internetwork_id: str | None = Field(default=None, max_length=255)
    base_read_set: tuple[BacnetProperty, ...] = _BASE_READ_SET
    authorized_property_ceiling: tuple[BacnetProperty, ...] = _BASE_READ_SET

    @model_validator(mode="after")
    def validate_plan(self) -> BacnetScanParametersV1:
        lanes = _ordered_unique(self.lanes, _LANE_ORDER)
        base = _ordered_unique(self.base_read_set, _PROPERTY_ORDER)
        ceiling = _ordered_unique(self.authorized_property_ceiling, _PROPERTY_ORDER)
        if not lanes:
            raise ValueError("at least one BACnet discovery lane is required")
        if "foreign_device" in lanes and not str(self.bbmd_address or "").strip():
            raise ValueError("foreign_device lane requires bbmd_address")
        if self.instance_high < self.instance_low:
            raise ValueError("instance_high must be greater than or equal to instance_low")
        local_address = str(self.local_address or "").strip()
        if local_address:
            if "/" not in local_address:
                raise ValueError("local_address must include an IPv4 prefix")
            try:
                interface = ipaddress.ip_interface(local_address)
            except ValueError as error:
                raise ValueError("local_address must be a valid IPv4 interface") from error
            if not isinstance(interface, ipaddress.IPv4Interface):
                raise ValueError("local_address must be a valid IPv4 interface")
            object.__setattr__(self, "local_address", interface.with_prefixlen)
        missing = tuple(value for value in base if value not in ceiling)
        if missing:
            raise ValueError(
                "base_read_set must be contained by authorized_property_ceiling: "
                + ", ".join(missing)
            )
        object.__setattr__(self, "lanes", lanes)
        object.__setattr__(self, "base_read_set", base)
        object.__setattr__(self, "authorized_property_ceiling", ceiling)
        if self.bbmd_address is not None:
            object.__setattr__(self, "bbmd_address", self.bbmd_address.strip() or None)
        if self.internetwork_id is not None:
            object.__setattr__(self, "internetwork_id", self.internetwork_id.strip() or None)
        return self


def normalize_ipv4_targets(
    *,
    expressions: Sequence[IPv4TargetExpressionV1],
    exclusions: Sequence[IPv4TargetExpressionV1] = (),
    max_hosts: int = MAX_IPV4_HOSTS,
) -> NormalizedIPv4TargetsV1:
    """Return one deterministic numeric target set after exclusions."""

    if not expressions:
        raise ValueError("IP discovery requires at least one target expression")
    if not isinstance(max_hosts, int) or isinstance(max_hosts, bool) or max_hosts < 1:
        raise ValueError("max_hosts must be a positive integer")
    if max_hosts > MAX_IPV4_HOSTS:
        raise ValueError(f"max_hosts cannot exceed the policy ceiling {MAX_IPV4_HOSTS}")

    canonical_expressions = _canonical_expressions(expressions)
    canonical_exclusions = _canonical_expressions(exclusions)
    included = _merge_intervals(item.interval() for item in canonical_expressions)
    excluded = _merge_intervals(item.interval(exclusion=True) for item in canonical_exclusions)
    effective = _subtract_intervals(included, excluded)
    included_count = _interval_count(included)
    target_count = _interval_count(effective)
    excluded_count = included_count - target_count

    if target_count == 0:
        raise ValueError("IP discovery target spec expanded to zero hosts after exclusions")
    if target_count > max_hosts:
        raise ValueError(
            f"IP discovery target spec expands to {target_count} hosts, "
            f"exceeding max_hosts={max_hosts}"
        )

    addresses = tuple(
        str(ipaddress.IPv4Address(value))
        for low, high in effective
        for value in range(low, high + 1)
    )
    grouped_ranges = tuple(
        IPv4RangeV1(
            start=str(ipaddress.IPv4Address(low)),
            end=str(ipaddress.IPv4Address(high)),
            count=high - low + 1,
        )
        for low, high in effective
    )
    sample = _bounded_sample(addresses)
    return NormalizedIPv4TargetsV1(
        target_expressions=canonical_expressions,
        exclusions=canonical_exclusions,
        target_count=target_count,
        excluded_count=excluded_count,
        expanded_target_sha256=canonical_sha256(addresses),
        grouped_ranges=grouped_ranges,
        sample_addresses=sample,
    )


def normalize_protocol_ports(
    ports: Sequence[ProtocolPortV1],
    *,
    max_ports: int = MAX_PROTOCOL_PORTS,
) -> tuple[ProtocolPortV1, ...]:
    """Deduplicate protocol-aware ports and apply the explicit schema-axis cap."""

    if not isinstance(max_ports, int) or isinstance(max_ports, bool) or max_ports < 1:
        raise ValueError("max_ports must be a positive integer")
    if max_ports > MAX_PROTOCOL_PORTS:
        raise ValueError(f"max_ports cannot exceed the policy ceiling {MAX_PROTOCOL_PORTS}")
    unique = {(item.protocol, item.port): item for item in ports}
    normalized = tuple(
        unique[key]
        for key in sorted(unique, key=lambda item: (0 if item[0] == "tcp" else 1, item[1]))
    )
    if not normalized:
        raise ValueError("at least one protocol port is required")
    if len(normalized) > max_ports:
        raise ValueError(
            f"IP discovery resolves to {len(normalized)} protocol ports, "
            f"exceeding max_ports={max_ports}"
        )
    return normalized


def estimate_discovery_work(
    *,
    base_dispatch_units_by_target: Mapping[str, int],
    policy: DiscoveryPolicyV1,
    effective_throttle: EffectiveThrottleV1,
    estimate_basis: WorkEstimateBasis,
    register_added_dispatch_units: int = 0,
    planned_dispatch_attempts: int | None = None,
    dispatch_plan_digest_payload: object | None = None,
    authorization_window_seconds: object | None = None,
    executor_limit_seconds: object = 3_600,
) -> DiscoveryWorkEstimateV1:
    """Freeze count and duration math without performing discovery I/O.

    ``exact_cartesian`` is used when every target/transport pair is known, as
    it is for IP scanning. ``bounded_response_driven`` is used for BACnet,
    where a response can authorize later APDUs but the frozen total-attempt
    ceiling still bounds the executor. The conservative duration assumes every
    planned attempt consumes the longest configured deadline and every retry;
    rate, global concurrency, per-target concurrency, spacing, and retry
    backoff are all represented in the bound.
    """

    if estimate_basis not in {"exact_cartesian", "bounded_response_driven"}:
        raise ValueError(f"unsupported work-estimate basis: {estimate_basis!r}")
    normalized: list[tuple[str, int]] = []
    for raw_target, raw_count in base_dispatch_units_by_target.items():
        target = str(raw_target).strip()
        if not target:
            raise ValueError("dispatch work contains a blank target key")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
            raise ValueError("dispatch-unit counts must be positive integers")
        normalized.append((target, raw_count))
    normalized.sort(key=lambda item: item[0])
    if not normalized:
        raise ValueError("discovery work requires at least one dispatch target")
    if len(normalized) > policy.max_targets:
        raise ValueError(
            f"discovery work has {len(normalized):,} targets, exceeding "
            f"the profile maximum {policy.max_targets:,}"
        )
    if isinstance(register_added_dispatch_units, bool) or not isinstance(
        register_added_dispatch_units, int
    ) or register_added_dispatch_units < 0:
        raise ValueError("register_added_dispatch_units must be a non-negative integer")

    retry_multiplier = policy.retries + 1
    known_units = sum(count for _, count in normalized)
    known_attempts = known_units * retry_multiplier
    if planned_dispatch_attempts is None:
        planned_attempts = known_attempts
    elif (
        isinstance(planned_dispatch_attempts, bool)
        or not isinstance(planned_dispatch_attempts, int)
        or planned_dispatch_attempts < known_attempts
    ):
        raise ValueError(
            "planned_dispatch_attempts must be an integer covering all known work"
        )
    else:
        planned_attempts = planned_dispatch_attempts

    if planned_attempts > policy.total_dispatch_attempt_ceiling:
        raise ValueError(
            f"scan plan resolves to {planned_attempts:,} dispatch attempts, exceeding "
            f"the profile ceiling {policy.total_dispatch_attempt_ceiling:,}"
        )

    derived_ceiling = _derived_executable_attempt_ceiling(
        target_count=len(normalized),
        policy=policy,
        effective_throttle=effective_throttle,
        include_target_capacity=estimate_basis == "exact_cartesian",
    )
    if derived_ceiling < 1:
        raise ValueError(
            "the effective throttle cannot complete one dispatch within the dispatch phase"
        )
    if planned_attempts > derived_ceiling:
        raise ValueError(
            f"scan plan resolves to {planned_attempts:,} dispatch attempts, exceeding "
            f"the derived executable maximum {derived_ceiling:,} for the effective throttle"
        )

    spacing_s = policy.min_target_spacing_ms / 1_000.0
    retry_backoff_s = policy.retry_backoff_ms / 1_000.0
    base_counts = [count for _, count in normalized]
    attempted_counts = [count * retry_multiplier for count in base_counts]

    optimistic_rate = (
        0.0
        if known_units <= 1
        else (known_units - 1) / effective_throttle.rate_limit_per_sec
    )
    optimistic_spacing = max((count - 1) * spacing_s for count in base_counts)
    optimistic_dispatch = round(max(optimistic_rate, optimistic_spacing), 3)

    timeout_s = effective_throttle.connect_timeout_s
    global_rate_duration = (
        (planned_attempts - 1) / effective_throttle.rate_limit_per_sec
    ) + timeout_s
    global_concurrency_duration = (
        math.ceil(planned_attempts / effective_throttle.max_concurrency) * timeout_s
        + policy.retries * retry_backoff_s
    )
    target_concurrency_duration = max(
        math.ceil(attempts / policy.per_target_concurrency) * timeout_s
        + policy.retries * retry_backoff_s
        for attempts in attempted_counts
    )
    target_spacing_duration = max(
        (attempts - 1) * spacing_s + timeout_s
        for attempts in attempted_counts
    )
    retry_chain_duration = (
        retry_multiplier * timeout_s + policy.retries * retry_backoff_s
    )
    conservative_dispatch = round(
        max(
            global_rate_duration,
            global_concurrency_duration,
            target_concurrency_duration,
            target_spacing_duration,
            retry_chain_duration,
        ),
        3,
    )
    if conservative_dispatch > policy.dispatch_phase_seconds:
        raise ValueError(
            f"conservative dispatch estimate {conservative_dispatch:g}s exceeds "
            f"the {policy.dispatch_phase_seconds}s dispatch phase"
        )
    optimistic_run = round(
        optimistic_dispatch + policy.cleanup_margin_seconds,
        3,
    )
    conservative_run = round(
        conservative_dispatch + policy.cleanup_margin_seconds,
        3,
    )
    if conservative_run > policy.run_deadline_seconds:
        raise ValueError(
            f"conservative run estimate {conservative_run:g}s exceeds "
            f"the {policy.run_deadline_seconds}s run deadline"
        )
    required_window = math.ceil(conservative_run)
    executor_limit = _finite_positive_seconds(
        executor_limit_seconds,
        label="executor window",
    )
    if required_window > executor_limit:
        raise ValueError(
            f"executor window {executor_limit:g}s is shorter than the required "
            f"{required_window}s conservative run window"
        )
    if authorization_window_seconds is not None:
        authorization_window = _finite_positive_seconds(
            authorization_window_seconds,
            label="authorization window",
        )
        if required_window > authorization_window:
            raise ValueError(
                f"authorization window {authorization_window:g}s is shorter than the "
                f"required {required_window}s conservative run window"
            )

    sample_items = (
        normalized
        if len(normalized) <= _MAX_PREVIEW_SAMPLE
        else normalized[: _MAX_PREVIEW_SAMPLE // 2]
        + normalized[-(_MAX_PREVIEW_SAMPLE // 2) :]
    )
    samples = tuple(
        DispatchTargetSampleV1(
            target=target,
            base_dispatch_units=count,
            planned_dispatch_attempts=count * retry_multiplier,
        )
        for target, count in sample_items
    )
    digest_payload = (
        dispatch_plan_digest_payload
        if dispatch_plan_digest_payload is not None
        else tuple(normalized)
    )
    return DiscoveryWorkEstimateV1(
        estimate_basis=estimate_basis,
        target_count=len(normalized),
        known_initial_dispatch_units=known_units,
        known_initial_dispatch_attempts=known_attempts,
        register_added_dispatch_units=register_added_dispatch_units,
        maximum_base_dispatch_units_per_target=max(base_counts),
        planned_dispatch_attempts=planned_attempts,
        derived_executable_attempt_ceiling=derived_ceiling,
        dispatch_plan_sha256=canonical_sha256(digest_payload),
        dispatch_samples=samples,
        optimistic_dispatch_seconds=optimistic_dispatch,
        conservative_dispatch_seconds=conservative_dispatch,
        cleanup_margin_seconds=policy.cleanup_margin_seconds,
        optimistic_run_seconds=optimistic_run,
        conservative_run_seconds=conservative_run,
        required_authorization_window_seconds=required_window,
        executor_limit_seconds=executor_limit,
    )


def _derived_executable_attempt_ceiling(
    *,
    target_count: int,
    policy: DiscoveryPolicyV1,
    effective_throttle: EffectiveThrottleV1,
    include_target_capacity: bool,
) -> int:
    timeout_s = effective_throttle.connect_timeout_s
    retry_backoff_s = policy.retry_backoff_ms / 1_000.0
    available_service_s = (
        policy.dispatch_phase_seconds - policy.retries * retry_backoff_s
    )
    if available_service_s < timeout_s:
        return 0

    global_concurrency_capacity = (
        math.floor(available_service_s / timeout_s)
        * effective_throttle.max_concurrency
    )
    rate_capacity = (
        math.floor(
            max(0.0, policy.dispatch_phase_seconds - timeout_s)
            * effective_throttle.rate_limit_per_sec
        )
        + 1
    )
    capacities = [
        policy.total_dispatch_attempt_ceiling,
        global_concurrency_capacity,
        rate_capacity,
    ]
    retry_chain_s = (
        (policy.retries + 1) * timeout_s
        + policy.retries * retry_backoff_s
    )
    if retry_chain_s > policy.dispatch_phase_seconds:
        return 0
    if include_target_capacity:
        per_target_service_capacity = (
            math.floor(available_service_s / timeout_s)
            * policy.per_target_concurrency
        )
        spacing_s = policy.min_target_spacing_ms / 1_000.0
        per_target_spacing_capacity = (
            math.floor(
                max(0.0, policy.dispatch_phase_seconds - timeout_s) / spacing_s
            )
            + 1
        )
        capacities.extend(
            (
                per_target_service_capacity * target_count,
                per_target_spacing_capacity * target_count,
            )
        )
    return max(0, min(capacities))


def _finite_positive_seconds(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a finite positive number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return parsed


def parse_protocol_port_spec(
    value: str,
    *,
    max_ports: int = MAX_PROTOCOL_PORTS,
) -> tuple[ProtocolPortV1, ...]:
    """Parse comma-separated ports/ranges while retaining each protocol."""

    parsed: list[ProtocolPortV1] = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if not token:
            continue
        match = _PORT_TOKEN_RE.fullmatch(token)
        if match is None:
            raise ValueError(f"invalid protocol port token: {token!r}")
        low = int(match.group("low"))
        high = int(match.group("high") or low)
        if high < low:
            raise ValueError(f"reversed protocol port range: {token!r}")
        protocol = str(match.group("protocol") or "tcp").casefold()
        for port in range(low, high + 1):
            parsed.append(ProtocolPortV1(port=port, protocol=protocol))
            if len(parsed) > max_ports:
                raise ValueError(
                    f"IP discovery resolves to more than max_ports={max_ports} protocol ports"
                )
    return normalize_protocol_ports(parsed, max_ports=max_ports)


def _ipv4_address(value: str) -> ipaddress.IPv4Address:
    try:
        parsed = ipaddress.ip_address(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"invalid IPv4 address: {value!r}") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError("IPv6 targets are not supported")
    return parsed


def _ipv4_network(value: str) -> ipaddress.IPv4Network:
    try:
        parsed = ipaddress.ip_network(value.strip(), strict=False)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"invalid IPv4 CIDR: {value!r}") from error
    if not isinstance(parsed, ipaddress.IPv4Network):
        raise ValueError("IPv6 targets are not supported")
    return parsed


def _network_host_bounds(network: ipaddress.IPv4Network) -> tuple[int, int]:
    low = int(network.network_address)
    high = int(network.broadcast_address)
    if network.num_addresses > 2:
        low += 1
        high -= 1
    return low, high


def _reject_disallowed_interval(low: int, high: int) -> None:
    disallowed = (
        (0, 0, "the unspecified address"),
        (_MULTICAST_START, _MULTICAST_END, "multicast space"),
        (_RESERVED_START, _LIMITED_BROADCAST, "reserved or limited-broadcast space"),
    )
    for denied_low, denied_high, label in disallowed:
        if low <= denied_high and denied_low <= high:
            raise ValueError(f"IPv4 target intersects {label}")


def _canonical_expressions(
    expressions: Sequence[IPv4TargetExpressionV1],
) -> tuple[IPv4TargetExpressionV1, ...]:
    unique = {
        (
            item.kind,
            item.cidr or "",
            item.start or "",
            item.end or "",
            item.address or "",
        ): item
        for item in expressions
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda key: (
                unique[key].interval(exclusion=True)[0],
                unique[key].interval(exclusion=True)[1],
                key,
            ),
        )
    )


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for low, high in sorted(intervals):
        if not merged or low > merged[-1][1] + 1:
            merged.append([low, high])
        else:
            merged[-1][1] = max(merged[-1][1], high)
    return tuple((low, high) for low, high in merged)


def _subtract_intervals(
    included: Sequence[tuple[int, int]],
    excluded: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    remaining: list[tuple[int, int]] = []
    for include_low, include_high in included:
        cursor = include_low
        for exclude_low, exclude_high in excluded:
            if exclude_high < cursor:
                continue
            if exclude_low > include_high:
                break
            if exclude_low > cursor:
                remaining.append((cursor, min(include_high, exclude_low - 1)))
            cursor = max(cursor, exclude_high + 1)
            if cursor > include_high:
                break
        if cursor <= include_high:
            remaining.append((cursor, include_high))
    return tuple(remaining)


def _interval_count(intervals: Sequence[tuple[int, int]]) -> int:
    return sum(high - low + 1 for low, high in intervals)


def _bounded_sample(addresses: tuple[str, ...]) -> tuple[str, ...]:
    if len(addresses) <= _MAX_PREVIEW_SAMPLE:
        return addresses
    half = _MAX_PREVIEW_SAMPLE // 2
    return (*addresses[:half], *addresses[-half:])


def _ordered_unique(values: Sequence[str], order: Sequence[str]) -> tuple:
    selected = set(values)
    return tuple(value for value in order if value in selected)
