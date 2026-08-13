"""Persist-first, bounded normalization of operator-provided Nmap XML."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from smart_commissioning_core.db.run_lifecycle import (
    DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES,
    DISCOVERY_OBSERVATION_FOLD_MAX_ROWS,
)
from smart_commissioning_core.discovery_observations import (
    DiscoveryObservationInputV1,
)
from smart_commissioning_core.engines.ip.nmap_xml import (
    NmapHostObservationV1,
    NmapPortObservationV1,
    NmapXmlLimitsV1,
    NmapXmlParseError,
    NmapXmlResultV1,
    parse_nmap_xml,
)
from smart_commissioning_core.owned_run_heartbeat import OwnedRunHeartbeat
from smart_commissioning_core.run_context import canonical_json_bytes, canonical_sha256
from sqlalchemy.engine import Engine

from app.core.auth import AuthPrincipal
from app.core.config import get_settings
from app.core.db import get_engine
from app.schemas.jobs import JobCreateRequest
from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapCapabilityState,
    NmapProviderMode,
)
from app.schemas.nmap_xml_import import NmapXmlImportResponse
from app.services.raw_evidence_artifacts import (
    RawEvidenceArtifactDescriptor,
    RawEvidenceArtifactStore,
    RawEvidenceError,
)
from app.services.run_service import RunService

NMAP_XML_IMPORT_MAX_BYTES = 16 * 1024 * 1024
NMAP_XML_IMPORT_CAPTURE_BYTES = NMAP_XML_IMPORT_MAX_BYTES + 1
NMAP_XML_IMPORT_MAX_OBSERVATIONS = DISCOVERY_OBSERVATION_FOLD_MAX_ROWS
NMAP_XML_IMPORT_MAX_OBSERVATION_PAYLOAD_BYTES = DISCOVERY_OBSERVATION_FOLD_MAX_PAYLOAD_BYTES

NmapXmlParser = Callable[..., NmapXmlResultV1]


class NmapXmlImportLifecycleError(RuntimeError):
    """The accepted import could not enter its guarded lifecycle."""


class NmapXmlImportStorageError(NmapXmlImportLifecycleError):
    """Protected evidence storage failed after the import run was accepted."""


class NmapXmlImportService:
    """Create one scoped discovery run around a protected XML artifact."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        raw_store: RawEvidenceArtifactStore | None = None,
        run_service: RunService | None = None,
        parser: NmapXmlParser = parse_nmap_xml,
        producer_executor_id: str | None = None,
        heartbeat_factory: Callable[..., OwnedRunHeartbeat] = OwnedRunHeartbeat,
    ) -> None:
        self.engine = engine or get_engine()
        self.raw_store = raw_store or RawEvidenceArtifactStore(self.engine)
        self.run_service = run_service or RunService(self.engine)
        self.parser = parser
        settings = get_settings()
        self.lease_seconds = settings.run_lease_seconds
        self.heartbeat_seconds = settings.run_heartbeat_seconds
        self.heartbeat_factory = heartbeat_factory
        self.producer_executor_id = producer_executor_id or (
            settings.network_executor_id or f"inline:{settings.deployment_id}"
        )

    def import_payload(
        self,
        *,
        project_id: str,
        site_id: str,
        principal: AuthPrincipal,
        capability: NmapCapabilityResponse,
        payload: bytes,
        capture_complete: bool,
    ) -> NmapXmlImportResponse:
        """Seal exact bytes before parsing and seal every accepted attempt."""

        self._validate_request(capability, payload, capture_complete)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        import_plan_sha256 = canonical_sha256(
            {
                "schema_version": "1.0",
                "provider": "operator_xml_import",
                "project_id": project_id,
                "site_id": site_id,
                "artifact_sha256": payload_sha256,
                "artifact_size_bytes": len(payload),
                "capture_complete": capture_complete,
                "policy_id": capability.policy_id,
                "policy_revision": capability.policy_revision,
            }
        )
        run = self.run_service.create_job_run(
            JobCreateRequest(
                project_id=project_id,
                site_id=site_id,
                job_type="ip_discovery",
                parameters={
                    # Offline evidence normalization sends no packets and needs
                    # no live scan authorization. The lifecycle's dry-context
                    # fence is the existing explicit contract for that lane.
                    "dry_run": True,
                    "provider": "operator_xml_import",
                    "operator_xml_import_v1": {
                        "schema_version": "1.0",
                        "provider": "operator_xml_import",
                        "policy_id": capability.policy_id,
                        "policy_revision": capability.policy_revision,
                        "import_plan_sha256": import_plan_sha256,
                        "artifact_sha256": payload_sha256,
                        "artifact_size_bytes": len(payload),
                        "capture_complete": capture_complete,
                    },
                },
            ),
            expected_job_type="ip_discovery",
            requesting_principal=principal.username,
        )
        owned = self.run_service.claim_owned_run(
            run.run_id,
            lease_seconds=self.lease_seconds,
        )
        if owned is None:  # pragma: no cover - a fresh synchronous run is claimable
            raise RuntimeError("Nmap XML import run could not be claimed.")
        heartbeat: OwnedRunHeartbeat | None = None
        try:
            heartbeat = self.heartbeat_factory(
                owned,
                lease_seconds=self.lease_seconds,
                interval_seconds=self.heartbeat_seconds,
                executor_label="operator-xml-import",
                thread_name_prefix="operator-xml-import-heartbeat",
            )
            heartbeat.start()
        except Exception:
            try:
                self._finalize_preartifact_failure(
                    owned=owned,
                    diagnostic_code="heartbeat_start_failed",
                    stage="operator_xml_import_heartbeat_start_failed",
                    error_message="Nmap XML import could not start safely.",
                )
            except Exception:
                pass
            finally:
                if heartbeat is not None:
                    heartbeat.stop_and_join()
            raise NmapXmlImportLifecycleError("Nmap XML import could not be completed.") from None

        try:
            return self._import_claimed_payload(
                owned=owned,
                run_id=run.run_id,
                project_id=project_id,
                site_id=site_id,
                payload=payload,
                capture_complete=capture_complete,
                payload_sha256=payload_sha256,
                import_plan_sha256=import_plan_sha256,
            )
        finally:
            heartbeat.stop_and_join()

    def _import_claimed_payload(
        self,
        *,
        owned: Any,
        run_id: str,
        project_id: str,
        site_id: str,
        payload: bytes,
        capture_complete: bool,
        payload_sha256: str,
        import_plan_sha256: str,
    ) -> NmapXmlImportResponse:
        dispatch = self.run_service.get_dispatch_for_run(run_id)
        self.run_service.mark_dispatch_published(dispatch.dispatch_id)
        owned.update_run_status(
            run_id,
            status="running",
            stage="operator_xml_import_storage",
            progress_percent=10,
        )
        try:
            descriptor = self.raw_store.import_bytes(
                run_id=run_id,
                artifact_type="nmap_xml",
                media_type="application/xml",
                payload=payload,
                capture_complete=capture_complete,
                producer_executor_id=self.producer_executor_id,
                max_bytes=NMAP_XML_IMPORT_CAPTURE_BYTES,
            )
        except (RawEvidenceError, OSError):
            self._finalize_storage_failure(owned=owned)
            raise NmapXmlImportStorageError("Nmap XML import could not be completed.") from None

        if not capture_complete:
            code = "xml_byte_limit" if len(payload) > NMAP_XML_IMPORT_MAX_BYTES else "incomplete_capture"
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code=code,
            )

        try:
            verified, exact_payload = self.raw_store.read_for_normalization(
                run_id=run_id,
                artifact_id=descriptor.artifact_id,
            )
            self._verify_descriptor(
                verified,
                payload_sha256=payload_sha256,
                payload_size=len(payload),
            )
            parsed = self.parser(
                exact_payload,
                limits=NmapXmlLimitsV1(max_bytes=NMAP_XML_IMPORT_MAX_BYTES),
            )
        except NmapXmlParseError as error:
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code=f"xml_{error.reason}",
            )
        except (RawEvidenceError, ValidationError, TypeError, ValueError, OSError):
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code="evidence_integrity_failed",
            )

        try:
            observations = []
            device_position = 0
            for host in parsed.hosts:
                seen_ports: set[tuple[str, int]] = set()
                projection_position = device_position if host.state == "up" else None
                if projection_position is not None:
                    device_position += 1
                observations.append(
                    self._host_observation(
                        host,
                        parsed=parsed,
                        descriptor=descriptor,
                        project_id=project_id,
                        site_id=site_id,
                        plan_sha256=import_plan_sha256,
                        position=projection_position,
                    )
                )
                for port in host.ports:
                    port_identity = (port.protocol, port.port)
                    if port_identity in seen_ports:
                        raise ValueError("duplicate Nmap port observation identity")
                    seen_ports.add(port_identity)
                    observations.append(
                        self._port_observation(
                            host,
                            port,
                            parsed=parsed,
                            descriptor=descriptor,
                            plan_sha256=import_plan_sha256,
                        )
                    )
        except (ValidationError, TypeError, ValueError):
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code="normalization_contract_failed",
            )

        if len(observations) > NMAP_XML_IMPORT_MAX_OBSERVATIONS:
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code="normalization_row_limit",
            )
        canonical_payload_bytes = sum(len(canonical_json_bytes(observation.payload)) for observation in observations)
        if canonical_payload_bytes > NMAP_XML_IMPORT_MAX_OBSERVATION_PAYLOAD_BYTES:
            return self._finalize_failure(
                owned=owned,
                descriptor=descriptor,
                diagnostic_code="normalization_payload_limit",
            )

        for observation in observations:
            owned.append_observation(observation)

        port_count = sum(len(host.ports) for host in parsed.hosts)
        summary = self._summary(
            descriptor=descriptor,
            diagnostic_code="import_complete",
            nmap_version=parsed.nmap_version,
            xml_output_version=parsed.xml_output_version,
            host_count=len(parsed.hosts),
            port_count=port_count,
        )
        owned.update_result_summary(run_id, summary)
        owned.update_run_status(
            run_id,
            status="succeeded",
            stage="operator_xml_import_complete",
            progress_percent=100,
        )
        return NmapXmlImportResponse(
            run_id=run_id,
            status="succeeded",
            diagnostic_code="import_complete",
            artifact_id=descriptor.artifact_id,
            artifact_sha256=descriptor.sha256,
            artifact_size_bytes=descriptor.size_bytes,
            capture_complete=descriptor.capture_complete,
            nmap_version=parsed.nmap_version,
            xml_output_version=parsed.xml_output_version,
            host_count=len(parsed.hosts),
            port_count=port_count,
        )

    @staticmethod
    def _validate_request(
        capability: NmapCapabilityResponse,
        payload: bytes,
        capture_complete: bool,
    ) -> None:
        if (
            capability.state is not NmapCapabilityState.XML_IMPORT_ONLY
            or capability.provider_mode is not NmapProviderMode.OPERATOR_XML_IMPORT
            or not capability.xml_import_allowed
        ):
            raise ValueError("Nmap XML import capability is not approved.")
        if not isinstance(payload, bytes):
            raise TypeError("Nmap XML import payload must be bytes.")
        if type(capture_complete) is not bool:
            raise TypeError("capture_complete must be a boolean.")
        if len(payload) > NMAP_XML_IMPORT_CAPTURE_BYTES:
            raise ValueError("Nmap XML import capture exceeded its hard read bound.")
        if capture_complete and len(payload) > NMAP_XML_IMPORT_MAX_BYTES:
            raise ValueError("Oversized Nmap XML cannot be marked complete.")

    @staticmethod
    def _verify_descriptor(
        descriptor: RawEvidenceArtifactDescriptor,
        *,
        payload_sha256: str,
        payload_size: int,
    ) -> None:
        if (
            descriptor.sha256 != payload_sha256
            or descriptor.size_bytes != payload_size
            or not descriptor.capture_complete
        ):
            raise ValueError("Protected Nmap XML evidence changed before parsing.")

    def _finalize_failure(
        self,
        *,
        owned: Any,
        descriptor: RawEvidenceArtifactDescriptor,
        diagnostic_code: str,
    ) -> NmapXmlImportResponse:
        summary = self._summary(
            descriptor=descriptor,
            diagnostic_code=diagnostic_code,
            nmap_version=None,
            xml_output_version=None,
            host_count=0,
            port_count=0,
        )
        owned.update_result_summary(owned.lease.run_id, summary)
        owned.update_run_status(
            owned.lease.run_id,
            status="failed",
            stage="operator_xml_import_rejected",
            progress_percent=100,
            error_message="Nmap XML evidence could not be normalized.",
        )
        return NmapXmlImportResponse(
            run_id=owned.lease.run_id,
            status="failed",
            diagnostic_code=diagnostic_code,
            artifact_id=descriptor.artifact_id,
            artifact_sha256=descriptor.sha256,
            artifact_size_bytes=descriptor.size_bytes,
            capture_complete=descriptor.capture_complete,
        )

    @staticmethod
    def _finalize_preartifact_failure(
        *,
        owned: Any,
        diagnostic_code: str,
        stage: str,
        error_message: str,
    ) -> None:
        owned.update_result_summary(
            owned.lease.run_id,
            {
                "provider": "operator_xml_import",
                "provider_contract_version": "1.0",
                "xml_parser_contract_version": "1.0",
                "diagnostic_code": diagnostic_code,
                "hosts_scanned": 0,
                "port_observation_count": 0,
                "acceptance_eligible": False,
            },
        )
        owned.update_run_status(
            owned.lease.run_id,
            status="failed",
            stage=stage,
            progress_percent=100,
            error_message=error_message,
        )

    @classmethod
    def _finalize_storage_failure(cls, *, owned: Any) -> None:
        cls._finalize_preartifact_failure(
            owned=owned,
            diagnostic_code="evidence_storage_failed",
            stage="operator_xml_import_storage_failed",
            error_message="Nmap XML evidence could not be stored.",
        )

    @staticmethod
    def _summary(
        *,
        descriptor: RawEvidenceArtifactDescriptor,
        diagnostic_code: str,
        nmap_version: str | None,
        xml_output_version: str | None,
        host_count: int,
        port_count: int,
    ) -> dict[str, object]:
        return {
            "provider": "operator_xml_import",
            "provider_version": nmap_version,
            "provider_contract_version": "1.0",
            "xml_parser_contract_version": "1.0",
            "xml_output_version": xml_output_version,
            "xml_artifact_id": descriptor.artifact_id,
            "xml_artifact_sha256": descriptor.sha256,
            "xml_artifact_size_bytes": descriptor.size_bytes,
            "xml_capture_complete": descriptor.capture_complete,
            "diagnostic_code": diagnostic_code,
            "hosts_scanned": host_count,
            "port_observation_count": port_count,
            "acceptance_eligible": diagnostic_code == "import_complete",
        }

    @staticmethod
    def _provenance(*, plan_sha256: str) -> dict[str, object]:
        return {
            "profile": "operator_xml_import",
            "source_ip": None,
            "source_interface": None,
            "packet_plan_sha256": plan_sha256,
            "register_import_id": None,
            "register_rows_sha256": None,
        }

    def _host_observation(
        self,
        host: NmapHostObservationV1,
        *,
        parsed: NmapXmlResultV1,
        descriptor: RawEvidenceArtifactDescriptor,
        project_id: str,
        site_id: str,
        plan_sha256: str,
        position: int | None,
    ) -> DiscoveryObservationInputV1:
        reachable = host.state == "up"
        payload: dict[str, object] = {
            "xml_artifact_id": descriptor.artifact_id,
            "state": host.state,
            "ip_v1": {
                "schema_version": "1.0",
                "coverage_state": "attempted",
                "reachability_state": "reachable" if reachable else "unconfirmed",
                "probe_outcome": None,
                "register_match": "not_configured",
                "policy_verdict": "not_applicable",
                "target": host.address,
                "provider": "operator_xml_import",
                "provider_version": parsed.nmap_version,
                "provider_contract_version": "1.0",
                "provenance": self._provenance(plan_sha256=plan_sha256),
                "reason": "Normalized from complete protected Nmap XML evidence.",
                "attempts": 0,
                "elapsed_ms": 0,
            },
        }
        if reachable:
            if position is None:  # pragma: no cover - caller assigns up hosts densely
                raise ValueError("reachable Nmap host lacks a projection position")
            payload["projection_v1"] = {
                "collection": "devices",
                "position": position,
                "present": True,
                "record": {
                    "project_id": project_id,
                    "site_id": site_id,
                    "address": host.address,
                    "device_type": "ip_host",
                    "name": host.hostname,
                    "attributes": {
                        "hostname": host.hostname,
                        "reachable": True,
                        "position": position,
                    },
                },
            }
        return DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="host",
            entity_key=f"host:{host.address}",
            entity_version=1,
            event_key=f"host:{host.address}:operator-xml-v1",
            phase="finalize",
            outcome="observed" if reachable else "unconfirmed",
            payload=payload,
        )

    def _port_observation(
        self,
        host: NmapHostObservationV1,
        port: NmapPortObservationV1,
        *,
        parsed: NmapXmlResultV1,
        descriptor: RawEvidenceArtifactDescriptor,
        plan_sha256: str,
    ) -> DiscoveryObservationInputV1:
        reachable = port.state == "open"
        return DiscoveryObservationInputV1(
            protocol="ip",
            entity_kind="port",
            entity_key=f"port:{host.address}:{port.port}:{port.protocol}",
            entity_version=1,
            event_key=(f"port:{host.address}:{port.port}:{port.protocol}:operator-xml-v1"),
            phase="finalize",
            outcome="observed",
            payload={
                "xml_artifact_id": descriptor.artifact_id,
                "state": port.state,
                "ip_v1": {
                    "schema_version": "1.0",
                    "coverage_state": "attempted",
                    "reachability_state": "reachable" if reachable else "unconfirmed",
                    "probe_outcome": None,
                    "register_match": "not_configured",
                    "policy_verdict": "unconfirmed",
                    "target": host.address,
                    "port": port.port,
                    "transport": port.protocol,
                    "provider": "operator_xml_import",
                    "provider_version": parsed.nmap_version,
                    "provider_contract_version": "1.0",
                    "provenance": self._provenance(plan_sha256=plan_sha256),
                    "reason": "Normalized from complete protected Nmap XML evidence.",
                    "attempts": 0,
                    "elapsed_ms": 0,
                    "detected_service": port.detected_service,
                    "detected_version": (port.detected_version if port.detected_service is not None else None),
                },
            },
        )


__all__ = [
    "NMAP_XML_IMPORT_CAPTURE_BYTES",
    "NMAP_XML_IMPORT_MAX_BYTES",
    "NMAP_XML_IMPORT_MAX_OBSERVATIONS",
    "NMAP_XML_IMPORT_MAX_OBSERVATION_PAYLOAD_BYTES",
    "NmapXmlImportLifecycleError",
    "NmapXmlImportStorageError",
    "NmapXmlImportService",
]
