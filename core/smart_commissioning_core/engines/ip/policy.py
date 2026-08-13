"""Shared frozen-provider execution guard for inline and queued IP runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.scan_contract import (
    IPNotAttemptedPortV1,
    NormalizedIPv4TargetsV1,
    ProtocolPortV1,
    normalize_protocol_ports,
)
from smart_commissioning_core.source_interface import FrozenSourceInterfaceV1

BUILTIN_TCP_PROVIDER = "builtin_tcp_connect"

_PROFILE_CEILINGS = {
    "gentle": {"targets": 256, "ports": 64, "attempts": 6_000},
    "planned_extended": {"targets": 4_096, "ports": 4_096, "attempts": 14_000},
}


def _strict_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _contract_ip(parameters: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, Mapping):
        raise ValueError("live IP execution requires scan_contract_v1")
    if contract.get("scan_contract_version") != "1.0":
        raise ValueError("live IP execution requires scan_contract_v1 version 1.0")
    if contract.get("job_type") != "ip_discovery":
        raise ValueError("live IP execution requires an IP discovery contract")
    expected_digest = contract.get("packet_plan_sha256")
    unsigned = {key: value for key, value in contract.items() if key != "packet_plan_sha256"}
    if (
        not isinstance(expected_digest, str)
        or expected_digest != canonical_sha256(unsigned)
    ):
        raise ValueError("frozen IP packet plan digest verification failed")
    ip_contract = contract.get("ip")
    if not isinstance(ip_contract, Mapping):
        raise ValueError("frozen IP provider state is unavailable")
    return contract, ip_contract


def _validate_source_identity(
    parameters: Mapping[str, Any], contract: Mapping[str, Any]
) -> None:
    raw_contract_identity = contract.get("source_interface")
    raw_parameter_identity = parameters.get("source_interface_identity_v1")
    if not isinstance(raw_contract_identity, Mapping) or not isinstance(
        raw_parameter_identity, Mapping
    ):
        raise ValueError("live IP execution requires source_interface_identity_v1")
    contract_identity = FrozenSourceInterfaceV1.model_validate(
        dict(raw_contract_identity)
    )
    parameter_identity = FrozenSourceInterfaceV1.model_validate(
        dict(raw_parameter_identity)
    )
    if contract_identity != parameter_identity:
        raise ValueError("frozen source-interface identity does not match the packet plan")
    if parameters.get("source_ip") != contract_identity.source_ip or parameters.get(
        "local_address"
    ) != contract_identity.local_address:
        raise ValueError("flat source interface does not match the frozen packet plan")


def _validate_flat_plan(
    parameters: Mapping[str, Any], ip_contract: Mapping[str, Any]
) -> None:
    raw_targets = ip_contract.get("targets")
    if not isinstance(raw_targets, Mapping):
        raise ValueError("frozen IP target plan is unavailable")
    targets = NormalizedIPv4TargetsV1.model_validate(dict(raw_targets))
    addresses = targets.expanded_addresses
    if canonical_sha256(addresses) != targets.expanded_target_sha256:
        raise ValueError("frozen IP target digest verification failed")
    raw_addresses = parameters.get("addresses")
    if not isinstance(raw_addresses, Sequence) or isinstance(raw_addresses, (str, bytes)):
        raise ValueError("live IP execution requires flat frozen addresses")
    if list(raw_addresses) != list(addresses):
        raise ValueError("flat IP addresses do not match the frozen target plan")

    raw_ports = ip_contract.get("ports")
    if not isinstance(raw_ports, Sequence) or isinstance(raw_ports, (str, bytes)):
        raise ValueError("frozen IP port plan is unavailable")
    ports = normalize_protocol_ports(
        tuple(ProtocolPortV1.model_validate(value) for value in raw_ports)
    )
    flat_ports = parameters.get("ports")
    if not isinstance(flat_ports, Sequence) or isinstance(flat_ports, (str, bytes)):
        raise ValueError("live IP execution requires flat frozen ports")
    tcp_ports = [item.port for item in ports if item.protocol == "tcp"]
    if list(flat_ports) != tcp_ports:
        raise ValueError("flat IP ports do not match the frozen TCP port plan")

    raw_omissions = ip_contract.get("not_attempted_ports_by_address", {})
    if not isinstance(raw_omissions, Mapping):
        raise ValueError("frozen IP omissions must be a mapping")
    omitted_protocol_ports: set[tuple[str, str, int]] = set()
    for host, entries in raw_omissions.items():
        if host not in addresses or not isinstance(entries, Sequence) or isinstance(
            entries, (str, bytes)
        ):
            raise ValueError("frozen IP omissions are malformed")
        for entry in entries:
            omission = IPNotAttemptedPortV1.model_validate(entry)
            omitted_protocol_ports.add((str(host), omission.protocol, omission.port))
    for item in ports:
        if item.protocol == "udp" and any(
            (host, "udp", item.port) not in omitted_protocol_ports for host in addresses
        ):
            raise ValueError(
                "every frozen UDP port must be represented as a trusted non-dispatch omission"
            )
    if canonical_sha256(parameters.get("not_attempted_ports_by_address", {})) != canonical_sha256(raw_omissions):
        raise ValueError("flat IP omissions do not match the frozen packet plan")


def _validate_profile_ceiling(ip_contract: Mapping[str, Any]) -> None:
    profile = ip_contract.get("profile")
    limits = _PROFILE_CEILINGS.get(profile)
    policy = ip_contract.get("policy")
    if limits is None or not isinstance(policy, Mapping) or policy.get("profile") != profile:
        raise ValueError("frozen IP profile is invalid")
    axes = (
        ("max_targets", limits["targets"]),
        ("max_protocol_ports_per_target", limits["ports"]),
        ("total_dispatch_attempt_ceiling", limits["attempts"]),
    )
    for name, ceiling in axes:
        if _strict_positive_int(policy.get(name), label=f"frozen IP policy {name}") > ceiling:
            raise ValueError(f"frozen IP policy {name} exceeds the {profile} ceiling")


def require_frozen_ip_provider_execution(
    parameters: Mapping[str, Any], *, strict_live: bool = False
) -> None:
    """Validate selected provider state, and seal every live TCP dispatch input.

    Old RunContextV1 rows remain readable in non-live paths.  A progressive
    production execution has no legacy fallback: it must carry a digest-verified
    packet plan whose source, targets, ports, provider, omissions, and policy
    limits agree with the flattened engine parameters.
    """

    if strict_live:
        contract, ip_contract = _contract_ip(parameters)
        _validate_source_identity(parameters, contract)
        _validate_profile_ceiling(ip_contract)
        _validate_flat_plan(parameters, ip_contract)
    else:
        contract = parameters.get("scan_contract_v1")
        if contract is None:
            return
        if not isinstance(contract, Mapping) or contract.get("job_type") != "ip_discovery":
            return
        ip_contract = contract.get("ip")
        if not isinstance(ip_contract, Mapping):
            raise ValueError("frozen IP provider state is unavailable")

    selected_provider = ip_contract.get("provider")
    provider_state = ip_contract.get("provider_state")
    if (
        selected_provider != BUILTIN_TCP_PROVIDER
        or not isinstance(provider_state, Mapping)
        or provider_state.get("provider") != selected_provider
        or provider_state.get("execution_enabled") is not True
    ):
        provider = str(selected_provider or "selected IP provider")
        raise ValueError(f"{provider} execution is disabled by the frozen provider state")


__all__ = ["BUILTIN_TCP_PROVIDER", "require_frozen_ip_provider_execution"]
