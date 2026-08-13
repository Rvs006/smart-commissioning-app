"""Executor-owned adapter from protected Nmap evidence to IP observations.

The provider owns no persistence or run lifecycle state.  It validates one
server-built :class:`NmapScanPlanV1`, delegates process containment to the
injected runner, and submits only normalized viewer-safe observations through
the active :class:`EngineContext` sink.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.discovery_observations import DiscoveryObservationInputV1
from smart_commissioning_core.engines.base import EngineContext, EngineResult
from smart_commissioning_core.engines.ip.comparison import (
    IPHostComparisonInputV1,
    IPRegisterAuthorityV1,
    IPRegisterComparisonV1,
    compare_ip_register,
)
from smart_commissioning_core.engines.ip.metrics import (
    IPHeadlineMetricScopeV1,
    IPHostMetricStateV1,
    reduce_ip_headline_metrics,
)
from smart_commissioning_core.engines.ip.models import ProviderIdentityEvidenceV1
from smart_commissioning_core.engines.ip.nmap_profiles import NmapScanPlanV1
from smart_commissioning_core.engines.ip.nmap_runner import (
    NmapPrivateArtifacts,
    NmapProcessReason,
    NmapProcessResultV1,
    NmapProcessStatus,
    NmapRuntimeCapabilityProbe,
    NmapWindowsProcessApi,
    run_nmap_scan,
)
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapTrustReason,
    NmapTrustResultV1,
)
from smart_commissioning_core.engines.ip.nmap_xml import (
    NmapHostObservationV1,
    NmapPortObservationV1,
    NmapXmlLimitsV1,
    NmapXmlParseError,
    NmapXmlResultV1,
    parse_nmap_xml,
)
from smart_commissioning_core.engines.safety import require_scan_authorization
from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.scan_contract import NormalizedIPv4TargetsV1

NmapProcessRunner = Callable[..., NmapProcessResultV1]
NmapArtifactReader = Callable[[str], tuple[object, bytes]]


class _NmapControlFailure(RuntimeError):
    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__("active Nmap control was lost")


@dataclass(frozen=True, slots=True)
class NmapProviderDependencies:
    """Injected machine and protected-evidence boundaries for one live run."""

    artifacts: NmapPrivateArtifacts
    windows: NmapWindowsProcessApi
    runtime_probe: NmapRuntimeCapabilityProbe
    revalidate_trust: Callable[[], NmapTrustResultV1]
    read_xml_artifact: NmapArtifactReader
    run_scan: NmapProcessRunner = run_nmap_scan


async def run_nmap_provider(
    ctx: EngineContext,
    *,
    authority: IPRegisterAuthorityV1 | None,
    dependencies: NmapProviderDependencies | None,
) -> EngineResult:
    """Execute or preview one sealed operator-managed Nmap plan."""

    plan, provider_state = _sealed_nmap_plan(ctx.parameters)
    if ctx.dry_run:
        return EngineResult(
            result_summary_extra={
                "dry_run_plan": {
                    "provider": plan.provider,
                    "provider_contract_version": plan.provider_contract_version,
                    "profile": plan.profile.value,
                    "profile_version": plan.profile_version,
                    "profile_fingerprint": plan.profile_fingerprint,
                    "packet_plan_sha256": plan.packet_plan_sha256,
                    "targets": list(plan.targets),
                    "protocol_ports": [
                        *(f"{port}/tcp" for port in plan.tcp_ports),
                        *(f"{port}/udp" for port in plan.udp_ports),
                    ],
                    "source_ip": plan.source_ip,
                    "source_interface": plan.interface_name,
                    "interface_id": plan.interface_id,
                    "planned_attempts": plan.planned_attempts,
                    "max_rate_per_second": plan.max_rate_per_second,
                    "max_parallelism": plan.max_parallelism,
                    "retries": plan.retries,
                    "host_timeout_seconds": plan.host_timeout_seconds,
                    "parent_deadline_seconds": plan.parent_deadline_seconds,
                    "output_max_bytes": plan.output_max_bytes,
                    "provider_version": provider_state["version"],
                    "publisher": provider_state["publisher"],
                    "installation_fingerprint_sha256": provider_state[
                        "installation_fingerprint_sha256"
                    ],
                    "expected_executor_identity": provider_state[
                        "expected_executor_identity"
                    ],
                    "notes": "No packets sent and no external process started in dry run.",
                },
                "hosts_scanned": 0,
                "hosts_responsive": 0,
            }
        )

    require_scan_authorization(ctx.parameters)
    if dependencies is None:
        raise ValueError("live operator-managed Nmap dependencies are unavailable")
    process_result = await _run_process(
        ctx,
        plan=plan,
        provider_state=provider_state,
        dependencies=dependencies,
    )
    xml_result: NmapXmlResultV1 | None = None
    normalization_failed = False
    if process_result.status is NmapProcessStatus.SUCCEEDED:
        try:
            xml_result = await _read_and_parse_xml(
                ctx,
                plan=plan,
                process_result=process_result,
                dependencies=dependencies,
            )
        except _NmapControlFailure as control_failure:
            process_result = _control_failure_result(
                control_failure.error,
                previous=process_result,
            )
        except (NmapXmlParseError, ValueError, TypeError, OSError):
            normalization_failed = True

    return await _publish_result(
        ctx,
        plan=plan,
        authority=authority,
        process_result=process_result,
        xml_result=xml_result,
        normalization_failed=normalization_failed,
    )


async def _run_process(
    ctx: EngineContext,
    *,
    plan: NmapScanPlanV1,
    provider_state: Mapping[str, Any],
    dependencies: NmapProviderDependencies,
) -> NmapProcessResultV1:
    # Bind trust to the exact installation sealed into the preview before the
    # injected runner can create any process or artifact. The runner repeats
    # this callback immediately before launch to close the TOCTOU window.
    try:
        ctx.require_active_control()
    except Exception as error:
        return _control_failure_result(error)
    try:
        trust = NmapTrustResultV1.model_validate(
            await _run_in_thread(dependencies.revalidate_trust)
        )
    except Exception:
        trust = NmapTrustResultV1(
            available=False,
            reason=NmapTrustReason.INSPECTION_FAILED,
            fingerprint=None,
        )
    if not trust.available or trust.fingerprint is None:
        return NmapProcessResultV1(
            status=NmapProcessStatus.UNAVAILABLE,
            reason=NmapProcessReason.TRUST_UNAVAILABLE,
            process_tree_gone=True,
            duration_seconds=0,
        )
    _require_preview_bound_trust(trust, provider_state)

    def revalidate_preview_bound_trust() -> NmapTrustResultV1:
        current = NmapTrustResultV1.model_validate(dependencies.revalidate_trust())
        if current.available and current.fingerprint is not None:
            _require_preview_bound_trust(current, provider_state)
        return current

    pending = asyncio.create_task(
        asyncio.to_thread(
            dependencies.run_scan,
            plan=plan,
            deployment_mode="internal_operator_managed",
            expected_executor_identity=str(
                provider_state["expected_executor_identity"]
            ),
            revalidate_trust=revalidate_preview_bound_trust,
            runtime_probe=dependencies.runtime_probe,
            control_guard=ctx.require_active_control,
            artifacts=dependencies.artifacts,
            windows=dependencies.windows,
        )
    )
    try:
        result = NmapProcessResultV1.model_validate(await asyncio.shield(pending))
    except asyncio.CancelledError:
        try:
            await pending
        except Exception:
            pass
        raise
    _require_process_binding(result, plan=plan, provider_state=provider_state)
    return result


async def _run_in_thread(call: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run one blocking boundary and drain its thread if the task is cancelled."""

    pending = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            await pending
        except Exception:
            pass
        raise


def _require_preview_bound_trust(
    trust: NmapTrustResultV1,
    provider_state: Mapping[str, Any],
) -> None:
    fingerprint = trust.fingerprint
    if fingerprint is None or (
        fingerprint.fingerprint_sha256,
        fingerprint.publisher,
        fingerprint.version,
    ) != (
        provider_state["installation_fingerprint_sha256"],
        provider_state["publisher"],
        provider_state["version"],
    ):
        raise ValueError("Nmap installation identity drifted after preview")


def _require_process_binding(
    result: NmapProcessResultV1,
    *,
    plan: NmapScanPlanV1,
    provider_state: Mapping[str, Any],
) -> None:
    provenance = result.provenance
    if provenance is None:
        return
    if (
        provenance.profile,
        provenance.profile_version,
        provenance.profile_fingerprint,
        provenance.packet_plan_sha256,
        provenance.installation_fingerprint_sha256,
        provenance.publisher,
        provenance.nmap_version,
        provenance.executor_identity,
        provenance.interface_id,
        provenance.interface_name,
        provenance.source_ip,
    ) != (
        plan.profile,
        plan.profile_version,
        plan.profile_fingerprint,
        plan.packet_plan_sha256,
        provider_state["installation_fingerprint_sha256"],
        provider_state["publisher"],
        provider_state["version"],
        provider_state["expected_executor_identity"],
        plan.interface_id,
        plan.interface_name,
        plan.source_ip,
    ):
        raise ValueError("Nmap process provenance drifted from the sealed plan")


async def _read_and_parse_xml(
    ctx: EngineContext,
    *,
    plan: NmapScanPlanV1,
    process_result: NmapProcessResultV1,
    dependencies: NmapProviderDependencies,
) -> NmapXmlResultV1:
    artifact = process_result.xml_artifact
    provenance = process_result.provenance
    if artifact is None or provenance is None or not artifact.capture_complete:
        raise ValueError("complete Nmap XML evidence is required")
    descriptor, payload = await _run_in_thread(
        dependencies.read_xml_artifact,
        artifact.artifact_id,
    )
    if not isinstance(payload, bytes):
        raise TypeError("protected Nmap XML reader must return bytes")
    descriptor_values = (
        getattr(descriptor, "artifact_id", None),
        getattr(descriptor, "sha256", None),
        getattr(descriptor, "size_bytes", None),
        getattr(descriptor, "capture_complete", None),
    )
    if descriptor_values != (
        artifact.artifact_id,
        artifact.sha256,
        artifact.size_bytes,
        True,
    ):
        raise ValueError("protected Nmap XML descriptor does not match process evidence")
    if (
        len(payload) != artifact.size_bytes
        or hashlib.sha256(payload).hexdigest() != artifact.sha256
    ):
        raise ValueError("protected Nmap XML bytes do not match process evidence")
    _require_active_control(ctx)
    limits = NmapXmlLimitsV1(
        max_bytes=plan.output_max_bytes,
        max_hosts=len(plan.targets),
        max_ports_per_host=len(plan.tcp_ports) + len(plan.udp_ports),
        max_total_ports=len(plan.targets)
        * (len(plan.tcp_ports) + len(plan.udp_ports)),
    )
    parsed = await _run_in_thread(
        parse_nmap_xml,
        payload,
        limits=limits,
    )
    if parsed.nmap_version != provenance.nmap_version:
        raise ValueError("Nmap XML version does not match process provenance")
    planned_targets = set(plan.targets)
    if any(host.address not in planned_targets for host in parsed.hosts):
        raise ValueError("Nmap XML contains a host outside the sealed plan")
    planned_ports = {
        *(('tcp', port) for port in plan.tcp_ports),
        *(('udp', port) for port in plan.udp_ports),
    }
    if any(
        (port.protocol, port.port) not in planned_ports
        for host in parsed.hosts
        for port in host.ports
    ):
        raise ValueError("Nmap XML contains a port outside the sealed plan")
    _require_active_control(ctx)
    return parsed


def _require_active_control(ctx: EngineContext) -> None:
    try:
        ctx.require_active_control()
    except Exception as error:
        raise _NmapControlFailure(error) from None


def _control_failure_result(
    error: Exception,
    *,
    previous: NmapProcessResultV1 | None = None,
) -> NmapProcessResultV1:
    text = f"{getattr(error, 'reason', '')} {error}".casefold()
    if "stop" in text or "cancel" in text:
        reason = NmapProcessReason.STOP_REQUESTED
    elif "authorization" in text or "grant" in text or "initiating user" in text:
        reason = NmapProcessReason.AUTHORIZATION_LOST
    elif "owner" in text or "attempt" in text or "lease" in text:
        reason = NmapProcessReason.OWNERSHIP_LOST
    elif "deadline" in text or "window" in text or "expir" in text:
        reason = NmapProcessReason.DEADLINE_EXCEEDED
    else:
        reason = NmapProcessReason.CONTROL_STORE_FAILURE
    status = (
        NmapProcessStatus.CANCELLED
        if reason
        in {
            NmapProcessReason.STOP_REQUESTED,
            NmapProcessReason.AUTHORIZATION_LOST,
            NmapProcessReason.OWNERSHIP_LOST,
        }
        else NmapProcessStatus.FAILED
    )
    if previous is None:
        return NmapProcessResultV1(
            status=status,
            reason=reason,
            process_tree_gone=True,
            duration_seconds=0,
        )
    return NmapProcessResultV1.model_validate(
        {
            **previous.model_dump(mode="python"),
            "status": status,
            "reason": reason,
        }
    )


async def _publish_result(
    ctx: EngineContext,
    *,
    plan: NmapScanPlanV1,
    authority: IPRegisterAuthorityV1 | None,
    process_result: NmapProcessResultV1,
    xml_result: NmapXmlResultV1 | None,
    normalization_failed: bool,
) -> EngineResult:
    hosts_by_address = (
        {host.address: host for host in xml_result.hosts}
        if xml_result is not None
        else {}
    )
    xml_artifact_id = (
        process_result.xml_artifact.artifact_id
        if process_result.xml_artifact is not None
        else None
    )
    stderr_artifact_id = (
        process_result.stderr_artifact.artifact_id
        if process_result.stderr_artifact is not None
        else None
    )
    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    metric_states: list[IPHostMetricStateV1] = []
    responsive = 0
    expected_by_address = _port_map(
        ctx.parameters,
        "expected_ports_by_address",
    )
    forbidden_by_address = _port_map(
        ctx.parameters,
        "forbidden_ports_by_address",
    )
    global_forbidden = _global_forbidden_ports(ctx.parameters)

    for position, target in enumerate(plan.targets):
        host = hosts_by_address.get(target)
        reachable = host is not None and host.state == "up"
        if reachable:
            responsive += 1
        comparison_reachability = (
            "not_applicable"
            if _coverage_state(process_result) == "not_attempted"
            else "reachable"
            if reachable
            else "unconfirmed"
        )
        comparison = compare_ip_register(
            IPHostComparisonInputV1(
                observed_ip=target,
                reachability_state=comparison_reachability,
                identity_evidence=_identity_evidence(host),
            ),
            authority=authority,
        )
        normalized_ports = _planned_port_evidence(plan, host)
        evidence_host_verdict = _host_policy_verdict(
            normalized_ports,
            expected=expected_by_address.get(target),
            forbidden=forbidden_by_address.get(target, global_forbidden),
        )
        host_verdict = _effective_host_verdict(
            process_result,
            normalization_failed=normalization_failed,
            evidence_verdict=evidence_host_verdict,
        )
        elapsed_ms = process_result.duration_seconds * 1000.0
        references = _artifact_references(xml_artifact_id, stderr_artifact_id)
        provenance = _public_provenance(ctx, plan, authority)
        failure_reason = _process_failure_reason(process_result, normalization_failed)

        await _append(
            ctx,
            entity_kind="host",
            entity_key=f"host:{target}",
            entity_version=1,
            event_key=f"host:{target}:planned",
            phase="planned",
            outcome=(
                "not_attempted"
                if _coverage_state(process_result) == "not_attempted"
                else "planned"
            ),
            ip_payload=_host_payload(
                plan=plan,
                target=target,
                provenance=provenance,
                comparison=comparison,
                reachable=reachable,
                process_result=process_result,
                host_verdict=host_verdict,
                elapsed_ms=elapsed_ms,
                reason="The IP target is in the sealed Nmap packet plan.",
                planned=True,
            ),
            references=references,
        )
        await _append(
            ctx,
            entity_kind="host",
            entity_key=f"host:{target}",
            entity_version=2,
            event_key=f"host:{target}:reachability",
            phase="reachability",
            outcome=_host_outcome(process_result, reachable, normalization_failed),
            ip_payload=_host_payload(
                plan=plan,
                target=target,
                provenance=provenance,
                comparison=comparison,
                reachable=reachable,
                process_result=process_result,
                host_verdict=host_verdict,
                elapsed_ms=elapsed_ms,
                reason=failure_reason or _host_reason(host),
            ),
            references=references,
        )
        for protocol, port, evidence in normalized_ports:
            probe_outcome = _probe_outcome(evidence, process_result, normalization_failed)
            port_verdict = _port_policy_verdict(
                protocol=protocol,
                port=port,
                probe_outcome=probe_outcome,
                expected=expected_by_address.get(target),
                forbidden=forbidden_by_address.get(target, global_forbidden),
            )
            await _append(
                ctx,
                entity_kind="port",
                entity_key=f"port:{target}:{port}:{protocol}",
                entity_version=1,
                event_key=f"port:{target}:{port}:{protocol}:v1",
                phase="reachability",
                outcome=probe_outcome or "not_attempted",
                ip_payload=_port_payload(
                    plan=plan,
                    target=target,
                    protocol=protocol,
                    port=port,
                    evidence=evidence,
                    provenance=provenance,
                    comparison=comparison,
                    process_result=process_result,
                    normalization_failed=normalization_failed,
                    policy_verdict=port_verdict,
                ),
                references=references,
            )

        await _append(
            ctx,
            entity_kind="host",
            entity_key=f"host:{target}",
            entity_version=3,
            event_key=f"host:{target}:comparison",
            phase="comparison",
            outcome=_comparison_outcome(process_result, xml_result),
            ip_payload=_host_payload(
                plan=plan,
                target=target,
                provenance=provenance,
                comparison=comparison,
                reachable=reachable,
                process_result=process_result,
                host_verdict=host_verdict,
                elapsed_ms=elapsed_ms,
                reason=failure_reason or _host_reason(host),
            ),
            references=references,
        )

        asset, device_record = _result_rows(
            ctx,
            plan=plan,
            target=target,
            host=host,
            authority=authority,
            comparison=comparison,
            normalized_ports=normalized_ports,
            reachable=reachable,
            failure_reason=failure_reason,
            position=position,
        )
        discovered_assets.append(asset)
        metric_states.append(
            IPHostMetricStateV1(
                target_ip=target,
                entity_version=4,
                lifecycle_state=_metric_lifecycle(process_result),
                probe_outcomes=_metric_probe_outcomes(
                    normalized_ports,
                    host=host,
                    process_result=process_result,
                    normalization_failed=normalization_failed,
                ),
                comparison=comparison,
            )
        )
        projection = None
        if device_record is not None:
            structured_records.append(device_record)
            projection = {
                "collection": "devices",
                "position": position,
                "present": True,
                "record": device_record,
            }
        await _append(
            ctx,
            entity_kind="host",
            entity_key=f"host:{target}",
            entity_version=4,
            event_key=f"host:{target}:finalize",
            phase="finalize",
            outcome=_host_outcome(process_result, reachable, normalization_failed),
            ip_payload=_host_payload(
                plan=plan,
                target=target,
                provenance=provenance,
                comparison=comparison,
                reachable=reachable,
                process_result=process_result,
                host_verdict=host_verdict,
                elapsed_ms=elapsed_ms,
                reason=failure_reason or _host_reason(host),
            ),
            references=references,
            projection=projection,
        )

    status_override, error_message = _terminal_status(
        process_result,
        normalization_failed,
    )
    metrics = reduce_ip_headline_metrics(
        IPHeadlineMetricScopeV1(
            frozen_targets=plan.targets,
            authority=authority,
        ),
        tuple(metric_states),
    )
    confirmed_hosts_scanned = (
        len(plan.targets)
        if process_result.status is NmapProcessStatus.SUCCEEDED
        else 0
    )
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=(
            [] if ctx.supports_progressive_observations else structured_records
        ),
        result_summary_extra={
            "provider": plan.provider,
            "provider_version": (
                process_result.provenance.nmap_version
                if process_result.provenance is not None
                else None
            ),
            "provider_profile": plan.profile.value,
            "provider_profile_fingerprint": plan.profile_fingerprint,
            "provider_process_status": process_result.status.value,
            "provider_process_reason": (
                "xml_normalization_failed"
                if normalization_failed
                else process_result.reason.value
            ),
            "xml_artifact_id": xml_artifact_id,
            "stderr_evidence_id": stderr_artifact_id,
            "hosts_scanned": confirmed_hosts_scanned,
            "hosts_responsive": responsive,
            "ports_scanned": [
                *(f"{port}/tcp" for port in plan.tcp_ports),
                *(f"{port}/udp" for port in plan.udp_ports),
            ] if process_result.status is NmapProcessStatus.SUCCEEDED else [],
            "ports_planned": [
                *(f"{port}/tcp" for port in plan.tcp_ports),
                *(f"{port}/udp" for port in plan.udp_ports),
            ],
            "ip_headline_metrics_v1": metrics.model_dump(mode="json"),
        },
        status_override=status_override,
        error_message=error_message,
    )


def _identity_evidence(
    host: NmapHostObservationV1 | None,
) -> ProviderIdentityEvidenceV1 | None:
    if host is None or host.hostname is None:
        return None
    return ProviderIdentityEvidenceV1(
        evidence_kind="approved_provider",
        confidence="medium",
        hostname=host.hostname,
        corroborating_fields=("hostname",),
    )


def _planned_port_evidence(
    plan: NmapScanPlanV1,
    host: NmapHostObservationV1 | None,
) -> list[tuple[str, int, NmapPortObservationV1 | None]]:
    by_key = (
        {(port.protocol, port.port): port for port in host.ports}
        if host is not None
        else {}
    )
    return [
        *(
            ("tcp", port, by_key.get(("tcp", port)))
            for port in plan.tcp_ports
        ),
        *(
            ("udp", port, by_key.get(("udp", port)))
            for port in plan.udp_ports
        ),
    ]


def _artifact_references(
    xml_artifact_id: str | None,
    stderr_artifact_id: str | None,
) -> dict[str, str]:
    references: dict[str, str] = {}
    if xml_artifact_id is not None:
        references["xml_artifact_id"] = xml_artifact_id
    if stderr_artifact_id is not None:
        references["stderr_evidence_id"] = stderr_artifact_id
    return references


def _public_provenance(
    ctx: EngineContext,
    plan: NmapScanPlanV1,
    authority: IPRegisterAuthorityV1 | None,
) -> dict[str, Any]:
    raw_ip = ctx.parameters["scan_contract_v1"]["ip"]
    return {
        "profile": raw_ip["profile"],
        "source_ip": plan.source_ip,
        "source_interface": plan.interface_name,
        "packet_plan_sha256": plan.packet_plan_sha256,
        "register_import_id": authority.import_id if authority is not None else None,
        "register_rows_sha256": (
            authority.accepted_rows_sha256 if authority is not None else None
        ),
    }


def _coverage_state(process_result: NmapProcessResultV1) -> str:
    # The runner has no per-target dispatch ledger. Only a completed protected
    # XML document proves that the frozen work units ran. A killed or failed
    # process may have stopped before resume, so its target/port rows stay
    # not_attempted rather than fabricating cancelled packet work.
    return (
        "attempted"
        if process_result.status is NmapProcessStatus.SUCCEEDED
        else "not_attempted"
    )


def _host_payload(
    *,
    plan: NmapScanPlanV1,
    target: str,
    provenance: Mapping[str, Any],
    comparison: IPRegisterComparisonV1,
    reachable: bool,
    process_result: NmapProcessResultV1,
    host_verdict: str,
    elapsed_ms: float,
    reason: str,
    planned: bool = False,
) -> dict[str, Any]:
    not_attempted = _coverage_state(process_result) == "not_attempted"
    if planned:
        coverage_state = "not_attempted" if not_attempted else "pending"
        reachability_state = "not_applicable" if not_attempted else "pending"
        policy_verdict = "not_attempted" if not_attempted else "pending"
        attempts = 0
        payload_elapsed_ms = 0.0
    else:
        coverage_state = _coverage_state(process_result)
        reachability_state = (
            "not_applicable"
            if not_attempted
            else "reachable"
            if reachable
            else "unconfirmed"
        )
        policy_verdict = host_verdict
        attempts = (
            max(1, len(plan.tcp_ports) + len(plan.udp_ports))
            if process_result.status is NmapProcessStatus.SUCCEEDED
            else 0
        )
        payload_elapsed_ms = elapsed_ms
    return {
        "schema_version": "1.0",
        "coverage_state": coverage_state,
        "reachability_state": reachability_state,
        "probe_outcome": None,
        "register_match": comparison.register_match,
        "policy_verdict": policy_verdict,
        "target": target,
        "port": None,
        "transport": None,
        "provider": plan.provider,
        "provider_version": _provider_version(process_result),
        "provider_contract_version": plan.provider_contract_version,
        "provenance": dict(provenance),
        "reason": reason,
        "attempts": attempts,
        "elapsed_ms": payload_elapsed_ms,
        "port_hint": None,
        "detected_service": None,
        "detected_version": None,
        "capability_action": None,
        "control_reason": _control_reason(process_result.reason),
        "last_packet_dispatched_at": None,
    }


def _port_payload(
    *,
    plan: NmapScanPlanV1,
    target: str,
    protocol: str,
    port: int,
    evidence: NmapPortObservationV1 | None,
    provenance: Mapping[str, Any],
    comparison: IPRegisterComparisonV1,
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
    policy_verdict: str,
) -> dict[str, Any]:
    outcome = _probe_outcome(evidence, process_result, normalization_failed)
    not_attempted = _coverage_state(process_result) == "not_attempted"
    detected_version = None
    if evidence is not None and not not_attempted:
        detected_version = " ".join(
            item
            for item in (evidence.detected_product, evidence.detected_version)
            if item
        ) or None
    return {
        "schema_version": "1.0",
        "coverage_state": _coverage_state(process_result),
        "reachability_state": (
            "not_applicable"
            if not_attempted
            else "reachable"
            if outcome == "connected"
            else "unconfirmed"
        ),
        "probe_outcome": outcome,
        "register_match": comparison.register_match,
        "policy_verdict": policy_verdict,
        "target": target,
        "port": port,
        "transport": protocol,
        "provider": plan.provider,
        "provider_version": _provider_version(process_result),
        "provider_contract_version": plan.provider_contract_version,
        "provenance": dict(provenance),
        "reason": _port_reason(protocol, port, evidence, process_result, normalization_failed),
        "attempts": (
            1 if process_result.status is NmapProcessStatus.SUCCEEDED else 0
        ),
        "elapsed_ms": process_result.duration_seconds * 1000.0,
        "port_hint": None,
        "detected_service": (
            evidence.detected_service
            if evidence is not None and not not_attempted
            else None
        ),
        "detected_version": detected_version,
        "capability_action": None,
        "control_reason": _control_reason(process_result.reason),
        "last_packet_dispatched_at": None,
    }


async def _append(
    ctx: EngineContext,
    *,
    entity_kind: str,
    entity_key: str,
    entity_version: int,
    event_key: str,
    phase: str,
    outcome: str,
    ip_payload: Mapping[str, Any],
    references: Mapping[str, str],
    projection: Mapping[str, Any] | None = None,
) -> None:
    if not ctx.supports_progressive_observations:
        return
    payload: dict[str, Any] = {"ip_v1": dict(ip_payload), **references}
    if projection is not None:
        payload["projection_v1"] = dict(projection)
    await ctx.append_observation(
        DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind=entity_kind,
            entity_key=entity_key,
            entity_version=entity_version,
            event_key=event_key,
            phase=phase,
            outcome=outcome,
            payload=payload,
            observed_at=datetime.now(UTC),
        )
    )


def _provider_version(process_result: NmapProcessResultV1) -> str:
    return (
        process_result.provenance.nmap_version
        if process_result.provenance is not None
        else "unavailable"
    )


def _failure_probe_outcome(process_result: NmapProcessResultV1) -> str | None:
    if _coverage_state(process_result) == "not_attempted":
        return None
    return (
        "cancelled"
        if process_result.status is NmapProcessStatus.CANCELLED
        else None
        if process_result.status is NmapProcessStatus.UNAVAILABLE
        else "provider_error"
        if process_result.status is not NmapProcessStatus.SUCCEEDED
        else "timed_out"
    )


def _probe_outcome(
    evidence: NmapPortObservationV1 | None,
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
) -> str | None:
    if normalization_failed or process_result.status is not NmapProcessStatus.SUCCEEDED:
        return _failure_probe_outcome(process_result)
    if evidence is None:
        return "timed_out"
    if evidence.state == "open":
        return "connected"
    if evidence.protocol == "tcp" and evidence.state == "closed":
        return "connection_refused"
    return "timed_out"


def _port_reason(
    protocol: str,
    port: int,
    evidence: NmapPortObservationV1 | None,
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
) -> str:
    failure = _process_failure_reason(process_result, normalization_failed)
    if failure is not None:
        return failure
    if evidence is None:
        return f"Nmap returned no individual {protocol}/{port} state; the result is unconfirmed."
    return f"Nmap state {evidence.state} for {protocol}/{port}."


def _host_reason(host: NmapHostObservationV1 | None) -> str:
    if host is None:
        return "Nmap returned no host record for this frozen target; reachability is unconfirmed."
    return f"Nmap host state {host.state}."


def _process_failure_reason(
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
) -> str | None:
    if normalization_failed:
        return "Complete Nmap XML evidence could not be normalized; no provider claim was accepted."
    if process_result.status is NmapProcessStatus.SUCCEEDED:
        return None
    return {
        NmapProcessStatus.CANCELLED: "The Nmap process was cancelled before complete evidence was available.",
        NmapProcessStatus.UNAVAILABLE: "The Nmap provider was unavailable and no scan claim was accepted.",
        NmapProcessStatus.FAILED: "The Nmap process failed before complete evidence was available.",
    }[process_result.status]


def _control_reason(reason: NmapProcessReason) -> str | None:
    return {
        NmapProcessReason.STOP_REQUESTED: "stop_requested",
        NmapProcessReason.AUTHORIZATION_LOST: "authorization_revoked",
        NmapProcessReason.OWNERSHIP_LOST: "ownership_lost",
        NmapProcessReason.CONTROL_STORE_FAILURE: "control_store_error",
        NmapProcessReason.DEADLINE_EXCEEDED: "dispatch_deadline_elapsed",
    }.get(reason)


def _host_outcome(
    process_result: NmapProcessResultV1,
    reachable: bool,
    normalization_failed: bool,
) -> str:
    if _coverage_state(process_result) == "not_attempted":
        return "not_attempted"
    if process_result.status is NmapProcessStatus.CANCELLED:
        return "cancelled"
    if process_result.status is NmapProcessStatus.UNAVAILABLE:
        return "not_attempted"
    if normalization_failed or process_result.status is not NmapProcessStatus.SUCCEEDED:
        return "provider_error"
    return "observed" if reachable else "unconfirmed"


def _comparison_outcome(
    process_result: NmapProcessResultV1,
    xml_result: NmapXmlResultV1 | None,
) -> str:
    if _coverage_state(process_result) == "not_attempted":
        return "not_attempted"
    if process_result.status is NmapProcessStatus.CANCELLED:
        return "cancelled"
    return "evaluated" if xml_result is not None else "unconfirmed"


def _effective_host_verdict(
    process_result: NmapProcessResultV1,
    *,
    normalization_failed: bool,
    evidence_verdict: str,
) -> str:
    if _coverage_state(process_result) == "not_attempted" or (
        process_result.status is NmapProcessStatus.CANCELLED
    ):
        return "not_attempted"
    if normalization_failed or process_result.status is NmapProcessStatus.FAILED:
        return "unconfirmed"
    return evidence_verdict


def _metric_lifecycle(process_result: NmapProcessResultV1) -> str:
    if _coverage_state(process_result) == "not_attempted":
        return "not_attempted"
    if process_result.status is NmapProcessStatus.CANCELLED:
        return "cancelled"
    return "finalized"


def _metric_probe_outcomes(
    normalized_ports: list[tuple[str, int, NmapPortObservationV1 | None]],
    *,
    host: NmapHostObservationV1 | None,
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
) -> tuple[str, ...]:
    if _coverage_state(process_result) == "not_attempted":
        return ()
    if process_result.status is NmapProcessStatus.CANCELLED:
        return ("cancelled",)
    if normalization_failed or process_result.status is NmapProcessStatus.FAILED:
        return ("provider_error",)
    outcomes = tuple(
        outcome
        for _protocol, _port, evidence in normalized_ports
        if (
            outcome := _probe_outcome(
                evidence,
                process_result,
                normalization_failed=False,
            )
        )
        is not None
    )
    if outcomes:
        return outcomes
    return ("connected",) if host is not None and host.state == "up" else ("timed_out",)


def _port_map(parameters: Mapping[str, Any], key: str) -> dict[str, set[int]]:
    raw = parameters.get(key)
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, set[int]] = {}
    for address, specification in raw.items():
        ports: set[int] = set()
        if isinstance(specification, str):
            for token in specification.split(","):
                number = token.strip().split("/", 1)[0]
                if number.isdigit() and 1 <= int(number) <= 65_535:
                    ports.add(int(number))
        elif isinstance(specification, (list, tuple, set)):
            ports.update(
                int(item)
                for item in specification
                if type(item) is int and 1 <= item <= 65_535
            )
        if ports:
            result[str(address)] = ports
    return result


def _global_forbidden_ports(parameters: Mapping[str, Any]) -> set[int]:
    raw = parameters.get("forbidden_ports")
    if not isinstance(raw, str):
        return set()
    return {
        int(token.strip().split("/", 1)[0])
        for token in raw.split(",")
        if token.strip().split("/", 1)[0].isdigit()
        and 1 <= int(token.strip().split("/", 1)[0]) <= 65_535
    }


def _port_policy_verdict(
    *,
    protocol: str,
    port: int,
    probe_outcome: str | None,
    expected: set[int] | None,
    forbidden: set[int],
) -> str:
    if probe_outcome == "connected":
        if protocol == "tcp" and port in forbidden:
            return "forbidden_open"
        if expected is not None and port not in expected:
            return "unexpected_open_review"
        return "pass"
    if protocol == "tcp" and probe_outcome == "connection_refused":
        return "expected_closed" if expected is not None and port in expected else "pass"
    if probe_outcome in {None, "cancelled"}:
        return "not_attempted"
    return "unconfirmed"


def _host_policy_verdict(
    normalized_ports: list[tuple[str, int, NmapPortObservationV1 | None]],
    *,
    expected: set[int] | None,
    forbidden: set[int],
) -> str:
    verdicts = [
        _port_policy_verdict(
            protocol=protocol,
            port=port,
            probe_outcome=(
                "connected"
                if evidence is not None and evidence.state == "open"
                else "connection_refused"
                if evidence is not None
                and evidence.protocol == "tcp"
                and evidence.state == "closed"
                else "timed_out"
            ),
            expected=expected,
            forbidden=forbidden,
        )
        for protocol, port, evidence in normalized_ports
    ]
    for verdict in ("forbidden_open", "unexpected_open_review", "expected_closed"):
        if verdict in verdicts:
            return verdict
    if verdicts and all(verdict == "pass" for verdict in verdicts):
        return "pass"
    return "unconfirmed"


def _matched_row(
    authority: IPRegisterAuthorityV1 | None,
    comparison: IPRegisterComparisonV1,
) -> object | None:
    if authority is None or comparison.matched_device_key is None:
        return None
    return next(
        iter(authority._rows_by_device_key.get(comparison.matched_device_key, ())),
        None,
    )


def _result_rows(
    ctx: EngineContext,
    *,
    plan: NmapScanPlanV1,
    target: str,
    host: NmapHostObservationV1 | None,
    authority: IPRegisterAuthorityV1 | None,
    comparison: IPRegisterComparisonV1,
    normalized_ports: list[tuple[str, int, NmapPortObservationV1 | None]],
    reachable: bool,
    failure_reason: str | None,
    position: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    matched_row = _matched_row(authority, comparison)
    open_ports = [
        {
            "port": port,
            "protocol": protocol,
            "service": evidence.detected_service,
        }
        for protocol, port, evidence in normalized_ports
        if evidence is not None and evidence.state == "open"
    ]
    port_observations = [
        {
            "port": port,
            "protocol": protocol,
            "state": evidence.state if evidence is not None else "unconfirmed",
            "detected_service": evidence.detected_service if evidence is not None else None,
            "detected_version": (
                evidence.detected_version if evidence is not None else None
            ),
        }
        for protocol, port, evidence in normalized_ports
    ]
    status_detail = failure_reason or _host_reason(host)
    asset = {
        "asset_id": matched_row.asset_id if matched_row is not None else None,
        "ip_address": target,
        "mac_address": None,
        "hostname": host.hostname if host is not None else None,
        "observed_ports": open_ports,
        "port_observations": port_observations,
        "match_basis": "+".join(comparison.match_basis) or "none",
        "status_detail": status_detail,
        "last_seen_at": datetime.now(UTC).isoformat() if reachable else None,
    }
    if not reachable:
        return asset, None
    attributes = {
        "asset_id": asset["asset_id"],
        "hostname": host.hostname if host is not None else None,
        "open_ports": sorted(
            item["port"] for item in open_ports if item["protocol"] == "tcp"
        ),
        "scanned_ports": list(plan.tcp_ports),
        "scanned_port_count": len(plan.tcp_ports) + len(plan.udp_ports),
        "register_match": comparison.register_match,
        "reachable": reachable,
        "position": position,
    }
    if comparison.expected_ip is not None:
        attributes["register_address"] = comparison.expected_ip
    if matched_row is not None and matched_row.asset_name is not None:
        attributes["register_asset_name"] = matched_row.asset_name
    return asset, {
        "project_id": str(ctx.parameters.get("project_id") or ""),
        "site_id": str(ctx.parameters.get("site_id") or ""),
        "address": target,
        "device_type": "ip_host",
        "name": host.hostname if host is not None else None,
        "attributes": attributes,
    }


def _terminal_status(
    process_result: NmapProcessResultV1,
    normalization_failed: bool,
) -> tuple[str | None, str | None]:
    if process_result.status is NmapProcessStatus.CANCELLED:
        if process_result.reason is NmapProcessReason.STOP_REQUESTED:
            return "cancelled", None
        return "failed", "Nmap stopped because active control was lost."
    if normalization_failed:
        return "failed", "Nmap XML evidence could not be normalized."
    if process_result.status is NmapProcessStatus.UNAVAILABLE:
        return "failed", "Nmap provider capability was unavailable."
    if process_result.status is NmapProcessStatus.FAILED:
        return "failed", "Nmap did not complete the sealed scan."
    return None, None


def _sealed_nmap_plan(
    parameters: Mapping[str, Any],
) -> tuple[NmapScanPlanV1, Mapping[str, Any]]:
    raw_contract = parameters.get("scan_contract_v1")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("operator-managed Nmap requires scan_contract_v1")
    contract = dict(raw_contract)
    packet_digest = contract.pop("packet_plan_sha256", None)
    if not isinstance(packet_digest, str) or canonical_sha256(contract) != packet_digest:
        raise ValueError("operator-managed Nmap packet-plan digest verification failed")
    if contract.get("scan_contract_version") != "1.0" or contract.get("job_type") != "ip_discovery":
        raise ValueError("operator-managed Nmap requires an IP scan contract v1")

    raw_ip = contract.get("ip")
    if not isinstance(raw_ip, Mapping) or raw_ip.get("provider") != "operator_managed_nmap":
        raise ValueError("operator-managed Nmap provider selection is not frozen")
    raw_state = raw_ip.get("provider_state")
    if not isinstance(raw_state, Mapping):
        raise ValueError("operator-managed Nmap provider capability is not frozen")
    if (
        raw_state.get("provider") != "operator_managed_nmap"
        or raw_state.get("provider_contract_version") != "1.0"
        or raw_state.get("execution_boundary") != "operator_managed_internal_only"
        or raw_state.get("execution_enabled") is not True
        or raw_state.get("capability_state") != "available"
    ):
        raise ValueError("operator-managed Nmap provider capability is unavailable")
    required_text = {
        "expected_executor_identity": 255,
        "policy_id": 255,
        "publisher": 512,
        "version": 128,
    }
    for field_name, max_length in required_text.items():
        value = raw_state.get(field_name)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > max_length
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(
                f"operator-managed Nmap provider {field_name} is not frozen"
            )
    installation_fingerprint = raw_state.get(
        "installation_fingerprint_sha256"
    )
    if (
        not isinstance(installation_fingerprint, str)
        or len(installation_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in installation_fingerprint)
    ):
        raise ValueError(
            "operator-managed Nmap installation fingerprint is not frozen"
        )
    policy_revision = raw_state.get("policy_revision")
    if (
        isinstance(policy_revision, bool)
        or not isinstance(policy_revision, int)
        or policy_revision < 1
    ):
        raise ValueError("operator-managed Nmap policy revision is not frozen")

    plan = NmapScanPlanV1.model_validate(parameters.get("nmap_scan_plan_v1"))
    if plan.packet_plan_sha256 != packet_digest:
        raise ValueError("operator-managed Nmap plan is not bound to the packet plan")
    if (
        raw_state.get("profile") != plan.profile.value
        or raw_state.get("profile_fingerprint") != plan.profile_fingerprint
    ):
        raise ValueError("operator-managed Nmap profile drifted after preview")
    expected_executor = raw_state.get("expected_executor_identity")
    if not isinstance(expected_executor, str) or not expected_executor.strip():
        raise ValueError("operator-managed Nmap executor identity is not frozen")
    supported_protocols = raw_state.get("supported_protocols")
    required_protocols = {
        *("tcp" for _port in plan.tcp_ports),
        *("udp" for _port in plan.udp_ports),
    }
    if (
        not isinstance(supported_protocols, list)
        or any(item not in {"tcp", "udp"} for item in supported_protocols)
        or not required_protocols.issubset(set(supported_protocols))
    ):
        raise ValueError("operator-managed Nmap protocol capability drifted")
    if plan.raw_capability_required and raw_state.get("raw_capable") is not True:
        raise ValueError("operator-managed Nmap raw capability is unavailable")

    targets = NormalizedIPv4TargetsV1.model_validate(raw_ip.get("targets"))
    if targets.expanded_addresses != plan.targets:
        raise ValueError("operator-managed Nmap targets drifted after preview")
    source = contract.get("source_interface")
    if not isinstance(source, Mapping) or (
        source.get("source_ip"),
        source.get("interface_id"),
        source.get("interface_name"),
    ) != (plan.source_ip, plan.interface_id, plan.interface_name):
        raise ValueError("operator-managed Nmap source interface drifted after preview")
    flattened_source = parameters.get("source_ip")
    if flattened_source is not None and flattened_source != plan.source_ip:
        raise ValueError("operator-managed Nmap flattened source drifted after preview")

    raw_ports = raw_ip.get("ports")
    if not isinstance(raw_ports, list):
        raise ValueError("operator-managed Nmap protocol ports are not frozen")
    frozen_ports = tuple(
        (str(item.get("protocol")), item.get("port"))
        for item in raw_ports
        if isinstance(item, Mapping)
    )
    planned_ports = tuple(
        [("tcp", port) for port in plan.tcp_ports]
        + [("udp", port) for port in plan.udp_ports]
    )
    if len(frozen_ports) != len(raw_ports) or frozen_ports != planned_ports:
        raise ValueError("operator-managed Nmap protocol ports drifted after preview")
    return plan, raw_state


__all__ = ["NmapProviderDependencies", "run_nmap_provider"]
