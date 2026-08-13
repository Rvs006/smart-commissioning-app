from __future__ import annotations

import asyncio
import hashlib
import threading
import unittest
from types import SimpleNamespace

from smart_commissioning_core.engines.base import EngineContext
from smart_commissioning_core.engines.ip.comparison import build_ip_register_authority
from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    build_nmap_scan_plan,
)
from smart_commissioning_core.engines.ip.nmap_provider import (
    NmapProviderDependencies,
    run_nmap_provider,
)
from smart_commissioning_core.engines.ip.nmap_runner import (
    NmapProcessProvenanceV1,
    NmapProcessReason,
    NmapProcessResultV1,
    NmapProcessStatus,
    NmapRawXmlArtifactV1,
)
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapTrustReason,
    NmapTrustResultV1,
)
from smart_commissioning_core.engines.safety import ScanNotAuthorized
from smart_commissioning_core.run_context import canonical_sha256
from smart_commissioning_core.scan_contract import (
    IPv4TargetExpressionV1,
    normalize_ipv4_targets,
)


class _ObservationStore:
    def __init__(self, *, control_error: Exception | None = None) -> None:
        self.observations: list[object] = []
        self.control_checks = 0
        self.control_error = control_error

    def append_observation(self, observation: object) -> None:
        self.observations.append(observation)

    def require_active_control(self) -> None:
        self.control_checks += 1
        if self.control_error is not None:
            raise self.control_error


class _ExplodingDependencies:
    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)
        raise AssertionError(f"dry run touched Nmap dependency {name}")


class _LiveBoundary:
    def __init__(
        self,
        parameters: dict[str, object],
        xml: bytes,
        *,
        status: NmapProcessStatus = NmapProcessStatus.SUCCEEDED,
        reason: NmapProcessReason = NmapProcessReason.COMPLETED,
        trust_result: NmapTrustResultV1 | None = None,
    ) -> None:
        self.parameters = parameters
        self.xml = xml
        self.status = status
        self.reason = reason
        self.trust_result = (
            _available_trust() if trust_result is None else trust_result
        )
        self.calls = 0

    def run_scan(self, **values: object) -> NmapProcessResultV1:
        self.calls += 1
        control_guard = values["control_guard"]
        assert callable(control_guard)
        control_guard()
        plan = values["plan"]
        assert hasattr(plan, "profile")
        if self.status is NmapProcessStatus.UNAVAILABLE:
            return NmapProcessResultV1(
                status=self.status,
                reason=self.reason,
                process_tree_gone=True,
                duration_seconds=0.01,
            )
        xml_artifact = NmapRawXmlArtifactV1(
            artifact_id="artifact_nmap_xml_test",
            sha256=hashlib.sha256(self.xml).hexdigest(),
            size_bytes=len(self.xml),
            capture_complete=self.status is NmapProcessStatus.SUCCEEDED,
        )
        stderr = b"provider diagnostic kept protected"
        stderr_artifact = NmapRawXmlArtifactV1(
            artifact_id="evidence_nmap_stderr_test",
            sha256=hashlib.sha256(stderr).hexdigest(),
            size_bytes=len(stderr),
            capture_complete=self.status is NmapProcessStatus.SUCCEEDED,
        )
        if self.status is not NmapProcessStatus.SUCCEEDED:
            return NmapProcessResultV1(
                status=self.status,
                reason=self.reason,
                xml_artifact=xml_artifact,
                stderr_artifact=stderr_artifact,
                stderr_sha256=stderr_artifact.sha256,
                stderr_size_bytes=stderr_artifact.size_bytes,
                process_tree_gone=True,
                duration_seconds=0.1,
            )
        return NmapProcessResultV1(
            status=NmapProcessStatus.SUCCEEDED,
            reason=NmapProcessReason.COMPLETED,
            exit_code=0,
            xml_artifact=xml_artifact,
            stderr_artifact=stderr_artifact,
            stderr_sha256=stderr_artifact.sha256,
            stderr_size_bytes=stderr_artifact.size_bytes,
            process_tree_gone=True,
            duration_seconds=0.25,
            provenance=NmapProcessProvenanceV1(
                profile=plan.profile,
                profile_fingerprint=plan.profile_fingerprint,
                packet_plan_sha256=plan.packet_plan_sha256,
                nmap_version="7.98",
                publisher="Insecure.Com LLC",
                installation_fingerprint_sha256=(
                    self.trust_result.fingerprint.fingerprint_sha256
                    if self.trust_result.fingerprint is not None
                    else "0" * 64
                ),
                executor_identity="machine:test",
                interface_id=plan.interface_id,
                interface_name=plan.interface_name,
                source_ip=plan.source_ip,
                npcap_version="1.80",
            ),
        )

    def read_xml(self, artifact_id: str) -> tuple[object, bytes]:
        self.assert_artifact_id = artifact_id
        return (
            SimpleNamespace(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(self.xml).hexdigest(),
                size_bytes=len(self.xml),
                capture_complete=True,
            ),
            self.xml,
        )

    def dependencies(self) -> NmapProviderDependencies:
        return NmapProviderDependencies(
            artifacts=object(),  # type: ignore[arg-type]
            windows=object(),  # type: ignore[arg-type]
            runtime_probe=object(),  # type: ignore[arg-type]
            revalidate_trust=lambda: self.trust_result,
            read_xml_artifact=self.read_xml,
            run_scan=self.run_scan,
        )


def _fingerprint(
    *,
    publisher: str = "Insecure.Com LLC",
    version: str = "7.98",
) -> NmapInstallationFingerprintV1:
    payload = {
        "install_root": r"C:\Program Files\Nmap",
        "executable_path": r"C:\Program Files\Nmap\nmap.exe",
        "data_directory": r"C:\Program Files\Nmap",
        "publisher": publisher,
        "signer_sha256": "1" * 64,
        "version": version,
        "executable_sha256": "2" * 64,
        "data_manifest_sha256": "3" * 64,
        "data_file_count": 100,
        "data_total_bytes": 1_000_000,
        "licence_relative_path": "license",
        "licence_sha256": "4" * 64,
        "npsl_version": "0.95",
        "reviewed_scripts": (),
    }
    return NmapInstallationFingerprintV1(
        **payload,
        fingerprint_sha256=canonical_sha256(payload),
    )


def _available_trust(
    *,
    publisher: str = "Insecure.Com LLC",
    version: str = "7.98",
) -> NmapTrustResultV1:
    return NmapTrustResultV1(
        available=True,
        reason=NmapTrustReason.AVAILABLE,
        fingerprint=_fingerprint(publisher=publisher, version=version),
    )


def _xml(*host_fragments: bytes) -> bytes:
    up = sum(b"state='up'" in item for item in host_fragments)
    down = sum(b"state='down'" in item for item in host_fragments)
    return (
        b"<nmaprun scanner='nmap' version='7.98' xmloutputversion='1.05'>"
        + b"".join(host_fragments)
        + b"<runstats><finished elapsed='0.25' exit='success'/>"
        + f"<hosts up='{up}' down='{down}' total='{len(host_fragments)}'/>".encode()
        + b"</runstats></nmaprun>"
    )


def _host(
    address: str,
    *,
    state: str = "up",
    ports: bytes = b"",
    hostname: str | None = None,
) -> bytes:
    hostnames = (
        b""
        if hostname is None
        else f"<hostnames><hostname name='{hostname}' type='user'/></hostnames>".encode()
    )
    return (
        f"<host><status state='{state}' reason='syn-ack'/>".encode()
        + f"<address addr='{address}' addrtype='ipv4'/>".encode()
        + hostnames
        + b"<ports>"
        + ports
        + b"</ports></host>"
    )


def _port(
    port: int,
    *,
    protocol: str = "tcp",
    state: str = "open",
    service: str | None = None,
    product: str | None = None,
    version: str | None = None,
) -> bytes:
    service_attributes = ""
    if service is not None:
        service_attributes = f" name='{service}'"
        if product is not None:
            service_attributes += f" product='{product}'"
        if version is not None:
            service_attributes += f" version='{version}'"
    service_element = (
        "" if not service_attributes else f"<service{service_attributes}/>"
    )
    return (
        f"<port protocol='{protocol}' portid='{port}'>"
        f"<state state='{state}' reason='syn-ack'/>{service_element}</port>"
    ).encode()


def _parameters(
    *,
    targets: tuple[str, ...] = ("192.0.2.10",),
    tcp_ports: tuple[int, ...] = (80, 443),
    udp_ports: tuple[int, ...] = (),
    nmap_profile: NmapProfileName = NmapProfileName.TCP_CONNECT_INVENTORY,
) -> dict[str, object]:
    target_plan = normalize_ipv4_targets(
        expressions=tuple(
            IPv4TargetExpressionV1(kind="address", address=target)
            for target in targets
        ),
        exclusions=(),
        max_hosts=4096,
    )
    source = {
        "schema_version": "1.0",
        "selection": "explicit",
        "executor_scope": "portable:test",
        "interface_id": "windows-guid:12345678-1234-1234-1234-1234567890ab",
        "interface_name": "Test field NIC",
        "source_ip": "192.0.2.1",
        "prefix_length": 24,
        "local_address": "192.0.2.1/24",
        "default_route_metric": None,
    }
    provider_state = {
        "provider": "operator_managed_nmap",
        "provider_contract_version": "1.0",
        "capability_state": "available",
        "execution_boundary": "operator_managed_internal_only",
        "execution_enabled": True,
        "supported_protocols": ["tcp", "udp"],
        "profile": nmap_profile.value,
        "profile_fingerprint": "pending",
        "expected_executor_identity": "machine:test",
        "policy_id": "policy_test",
        "policy_revision": 1,
        "installation_fingerprint_sha256": _fingerprint().fingerprint_sha256,
        "publisher": "Insecure.Com LLC",
        "version": "7.98",
        "npcap_version": "1.80",
        "npcap_state": "running",
        "raw_capable": True,
    }
    ip_contract = {
        "provider": "operator_managed_nmap",
        "profile": "gentle",
        "use_register_addresses": False,
        "targets": target_plan.model_dump(mode="json"),
        "ports": [
            {"port": port, "protocol": "tcp"}
            for port in tcp_ports
        ]
        + [{"port": port, "protocol": "udp"} for port in udp_ports],
        "authority": None,
        "unsupported_register_ports_by_address": {},
        "not_attempted_ports_by_address": {},
        "policy": {"profile": "gentle"},
        "provider_state": provider_state,
        "work_estimate": {},
        "observation_budget": {},
    }
    packet_plan = {
        "scan_contract_version": "1.0",
        "job_type": "ip_discovery",
        "source_interface": source,
        "resource_keys": ["nic:v1:" + "2" * 64],
        "effective_throttle": {
            "max_concurrency": 4,
            "rate_limit_per_sec": 2.0,
            "connect_timeout_s": 3.0,
        },
        "ip": ip_contract,
    }
    digest = canonical_sha256(packet_plan)
    plan = build_nmap_scan_plan(
        profile=nmap_profile,
        targets=targets,
        tcp_ports=tcp_ports,
        udp_ports=udp_ports,
        source_ip="192.0.2.1",
        interface_id="windows-guid:12345678-1234-1234-1234-1234567890ab",
        interface_name="Test field NIC",
        packet_plan_sha256=digest,
        max_rate_per_second=2,
        host_timeout_seconds=3,
        parent_deadline_seconds=30,
        output_max_bytes=4096,
    )
    provider_state["profile_fingerprint"] = plan.profile_fingerprint
    # The profile fingerprint is itself frozen packet-plan input.
    digest = canonical_sha256(packet_plan)
    plan = plan.model_copy(update={"packet_plan_sha256": digest})
    return {
        "project_id": "project-test",
        "site_id": "site-test",
        "authorized": True,
        "source_ip": "192.0.2.1",
        "scan_contract_v1": {**packet_plan, "packet_plan_sha256": digest},
        "nmap_scan_plan_v1": plan.model_dump(mode="json"),
    }


def _context(*, dry_run: bool) -> tuple[EngineContext, _ObservationStore]:
    store = _ObservationStore()
    return (
        EngineContext(
            run_id="run-test",
            parameters=_parameters(),
            run_store=store,  # type: ignore[arg-type]
            execution_mode="test",
            dry_run=dry_run,
        ),
        store,
    )


class NmapProviderTests(unittest.TestCase):
    def test_dry_run_validates_and_describes_the_sealed_plan_without_dependencies(self) -> None:
        ctx, store = _context(dry_run=True)

        result = asyncio.run(
            run_nmap_provider(
                ctx,
                authority=None,
                dependencies=_ExplodingDependencies(),  # type: ignore[arg-type]
            )
        )

        preview = result.result_summary_extra["dry_run_plan"]
        self.assertEqual(preview["provider"], "operator_managed_nmap")
        self.assertEqual(preview["profile"], "tcp_connect_inventory")
        self.assertEqual(preview["targets"], ["192.0.2.10"])
        self.assertEqual(preview["protocol_ports"], ["80/tcp", "443/tcp"])
        self.assertEqual(store.control_checks, 0)
        self.assertEqual(store.observations, [])

    def test_live_success_normalizes_complete_evidence_in_frozen_order(self) -> None:
        parameters = _parameters()
        store = _ObservationStore()
        ctx = EngineContext(
            run_id="run-test",
            parameters=parameters,
            run_store=store,  # type: ignore[arg-type]
            execution_mode="test",
        )
        boundary = _LiveBoundary(
            parameters,
            _xml(
                _host(
                    "192.0.2.10",
                    hostname="ahu-10.example.internal",
                    ports=(
                        _port(
                            80,
                            service="http",
                            product="Example Controller",
                            version="1.2.3",
                        )
                        + _port(443, state="closed")
                    ),
                )
            ),
        )

        result = asyncio.run(
            run_nmap_provider(
                ctx,
                authority=None,
                dependencies=boundary.dependencies(),
            )
        )

        self.assertEqual(boundary.calls, 1)
        self.assertGreaterEqual(store.control_checks, 1)
        self.assertEqual(
            [item.event_key for item in store.observations],
            [
                "host:192.0.2.10:planned",
                "host:192.0.2.10:reachability",
                "port:192.0.2.10:80:tcp:v1",
                "port:192.0.2.10:443:tcp:v1",
                "host:192.0.2.10:comparison",
                "host:192.0.2.10:finalize",
            ],
        )
        open_payload = store.observations[2].payload
        self.assertEqual(open_payload["xml_artifact_id"], "artifact_nmap_xml_test")
        self.assertEqual(
            open_payload["stderr_evidence_id"], "evidence_nmap_stderr_test"
        )
        self.assertEqual(open_payload["ip_v1"]["transport"], "tcp")
        self.assertEqual(open_payload["ip_v1"]["probe_outcome"], "connected")
        self.assertEqual(open_payload["ip_v1"]["detected_service"], "http")
        self.assertEqual(
            open_payload["ip_v1"]["detected_version"],
            "Example Controller 1.2.3",
        )
        self.assertIn("Nmap state open", open_payload["ip_v1"]["reason"])
        closed_payload = store.observations[3].payload["ip_v1"]
        self.assertEqual(closed_payload["probe_outcome"], "connection_refused")
        self.assertEqual(result.status_override, None)
        self.assertEqual(result.result_summary_extra["hosts_scanned"], 1)
        self.assertEqual(result.result_summary_extra["hosts_responsive"], 1)
        self.assertEqual(len(result.discovered_assets), 1)
        self.assertEqual(result.structured_records, [])
        self.assertEqual(
            store.observations[-1].payload["projection_v1"]["record"]["address"],
            "192.0.2.10",
        )
        self.assertNotIn("provider diagnostic", repr(store.observations))
        self.assertNotIn("\\", repr(store.observations))

    def test_complete_xml_still_emits_unconfirmed_rows_for_missing_targets(self) -> None:
        parameters = _parameters(targets=("192.0.2.10", "192.0.2.11"))
        store = _ObservationStore()
        ctx = EngineContext(
            run_id="run-test",
            parameters=parameters,
            run_store=store,  # type: ignore[arg-type]
            execution_mode="test",
        )
        boundary = _LiveBoundary(
            parameters,
            _xml(_host("192.0.2.10", ports=_port(80))),
        )

        result = asyncio.run(
            run_nmap_provider(
                ctx,
                authority=None,
                dependencies=boundary.dependencies(),
            )
        )

        missing = next(
            item
            for item in store.observations
            if item.event_key == "host:192.0.2.11:finalize"
        )
        self.assertEqual(missing.payload["ip_v1"]["reachability_state"], "unconfirmed")
        self.assertIsNone(missing.payload["ip_v1"]["probe_outcome"])
        self.assertNotIn("projection_v1", missing.payload)
        missing_ports = [
            item
            for item in store.observations
            if item.entity_kind == "port" and ":192.0.2.11:" in item.entity_key
        ]
        self.assertEqual(len(missing_ports), 2)
        self.assertTrue(
            all(item.payload["ip_v1"]["probe_outcome"] == "timed_out" for item in missing_ports)
        )
        self.assertEqual(len(result.discovered_assets), 2)
        self.assertEqual(result.structured_records, [])
        projections = [
            item.payload["projection_v1"]
            for item in store.observations
            if "projection_v1" in item.payload
        ]
        self.assertEqual(len(projections), 1)

    def test_filtered_udp_keeps_transport_and_nmap_state_without_open_claim(self) -> None:
        parameters = _parameters(
            tcp_ports=(),
            udp_ports=(47808,),
            nmap_profile=NmapProfileName.SELECTED_UDP,
        )
        store = _ObservationStore()
        ctx = EngineContext(
            run_id="run-test",
            parameters=parameters,
            run_store=store,  # type: ignore[arg-type]
            execution_mode="test",
        )
        boundary = _LiveBoundary(
            parameters,
            _xml(
                _host(
                    "192.0.2.10",
                    ports=_port(47808, protocol="udp", state="open|filtered"),
                )
            ),
        )

        asyncio.run(
            run_nmap_provider(
                ctx,
                authority=None,
                dependencies=boundary.dependencies(),
            )
        )

        port = next(item for item in store.observations if item.entity_kind == "port")
        self.assertEqual(port.entity_key, "port:192.0.2.10:47808:udp")
        self.assertEqual(port.payload["ip_v1"]["transport"], "udp")
        self.assertEqual(port.payload["ip_v1"]["probe_outcome"], "timed_out")
        self.assertEqual(port.payload["ip_v1"]["policy_verdict"], "unconfirmed")
        self.assertIn("Nmap state open|filtered", port.payload["ip_v1"]["reason"])

    def test_hostile_or_incomplete_xml_fails_without_publishing_raw_input(self) -> None:
        cases = (
            b"<!DOCTYPE nmaprun [<!ENTITY x SYSTEM 'file:///etc/passwd'>]>"
            b"<nmaprun>&x;</nmaprun>",
            b"<nmaprun scanner='nmap' version='7.98' xmloutputversion='1.05'>",
        )
        for xml in cases:
            with self.subTest(xml=xml[:32]):
                parameters = _parameters()
                store = _ObservationStore()
                ctx = EngineContext(
                    run_id="run-test",
                    parameters=parameters,
                    run_store=store,  # type: ignore[arg-type]
                    execution_mode="test",
                )

                result = asyncio.run(
                    run_nmap_provider(
                        ctx,
                        authority=None,
                        dependencies=_LiveBoundary(parameters, xml).dependencies(),
                    )
                )

                self.assertEqual(result.status_override, "failed")
                self.assertEqual(
                    result.result_summary_extra["provider_process_reason"],
                    "xml_normalization_failed",
                )
                self.assertEqual(
                    [item.entity_version for item in store.observations if item.entity_kind == "host"],
                    [1, 2, 3, 4],
                )
                public = repr(store.observations) + repr(result.result_summary_extra)
                self.assertNotIn("DOCTYPE", public)
                self.assertNotIn("etc/passwd", public)
                self.assertNotIn("file:", public)

    def test_incomplete_or_tampered_protected_artifact_is_never_normalized(self) -> None:
        xml = _xml(_host("192.0.2.10", ports=_port(80)))
        expected_sha256 = hashlib.sha256(xml).hexdigest()
        cases = (
            ("incomplete", False, expected_sha256, xml),
            ("descriptor-drift", True, "f" * 64, xml),
            ("byte-drift", True, expected_sha256, b"C:\\Users\\operator\\raw.xml"),
        )
        for label, capture_complete, descriptor_sha256, payload in cases:
            with self.subTest(label=label):
                parameters = _parameters()
                boundary = _LiveBoundary(parameters, xml)
                current = boundary.dependencies()

                def read_artifact(
                    artifact_id: str,
                    *,
                    capture_complete: bool = capture_complete,
                    descriptor_sha256: str = descriptor_sha256,
                    payload: bytes = payload,
                ) -> tuple[object, bytes]:
                    return (
                        SimpleNamespace(
                            artifact_id=artifact_id,
                            sha256=descriptor_sha256,
                            size_bytes=len(xml),
                            capture_complete=capture_complete,
                        ),
                        payload,
                    )

                dependencies = NmapProviderDependencies(
                    artifacts=current.artifacts,
                    windows=current.windows,
                    runtime_probe=current.runtime_probe,
                    revalidate_trust=current.revalidate_trust,
                    read_xml_artifact=read_artifact,
                    run_scan=current.run_scan,
                )
                store = _ObservationStore()
                result = asyncio.run(
                    run_nmap_provider(
                        EngineContext(
                            run_id="run-test",
                            parameters=parameters,
                            run_store=store,  # type: ignore[arg-type]
                            execution_mode="test",
                        ),
                        authority=None,
                        dependencies=dependencies,
                    )
                )

                self.assertEqual(result.status_override, "failed")
                self.assertEqual(
                    result.result_summary_extra["provider_process_reason"],
                    "xml_normalization_failed",
                )
                public = repr(store.observations) + repr(result.result_summary_extra)
                self.assertNotIn("Users", public)
                self.assertNotIn("raw.xml", public)

    def test_unavailable_capability_and_stop_emit_truthful_partial_rows(self) -> None:
        cases = (
            (
                NmapProcessStatus.UNAVAILABLE,
                NmapProcessReason.CAPABILITY_UNAVAILABLE,
                "failed",
                "not_attempted",
                None,
                None,
            ),
            (
                NmapProcessStatus.CANCELLED,
                NmapProcessReason.STOP_REQUESTED,
                "cancelled",
                "not_attempted",
                None,
                "stop_requested",
            ),
            (
                NmapProcessStatus.CANCELLED,
                NmapProcessReason.AUTHORIZATION_LOST,
                "failed",
                "not_attempted",
                None,
                "authorization_revoked",
            ),
        )
        for status, reason, terminal, coverage, probe, control in cases:
            with self.subTest(status=status):
                parameters = _parameters()
                store = _ObservationStore()
                ctx = EngineContext(
                    run_id="run-test",
                    parameters=parameters,
                    run_store=store,  # type: ignore[arg-type]
                    execution_mode="test",
                )
                result = asyncio.run(
                    run_nmap_provider(
                        ctx,
                        authority=None,
                        dependencies=_LiveBoundary(
                            parameters,
                            b"",
                            status=status,
                            reason=reason,
                        ).dependencies(),
                    )
                )

                self.assertEqual(result.status_override, terminal)
                self.assertEqual(
                    [item.entity_version for item in store.observations if item.entity_kind == "host"],
                    [1, 2, 3, 4],
                )
                ports = [item.payload["ip_v1"] for item in store.observations if item.entity_kind == "port"]
                self.assertEqual(len(ports), 2)
                self.assertTrue(all(item["coverage_state"] == coverage for item in ports))
                self.assertTrue(all(item["probe_outcome"] == probe for item in ports))
                self.assertTrue(all(item["control_reason"] == control for item in ports))

    def test_trust_unavailable_emits_unattempted_rows_without_runner_work(self) -> None:
        parameters = _parameters()
        store = _ObservationStore()
        boundary = _LiveBoundary(
            parameters,
            b"",
            trust_result=NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.FINGERPRINT_DRIFT,
                fingerprint=None,
            ),
        )

        result = asyncio.run(
            run_nmap_provider(
                EngineContext(
                    run_id="run-test",
                    parameters=parameters,
                    run_store=store,  # type: ignore[arg-type]
                    execution_mode="test",
                ),
                authority=None,
                dependencies=boundary.dependencies(),
            )
        )

        self.assertEqual(boundary.calls, 0)
        self.assertEqual(result.status_override, "failed")
        self.assertEqual(
            result.result_summary_extra["provider_process_reason"],
            "trust_unavailable",
        )
        self.assertTrue(
            all(
                item.payload["ip_v1"]["coverage_state"] == "not_attempted"
                for item in store.observations
                if item.entity_kind == "port"
            )
        )

    def test_preview_bound_installation_drift_is_rejected_before_runner_work(self) -> None:
        parameters = _parameters()
        boundary = _LiveBoundary(parameters, b"")
        current = boundary.dependencies()
        dependencies = NmapProviderDependencies(
            artifacts=current.artifacts,
            windows=current.windows,
            runtime_probe=current.runtime_probe,
            revalidate_trust=lambda: _available_trust(version="7.99"),
            read_xml_artifact=current.read_xml_artifact,
            run_scan=current.run_scan,
        )

        with self.assertRaisesRegex(
            ValueError,
            "installation identity drifted after preview",
        ):
            asyncio.run(
                run_nmap_provider(
                    EngineContext(
                        run_id="run-test",
                        parameters=parameters,
                        run_store=_ObservationStore(),  # type: ignore[arg-type]
                        execution_mode="test",
                    ),
                    authority=None,
                    dependencies=dependencies,
                )
            )

        self.assertEqual(boundary.calls, 0)

    def test_frozen_register_comparator_is_used_without_fabricating_asset_id(self) -> None:
        parameters = _parameters()
        rows = (
            {
                "Asset ID": "",
                "Asset name": "Level 1 AHU",
                "Expected IP address": "192.0.2.10",
            },
        )
        authority = build_ip_register_authority(
            rows,
            import_id="import-ip-register-test",
            accepted_rows_sha256=canonical_sha256(list(rows)),
        )
        store = _ObservationStore()

        result = asyncio.run(
            run_nmap_provider(
                EngineContext(
                    run_id="run-test",
                    parameters=parameters,
                    run_store=store,  # type: ignore[arg-type]
                    execution_mode="test",
                ),
                authority=authority,
                dependencies=_LiveBoundary(
                    parameters,
                    _xml(_host("192.0.2.10", ports=_port(80))),
                ).dependencies(),
            )
        )

        finalized = store.observations[-1].payload["ip_v1"]
        self.assertEqual(finalized["register_match"], "expected_match")
        self.assertEqual(
            finalized["provenance"]["register_import_id"],
            "import-ip-register-test",
        )
        self.assertIsNone(result.discovered_assets[0]["asset_id"])
        projected = store.observations[-1].payload["projection_v1"]["record"]
        self.assertEqual(
            projected["attributes"]["register_asset_name"],
            "Level 1 AHU",
        )

    def test_authorization_and_active_control_fail_before_trust_or_runner_work(self) -> None:
        parameters = _parameters()
        parameters["authorized"] = False
        boundary = _LiveBoundary(parameters, b"")
        with self.assertRaises(ScanNotAuthorized):
            asyncio.run(
                run_nmap_provider(
                    EngineContext(
                        run_id="run-test",
                        parameters=parameters,
                        run_store=_ObservationStore(),  # type: ignore[arg-type]
                        execution_mode="test",
                    ),
                    authority=None,
                    dependencies=boundary.dependencies(),
                )
            )
        self.assertEqual(boundary.calls, 0)

        parameters = _parameters()
        control_store = _ObservationStore(
            control_error=RuntimeError("active control store unavailable")
        )
        result = asyncio.run(
            run_nmap_provider(
                EngineContext(
                    run_id="run-test",
                    parameters=parameters,
                    run_store=control_store,  # type: ignore[arg-type]
                    execution_mode="test",
                ),
                authority=None,
                dependencies=boundary.dependencies(),
            )
        )
        self.assertEqual(result.status_override, "failed")
        self.assertEqual(
            result.result_summary_extra["provider_process_reason"],
            "control_store_failure",
        )
        self.assertEqual(boundary.calls, 0)
        self.assertTrue(
            all(
                item.payload["ip_v1"]["coverage_state"] == "not_attempted"
                for item in control_store.observations
            )
        )

    def test_sealed_plan_drift_is_rejected_before_any_process_work(self) -> None:
        parameters = _parameters()
        parameters["nmap_scan_plan_v1"]["source_ip"] = "192.0.2.99"  # type: ignore[index]
        store = _ObservationStore()
        ctx = EngineContext(
            run_id="run-test",
            parameters=parameters,
            run_store=store,  # type: ignore[arg-type]
            execution_mode="test",
        )
        boundary = _LiveBoundary(parameters, b"")

        with self.assertRaisesRegex(ValueError, "source interface drifted"):
            asyncio.run(
                run_nmap_provider(
                    ctx,
                    authority=None,
                    dependencies=boundary.dependencies(),
                )
            )

        self.assertEqual(boundary.calls, 0)
        self.assertEqual(store.control_checks, 0)
        self.assertEqual(store.observations, [])

    def test_xml_host_order_cannot_change_frozen_observation_order(self) -> None:
        parameters = _parameters(targets=("192.0.2.10", "192.0.2.11"))

        def run(xml: bytes) -> list[str]:
            store = _ObservationStore()
            result = asyncio.run(
                run_nmap_provider(
                    EngineContext(
                        run_id="run-test",
                        parameters=parameters,
                        run_store=store,  # type: ignore[arg-type]
                        execution_mode="test",
                    ),
                    authority=None,
                    dependencies=_LiveBoundary(parameters, xml).dependencies(),
                )
            )
            self.assertIsNone(result.status_override)
            return [item.event_key for item in store.observations]

        first = run(
            _xml(
                _host("192.0.2.10", ports=_port(80)),
                _host("192.0.2.11", ports=_port(443)),
            )
        )
        second = run(
            _xml(
                _host("192.0.2.11", ports=_port(443)),
                _host("192.0.2.10", ports=_port(80)),
            )
        )
        self.assertEqual(first, second)

    def test_task_cancellation_drains_the_running_process_thread(self) -> None:
        parameters = _parameters()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_runner(**_values: object) -> NmapProcessResultV1:
            started.set()
            release.wait(2)
            finished.set()
            return NmapProcessResultV1(
                status=NmapProcessStatus.CANCELLED,
                reason=NmapProcessReason.STOP_REQUESTED,
                process_tree_gone=True,
                duration_seconds=0.1,
            )

        boundary = _LiveBoundary(parameters, b"")
        dependencies = boundary.dependencies()
        dependencies = NmapProviderDependencies(
            artifacts=dependencies.artifacts,
            windows=dependencies.windows,
            runtime_probe=dependencies.runtime_probe,
            revalidate_trust=dependencies.revalidate_trust,
            read_xml_artifact=dependencies.read_xml_artifact,
            run_scan=blocking_runner,
        )

        async def exercise() -> None:
            task = asyncio.create_task(
                run_nmap_provider(
                    EngineContext(
                        run_id="run-test",
                        parameters=parameters,
                        run_store=_ObservationStore(),  # type: ignore[arg-type]
                        execution_mode="test",
                    ),
                    authority=None,
                    dependencies=dependencies,
                )
            )
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
