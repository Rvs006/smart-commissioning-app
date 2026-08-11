"""Resolve discovery requests into bounded, reproducible scan contracts."""

from __future__ import annotations

import copy
import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import ValidationError
from smart_commissioning_core.engines.bacnet_params import (
    DEFAULT_BBMD_PORT,
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_TARGETS,
    TARGET_ADDRESS,
    TARGET_ASSET_ID,
    TARGET_ASSET_NAME,
    TARGET_DEVICE_INSTANCE,
    TARGET_INTERNETWORK_ID,
    TARGET_NETWORK,
    bacnet_mode,
    bbmd_address,
    bbmd_port,
    fd_ttl,
    parse_targets,
)
from smart_commissioning_core.run_context import (
    canonical_bacnet_protocol_key,
    canonical_json_bytes,
    canonical_sha256,
)
from smart_commissioning_core.scan_contract import (
    MAX_IPV4_HOSTS,
    MAX_PROTOCOL_PORTS,
    BacnetExpectedDeviceV1,
    BacnetScanParametersV1,
    DiscoveryPolicyV1,
    EffectiveThrottleV1,
    ImportAuthorityReferenceV1,
    IPScanParametersV1,
    IPv4TargetExpressionV1,
    ProtocolPortV1,
    estimate_discovery_work,
    normalize_ipv4_targets,
    normalize_protocol_ports,
    parse_protocol_port_spec,
)

SCAN_CONTRACT_MAX_BYTES = 256 * 1024
MAX_BACNET_EXPECTED_DEVICES = 4096
DEFAULT_EXECUTOR_LIMIT_SECONDS = 60 * 60

_IP_PROFILE_LIMITS: dict[str, dict[str, int | float | bool]] = {
    "gentle": {
        "max_hosts": 256,
        "max_ports": 64,
        "total_dispatch_attempts": 6_000,
        "max_concurrency": 8,
        "per_target_concurrency": 1,
        "max_rate_limit_per_sec": 10.0,
        "target_spacing_ms": 100.0,
        "retries": 0,
        "retry_backoff_ms": 250.0,
        "dispatch_phase_seconds": 40 * 60,
        "cleanup_margin_seconds": 5 * 60,
        "run_deadline_seconds": 45 * 60,
        "risk_acknowledgement_required": False,
    },
    "planned_extended": {
        "max_hosts": MAX_IPV4_HOSTS,
        "max_ports": MAX_PROTOCOL_PORTS,
        "total_dispatch_attempts": 14_000,
        "max_concurrency": 16,
        "per_target_concurrency": 2,
        "max_rate_limit_per_sec": 10.0,
        "target_spacing_ms": 25.0,
        "retries": 1,
        "retry_backoff_ms": 250.0,
        "dispatch_phase_seconds": 45 * 60,
        "cleanup_margin_seconds": 5 * 60,
        "run_deadline_seconds": 50 * 60,
        "risk_acknowledgement_required": True,
    },
}

_BACNET_POLICY_LIMITS: dict[str, int | float] = {
    "total_dispatch_attempts": 3_500,
    "max_concurrency": 4,
    "per_target_concurrency": 1,
    "max_rate_limit_per_sec": 10.0,
    "target_spacing_ms": 100.0,
    "retries": 1,
    "retry_backoff_ms": 250.0,
    "dispatch_phase_seconds": 44 * 60,
    "cleanup_margin_seconds": 6 * 60,
    "run_deadline_seconds": 50 * 60,
}


class ImportAuthorityRepository(Protocol):
    def list(self, **filters: object) -> list[dict[str, object]]: ...

    def get(self, import_id: str) -> dict[str, object]: ...


def resolve_ip_discovery_parameters(
    parameters: Mapping[str, Any],
    *,
    project_id: str,
    site_id: str,
    import_repository: ImportAuthorityRepository,
    effective_throttle: Mapping[str, object] | None = None,
    authorization_window_seconds: object | None = None,
    executor_limit_seconds: object = DEFAULT_EXECUTOR_LIMIT_SECONDS,
) -> dict[str, Any]:
    """Return a new parameter mapping backed by one selected IP authority.

    Legacy target and integer-port shapes remain accepted at the API boundary.
    They are translated once into ``scan_contract_v1`` and flattened back to the
    current engine keys until the engine consumes the typed contract directly.
    """

    resolved = copy.deepcopy(dict(parameters))
    authority_record, explicit_authority = _select_authority_record(
        repository=import_repository,
        import_id=_optional_text(resolved.get("ip_register_import_id")),
        import_type="ip_register",
        project_id=project_id,
        site_id=site_id,
    )
    authority: ImportAuthorityReferenceV1 | None = None
    accepted_rows: list[dict[str, object]] = []
    if authority_record is not None:
        accepted_rows = _accepted_rows(authority_record)
        if accepted_rows:
            authority = _authority_reference(authority_record, accepted_rows)
        elif explicit_authority:
            raise ValueError("selected IP register has zero accepted rows")

    expressions = _ip_target_expressions(resolved)
    use_register_addresses = bool(resolved.get("use_register_addresses"))
    if not expressions or use_register_addresses:
        register_expressions = _register_address_expressions(accepted_rows)
        if not register_expressions and not expressions:
            if authority_record is not None:
                raise ValueError(
                    "The newest IP register has no accepted scan targets. "
                    "Select an older accepted import explicitly or provide target expressions."
                )
            raise ValueError(
                "No scan targets found. Import an IP register or provide target expressions."
            )
        expressions.extend(register_expressions)

    exclusions = _expression_list(resolved.get("exclusions"), label="exclusions")
    profile = str(resolved.get("profile") or "gentle").strip().casefold()
    limits = _IP_PROFILE_LIMITS.get(profile)
    if limits is None:
        raise ValueError("profile must be 'gentle' or 'planned_extended'")
    if bool(limits["risk_acknowledgement_required"]) and (
        resolved.get("planned_extended_risk_acknowledged") is not True
    ):
        raise ValueError(
            "planned_extended requires an explicit risk acknowledgement"
        )
    requested_max_hosts = _bounded_positive_int(
        resolved.get("max_hosts"),
        default=MAX_IPV4_HOSTS,
        ceiling=MAX_IPV4_HOSTS,
        label="max_hosts",
    )
    max_hosts = min(requested_max_hosts, int(limits["max_hosts"]))
    requested_max_ports = _bounded_positive_int(
        resolved.get("max_ports"),
        default=MAX_PROTOCOL_PORTS,
        ceiling=MAX_PROTOCOL_PORTS,
        label="max_ports",
    )
    max_ports = min(requested_max_ports, int(limits["max_ports"]))
    ports = _request_ports(resolved, max_ports=max_ports)
    request = IPScanParametersV1(
        target_expressions=tuple(expressions),
        exclusions=tuple(exclusions),
        provider=str(resolved.get("provider") or "builtin_tcp_connect"),
        profile=profile,
        ports=ports,
        use_register_addresses=use_register_addresses,
    )
    target_plan = normalize_ipv4_targets(
        expressions=request.target_expressions,
        exclusions=request.exclusions,
        max_hosts=max_hosts,
    )
    normalized_ports = normalize_protocol_ports(request.ports, max_ports=max_ports)

    source_interface = _source_interface_snapshot(resolved)
    policy = _ip_policy(
        resolved,
        profile=profile,
        max_hosts=max_hosts,
        max_ports=max_ports,
    )
    throttle = _ip_effective_throttle(
        parameters=resolved,
        supplied=effective_throttle,
        policy=policy,
        source_interface=source_interface,
        target_addresses=target_plan.expanded_addresses,
    )
    _freeze_execution_policy_parameters(
        resolved,
        policy=policy,
        throttle=throttle,
        attempt_ceiling_key="max_dispatch_attempts",
    )
    mappings = _ip_authority_mappings(accepted_rows)
    work_counts, dispatch_payload, register_added = _ip_dispatch_work(
        target_addresses=target_plan.expanded_addresses,
        selected_ports=normalized_ports,
        mappings=mappings,
        max_ports_per_target=max_ports,
    )
    work_estimate = estimate_discovery_work(
        base_dispatch_units_by_target=work_counts,
        policy=policy,
        effective_throttle=throttle,
        estimate_basis="exact_cartesian",
        register_added_dispatch_units=register_added,
        dispatch_plan_digest_payload=dispatch_payload,
        authorization_window_seconds=authorization_window_seconds,
        executor_limit_seconds=executor_limit_seconds,
    )
    _remove_legacy_derived_fields(resolved)
    resolved["addresses"] = list(target_plan.expanded_addresses)
    resolved["ports"] = [
        item.port for item in normalized_ports if item.protocol == "tcp"
    ]
    if authority is not None:
        resolved["ip_register_import_id"] = authority.import_id
    else:
        resolved.pop("ip_register_import_id", None)
    for key, value in mappings["engine"].items():
        if value:
            resolved[key] = copy.deepcopy(value)

    ip_contract = {
        "provider": request.provider,
        "profile": request.profile,
        "use_register_addresses": request.use_register_addresses,
        "targets": target_plan.model_dump(mode="json"),
        "ports": [item.model_dump(mode="json") for item in normalized_ports],
        "authority": authority.model_dump(mode="json") if authority is not None else None,
        "unsupported_register_ports_by_address": mappings["unsupported"],
        "policy": policy.model_dump(mode="json"),
        "provider_state": _ip_provider_state(request.provider),
        "work_estimate": work_estimate.model_dump(mode="json"),
    }
    packet_plan = {
        "scan_contract_version": "1.0",
        "job_type": "ip_discovery",
        "source_interface": source_interface,
        "resource_keys": [_nic_resource_key(source_interface)],
        "effective_throttle": throttle.model_dump(mode="json"),
        "ip": ip_contract,
    }
    contract = {
        **packet_plan,
        "packet_plan_sha256": canonical_sha256(packet_plan),
    }
    _guard_contract_size(contract)
    resolved["scan_contract_v1"] = copy.deepcopy(contract)
    return resolved


def resolve_bacnet_discovery_parameters(
    parameters: Mapping[str, Any],
    *,
    project_id: str,
    site_id: str,
    import_repository: ImportAuthorityRepository,
    effective_throttle: Mapping[str, object] | None = None,
    authorization_window_seconds: object | None = None,
    executor_limit_seconds: object = DEFAULT_EXECUTOR_LIMIT_SECONDS,
) -> dict[str, Any]:
    """Freeze BACnet transport and selected device/point authorities.

    Device targets are bounded and copied because the existing engine consumes
    them directly.  Point rows stay in their immutable import snapshot; the run
    context carries only the selected record identity, digest, schema, and row
    counts, which keeps a 25,000-point manifest comfortably below the context
    ceiling.
    """

    resolved = copy.deepcopy(dict(parameters))
    device_record, _ = _select_authority_record(
        repository=import_repository,
        import_id=_optional_text(resolved.get("bacnet_register_import_id")),
        import_type="bacnet_register",
        project_id=project_id,
        site_id=site_id,
    )
    point_record, _ = _select_authority_record(
        repository=import_repository,
        import_id=_optional_text(resolved.get("bacnet_points_import_id")),
        import_type="bacnet_points",
        project_id=project_id,
        site_id=site_id,
    )
    if device_record is not None:
        device_rows, device_digest_rows, device_is_legacy = _accepted_bacnet_rows(
            device_record
        )
    else:
        device_rows, device_digest_rows, device_is_legacy = [], [], False
    if point_record is not None:
        point_rows, point_digest_rows, point_is_legacy = _accepted_bacnet_rows(
            point_record
        )
    else:
        point_rows, point_digest_rows, point_is_legacy = [], [], False
    device_authority = (
        _authority_reference(
            device_record,
            device_digest_rows,
            legacy_source_unavailable=device_is_legacy,
        )
        if device_record is not None
        else None
    )
    point_authority = (
        _authority_reference(
            point_record,
            point_digest_rows,
            legacy_source_unavailable=point_is_legacy,
        )
        if point_record is not None
        else None
    )

    internetwork_id = _bacnet_internetwork_id(
        resolved,
        device_rows=device_rows,
        point_rows=point_rows,
        project_id=project_id,
        site_id=site_id,
    )
    expected_devices = _bacnet_expected_devices(
        resolved,
        device_rows=device_rows,
        internetwork_id=internetwork_id,
    )
    if len(expected_devices) > MAX_BACNET_EXPECTED_DEVICES:
        raise ValueError(
            f"BACnet device authority expands to {len(expected_devices)} devices, "
            f"exceeding the {MAX_BACNET_EXPECTED_DEVICES}-device ceiling"
        )

    lanes = ["local_broadcast"]
    mode = bacnet_mode(resolved)
    if mode == MODE_FOREIGN_DEVICE:
        lanes.append("foreign_device")
    if expected_devices:
        lanes.append("directed_unicast")
    request = BacnetScanParametersV1(
        lanes=tuple(lanes),
        local_address=_optional_text(resolved.get("local_address")),
        bacnet_port=resolved.get("bacnet_port") or DEFAULT_BBMD_PORT,
        bbmd_address=bbmd_address(resolved),
        bbmd_port=bbmd_port(resolved),
        bbmd_ttl_seconds=fd_ttl(resolved),
        instance_low=resolved.get("device_instance_low", 0),
        instance_high=resolved.get("device_instance_high", 4_194_302),
        internetwork_id=internetwork_id,
        base_read_set=resolved.get(
            "base_read_set",
            BacnetScanParametersV1.model_fields["base_read_set"].default,
        ),
        authorized_property_ceiling=resolved.get(
            "authorized_property_ceiling",
            BacnetScanParametersV1.model_fields["authorized_property_ceiling"].default,
        ),
    )

    source_interface = _source_interface_snapshot(resolved)
    policy = _bacnet_policy(resolved)
    throttle = _bacnet_effective_throttle(
        parameters=resolved,
        supplied=effective_throttle,
        policy=policy,
    )
    _freeze_execution_policy_parameters(
        resolved,
        policy=policy,
        throttle=throttle,
        attempt_ceiling_key="max_apdu_attempts",
    )
    work_counts = _bacnet_initial_dispatch_work(request, expected_devices)
    work_estimate = estimate_discovery_work(
        base_dispatch_units_by_target=work_counts,
        policy=policy,
        effective_throttle=throttle,
        estimate_basis="bounded_response_driven",
        planned_dispatch_attempts=policy.total_dispatch_attempt_ceiling,
        dispatch_plan_digest_payload={
            "lanes": list(request.lanes),
            "expected_devices": [
                item.model_dump(mode="json") for item in expected_devices
            ],
            "base_read_set": list(request.base_read_set),
            "authorized_property_ceiling": list(
                request.authorized_property_ceiling
            ),
            "total_dispatch_attempt_ceiling": policy.total_dispatch_attempt_ceiling,
        },
        authorization_window_seconds=authorization_window_seconds,
        executor_limit_seconds=executor_limit_seconds,
    )
    raw_targets = resolved.get(PARAM_BACNET_TARGETS)
    include_flat_internetwork = bool(
        _optional_text(parameters.get("internetwork_id"))
        or any(_optional_text(row.get("Internetwork ID")) for row in device_rows)
        or (
            isinstance(raw_targets, (list, tuple))
            and any(
                isinstance(row, Mapping)
                and _optional_text(row.get(TARGET_INTERNETWORK_ID))
                for row in raw_targets
            )
        )
    )
    target_rows = [
        _bacnet_engine_target(item, include_internetwork=include_flat_internetwork)
        for item in expected_devices
    ]
    if target_rows:
        resolved[PARAM_BACNET_TARGETS] = copy.deepcopy(target_rows)
    else:
        resolved.pop(PARAM_BACNET_TARGETS, None)
    if device_authority is not None:
        resolved["bacnet_register_import_id"] = device_authority.import_id
    else:
        resolved.pop("bacnet_register_import_id", None)
    if point_authority is not None:
        resolved["bacnet_points_import_id"] = point_authority.import_id
    else:
        resolved.pop("bacnet_points_import_id", None)
    resolved["internetwork_id"] = request.internetwork_id
    resolved["device_instance_low"] = request.instance_low
    resolved["device_instance_high"] = request.instance_high
    resolved["bacnet_port"] = request.bacnet_port
    resolved["base_read_set"] = list(request.base_read_set)
    resolved["authorized_property_ceiling"] = list(
        request.authorized_property_ceiling
    )

    bacnet_contract = {
        **request.model_dump(mode="json"),
        "authorities": {
            "devices": (
                device_authority.model_dump(mode="json")
                if device_authority is not None
                else None
            ),
            "points": (
                point_authority.model_dump(mode="json")
                if point_authority is not None
                else None
            ),
        },
        "expected_device_count": len(expected_devices),
        "expected_devices": [item.model_dump(mode="json") for item in expected_devices],
        "policy": policy.model_dump(mode="json"),
        "provider_state": _bacnet_provider_state(),
        "response_source_policy": _bacnet_response_source_policy(request),
        "work_estimate": work_estimate.model_dump(mode="json"),
    }
    packet_plan = {
        "scan_contract_version": "1.0",
        "job_type": "bacnet_discovery",
        "source_interface": source_interface,
        "resource_keys": list(_bacnet_resource_keys(request, source_interface)),
        "effective_throttle": throttle.model_dump(mode="json"),
        "bacnet": bacnet_contract,
    }
    contract = {
        **packet_plan,
        "packet_plan_sha256": canonical_sha256(packet_plan),
    }
    _guard_contract_size(contract)
    resolved["scan_contract_v1"] = copy.deepcopy(contract)
    return resolved


def _bacnet_internetwork_id(
    parameters: Mapping[str, Any],
    *,
    device_rows: Sequence[Mapping[str, object]],
    point_rows: Sequence[Mapping[str, object]],
    project_id: str,
    site_id: str,
) -> str:
    requested = _optional_text(parameters.get("internetwork_id"))
    imported = {
        value
        for rows in (device_rows, point_rows)
        for row in rows
        if (value := _optional_text(row.get("Internetwork ID"))) is not None
    }
    if requested is not None and any(value != requested for value in imported):
        raise ValueError(
            "selected BACnet authority internetwork_id does not match the requested site network"
        )
    if requested is None and len(imported) > 1:
        raise ValueError("selected BACnet authorities contain more than one internetwork_id")
    # Existing v0.1.40 configurations did not carry this field.  The scoped
    # fallback is deterministic for historical imports; new configuration and
    # templates stamp an explicit operator-approved value.
    return requested or next(iter(imported), f"{project_id}/{site_id}")


def _bacnet_expected_devices(
    parameters: Mapping[str, Any],
    *,
    device_rows: Sequence[Mapping[str, object]],
    internetwork_id: str,
) -> tuple[BacnetExpectedDeviceV1, ...]:
    raw_targets = parameters.get(PARAM_BACNET_TARGETS)
    if raw_targets:
        parsed = parse_targets(raw_targets)
    else:
        parsed = parse_targets(
            {
                TARGET_ADDRESS: row.get("IP address"),
                TARGET_DEVICE_INSTANCE: row.get("BACnet device instance"),
                TARGET_INTERNETWORK_ID: row.get("Internetwork ID"),
                TARGET_ASSET_ID: row.get("Asset ID"),
                TARGET_ASSET_NAME: row.get("Asset name"),
                TARGET_NETWORK: row.get("BACnet network"),
            }
            for row in device_rows
        )

    normalized: dict[tuple[str, int, str], BacnetExpectedDeviceV1] = {}
    for target in parsed:
        try:
            item = BacnetExpectedDeviceV1(
                internetwork_id=target.internetwork_id or internetwork_id,
                device_instance=target.device_instance,
                source_address=target.address,
                network=target.network,
                asset_id=target.asset_id,
                asset_name=target.asset_name,
            )
        except ValidationError:
            # Historical pre-validator rows followed the shipped skip contract.
            # Their immutable authority digest remains recorded even though an
            # unusable row cannot become a network target.
            continue
        if item.internetwork_id != internetwork_id:
            raise ValueError(
                "BACnet target internetwork_id does not match the selected site network"
            )
        key = (item.internetwork_id, item.device_instance, item.source_address)
        normalized.setdefault(key, item)
    return tuple(
        sorted(
            normalized.values(),
            key=lambda item: (
                item.internetwork_id.casefold(),
                item.device_instance,
                int(ipaddress.IPv4Address(item.source_address)),
            ),
        )
    )


def _bacnet_engine_target(
    item: BacnetExpectedDeviceV1,
    *,
    include_internetwork: bool,
) -> dict[str, object]:
    target: dict[str, object] = {
        TARGET_ADDRESS: item.source_address,
        TARGET_DEVICE_INSTANCE: item.device_instance,
    }
    if include_internetwork:
        target[TARGET_INTERNETWORK_ID] = item.internetwork_id
    if item.network is not None:
        target[TARGET_NETWORK] = item.network
    if item.asset_id is not None:
        target[TARGET_ASSET_ID] = item.asset_id
    if item.asset_name is not None:
        target[TARGET_ASSET_NAME] = item.asset_name
    return target


def _nic_resource_key(source_interface: Mapping[str, str | None]) -> str:
    return f"nic:{source_interface.get('source_ip') or 'auto-default-route'}"


def _bacnet_resource_keys(
    request: BacnetScanParametersV1,
    source_interface: Mapping[str, str | None],
) -> tuple[str, ...]:
    bind_address = (
        request.local_address
        or source_interface.get("source_ip")
        or "0.0.0.0"
    )
    keys = {
        _nic_resource_key(source_interface),
        canonical_bacnet_protocol_key(
            bind_address=bind_address,
            port=request.bacnet_port,
        ),
    }
    if "foreign_device" in request.lanes:
        keys.add(
            canonical_bacnet_protocol_key(
                bind_address=bind_address,
                port=47_809,
            )
        )
    return tuple(sorted(keys))


def _ip_policy(
    parameters: Mapping[str, Any],
    *,
    profile: str,
    max_hosts: int,
    max_ports: int,
) -> DiscoveryPolicyV1:
    limits = _IP_PROFILE_LIMITS[profile]
    total_attempts = _policy_positive_int(
        parameters.get("max_dispatch_attempts"),
        default=int(limits["total_dispatch_attempts"]),
        ceiling=int(limits["total_dispatch_attempts"]),
        label="max_dispatch_attempts",
    )
    retries = _policy_nonnegative_int(
        parameters.get("scan_retries"),
        default=int(limits["retries"]),
        ceiling=int(limits["retries"]),
        label="scan_retries",
    )
    spacing_ms = _policy_minimum_float(
        parameters.get("scan_target_spacing_ms"),
        default=float(limits["target_spacing_ms"]),
        minimum=float(limits["target_spacing_ms"]),
        ceiling=float(limits["dispatch_phase_seconds"]) * 1_000,
        label="scan_target_spacing_ms",
    )
    retry_backoff_ms = _policy_minimum_float(
        parameters.get("scan_retry_backoff_ms"),
        default=float(limits["retry_backoff_ms"]),
        minimum=(float(limits["retry_backoff_ms"]) if retries else 0.0),
        ceiling=float(limits["dispatch_phase_seconds"]) * 1_000,
        label="scan_retry_backoff_ms",
        allow_zero=retries == 0,
    )
    dispatch_phase = _policy_positive_int(
        parameters.get("scan_dispatch_phase_seconds"),
        default=int(limits["dispatch_phase_seconds"]),
        ceiling=int(limits["dispatch_phase_seconds"]),
        label="scan_dispatch_phase_seconds",
    )
    run_deadline = _policy_positive_int(
        parameters.get("scan_run_deadline_seconds"),
        default=int(limits["run_deadline_seconds"]),
        ceiling=int(limits["run_deadline_seconds"]),
        label="scan_run_deadline_seconds",
    )
    cleanup_margin = int(limits["cleanup_margin_seconds"])
    if parameters.get("scan_cleanup_margin_seconds") not in (None, ""):
        requested_cleanup = _policy_positive_int(
            parameters.get("scan_cleanup_margin_seconds"),
            default=cleanup_margin,
            ceiling=cleanup_margin,
            label="scan_cleanup_margin_seconds",
        )
        if requested_cleanup < cleanup_margin:
            raise ValueError(
                "scan_cleanup_margin_seconds cannot reduce the reserved cleanup margin"
            )
    try:
        return DiscoveryPolicyV1(
            profile=profile,
            max_targets=max_hosts,
            max_protocol_ports_per_target=max_ports,
            total_dispatch_attempt_ceiling=total_attempts,
            profile_max_concurrency=int(limits["max_concurrency"]),
            per_target_concurrency=int(limits["per_target_concurrency"]),
            profile_max_rate_limit_per_sec=float(
                limits["max_rate_limit_per_sec"]
            ),
            min_target_spacing_ms=spacing_ms,
            retries=retries,
            retry_backoff_ms=retry_backoff_ms,
            dispatch_phase_seconds=dispatch_phase,
            cleanup_margin_seconds=cleanup_margin,
            run_deadline_seconds=run_deadline,
            risk_acknowledgement_required=bool(
                limits["risk_acknowledgement_required"]
            ),
            risk_acknowledged=(
                parameters.get("planned_extended_risk_acknowledged") is True
            ),
        )
    except ValidationError as error:
        raise ValueError(f"invalid IP scan policy: {error}") from error


def _bacnet_policy(parameters: Mapping[str, Any]) -> DiscoveryPolicyV1:
    total_attempts = _policy_positive_int(
        parameters.get("max_apdu_attempts"),
        default=int(_BACNET_POLICY_LIMITS["total_dispatch_attempts"]),
        ceiling=int(_BACNET_POLICY_LIMITS["total_dispatch_attempts"]),
        label="max_apdu_attempts",
    )
    retries = _policy_nonnegative_int(
        parameters.get("scan_retries"),
        default=int(_BACNET_POLICY_LIMITS["retries"]),
        ceiling=int(_BACNET_POLICY_LIMITS["retries"]),
        label="scan_retries",
    )
    spacing_ms = _policy_minimum_float(
        parameters.get("scan_target_spacing_ms"),
        default=float(_BACNET_POLICY_LIMITS["target_spacing_ms"]),
        minimum=float(_BACNET_POLICY_LIMITS["target_spacing_ms"]),
        ceiling=float(_BACNET_POLICY_LIMITS["dispatch_phase_seconds"]) * 1_000,
        label="scan_target_spacing_ms",
    )
    retry_backoff_ms = _policy_minimum_float(
        parameters.get("scan_retry_backoff_ms"),
        default=float(_BACNET_POLICY_LIMITS["retry_backoff_ms"]),
        minimum=(float(_BACNET_POLICY_LIMITS["retry_backoff_ms"]) if retries else 0.0),
        ceiling=float(_BACNET_POLICY_LIMITS["dispatch_phase_seconds"]) * 1_000,
        label="scan_retry_backoff_ms",
        allow_zero=retries == 0,
    )
    dispatch_phase = _policy_positive_int(
        parameters.get("scan_dispatch_phase_seconds"),
        default=int(_BACNET_POLICY_LIMITS["dispatch_phase_seconds"]),
        ceiling=int(_BACNET_POLICY_LIMITS["dispatch_phase_seconds"]),
        label="scan_dispatch_phase_seconds",
    )
    run_deadline = _policy_positive_int(
        parameters.get("scan_run_deadline_seconds"),
        default=int(_BACNET_POLICY_LIMITS["run_deadline_seconds"]),
        ceiling=int(_BACNET_POLICY_LIMITS["run_deadline_seconds"]),
        label="scan_run_deadline_seconds",
    )
    cleanup_margin = int(_BACNET_POLICY_LIMITS["cleanup_margin_seconds"])
    try:
        return DiscoveryPolicyV1(
            profile="bacnet_conservative",
            max_targets=MAX_BACNET_EXPECTED_DEVICES,
            max_protocol_ports_per_target=None,
            total_dispatch_attempt_ceiling=total_attempts,
            profile_max_concurrency=int(_BACNET_POLICY_LIMITS["max_concurrency"]),
            per_target_concurrency=int(
                _BACNET_POLICY_LIMITS["per_target_concurrency"]
            ),
            profile_max_rate_limit_per_sec=float(
                _BACNET_POLICY_LIMITS["max_rate_limit_per_sec"]
            ),
            min_target_spacing_ms=spacing_ms,
            retries=retries,
            retry_backoff_ms=retry_backoff_ms,
            dispatch_phase_seconds=dispatch_phase,
            cleanup_margin_seconds=cleanup_margin,
            run_deadline_seconds=run_deadline,
        )
    except ValidationError as error:
        raise ValueError(f"invalid BACnet scan policy: {error}") from error


def _ip_effective_throttle(
    *,
    parameters: Mapping[str, Any],
    supplied: Mapping[str, object] | None,
    policy: DiscoveryPolicyV1,
    source_interface: Mapping[str, str | None],
    target_addresses: Sequence[str],
) -> EffectiveThrottleV1:
    timeout_ceiling = 5.0
    if policy.profile == "gentle":
        timeout_ceiling = (
            1.5
            if _all_targets_are_local(
                source_interface.get("local_address"),
                target_addresses,
            )
            else 3.0
        )
    return _effective_throttle(
        parameters=parameters,
        supplied=supplied,
        max_concurrency=policy.profile_max_concurrency,
        max_rate_limit_per_sec=policy.profile_max_rate_limit_per_sec,
        max_timeout_s=timeout_ceiling,
    )


def _bacnet_effective_throttle(
    *,
    parameters: Mapping[str, Any],
    supplied: Mapping[str, object] | None,
    policy: DiscoveryPolicyV1,
) -> EffectiveThrottleV1:
    return _effective_throttle(
        parameters=parameters,
        supplied=supplied,
        max_concurrency=policy.profile_max_concurrency,
        max_rate_limit_per_sec=policy.profile_max_rate_limit_per_sec,
        max_timeout_s=3.0,
    )


def _effective_throttle(
    *,
    parameters: Mapping[str, Any],
    supplied: Mapping[str, object] | None,
    max_concurrency: int,
    max_rate_limit_per_sec: float,
    max_timeout_s: float,
) -> EffectiveThrottleV1:
    if supplied is None:
        concurrency = _policy_positive_int(
            parameters.get("scan_max_concurrency"),
            default=max_concurrency,
            ceiling=2_147_483_647,
            label="scan_max_concurrency",
        )
        raw_rate = parameters.get("scan_rate_limit_per_sec")
        rate = (
            max_rate_limit_per_sec
            if raw_rate in (None, "", 0, 0.0, "0", "0.0")
            else _finite_positive_float(raw_rate, label="scan_rate_limit_per_sec")
        )
        timeout = (
            max_timeout_s
            if parameters.get("scan_connect_timeout_s") in (None, "")
            else _finite_positive_float(
                parameters.get("scan_connect_timeout_s"),
                label="scan_connect_timeout_s",
            )
        )
        supplied_values: Mapping[str, object] = {
            "max_concurrency": concurrency,
            "rate_limit_per_sec": rate,
            "connect_timeout_s": timeout,
        }
    else:
        supplied_values = supplied
    try:
        requested = EffectiveThrottleV1.model_validate(supplied_values)
        return EffectiveThrottleV1(
            max_concurrency=min(requested.max_concurrency, max_concurrency),
            rate_limit_per_sec=min(
                requested.rate_limit_per_sec,
                max_rate_limit_per_sec,
            ),
            connect_timeout_s=min(requested.connect_timeout_s, max_timeout_s),
        )
    except ValidationError as error:
        raise ValueError(f"invalid effective_throttle: {error}") from error


def _all_targets_are_local(
    local_address: str | None,
    target_addresses: Sequence[str],
) -> bool:
    if local_address is None:
        return False
    try:
        network = ipaddress.ip_interface(local_address).network
    except ValueError:
        return False
    return bool(target_addresses) and all(
        ipaddress.ip_address(address) in network for address in target_addresses
    )


def _freeze_execution_policy_parameters(
    parameters: dict[str, Any],
    *,
    policy: DiscoveryPolicyV1,
    throttle: EffectiveThrottleV1,
    attempt_ceiling_key: str,
) -> None:
    """Mirror the sealed policy into the existing engine parameter surface.

    Current engines still read these flat keys. Keeping them equal to the
    nested contract prevents a BACnet backend, inline runner, or historical
    worker from reinterpreting the pre-clamp request while consumers migrate to
    ``scan_contract_v1``.
    """

    parameters["scan_max_concurrency"] = throttle.max_concurrency
    parameters["scan_rate_limit_per_sec"] = throttle.rate_limit_per_sec
    parameters["scan_connect_timeout_s"] = throttle.connect_timeout_s
    parameters["scan_per_target_concurrency"] = policy.per_target_concurrency
    parameters["scan_retries"] = policy.retries
    parameters["scan_target_spacing_ms"] = policy.min_target_spacing_ms
    parameters["scan_retry_backoff_ms"] = policy.retry_backoff_ms
    parameters["scan_dispatch_phase_seconds"] = policy.dispatch_phase_seconds
    parameters["scan_cleanup_margin_seconds"] = policy.cleanup_margin_seconds
    parameters["scan_run_deadline_seconds"] = policy.run_deadline_seconds
    parameters[attempt_ceiling_key] = policy.total_dispatch_attempt_ceiling


def _ip_dispatch_work(
    *,
    target_addresses: Sequence[str],
    selected_ports: Sequence[ProtocolPortV1],
    mappings: Mapping[str, Mapping[str, object]],
    max_ports_per_target: int,
) -> tuple[dict[str, int], list[dict[str, object]], int]:
    base_ports = {
        f"{item.port}/{item.protocol}"
        for item in selected_ports
    }
    engine = mappings.get("engine", {})
    expected = engine.get("expected_ports_by_address")
    forbidden = engine.get("forbidden_ports_by_address")
    expected_map = expected if isinstance(expected, Mapping) else {}
    forbidden_map = forbidden if isinstance(forbidden, Mapping) else {}
    work_counts: dict[str, int] = {}
    digest_payload: list[dict[str, object]] = []
    register_added = 0
    for address in target_addresses:
        protocol_ports = set(base_ports)
        for values in (expected_map, forbidden_map):
            specification = values.get(address)
            if specification in (None, ""):
                continue
            protocol_ports.update(
                f"{item.port}/{item.protocol}"
                for item in parse_protocol_port_spec(str(specification))
            )
        if len(protocol_ports) > max_ports_per_target:
            raise ValueError(
                f"IP target {address} resolves to {len(protocol_ports):,} protocol ports "
                f"after register additions, exceeding the profile maximum "
                f"{max_ports_per_target:,}"
            )
        ordered = sorted(protocol_ports, key=_protocol_port_sort_key)
        work_counts[address] = len(ordered)
        register_added += len(protocol_ports - base_ports)
        digest_payload.append(
            {
                "target": address,
                "protocol_ports": ordered,
            }
        )
    return work_counts, digest_payload, register_added


def _bacnet_initial_dispatch_work(
    request: BacnetScanParametersV1,
    expected_devices: Sequence[BacnetExpectedDeviceV1],
) -> dict[str, int]:
    work = {
        (
            f"device:{item.internetwork_id}:{item.device_instance}:"
            f"{item.source_address}"
        ): 1
        for item in expected_devices
    }
    lane_seeds = sum(
        1 for lane in request.lanes if lane in {"local_broadcast", "foreign_device"}
    )
    if work:
        first_key = min(work)
        work[first_key] += lane_seeds
    else:
        work["lane:discovery"] = max(1, lane_seeds)
    return work


def _ip_provider_state(provider: str) -> dict[str, object]:
    if provider == "builtin_tcp_connect":
        return {
            "provider": provider,
            "capability_state": "available",
            "execution_boundary": "application_owned",
            "execution_enabled": True,
            "supported_protocols": ["tcp"],
        }
    return {
        "provider": provider,
        "capability_state": "requires_internal_operator_confirmation",
        "execution_boundary": "operator_managed_internal_only",
        "execution_enabled": False,
        "supported_protocols": ["tcp", "udp"],
    }


def _bacnet_provider_state() -> dict[str, object]:
    return {
        "provider": "bacpypes3",
        "capability_state": "runtime_preflight_required",
        "execution_boundary": "application_owned_optional_dependency",
        "execution_enabled": True,
        "supported_protocols": ["bacnet_ip_udp"],
    }


def _bacnet_response_source_policy(
    request: BacnetScanParametersV1,
) -> dict[str, object]:
    admission = {
        "local_broadcast": "selected_runtime_interface_subnet",
        "foreign_device": "registered_bbmd_path_and_selected_site_scope",
        "directed_unicast": "frozen_expected_device_sources",
    }
    return {
        "policy_version": "1.0",
        "unmatched_response_action": "quarantine_no_follow_on",
        "lanes": [
            {
                "lane": lane,
                "follow_on_admission": admission[lane],
            }
            for lane in request.lanes
        ],
    }


def _select_authority_record(
    *,
    repository: ImportAuthorityRepository,
    import_id: str | None,
    import_type: str,
    project_id: str,
    site_id: str,
) -> tuple[dict[str, object] | None, bool]:
    if import_id is not None:
        record = repository.get(import_id)
        if (
            record.get("import_type") != import_type
            or record.get("project_id") != project_id
            or record.get("site_id") != site_id
        ):
            raise FileNotFoundError(import_id)
        return copy.deepcopy(record), True
    records = repository.list(
        project_id=project_id,
        site_id=site_id,
        import_type=import_type,
    )
    return (copy.deepcopy(records[0]), False) if records else (None, False)


def _accepted_rows(record: Mapping[str, object]) -> list[dict[str, object]]:
    raw_rows = record.get("accepted_rows")
    if not isinstance(raw_rows, list):
        raise ValueError("selected import accepted rows are malformed")
    rows = [copy.deepcopy(row) for row in raw_rows if isinstance(row, dict)]
    if len(rows) != len(raw_rows):
        raise ValueError("selected import accepted rows contain a malformed row")
    summary = record.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("selected import summary is malformed")
    declared_count = _nonnegative_int(summary.get("accepted_rows"), label="accepted_rows")
    if declared_count != len(rows):
        raise ValueError(
            "selected import accepted-row count does not match the immutable row snapshot"
        )
    stored_digest = _optional_text(summary.get("accepted_rows_sha256"))
    actual_digest = canonical_sha256(rows)
    if stored_digest is not None and stored_digest != actual_digest:
        raise ValueError("selected import accepted-row digest verification failed")
    return rows


def _accepted_bacnet_rows(
    record: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[object], bool]:
    """Return usable rows plus the exact digest payload for historic imports.

    Current imports are strict and must pass the normal count/digest checks.
    Pre-v0.1.41 records may lack source/digest metadata and may contain a corrupt
    JSON value despite being stored in ``accepted_rows``.  Those records remain
    usable for a dry or live contract, but the authority explicitly records that
    its original file digest is unavailable while hashing the complete stored
    snapshot, including values that cannot become targets.
    """

    summary = record.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("selected import summary is malformed")
    raw_rows = record.get("accepted_rows")
    if not isinstance(raw_rows, list):
        raise ValueError("selected import accepted rows are malformed")
    raw_snapshot = copy.deepcopy(raw_rows)
    if summary.get("accepted_rows") is not None:
        declared_count = _nonnegative_int(
            summary.get("accepted_rows"), label="accepted_rows"
        )
        if declared_count != len(raw_snapshot):
            raise ValueError(
                "selected import accepted-row count does not match the immutable row snapshot"
            )
    stored_digest = _optional_text(summary.get("accepted_rows_sha256"))
    if stored_digest is not None and stored_digest != canonical_sha256(raw_snapshot):
        raise ValueError("selected import accepted-row digest verification failed")
    usable_rows = [copy.deepcopy(row) for row in raw_rows if isinstance(row, dict)]
    return (
        usable_rows,
        raw_snapshot,
        _optional_text(summary.get("file_sha256")) is None,
    )


def _authority_reference(
    record: Mapping[str, object],
    accepted_rows: Sequence[object],
    *,
    legacy_source_unavailable: bool = False,
) -> ImportAuthorityReferenceV1:
    summary = record.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("selected import summary is malformed")
    source_sha256 = _optional_text(summary.get("file_sha256"))
    if source_sha256 is None and not legacy_source_unavailable:
        raise ValueError("selected import is missing its original source SHA-256")
    rejected_count = _nonnegative_int(summary.get("rejected_rows", 0), label="rejected_rows")
    try:
        return ImportAuthorityReferenceV1(
            import_id=str(record.get("import_id") or ""),
            import_type=str(record.get("import_type") or ""),
            source_filename=str(record.get("original_filename") or ""),
            source_sha256=source_sha256,
            source_digest_kind=(
                "legacy_unavailable" if legacy_source_unavailable else "original_file"
            ),
            accepted_rows_sha256=canonical_sha256(list(accepted_rows)),
            accepted_count=len(accepted_rows),
            rejected_count=rejected_count,
            authority_schema_version=str(
                summary.get("authority_schema_version")
                or (
                    "legacy-accepted-snapshot-1.0"
                    if legacy_source_unavailable
                    else "legacy-1.0"
                )
            ),
        )
    except ValidationError as error:
        raise ValueError(f"selected import authority metadata is invalid: {error}") from error


def _ip_target_expressions(parameters: Mapping[str, Any]) -> list[IPv4TargetExpressionV1]:
    explicit = parameters.get("target_expressions")
    legacy_present = any(
        parameters.get(key)
        for key in ("cidr", "start", "start_ip", "end", "end_ip", "addresses")
    )
    if explicit is not None:
        if legacy_present:
            raise ValueError("target_expressions cannot be mixed with legacy target fields")
        return _expression_list(explicit, label="target_expressions")

    expressions: list[IPv4TargetExpressionV1] = []
    cidr = parameters.get("cidr")
    if cidr:
        expressions.append(IPv4TargetExpressionV1(kind="cidr", cidr=str(cidr)))
    start = parameters.get("start") or parameters.get("start_ip")
    end = parameters.get("end") or parameters.get("end_ip")
    if start or end:
        expressions.append(
            IPv4TargetExpressionV1(kind="range", start=str(start or ""), end=str(end or ""))
        )
    addresses = parameters.get("addresses")
    if addresses is not None:
        if not isinstance(addresses, (list, tuple)):
            raise ValueError("addresses must be a list of IPv4 address strings")
        expressions.extend(
            IPv4TargetExpressionV1(kind="address", address=str(value))
            for value in addresses
            if str(value).strip()
        )
    return expressions


def _expression_list(value: object, *, label: str) -> list[IPv4TargetExpressionV1]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of versioned target expressions")
    try:
        return [IPv4TargetExpressionV1.model_validate(item) for item in value]
    except ValidationError as error:
        raise ValueError(f"invalid {label}: {error}") from error


def _register_address_expressions(
    rows: Sequence[Mapping[str, object]],
) -> list[IPv4TargetExpressionV1]:
    seen: set[str] = set()
    expressions: list[IPv4TargetExpressionV1] = []
    for row in rows:
        address = _optional_text(row.get("Expected IP address"))
        if address is None or address in seen:
            continue
        seen.add(address)
        expressions.append(IPv4TargetExpressionV1(kind="address", address=address))
    return expressions


def _request_ports(
    parameters: Mapping[str, Any],
    *,
    max_ports: int,
) -> tuple[ProtocolPortV1, ...]:
    raw = parameters.get("ports")
    if raw is None:
        specification = _optional_text(parameters.get("port_specification"))
        if specification is not None:
            return parse_protocol_port_spec(specification, max_ports=max_ports)
        return IPScanParametersV1.model_fields["ports"].default
    if not isinstance(raw, (list, tuple)):
        raise ValueError("ports must be a list of integers or protocol-port objects")
    parsed: list[ProtocolPortV1] = []
    try:
        for value in raw:
            if isinstance(value, Mapping):
                parsed.append(ProtocolPortV1.model_validate(value))
            elif isinstance(value, str) and ("/" in value or "-" in value):
                parsed.extend(parse_protocol_port_spec(value, max_ports=max_ports))
            else:
                parsed.append(ProtocolPortV1(port=int(value), protocol="tcp"))
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError(f"invalid protocol-aware ports: {error}") from error
    return normalize_protocol_ports(parsed, max_ports=max_ports)


def _ip_authority_mappings(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    expected: dict[str, set[int]] = {}
    forbidden: dict[str, set[int]] = {}
    unsupported: dict[str, set[str]] = {}
    hostnames: dict[str, str] = {}
    assets: dict[str, str] = {}
    for row in rows:
        address = _optional_text(row.get("Expected IP address"))
        if address is None:
            continue
        _merge_register_ports(
            expected,
            unsupported,
            address,
            _optional_text(row.get("Expected services/ports")),
        )
        _merge_register_ports(
            forbidden,
            unsupported,
            address,
            _optional_text(row.get("Ports that should not be enabled")),
        )
        hostname = _optional_text(row.get("Expected hostname"))
        if hostname is not None:
            hostnames.setdefault(address, hostname)
        identity = _optional_text(row.get("Asset ID")) or _optional_text(row.get("Asset name"))
        if identity is not None:
            assets.setdefault(address, identity)

    expected_specs = _tcp_specs(expected)
    forbidden_specs = _tcp_specs(forbidden)
    forbidden_union = sorted({port for values in forbidden.values() for port in values})
    engine: dict[str, object] = {
        "expected_ports_by_address": expected_specs,
        "forbidden_ports_by_address": forbidden_specs,
        "expected_hostname_by_address": dict(sorted(hostnames.items())),
        "asset_id_by_address": dict(sorted(assets.items())),
    }
    if forbidden_union:
        engine["forbidden_ports"] = ",".join(f"{port}/tcp" for port in forbidden_union)
    return {
        "engine": engine,
        "unsupported": {
            address: sorted(values, key=_protocol_port_sort_key)
            for address, values in sorted(unsupported.items())
            if values
        },
    }


def _merge_register_ports(
    tcp_map: dict[str, set[int]],
    unsupported_map: dict[str, set[str]],
    address: str,
    specification: str | None,
) -> None:
    if specification is None:
        return
    ports = parse_protocol_port_spec(specification)
    for item in ports:
        if item.protocol == "tcp":
            tcp_map.setdefault(address, set()).add(item.port)
        else:
            unsupported_map.setdefault(address, set()).add(f"{item.port}/{item.protocol}")


def _tcp_specs(values: Mapping[str, set[int]]) -> dict[str, str]:
    return {
        address: ",".join(f"{port}/tcp" for port in sorted(ports))
        for address, ports in sorted(values.items())
        if ports
    }


def _protocol_port_sort_key(value: str) -> tuple[str, int]:
    port, protocol = value.split("/", 1)
    return protocol, int(port)


def _remove_legacy_derived_fields(parameters: dict[str, Any]) -> None:
    for key in (
        "cidr",
        "start",
        "start_ip",
        "end",
        "end_ip",
        "addresses",
        "target_expressions",
        "exclusions",
        "port_specification",
        "expected_ports_by_address",
        "forbidden_ports_by_address",
        "expected_hostname_by_address",
        "asset_id_by_address",
        "forbidden_ports",
    ):
        parameters.pop(key, None)


def _guard_contract_size(contract: Mapping[str, object]) -> None:
    size = len(canonical_json_bytes(contract))
    if size > SCAN_CONTRACT_MAX_BYTES:
        raise ValueError(
            f"scan_contract_v1 is {size} bytes, exceeding the {SCAN_CONTRACT_MAX_BYTES}-byte ceiling"
        )


def _source_interface_snapshot(parameters: dict[str, Any]) -> dict[str, str | None]:
    raw_source = _optional_text(parameters.get("source_ip"))
    raw_local = _optional_text(parameters.get("local_address"))
    source: ipaddress.IPv4Address | None = None
    local: ipaddress.IPv4Interface | None = None
    if raw_source is not None:
        try:
            parsed_source = ipaddress.ip_address(raw_source)
        except ValueError as error:
            raise ValueError("source_ip must be a valid IPv4 address") from error
        if not isinstance(parsed_source, ipaddress.IPv4Address):
            raise ValueError("source_ip must be a valid IPv4 address")
        source = parsed_source
        parameters["source_ip"] = str(source)
    if raw_local is not None:
        if "/" not in raw_local:
            raise ValueError("local_address must preserve an IPv4 prefix")
        try:
            parsed_local = ipaddress.ip_interface(raw_local)
        except ValueError as error:
            raise ValueError("local_address must be a valid IPv4 interface") from error
        if not isinstance(parsed_local, ipaddress.IPv4Interface):
            raise ValueError("local_address must be a valid IPv4 interface")
        local = parsed_local
        parameters["local_address"] = local.with_prefixlen
        if source is None:
            source = local.ip
            parameters["source_ip"] = str(source)
    if source is not None and local is not None and source != local.ip:
        raise ValueError("source_ip and local_address must identify the same interface")
    return {
        "source_ip": str(source) if source is not None else None,
        "local_address": local.with_prefixlen if local is not None else None,
    }


def _bounded_positive_int(
    value: object,
    *,
    default: int,
    ceiling: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a positive integer") from error
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    if parsed > ceiling:
        raise ValueError(f"{label} cannot exceed the policy ceiling {ceiling}")
    return parsed


def _policy_positive_int(
    value: object,
    *,
    default: int,
    ceiling: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    if isinstance(value, float) and (
        not (float("-inf") < value < float("inf")) or not value.is_integer()
    ):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a positive integer") from error
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer")
    if parsed > ceiling:
        raise ValueError(
            f"{label} cannot exceed the policy ceiling {ceiling:,}"
        )
    return parsed


def _policy_nonnegative_int(
    value: object,
    *,
    default: int,
    ceiling: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    if isinstance(value, float) and (
        not (float("-inf") < value < float("inf")) or not value.is_integer()
    ):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a non-negative integer") from error
    if parsed < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    if parsed > ceiling:
        raise ValueError(
            f"{label} cannot exceed the policy ceiling {ceiling:,}"
        )
    return parsed


def _policy_minimum_float(
    value: object,
    *,
    default: float,
    minimum: float,
    ceiling: float,
    label: str,
    allow_zero: bool = False,
) -> float:
    if value in (None, ""):
        return default
    parsed = _finite_nonnegative_float(value, label=label)
    if parsed == 0 and not allow_zero:
        raise ValueError(f"{label} must be a finite positive number")
    if parsed < minimum:
        raise ValueError(
            f"{label} cannot be below the policy minimum {minimum:g}"
        )
    if parsed > ceiling:
        raise ValueError(
            f"{label} cannot exceed the policy ceiling {ceiling:g}"
        )
    return parsed


def _finite_positive_float(value: object, *, label: str) -> float:
    parsed = _finite_nonnegative_float(value, label=label)
    if parsed <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return parsed


def _finite_nonnegative_float(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not (float("-inf") < parsed < float("inf")) or parsed < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return parsed


def _nonnegative_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be a non-negative integer") from error
    if parsed < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return parsed


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
