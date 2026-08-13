"""Resolve discovery requests into bounded, reproducible scan contracts."""

from __future__ import annotations

import copy
import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import ValidationError
from smart_commissioning_core.db.run_lifecycle import (
    DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES,
    DISCOVERY_OBSERVATION_FOLD_MAX_ROWS,
)
from smart_commissioning_core.discovery_observations import (
    PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    normalize_public_string_v1,
    observation_payload,
)
from smart_commissioning_core.engines.bacnet_params import (
    DEFAULT_BBMD_PORT,
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_PORT,
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
from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    NmapReviewedScriptV1,
    NmapScanPlanV1,
    build_nmap_scan_plan,
    nmap_profile_fingerprint,
)
from smart_commissioning_core.engines.ip_scan import (
    IP_PROVIDER_IDENTITY_MAX_MAC_ADDRESS,
    format_ip_status_detail,
    max_provider_identity_hostname_v1,
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
    IPNotAttemptedPortV1,
    IPObservationBudgetV1,
    IPScanParametersV1,
    IPv4TargetExpressionV1,
    ProtocolPortV1,
    estimate_discovery_work,
    normalize_ipv4_targets,
    normalize_protocol_ports,
    parse_protocol_port_spec,
)
from smart_commissioning_core.source_interface import (
    SOURCE_INTERFACE_IDENTITY_KEY,
    FrozenSourceInterfaceV1,
    source_interface_resource_key,
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
    nmap_execution_authority: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Return a new parameter mapping backed by one selected IP authority.

    Legacy target and integer-port shapes remain accepted at the API boundary.
    They are translated once into ``scan_contract_v1`` and flattened back to the
    current engine keys until the engine consumes the typed contract directly.
    """

    resolved = copy.deepcopy(dict(parameters))
    authority_record, _ = _select_authority_record(
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
        authority = _authority_reference(authority_record, accepted_rows)

    expressions = _ip_target_expressions(resolved)
    use_register_addresses = resolved.get("use_register_addresses") is True
    if not expressions and not use_register_addresses:
        raise ValueError(
            "No explicit IP targets were provided. Set use_register_addresses=true "
            "to authorize targets from the selected IP register."
        )
    if use_register_addresses:
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
    selected_provider = str(resolved.get("provider") or "builtin_tcp_connect")
    nmap_profile: NmapProfileName | None = None
    nmap_authority: dict[str, object] | None = None
    if selected_provider == "operator_managed_nmap":
        if profile != "gentle":
            raise ValueError(
                "operator_managed_nmap uses its named fixed profile and cannot widen "
                "the outer IP risk profile"
            )
        nmap_profile = NmapProfileName(
            str(resolved.get("nmap_profile") or NmapProfileName.TCP_CONNECT_INVENTORY.value)
        )
        nmap_authority = _validated_nmap_execution_authority(
            nmap_execution_authority,
            profile=nmap_profile,
        )
    ports = (
        ()
        if nmap_profile is NmapProfileName.HOST_DISCOVERY
        else _request_ports(resolved, max_ports=max_ports)
    )
    request = IPScanParametersV1(
        target_expressions=tuple(expressions),
        exclusions=tuple(exclusions),
        provider=selected_provider,
        profile=profile,
        ports=ports,
        use_register_addresses=use_register_addresses,
    )
    target_plan = normalize_ipv4_targets(
        expressions=request.target_expressions,
        exclusions=request.exclusions,
        max_hosts=max_hosts,
    )
    target_addresses = target_plan.expanded_addresses
    normalized_ports = (
        ()
        if nmap_profile is NmapProfileName.HOST_DISCOVERY
        else normalize_protocol_ports(request.ports, max_ports=max_ports)
    )

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
        target_addresses=target_addresses,
    )
    _freeze_execution_policy_parameters(
        resolved,
        policy=policy,
        throttle=throttle,
        attempt_ceiling_key="max_dispatch_attempts",
    )
    mappings = _ip_authority_mappings(accepted_rows)
    (
        work_counts,
        dispatch_payload,
        register_added,
        not_attempted_ports,
    ) = _ip_dispatch_work(
        target_addresses=target_addresses,
        selected_ports=normalized_ports,
        mappings=mappings,
        max_ports_per_target=max_ports,
    )
    provisional_nmap_plan: NmapScanPlanV1 | None = None
    if nmap_profile is not None:
        assert nmap_authority is not None
        global_protocol_ports = sorted(
            {
                str(protocol_port)
                for target in dispatch_payload
                for protocol_port in target.get("protocol_ports", ())
            },
            key=_protocol_port_sort_key,
        )
        for target in dispatch_payload:
            target["protocol_ports"] = list(global_protocol_ports)
        tcp_ports = tuple(
            int(item.removesuffix("/tcp"))
            for item in global_protocol_ports
            if item.endswith("/tcp")
        )
        udp_ports = tuple(
            int(item.removesuffix("/udp"))
            for item in global_protocol_ports
            if item.endswith("/udp")
        )
        reviewed_scripts = tuple(
            NmapReviewedScriptV1.model_validate(item)
            for item in nmap_authority["reviewed_scripts"]
        )
        if nmap_profile is not NmapProfileName.REVIEWED_SCRIPT_INVENTORY:
            reviewed_scripts = ()
        provisional_nmap_plan = build_nmap_scan_plan(
            profile=nmap_profile,
            targets=target_addresses,
            tcp_ports=tcp_ports,
            udp_ports=udp_ports,
            source_ip=str(source_interface["source_ip"]),
            interface_id=str(source_interface["interface_id"]),
            interface_name=str(source_interface["interface_name"]),
            packet_plan_sha256="0" * 64,
            reviewed_scripts=reviewed_scripts,
        )
        _require_nmap_plan_runtime_capability(
            provisional_nmap_plan,
            authority=nmap_authority,
        )
        attempts_per_target = provisional_nmap_plan.planned_attempts // len(target_addresses)
        work_counts = {address: attempts_per_target for address in target_addresses}
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
    observation_budget = _ip_observation_budget(
        dispatch_payload=dispatch_payload,
        mappings=mappings,
        authority=authority,
        accepted_rows=accepted_rows,
        profile=request.profile,
        source_interface=source_interface,
        retries=(
            provisional_nmap_plan.retries
            if provisional_nmap_plan is not None
            else policy.retries
        ),
        planned_dispatch_attempts=work_estimate.planned_dispatch_attempts,
        project_id=project_id,
        site_id=site_id,
        provider=request.provider,
    )
    _remove_legacy_derived_fields(resolved)
    resolved["addresses"] = list(target_addresses)
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
    resolved["not_attempted_ports_by_address"] = copy.deepcopy(not_attempted_ports)

    ip_contract = {
        "provider": request.provider,
        "profile": request.profile,
        "use_register_addresses": request.use_register_addresses,
        "targets": target_plan.model_dump(mode="json"),
        "ports": [item.model_dump(mode="json") for item in normalized_ports],
        "authority": authority.model_dump(mode="json") if authority is not None else None,
        "unsupported_register_ports_by_address": mappings["unsupported"],
        "not_attempted_ports_by_address": not_attempted_ports,
        "policy": policy.model_dump(mode="json"),
        "provider_state": _ip_provider_state(
            request.provider,
            nmap_authority=nmap_authority,
            nmap_profile=nmap_profile,
        ),
        "work_estimate": work_estimate.model_dump(mode="json"),
        "observation_budget": observation_budget.model_dump(mode="json"),
    }
    packet_plan = {
        "scan_contract_version": "1.0",
        "job_type": "ip_discovery",
        "source_interface": source_interface,
        "resource_keys": [source_interface_resource_key(source_interface)],
        "effective_throttle": throttle.model_dump(mode="json"),
        "ip": ip_contract,
    }
    contract = {
        **packet_plan,
        "packet_plan_sha256": canonical_sha256(packet_plan),
    }
    if provisional_nmap_plan is not None:
        resolved["nmap_scan_plan_v1"] = provisional_nmap_plan.model_copy(
            update={"packet_plan_sha256": contract["packet_plan_sha256"]}
        ).model_dump(mode="json")
    _guard_contract_size(contract)
    resolved["scan_contract_v1"] = copy.deepcopy(contract)
    _guard_resolved_ip_parameters_size(resolved)
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
        bacnet_port=resolved.get(PARAM_BACNET_PORT) or DEFAULT_BBMD_PORT,
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
    resolved[PARAM_BACNET_PORT] = request.bacnet_port
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
    mappings: Mapping[str, object],
    max_ports_per_target: int,
) -> tuple[
    dict[str, int],
    list[dict[str, object]],
    int,
    dict[str, list[dict[str, object]]],
]:
    base_ports = [f"{item.port}/{item.protocol}" for item in selected_ports]
    base_port_set = set(base_ports)
    raw_register_ports = mappings.get("register_ports")
    register_ports = raw_register_ports if isinstance(raw_register_ports, Mapping) else {}
    work_counts: dict[str, int] = {}
    digest_payload: list[dict[str, object]] = []
    register_added = 0
    not_attempted_by_address: dict[str, list[dict[str, object]]] = {}
    selected_register_tcp: dict[str, dict[str, set[int]]] = {
        "forbidden": {},
        "expected": {},
    }
    for address in target_addresses:
        selected = list(base_ports)
        selected_set = set(base_port_set)
        register_union = set(base_port_set)
        omissions: list[dict[str, object]] = []
        for source in ("forbidden", "expected"):
            raw_source_ports = register_ports.get(source)
            source_ports = (
                raw_source_ports if isinstance(raw_source_ports, Mapping) else {}
            )
            raw_values = source_ports.get(address)
            values = raw_values if isinstance(raw_values, Sequence) else ()
            for protocol_port in values:
                item = ProtocolPortV1.model_validate(protocol_port)
                key = f"{item.port}/{item.protocol}"
                register_union.add(key)
                if item.protocol == "udp":
                    omissions.append(
                        IPNotAttemptedPortV1(
                            port=item.port,
                            protocol=item.protocol,
                            source=source,
                            reason="unsupported_protocol",
                            capability=(
                                "use_bacnet_discovery"
                                if item.port == 47808
                                else None
                            ),
                        ).model_dump(mode="json", exclude_none=True)
                    )
                    continue
                if key in selected_set:
                    selected_register_tcp[source].setdefault(address, set()).add(
                        item.port
                    )
                    continue
                if len(selected) < max_ports_per_target:
                    selected.append(key)
                    selected_set.add(key)
                    selected_register_tcp[source].setdefault(address, set()).add(
                        item.port
                    )
                    register_added += 1
                    continue
                omissions.append(
                    IPNotAttemptedPortV1(
                        port=item.port,
                        protocol=item.protocol,
                        source=source,
                        reason="profile_port_cap",
                    ).model_dump(mode="json", exclude_none=True)
                )
        if len(register_union) > MAX_PROTOCOL_PORTS:
            raise ValueError(
                f"IP target {address} resolves to {len(register_union):,} protocol ports "
                f"after register additions, exceeding the hard maximum "
                f"{MAX_PROTOCOL_PORTS:,}"
            )
        ordered = sorted(selected, key=_protocol_port_sort_key)
        work_counts[address] = len(ordered)
        if omissions:
            not_attempted_by_address[address] = omissions
        digest_payload.append(
            {
                "target": address,
                "protocol_ports": ordered,
                "not_attempted_ports": omissions,
            }
        )
    raw_engine = mappings.get("engine")
    if isinstance(raw_engine, dict):
        bounded_expected = _tcp_specs(selected_register_tcp["expected"])
        bounded_forbidden = _tcp_specs(selected_register_tcp["forbidden"])
        raw_engine["expected_ports_by_address"] = bounded_expected
        raw_engine["forbidden_ports_by_address"] = bounded_forbidden
        forbidden_union = sorted(
            {
                port
                for ports in selected_register_tcp["forbidden"].values()
                for port in ports
            }
        )
        if forbidden_union:
            raw_engine["forbidden_ports"] = ",".join(
                f"{port}/tcp" for port in forbidden_union
            )
        else:
            raw_engine.pop("forbidden_ports", None)
    return work_counts, digest_payload, register_added, not_attempted_by_address


def _ip_observation_budget(
    *,
    dispatch_payload: Sequence[Mapping[str, object]],
    mappings: Mapping[str, object],
    authority: ImportAuthorityReferenceV1 | None,
    accepted_rows: Sequence[Mapping[str, object]],
    profile: str,
    source_interface: Mapping[str, object],
    retries: int,
    planned_dispatch_attempts: int,
    project_id: str,
    site_id: str,
    provider: str = "builtin_tcp_connect",
) -> IPObservationBudgetV1:
    """Project the complete U3 stream against the lifecycle fold ceilings.

    Every target can emit four host states. Every frozen dispatch attempt can
    emit one port state, every explicit omission emits one port state, and one
    row is reserved for the only possible active-control diagnostic. Payload
    bytes are a conservative canonical projection using the resolved target,
    authority, port, omission, and device metadata that the engine can emit.
    """

    host_state_rows = 4 * len(dispatch_payload)
    port_attempt_rows = sum(
        len(_ip_budget_protocol_ports(item)) * (retries + 1)
        for item in dispatch_payload
    )
    if provider == "builtin_tcp_connect" and port_attempt_rows != planned_dispatch_attempts:
        raise ValueError(
            "planned IP observation attempts do not match the frozen dispatch plan"
        )
    not_attempted_port_rows = sum(
        len(_ip_budget_omissions(item)) for item in dispatch_payload
    )
    planned_rows = (
        host_state_rows
        + port_attempt_rows
        + not_attempted_port_rows
        + 1
    )
    if planned_rows > DISCOVERY_OBSERVATION_FOLD_MAX_ROWS:
        raise ValueError(
            f"planned IP observations require {planned_rows:,} rows, exceeding the "
            f"{DISCOVERY_OBSERVATION_FOLD_MAX_ROWS:,}-row ceiling"
        )

    engine = mappings.get("engine")
    engine = engine if isinstance(engine, Mapping) else {}
    assets = _ip_budget_text_map(engine.get("asset_id_by_address"))
    expected_by_address = _ip_budget_port_map(
        engine.get("expected_ports_by_address")
    )
    forbidden_by_address = _ip_budget_port_map(
        engine.get("forbidden_ports_by_address")
    )
    global_forbidden = _ip_budget_tcp_ports(engine.get("forbidden_ports"))
    authority_candidates = _ip_budget_authority_candidates(accepted_rows)
    widest_candidates = _ip_budget_widest_authority_candidates(authority_candidates)
    provenance = {
        "profile": profile,
        "source_ip": source_interface.get("source_ip"),
        "source_interface": source_interface.get("local_address"),
        # The final digest has the same canonical byte length as this placeholder.
        "packet_plan_sha256": "0" * 64,
        "register_import_id": authority.import_id if authority is not None else None,
        "register_rows_sha256": (
            authority.accepted_rows_sha256 if authority is not None else None
        ),
    }
    terminal_timestamp = "9999-12-31T23:59:59.999999+00:00"
    # Runtime elapsed values are bounded by the run deadline, but their JSON
    # float spelling can be wider than the largest integer. This finite value
    # reserves Python's widest normal float representation without adding a
    # blind per-row byte allowance.
    canonical_elapsed_width = 1.7976931348623157e308
    planned_payload_bytes = 0
    largest_diagnostic_bytes = 0

    for position, item in enumerate(dispatch_payload):
        target = str(item.get("target") or "")
        protocol_ports = _ip_budget_protocol_ports(item)
        selected_ports = [port for _protocol, port in protocol_ports]
        omissions = _ip_budget_omissions(item)
        expected = expected_by_address.get(target)
        forbidden = forbidden_by_address.get(target, global_forbidden)
        asset_id = assets.get(target)
        register_match = (
            "not_configured"
            if authority is None
            else "expected_match"
            if target in assets or target in expected_by_address
            else "unregistered"
        )
        host_attempts = len(protocol_ports) * (retries + 1)
        worst_status = _ip_budget_status_detail(
            selected_ports=selected_ports,
            expected=expected,
            forbidden=forbidden,
            has_register_identity=(asset_id is not None or expected is not None),
        )
        host_payload = _ip_budget_payload(
            target=target,
            provenance=provenance,
            coverage_state="attempted",
            reachability_state="reachable",
            probe_outcome="connected",
            register_match=register_match,
            policy_verdict="unexpected_open_review",
            attempts=host_attempts,
            elapsed_ms=canonical_elapsed_width,
            reason=worst_status,
            control_reason="dispatch_deadline_elapsed",
            last_packet_dispatched_at=terminal_timestamp,
            provider=provider,
        )
        host_payload_bytes = _ip_budget_payload_bytes(host_payload)
        planned_payload_bytes += host_payload_bytes * 3

        omitted_ports = sorted(
            int(omission.get("port") or 0)
            for omission in omissions
            if int(omission.get("port") or 0) > 0
        )
        final_payload_bytes: list[int] = []
        for candidate in (None, *widest_candidates):
            projection = _ip_budget_device_projection(
                candidate=candidate,
                fallback_asset_id=asset_id,
                target=target,
                project_id=project_id,
                site_id=site_id,
                position=position,
                selected_ports=selected_ports,
                expected=expected,
                forbidden=forbidden,
                omitted_ports=omitted_ports,
                register_match=register_match,
            )
            final_payload = dict(host_payload)
            final_payload["projection_v1"] = projection
            final_payload_bytes.append(_ip_budget_payload_bytes(final_payload))
        planned_payload_bytes += max(final_payload_bytes)

        for protocol, port in protocol_ports:
            port_hint = {80: "http", 443: "https", 502: "modbus", 1883: "mqtt"}.get(
                port
            )
            for attempt in range(1, retries + 2):
                planned_payload_bytes += _ip_budget_payload_bytes(
                    _ip_budget_payload(
                        target=target,
                        provenance=provenance,
                        coverage_state="attempted",
                        reachability_state="unconfirmed",
                        probe_outcome="timed_out",
                        register_match=register_match,
                        policy_verdict="unconfirmed",
                        attempts=attempt,
                        elapsed_ms=canonical_elapsed_width,
                        reason=(
                            "The TCP connection did not complete before the application "
                            "deadline."
                        ),
                        transport=protocol,
                        port=port,
                        port_hint=port_hint,
                        last_packet_dispatched_at=terminal_timestamp,
                        provider=provider,
                    )
                )

        for omission in omissions:
            reason = str(omission.get("reason") or "not_attempted")
            capability = omission.get("capability")
            if isinstance(capability, str):
                reason = f"{reason}: {capability}"
            planned_payload_bytes += _ip_budget_payload_bytes(
                _ip_budget_payload(
                    target=target,
                    provenance=provenance,
                    coverage_state="not_attempted",
                    reachability_state="not_applicable",
                    register_match=register_match,
                    policy_verdict="not_attempted",
                    attempts=0,
                    elapsed_ms=0,
                    reason=reason,
                    transport=str(omission.get("protocol") or ""),
                    port=int(omission.get("port") or 0),
                    capability_action=(
                        capability
                        if capability == "use_bacnet_discovery"
                        else None
                    ),
                    provider=provider,
                )
            )

        diagnostic_payload = _ip_budget_payload(
            target=target,
            provenance=provenance,
            coverage_state="not_attempted",
            reachability_state="not_applicable",
            register_match=register_match,
            policy_verdict="not_attempted",
            attempts=host_attempts,
            elapsed_ms=canonical_elapsed_width,
            reason="Active control was lost before the packet plan completed.",
            control_reason="dispatch_deadline_elapsed",
            last_packet_dispatched_at=terminal_timestamp,
            provider=provider,
        )
        largest_diagnostic_bytes = max(
            largest_diagnostic_bytes,
            _ip_budget_payload_bytes(diagnostic_payload),
        )

    planned_payload_bytes += largest_diagnostic_bytes
    if planned_payload_bytes > DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"planned IP observation payload requires {planned_payload_bytes:,} canonical "
            f"UTF-8 bytes, exceeding the "
            f"{DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES:,}-byte ceiling"
        )
    return IPObservationBudgetV1(
        planned_observation_rows=planned_rows,
        planned_observation_payload_bytes=planned_payload_bytes,
        row_ceiling=DISCOVERY_OBSERVATION_FOLD_MAX_ROWS,
        canonical_payload_byte_ceiling=DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES,
        host_state_rows=host_state_rows,
        port_attempt_rows=port_attempt_rows,
        not_attempted_port_rows=not_attempted_port_rows,
    )


def _ip_budget_protocol_ports(
    item: Mapping[str, object],
) -> list[tuple[str, int]]:
    raw = item.get("protocol_ports")
    values = raw if isinstance(raw, list | tuple) else ()
    parsed: list[tuple[str, int]] = []
    for value in values:
        port, protocol = str(value).split("/", 1)
        parsed.append((protocol, int(port)))
    return parsed


def _ip_budget_omissions(item: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = item.get("not_attempted_ports")
    values = raw if isinstance(raw, list | tuple) else ()
    return [value for value in values if isinstance(value, Mapping)]


def _ip_budget_text_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def _ip_budget_port_map(value: object) -> dict[str, set[int]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _ip_budget_tcp_ports(item)
        for key, item in value.items()
        if _ip_budget_tcp_ports(item)
    }


def _ip_budget_tcp_ports(value: object) -> set[int]:
    if not isinstance(value, str):
        return set()
    ports: set[int] = set()
    for token in value.split(","):
        port, separator, protocol = token.strip().partition("/")
        if separator and protocol == "tcp" and port.isdigit():
            ports.add(int(port))
    return ports


def _ip_budget_authority_candidates(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, str | None]]:
    """Return every authority row that could contribute to a device projection.

    The observation fold only stores viewer-safe projections. Validate every
    candidate before reducing the set so a smaller unsafe row cannot hide behind
    a larger safe row during preview.
    """

    candidates: list[dict[str, str | None]] = []
    for index, row in enumerate(rows):
        expected_ip = _optional_text(row.get("Expected IP address"))
        if expected_ip is None:
            continue
        try:
            parsed_ip = ipaddress.ip_address(expected_ip)
        except ValueError as error:
            raise ValueError(
                f"IP authority row {index + 1} has an invalid expected IPv4 address"
            ) from error
        if not isinstance(parsed_ip, ipaddress.IPv4Address):
            raise ValueError(
                f"IP authority row {index + 1} has an invalid expected IPv4 address"
            )

        candidate: dict[str, str | None] = {"expected_ip": str(parsed_ip)}
        for source_key, target_key, max_chars in (
            ("Asset ID", "asset_id", PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS),
            ("Asset name", "asset_name", PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS),
            ("Expected hostname", "hostname", 253),
        ):
            value = _optional_text(row.get(source_key))
            candidate[target_key] = (
                normalize_public_string_v1(
                    value,
                    path=(
                        "payload.projection_v1.record.attributes."
                        f"{target_key}[authority_row={index + 1}]"
                    ),
                    max_chars=max_chars,
                )
                if value is not None
                else None
            )
        candidates.append(candidate)
    return candidates


def _ip_budget_widest_authority_candidates(
    candidates: Sequence[Mapping[str, str | None]],
) -> tuple[dict[str, str | None], ...]:
    """Build one conservative projection candidate from the widest fields.

    A terminal device projection can carry all four authority fields together.
    Combining each field's widest canonical UTF-8 value gives a deterministic
    upper bound without multiplying every target by every accepted register row.
    """

    if not candidates:
        return ()

    def widest(field: str) -> str | None:
        values = [candidate.get(field) for candidate in candidates]
        populated = [value for value in values if value is not None]
        if not populated:
            return None
        return max(
            populated,
            key=lambda value: (len(canonical_json_bytes(value)), value),
        )

    return (
        {
            "expected_ip": widest("expected_ip"),
            "asset_id": widest("asset_id"),
            "asset_name": widest("asset_name"),
            "hostname": widest("hostname"),
        },
    )


def _ip_budget_device_projection(
    *,
    candidate: Mapping[str, str | None] | None,
    fallback_asset_id: str | None,
    target: str,
    project_id: str,
    site_id: str,
    position: int,
    selected_ports: Sequence[int],
    expected: set[int] | None,
    forbidden: set[int],
    omitted_ports: Sequence[int],
    register_match: str,
) -> dict[str, object]:
    """Project the widest terminal IP device row accepted by the public schema."""

    authority = candidate or {}
    expected_ip = authority.get("expected_ip")
    register_asset_id = authority.get("asset_id")
    register_asset_name = authority.get("asset_name")
    expected_hostname = authority.get("hostname")
    runtime_hostname = max_provider_identity_hostname_v1()
    asset_id = register_asset_id or register_asset_name or fallback_asset_id
    ports = sorted(set(selected_ports))
    expected_ports = sorted(expected) if expected is not None else None
    forbidden_ports = sorted(forbidden)
    forbidden_open = [port for port in ports if port in forbidden]
    unexpected_open = (
        [port for port in ports if port not in expected]
        if expected is not None
        else ports
    )
    missing_expected = (
        [port for port in expected_ports if port in ports]
        if expected_ports is not None
        else ports
    )
    attributes: dict[str, object] = {
        "asset_id": asset_id,
        "open_ports": ports,
        "scanned_ports": ports,
        "scanned_port_count": len(ports),
        "expected_ports": expected_ports,
        "forbidden_ports": forbidden_ports,
        "forbidden_open_ports": forbidden_open,
        "unexpected_open_ports": unexpected_open,
        "missing_expected_ports": missing_expected,
        "register_ports_not_probed": sorted(set(omitted_ports)),
        "hostname": runtime_hostname,
        "expected_hostname": expected_hostname,
        "mac_address": IP_PROVIDER_IDENTITY_MAX_MAC_ADDRESS,
        "register_match": register_match,
        "reachable": True,
        "position": position,
    }
    if expected_ip is not None:
        attributes["register_address"] = expected_ip
    if register_asset_id is not None:
        attributes["register_asset_id"] = register_asset_id
    if register_asset_name is not None:
        attributes["register_asset_name"] = register_asset_name
    return {
        "collection": "devices",
        "position": position,
        "present": True,
        "record": {
            "project_id": project_id,
            "site_id": site_id,
            "address": target,
            "device_type": "ip_host",
            "name": runtime_hostname,
            "attributes": attributes,
        },
    }


def _ip_budget_status_detail(
    *,
    selected_ports: Sequence[int],
    expected: set[int] | None,
    forbidden: set[int],
    has_register_identity: bool,
) -> str:
    flagged = sorted(port for port in selected_ports if port in forbidden)
    unexpected = (
        sorted(port for port in selected_ports if port not in expected)
        if expected is not None
        else []
    )
    missing = sorted(port for port in expected or set() if port in selected_ports)
    silent = f"no response on scanned ports ({len(selected_ports)} probed)"
    if has_register_identity:
        silent += (
            " | EXPECTED BY REGISTER: expected from the register import but did not "
            "answer this scan - inconclusive, not proof the host is offline"
        )
    refused = "reachable: TCP connection refused on probed ports"
    responsive = format_ip_status_detail(
        "responsive",
        responsive_ports=selected_ports,
        forbidden_open_ports=flagged,
        unexpected_open_ports=unexpected,
        missing_expected_ports=missing,
    )
    refused = format_ip_status_detail(
        refused,
        missing_expected_ports=missing,
    )
    widest = max(
        (responsive, silent, refused),
        key=lambda item: len(item.encode("utf-8")),
    )
    return widest[:PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS]


def _ip_budget_payload(
    *,
    target: str,
    provenance: Mapping[str, object],
    coverage_state: str,
    reachability_state: str,
    register_match: str,
    policy_verdict: str,
    attempts: int,
    elapsed_ms: int | float,
    probe_outcome: str | None = None,
    reason: str | None = None,
    transport: str | None = None,
    port: int | None = None,
    port_hint: str | None = None,
    capability_action: str | None = None,
    control_reason: str | None = None,
    last_packet_dispatched_at: str | None = None,
    provider: str = "builtin_tcp_connect",
) -> dict[str, object]:
    return {
        "ip_v1": {
            "schema_version": "1.0",
            "coverage_state": coverage_state,
            "reachability_state": reachability_state,
            "probe_outcome": probe_outcome,
            "register_match": register_match,
            "policy_verdict": policy_verdict,
            "target": target,
            "port": port,
            "transport": transport,
            "provider": provider,
            "provider_version": "1.0",
            "provider_contract_version": "1.0",
            "provenance": dict(provenance),
            "reason": reason,
            "attempts": attempts,
            "elapsed_ms": elapsed_ms,
            "port_hint": port_hint,
            "detected_service": None,
            "detected_version": None,
            "capability_action": capability_action,
            "control_reason": control_reason,
            "last_packet_dispatched_at": last_packet_dispatched_at,
        }
    }


def _ip_budget_payload_bytes(payload: dict[str, object]) -> int:
    try:
        _normalized, encoded, _digest = observation_payload(payload)
    except ValueError as error:
        if "65,536-byte canonical UTF-8 byte limit" in str(error):
            raise ValueError(
                "planned IP observation row exceeds the 65,536-byte canonical limit"
            ) from error
        raise ValueError(f"planned IP observation payload is invalid: {error}") from error
    return len(encoded)


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


def _validated_nmap_execution_authority(
    authority: Mapping[str, object] | None,
    *,
    profile: NmapProfileName,
) -> dict[str, object]:
    if not isinstance(authority, Mapping):
        raise ValueError("Nmap execution authority is required before provider selection")
    capability = authority.get("capability")
    machine_identity = authority.get("machine_executor_identity")
    deployment_id = authority.get("deployment_id")
    network_executor_id = authority.get("network_executor_id")
    confirmation_id = authority.get("confirmation_id")
    reviewed_scripts = authority.get("reviewed_scripts", ())
    if (
        not isinstance(capability, Mapping)
        or capability.get("state") != "available"
        or capability.get("provider_mode") != "internal_operator_managed"
        or capability.get("process_selection_allowed") is not True
        or not isinstance(machine_identity, str)
        or not machine_identity.strip()
        or not isinstance(deployment_id, str)
        or not deployment_id.strip()
        or not isinstance(network_executor_id, str)
        or not network_executor_id.strip()
        or not isinstance(confirmation_id, str)
        or not confirmation_id.strip()
        or not isinstance(reviewed_scripts, Sequence)
        or isinstance(reviewed_scripts, (str, bytes))
    ):
        raise ValueError("Nmap execution authority is unavailable or incomplete")
    permitted_profiles = capability.get("permitted_profiles")
    if not isinstance(permitted_profiles, Sequence) or isinstance(
        permitted_profiles, (str, bytes)
    ):
        raise ValueError("Nmap execution authority has no profile policy")
    if profile.value not in {str(item) for item in permitted_profiles}:
        raise ValueError(f"Nmap profile {profile.value} is not permitted by deployment policy")
    fingerprints = {
        "policy_id": capability.get("policy_id"),
        "policy_revision": capability.get("policy_revision"),
        "publisher": capability.get("publisher"),
        "version": capability.get("version"),
        "fingerprint_sha256": capability.get("fingerprint_sha256"),
        "npcap_version": capability.get("npcap_version"),
        "npcap_state": capability.get("npcap_state"),
        "raw_capable": capability.get("raw_capable") is True,
        "machine_executor_identity": machine_identity.strip(),
        "deployment_id": deployment_id.strip(),
        "network_executor_id": network_executor_id.strip(),
        "confirmation_id": confirmation_id.strip(),
        "reviewed_scripts": [
            NmapReviewedScriptV1.model_validate(item).model_dump(mode="json")
            for item in reviewed_scripts
        ],
    }
    # This object is frozen into the packet plan and therefore may contain only
    # public identifiers and digests. Executable/data paths stay in the protected
    # execution-authority object held by the service.
    return fingerprints


def refresh_nmap_scan_plan_packet_digest(parameters: dict[str, object]) -> None:
    """Keep the typed Nmap plan bound to the final control-identity digest."""

    raw_plan = parameters.get("nmap_scan_plan_v1")
    contract = parameters.get("scan_contract_v1")
    if raw_plan is None:
        return
    if not isinstance(contract, Mapping):
        raise ValueError("Nmap scan plan requires scan_contract_v1")
    digest = contract.get("packet_plan_sha256")
    if not isinstance(digest, str):
        raise ValueError("Nmap scan plan requires a packet-plan digest")
    plan = NmapScanPlanV1.model_validate(raw_plan)
    parameters["nmap_scan_plan_v1"] = plan.model_copy(
        update={"packet_plan_sha256": digest}
    ).model_dump(mode="json")


def validate_nmap_scan_plan_execution_authority(
    parameters: Mapping[str, object],
    authority: Mapping[str, object],
) -> None:
    """Reject a sealed Nmap plan that the current runtime cannot execute."""

    raw_plan = parameters.get("nmap_scan_plan_v1")
    if not isinstance(raw_plan, Mapping):
        raise ValueError("Nmap scan plan is missing from the sealed preview")
    plan = NmapScanPlanV1.model_validate(raw_plan)
    validated = _validated_nmap_execution_authority(
        authority,
        profile=plan.profile,
    )
    _require_nmap_plan_runtime_capability(plan, authority=validated)


def _require_nmap_plan_runtime_capability(
    plan: NmapScanPlanV1,
    *,
    authority: Mapping[str, object],
) -> None:
    if plan.raw_capability_required and authority.get("raw_capable") is not True:
        raise ValueError(
            f"Nmap profile {plan.profile.value} requires raw packet capability"
        )


def _ip_provider_state(
    provider: str,
    *,
    nmap_authority: Mapping[str, object] | None = None,
    nmap_profile: NmapProfileName | None = None,
) -> dict[str, object]:
    if provider == "builtin_tcp_connect":
        return {
            "provider": provider,
            "capability_state": "available",
            "execution_boundary": "application_owned",
            "execution_enabled": True,
            "supported_protocols": ["tcp"],
        }
    if provider == "operator_managed_nmap" and nmap_authority is not None:
        return {
            "provider": provider,
            "provider_contract_version": "1.0",
            "capability_state": "available",
            "execution_boundary": "operator_managed_internal_only",
            "execution_enabled": True,
            "supported_protocols": ["tcp", "udp"],
            "profile": None if nmap_profile is None else nmap_profile.value,
            "profile_fingerprint": (
                None if nmap_profile is None else nmap_profile_fingerprint(nmap_profile)
            ),
            "expected_executor_identity": nmap_authority["machine_executor_identity"],
            "deployment_id": nmap_authority["deployment_id"],
            "network_executor_id": nmap_authority["network_executor_id"],
            "policy_id": nmap_authority["policy_id"],
            "policy_revision": nmap_authority["policy_revision"],
            "confirmation_id": nmap_authority["confirmation_id"],
            "installation_fingerprint_sha256": nmap_authority["fingerprint_sha256"],
            "publisher": nmap_authority["publisher"],
            "version": nmap_authority["version"],
            "npcap_version": nmap_authority["npcap_version"],
            "npcap_state": nmap_authority["npcap_state"],
            "raw_capable": nmap_authority["raw_capable"],
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
) -> dict[str, object]:
    expected: dict[str, set[int]] = {}
    forbidden: dict[str, set[int]] = {}
    unsupported: dict[str, set[str]] = {}
    expected_register_ports: dict[str, set[str]] = {}
    forbidden_register_ports: dict[str, set[str]] = {}
    hostnames: dict[str, str] = {}
    assets: dict[str, str] = {}
    for row in rows:
        address = _optional_text(row.get("Expected IP address"))
        if address is None:
            continue
        _merge_register_ports(
            expected,
            unsupported,
            expected_register_ports,
            address,
            _optional_text(row.get("Expected services/ports")),
        )
        _merge_register_ports(
            forbidden,
            unsupported,
            forbidden_register_ports,
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
        "register_ports": {
            "forbidden": _protocol_specs_by_address(forbidden_register_ports),
            "expected": _protocol_specs_by_address(expected_register_ports),
        },
    }


def _merge_register_ports(
    tcp_map: dict[str, set[int]],
    unsupported_map: dict[str, set[str]],
    all_ports_map: dict[str, set[str]],
    address: str,
    specification: str | None,
) -> None:
    if specification is None:
        return
    ports = parse_protocol_port_spec(specification)
    for item in ports:
        protocol_port = f"{item.port}/{item.protocol}"
        all_ports_map.setdefault(address, set()).add(protocol_port)
        if item.protocol == "tcp":
            tcp_map.setdefault(address, set()).add(item.port)
        else:
            unsupported_map.setdefault(address, set()).add(protocol_port)


def _tcp_specs(values: Mapping[str, set[int]]) -> dict[str, str]:
    return {
        address: ",".join(f"{port}/tcp" for port in sorted(ports))
        for address, ports in sorted(values.items())
        if ports
    }


def _protocol_specs_by_address(
    values: Mapping[str, set[str]],
) -> dict[str, list[dict[str, object]]]:
    return {
        address: [
            ProtocolPortV1(
                port=int(value.split("/", 1)[0]),
                protocol=value.split("/", 1)[1],
            ).model_dump(mode="json")
            for value in sorted(ports, key=_protocol_port_sort_key)
        ]
        for address, ports in sorted(
            values.items(),
            key=lambda item: int(ipaddress.IPv4Address(item[0])),
        )
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


def _guard_resolved_ip_parameters_size(parameters: Mapping[str, object]) -> None:
    size = len(canonical_json_bytes(parameters))
    if size > SCAN_CONTRACT_MAX_BYTES:
        raise ValueError(
            f"resolved IP engine parameters require {size:,} canonical UTF-8 bytes, "
            f"exceeding the {SCAN_CONTRACT_MAX_BYTES:,}-byte ceiling"
        )


def _source_interface_snapshot(parameters: dict[str, Any]) -> dict[str, object]:
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
    raw_identity = parameters.get(SOURCE_INTERFACE_IDENTITY_KEY)
    if not isinstance(raw_identity, Mapping):
        raise ValueError(
            "source_interface_identity_v1 is required; resolve one concrete source "
            "interface before building the scan contract"
        )
    identity = FrozenSourceInterfaceV1.model_validate(dict(raw_identity))
    if source is None or local is None:
        raise ValueError("source_ip and local_address must be frozen before the scan contract")
    if identity.source_ip != str(source) or identity.local_address != local.with_prefixlen:
        raise ValueError(
            "source_interface_identity_v1 must match the frozen source_ip and local_address"
        )
    return identity.model_dump(mode="json")


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
