"""Pure reducer for the four IP discovery headline metrics."""

from __future__ import annotations

import ipaddress
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.engines.ip.comparison import (
    IPRegisterAuthorityV1,
    IPRegisterComparisonV1,
)

IPMetricHeadingV1 = Literal[
    "Expected Devices",
    "Reachable Devices",
    "Register Matches",
    "Unexpected / Unregistered Hosts",
]
IPMetricLifecycleV1 = Literal["pending", "finalized", "cancelled", "not_attempted"]
IPMetricProbeOutcomeV1 = Literal[
    "connected",
    "connection_refused",
    "timed_out",
    "network_unreachable",
    "host_unreachable",
    "permission_denied",
    "cancelled",
    "unsupported",
    "provider_error",
]

_HEADINGS: tuple[IPMetricHeadingV1, ...] = (
    "Expected Devices",
    "Reachable Devices",
    "Register Matches",
    "Unexpected / Unregistered Hosts",
)
_POSITIVE_OUTCOMES = frozenset({"connected"})
_UNEXPECTED_MATCHES = frozenset(
    {"wrong_ip_review", "ambiguous_review", "unregistered"}
)
_TERMINAL_LIFECYCLES = frozenset({"finalized", "cancelled", "not_attempted"})


def _ipv4(value: str, *, field_name: str) -> str:
    try:
        parsed = ipaddress.ip_address(value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a numeric IPv4 address") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError(f"{field_name} must be a numeric IPv4 address")
    return str(parsed)


def _percentage(value: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    percentage = (Decimal(value) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(percentage)


class IPHeadlineMetricScopeV1(BaseModel):
    """Frozen target and register denominators for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    frozen_targets: tuple[str, ...] = Field(min_length=1)
    authority: IPRegisterAuthorityV1 | None = None

    @field_validator("frozen_targets")
    @classmethod
    def _normalize_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_ipv4(value, field_name="frozen_targets") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("frozen_targets must be unique")
        return normalized


class IPHostMetricStateV1(BaseModel):
    """One versioned host projection consumed by the metric fold."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    target_ip: str
    entity_version: int = Field(ge=1)
    lifecycle_state: IPMetricLifecycleV1
    probe_outcomes: tuple[IPMetricProbeOutcomeV1, ...] = ()
    comparison: IPRegisterComparisonV1

    @field_validator("target_ip")
    @classmethod
    def _normalize_target(cls, value: str) -> str:
        return _ipv4(value, field_name="target_ip")

    @model_validator(mode="after")
    def _validate_state(self) -> IPHostMetricStateV1:
        if self.comparison.observed_ip != self.target_ip:
            raise ValueError("comparison observed_ip must equal target_ip")
        if self.lifecycle_state == "not_attempted" and self.probe_outcomes:
            raise ValueError("not_attempted state cannot carry probe outcomes")
        return self

    @property
    def terminal(self) -> bool:
        return self.lifecycle_state in _TERMINAL_LIFECYCLES

    @property
    def finalized_reachable(self) -> bool:
        return self.lifecycle_state == "finalized" and any(
            outcome in _POSITIVE_OUTCOMES for outcome in self.probe_outcomes
        )


class IPHeadlineMetricV1(BaseModel):
    """One metric value with its immutable denominator and progress counts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    heading: IPMetricHeadingV1
    configured: bool
    value: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    percentage: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    pending_count: int | None = Field(default=None, ge=0)
    finalized_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_configuration(self) -> IPHeadlineMetricV1:
        values = (
            self.value,
            self.denominator,
            self.percentage,
            self.pending_count,
            self.finalized_count,
        )
        if not self.configured:
            if any(value is not None for value in values):
                raise ValueError("unconfigured metrics must use null values")
            return self
        if any(
            value is None
            for value in (
                self.value,
                self.denominator,
                self.pending_count,
                self.finalized_count,
            )
        ):
            raise ValueError("configured metrics require values and progress counts")
        assert self.value is not None
        assert self.denominator is not None
        assert self.pending_count is not None
        assert self.finalized_count is not None
        if self.denominator == 0 and self.percentage is not None:
            raise ValueError("a zero denominator requires a null percentage")
        if self.denominator > 0 and self.percentage is None:
            raise ValueError("a positive denominator requires a percentage")
        if self.value > self.denominator:
            raise ValueError("metric value cannot exceed its denominator")
        if self.pending_count + self.finalized_count != self.denominator:
            raise ValueError("pending and finalized counts must equal the denominator")
        return self


class IPHeadlineMetricsV1(BaseModel):
    """The exact four ordered IP discovery metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"] = "1.0"
    metrics: tuple[IPHeadlineMetricV1, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def _validate_headings(self) -> IPHeadlineMetricsV1:
        if tuple(metric.heading for metric in self.metrics) != _HEADINGS:
            raise ValueError("IP headline metrics must use the canonical heading order")
        return self

    @property
    def expected_devices(self) -> IPHeadlineMetricV1:
        return self.metrics[0]

    @property
    def reachable_devices(self) -> IPHeadlineMetricV1:
        return self.metrics[1]

    @property
    def register_matches(self) -> IPHeadlineMetricV1:
        return self.metrics[2]

    @property
    def unexpected_unregistered_hosts(self) -> IPHeadlineMetricV1:
        return self.metrics[3]


def _configured_metric(
    heading: IPMetricHeadingV1,
    *,
    value: int,
    denominator: int,
    finalized_count: int,
) -> IPHeadlineMetricV1:
    return IPHeadlineMetricV1(
        heading=heading,
        configured=True,
        value=value,
        denominator=denominator,
        percentage=_percentage(value, denominator),
        pending_count=denominator - finalized_count,
        finalized_count=finalized_count,
    )


def _not_configured_metric(heading: IPMetricHeadingV1) -> IPHeadlineMetricV1:
    return IPHeadlineMetricV1(heading=heading, configured=False)


def _latest_states(
    scope: IPHeadlineMetricScopeV1,
    states: tuple[IPHostMetricStateV1, ...],
) -> dict[str, IPHostMetricStateV1]:
    targets = set(scope.frozen_targets)
    latest: dict[str, IPHostMetricStateV1] = {}
    for state in states:
        if state.target_ip not in targets:
            raise ValueError("metric state target is outside the frozen target set")
        existing = latest.get(state.target_ip)
        if existing is None or state.entity_version > existing.entity_version:
            latest[state.target_ip] = state
            continue
        if state.entity_version == existing.entity_version and state != existing:
            raise ValueError("conflicting metric state for one target version")
    return latest


def _device_progress(
    scope: IPHeadlineMetricScopeV1,
    latest: dict[str, IPHostMetricStateV1],
) -> tuple[set[str], set[str]]:
    authority = scope.authority
    assert authority is not None
    scope_targets = set(scope.frozen_targets)
    expected_targets_by_device_key: dict[str, set[str]] = {}
    for row in authority.rows:
        if row.expected_ip in scope_targets:
            expected_targets_by_device_key.setdefault(row.device_key, set()).add(
                row.expected_ip
            )
    matched: set[str] = set()
    finalized: set[str] = set()
    for device_key in authority.expected_device_keys:
        expected_targets = expected_targets_by_device_key.get(device_key, set())
        device_states = tuple(
            latest[target] for target in expected_targets if target in latest
        )
        if any(
            state.finalized_reachable
            and state.comparison.register_match == "expected_match"
            and state.comparison.matched_device_key == device_key
            for state in device_states
        ):
            matched.add(device_key)
            finalized.add(device_key)
            continue
        if not expected_targets or all(
            target in latest and latest[target].terminal for target in expected_targets
        ):
            finalized.add(device_key)
    return matched, finalized


def reduce_ip_headline_metrics(
    scope: IPHeadlineMetricScopeV1,
    states: tuple[IPHostMetricStateV1, ...],
) -> IPHeadlineMetricsV1:
    """Fold latest host versions into stable, denominator-safe metrics."""

    latest = _latest_states(scope, states)
    authority = scope.authority
    for state in latest.values():
        if authority is None:
            if state.comparison.register_match != "not_configured":
                raise ValueError("host comparison does not match the unconfigured scope")
            continue
        if state.comparison.register_match == "not_configured":
            raise ValueError("host comparison is missing the frozen register authority")
        if (
            state.comparison.authority_import_id != authority.import_id
            or state.comparison.authority_rows_sha256 != authority.accepted_rows_sha256
        ):
            raise ValueError("host comparison uses a different register authority")

    target_denominator = len(scope.frozen_targets)
    finalized_targets = sum(state.terminal for state in latest.values())
    reachable_targets = sum(state.finalized_reachable for state in latest.values())
    reachable = _configured_metric(
        "Reachable Devices",
        value=reachable_targets,
        denominator=target_denominator,
        finalized_count=finalized_targets,
    )

    if authority is None:
        return IPHeadlineMetricsV1(
            metrics=(
                _not_configured_metric("Expected Devices"),
                reachable,
                _not_configured_metric("Register Matches"),
                _not_configured_metric("Unexpected / Unregistered Hosts"),
            )
        )

    expected_denominator = len(authority.expected_device_keys)
    matched_devices, finalized_devices = _device_progress(scope, latest)
    expected = _configured_metric(
        "Expected Devices",
        value=expected_denominator,
        denominator=expected_denominator,
        finalized_count=expected_denominator,
    )
    register_matches = _configured_metric(
        "Register Matches",
        value=len(matched_devices),
        denominator=expected_denominator,
        finalized_count=len(finalized_devices),
    )
    unexpected_count = sum(
        state.finalized_reachable
        and state.comparison.register_match in _UNEXPECTED_MATCHES
        for state in latest.values()
    )
    unexpected = _configured_metric(
        "Unexpected / Unregistered Hosts",
        value=unexpected_count,
        denominator=target_denominator,
        finalized_count=finalized_targets,
    )
    return IPHeadlineMetricsV1(
        metrics=(expected, reachable, register_matches, unexpected)
    )


__all__ = [
    "IPHeadlineMetricScopeV1",
    "IPHeadlineMetricV1",
    "IPHeadlineMetricsV1",
    "IPHostMetricStateV1",
    "IPMetricHeadingV1",
    "IPMetricLifecycleV1",
    "IPMetricProbeOutcomeV1",
    "reduce_ip_headline_metrics",
]
