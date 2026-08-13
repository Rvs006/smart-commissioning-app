"""Deployment authority for an operator-managed Nmap installation.

This module persists policy and confirmation history, then exposes narrow gates
that discovery and XML-import code can call before detection, parsing, or
process selection. It never downloads, installs, updates, elevates, or launches
Nmap. Windows detection and trust inspection enter through an injected probe.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    NmapDeploymentPolicy,
    NmapInstallationConfirmation,
)
from smart_commissioning_core.engines.ip.nmap_detection import (
    NmapDetectionCandidateV1,
    detect_nmap_candidates,
)
from smart_commissioning_core.engines.ip.nmap_profiles import NmapProfileName
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapReviewedScriptV1,
    NmapTrustPolicyV1,
    NmapTrustReason,
    NmapTrustResultV1,
    inspect_nmap_installation,
    revalidate_nmap_installation,
)
from smart_commissioning_core.engines.ip.nmap_windows import (
    CtypesNmapRuntimeCapabilityProbe,
    CtypesNmapTrustBackend,
    nmap_uninstall_registry,
)
from smart_commissioning_core.run_context import canonical_sha256
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.core.auth import AuthPrincipal
from app.core.db import get_engine
from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapCapabilityState,
    NmapDeploymentLane,
    NmapDeploymentPolicyCreateRequest,
    NmapDeploymentPolicyResponse,
    NmapDetectedInstallationResponse,
    NmapInstallationConfirmationRequest,
    NmapInstallationConfirmationResponse,
    NmapNpcapState,
    NmapProjectSiteScope,
    NmapProviderMode,
)


class NmapAuthorityError(ValueError):
    """Base error for an invalid or conflicting authority operation."""


class NmapPolicyStateError(NmapAuthorityError):
    """Persisted authority is absent, malformed, or incompatible."""


class NmapInstallationNotFoundError(NmapAuthorityError):
    """No currently inspected installation matches an exact fingerprint."""


class NmapCapabilityDeniedError(NmapAuthorityError):
    """A caller attempted work the current deployment authority rejects."""

    def __init__(self, capability: NmapCapabilityResponse) -> None:
        super().__init__(capability.reason)
        self.capability = capability


MachineExecutorIdentity = Annotated[
    str,
    Field(
        min_length=49,
        max_length=49,
        pattern=(
            r"^machine-guid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    ),
]

_RAW_CAPABILITY_PROFILES = frozenset(
    {
        NmapProfileName.HOST_DISCOVERY,
        NmapProfileName.OS_INVENTORY,
        NmapProfileName.SELECTED_UDP,
        NmapProfileName.TCP_SYN_INVENTORY,
        NmapProfileName.TRACEROUTE_INVENTORY,
    }
)


def _runtime_permitted_profiles(
    profiles: tuple[NmapProfileName, ...],
    *,
    raw_capable: bool,
) -> tuple[NmapProfileName, ...]:
    if raw_capable:
        return profiles
    return tuple(profile for profile in profiles if profile not in _RAW_CAPABILITY_PROFILES)


class NmapProbeObservationV1(BaseModel):
    """One sanitized trust and packet-driver observation from the local probe."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    candidate: NmapDetectionCandidateV1 | None = None
    trust: NmapTrustResultV1
    machine_executor_identity: MachineExecutorIdentity | None = None
    npcap_version: str | None = None
    npcap_state: NmapNpcapState = NmapNpcapState.NOT_CHECKED
    raw_capable: bool = False

    def model_post_init(self, _context: object) -> None:
        if self.raw_capable != (self.npcap_state is NmapNpcapState.RAW_CAPABLE):
            raise ValueError("Npcap state and raw capability are inconsistent")
        if self.npcap_version is not None and not (1 <= len(self.npcap_version) <= 128):
            raise ValueError("Npcap version must contain between 1 and 128 characters")
        if self.trust.available and not self.machine_executor_identity:
            raise ValueError("An available Nmap observation requires machine identity")
        if self.trust.available:
            if self.candidate is None or self.trust.fingerprint is None:
                raise ValueError("An available Nmap observation requires candidate evidence")
            _validate_detection_fingerprint(self.candidate, self.trust.fingerprint)


class NmapInstallationProbe(Protocol):
    """Injected local detector/trust adapter; implementations stay OS-specific."""

    def inspect(self, policy: NmapTrustPolicyV1) -> tuple[NmapProbeObservationV1, ...]: ...

    def revalidate(
        self,
        candidate: NmapDetectionCandidateV1,
        confirmed: NmapInstallationFingerprintV1,
        policy: NmapTrustPolicyV1,
    ) -> NmapProbeObservationV1: ...


class UnavailableNmapInstallationProbe:
    """Fail-closed default until the Windows adapter is wired by the runtime."""

    def inspect(self, policy: NmapTrustPolicyV1) -> tuple[NmapProbeObservationV1, ...]:
        del policy
        return ()

    def revalidate(
        self,
        candidate: NmapDetectionCandidateV1,
        confirmed: NmapInstallationFingerprintV1,
        policy: NmapTrustPolicyV1,
    ) -> NmapProbeObservationV1:
        del confirmed, policy
        return NmapProbeObservationV1(
            candidate=candidate,
            trust=NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.INSPECTION_FAILED,
                fingerprint=None,
            ),
        )


class WindowsNmapInstallationProbe:
    """Network-free adapter over the core HKLM, trust, Npcap, and token APIs."""

    def inspect(self, policy: NmapTrustPolicyV1) -> tuple[NmapProbeObservationV1, ...]:
        try:
            backend = CtypesNmapTrustBackend()
            candidates = detect_nmap_candidates(
                registry=nmap_uninstall_registry(),
                paths=backend,
            )
        except (OSError, TypeError, ValueError):
            return ()
        machine_identity, npcap_version, npcap_state, raw_capable = self._runtime_capability()
        return tuple(
            NmapProbeObservationV1(
                candidate=candidate,
                trust=inspect_nmap_installation(
                    candidate=candidate,
                    policy=policy,
                    backend=backend,
                ),
                machine_executor_identity=machine_identity,
                npcap_version=npcap_version,
                npcap_state=npcap_state,
                raw_capable=raw_capable,
            )
            for candidate in candidates
        )

    def revalidate(
        self,
        candidate: NmapDetectionCandidateV1,
        confirmed: NmapInstallationFingerprintV1,
        policy: NmapTrustPolicyV1,
    ) -> NmapProbeObservationV1:
        try:
            trust = revalidate_nmap_installation(
                candidate=candidate,
                confirmed=confirmed,
                policy=policy,
                backend=CtypesNmapTrustBackend(),
            )
        except (OSError, TypeError, ValueError):
            trust = NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.INSPECTION_FAILED,
                fingerprint=None,
            )
        machine_identity, npcap_version, npcap_state, raw_capable = self._runtime_capability()
        return NmapProbeObservationV1(
            candidate=candidate,
            trust=trust,
            machine_executor_identity=machine_identity,
            npcap_version=npcap_version,
            npcap_state=npcap_state,
            raw_capable=raw_capable,
        )

    @staticmethod
    def _runtime_capability() -> tuple[str | None, str | None, NmapNpcapState, bool]:
        try:
            snapshot = CtypesNmapRuntimeCapabilityProbe().snapshot()
        except (OSError, TypeError, ValueError):
            return None, None, NmapNpcapState.NOT_CHECKED, False
        if not snapshot.npcap_installed:
            state = NmapNpcapState.MISSING
        elif snapshot.npcap_admin_only and not snapshot.current_token_is_administrator:
            state = NmapNpcapState.ADMIN_ONLY
        elif snapshot.current_token_has_raw_rights:
            state = NmapNpcapState.RAW_CAPABLE
        else:
            state = NmapNpcapState.CONNECT_ONLY
        return (
            snapshot.executor_identity,
            snapshot.npcap_version,
            state,
            state is NmapNpcapState.RAW_CAPABLE,
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _load_canonical_json(raw: str, *, label: str) -> object:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise NmapPolicyStateError(f"Stored {label} is invalid.") from error
    if _canonical_json(value) != raw:
        raise NmapPolicyStateError(f"Stored {label} is not canonical.")
    return value


def _load_reviewed_scripts(raw: str) -> tuple[NmapReviewedScriptV1, ...]:
    value = _load_canonical_json(raw, label="Nmap reviewed-script fingerprint")
    if not isinstance(value, list) or len(value) > 64:
        raise NmapPolicyStateError("Stored Nmap reviewed-script fingerprint is invalid.")
    try:
        scripts = tuple(NmapReviewedScriptV1.model_validate(item) for item in value)
    except ValidationError as error:
        raise NmapPolicyStateError("Stored Nmap reviewed-script fingerprint is invalid.") from error
    if (
        len({item.name for item in scripts}) != len(scripts)
        or tuple(sorted(scripts, key=lambda item: item.name)) != scripts
    ):
        raise NmapPolicyStateError("Stored Nmap reviewed scripts are not canonical.")
    return scripts


def _validate_detection_fingerprint(
    candidate: NmapDetectionCandidateV1,
    fingerprint: NmapInstallationFingerprintV1,
) -> None:
    if (
        candidate.install_root != fingerprint.install_root
        or candidate.executable_path != fingerprint.executable_path
        or candidate.data_directory != fingerprint.data_directory
    ):
        raise NmapPolicyStateError("Nmap detection candidate and trust fingerprint are inconsistent.")


def _actor(principal: AuthPrincipal) -> str:
    return principal.user_id or principal.source


class NmapExecutionAuthorityV1(BaseModel):
    """Protected service-only launch authority; never use as an API response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    deployment_id: str
    network_executor_id: str
    machine_executor_identity: MachineExecutorIdentity
    policy_id: str
    policy_revision: int
    confirmation_id: str
    capability: NmapCapabilityResponse
    candidate: NmapDetectionCandidateV1
    fingerprint: NmapInstallationFingerprintV1
    trust_policy: NmapTrustPolicyV1


class NmapCapabilityService:
    """Append-only Nmap policy, confirmation, and deployment capability gates."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        deployment_id: str,
        network_executor_id: str,
        probe: NmapInstallationProbe | None = None,
    ) -> None:
        self.engine = engine if engine is not None else get_engine()
        self.deployment_id = deployment_id.strip()
        self.network_executor_id = network_executor_id.strip()
        if not self.deployment_id or len(self.deployment_id) > 255:
            raise ValueError("deployment_id must contain between 1 and 255 characters")
        if not self.network_executor_id or len(self.network_executor_id) > 255:
            raise ValueError("network_executor_id must contain between 1 and 255 characters")
        self._probe = probe if probe is not None else UnavailableNmapInstallationProbe()
        self._session_factory = session_factory(self.engine)

    def create_policy(
        self,
        request: NmapDeploymentPolicyCreateRequest,
        *,
        principal: AuthPrincipal,
    ) -> NmapDeploymentPolicyResponse:
        """Append one administrator-reviewed revision for this exact executor."""
        now = datetime.now(UTC)
        actor = _actor(principal)
        scopes_payload = [item.model_dump(mode="json") for item in request.permitted_project_sites]
        profile_payload = request.profile_policy.model_dump(mode="json")
        with self._session_factory.begin() as session:
            previous = session.scalar(
                select(NmapDeploymentPolicy)
                .where(
                    NmapDeploymentPolicy.deployment_id == self.deployment_id,
                    NmapDeploymentPolicy.network_executor_id == self.network_executor_id,
                )
                .order_by(NmapDeploymentPolicy.revision.desc())
                .limit(1)
            )
            revision = 1 if previous is None else previous.revision + 1
            row = NmapDeploymentPolicy(
                policy_id=str(uuid4()),
                deployment_id=self.deployment_id,
                network_executor_id=self.network_executor_id,
                revision=revision,
                deployment_lane=request.deployment_lane.value,
                provider_mode=request.provider_mode.value,
                organization_internal=(request.deployment_lane is NmapDeploymentLane.INTERNAL_SAME_ORGANIZATION),
                deployment_owner=request.deployment_owner,
                operator_install_responsibility=request.operator_install_responsibility,
                permitted_project_sites_json=_canonical_json(scopes_payload),
                permitted_project_sites_sha256=canonical_sha256(scopes_payload),
                update_owner=request.update_owner,
                reviewed_version_policy=request.reviewed_version_policy,
                permitted_publishers_json=_canonical_json(list(request.permitted_publishers)),
                permitted_versions_json=_canonical_json(list(request.permitted_versions)),
                permitted_signer_sha256_json=_canonical_json(list(request.permitted_signer_sha256)),
                permitted_executable_sha256_json=_canonical_json(list(request.permitted_executable_sha256)),
                permitted_data_manifest_sha256_json=_canonical_json(list(request.permitted_data_manifest_sha256)),
                permitted_licence_sha256_json=_canonical_json(list(request.permitted_licence_sha256)),
                permitted_npsl_versions_json=_canonical_json(list(request.permitted_npsl_versions)),
                reviewed_scripts_json=_canonical_json(
                    [item.model_dump(mode="json") for item in request.reviewed_scripts]
                ),
                max_data_files=request.max_data_files,
                max_file_bytes=request.max_file_bytes,
                max_manifest_bytes=request.max_manifest_bytes,
                profile_policy_json=_canonical_json(profile_payload),
                profile_policy_sha256=canonical_sha256(profile_payload),
                reviewed_at=now,
                acknowledged_no_redistribution=request.acknowledged_no_redistribution,
                created_by=actor,
                reason=request.reason,
                created_at=now,
                supersedes_policy_id=None if previous is None else previous.policy_id,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise NmapPolicyStateError(
                    "A concurrent Nmap policy revision was created; reload and retry."
                ) from error
            response = self._policy_response(row, request=request)
        return response

    def list_policy_history(self) -> tuple[NmapDeploymentPolicyResponse, ...]:
        """Return validated history for the local deployment and executor only."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(NmapDeploymentPolicy)
                .where(
                    NmapDeploymentPolicy.deployment_id == self.deployment_id,
                    NmapDeploymentPolicy.network_executor_id == self.network_executor_id,
                )
                .order_by(NmapDeploymentPolicy.revision.desc())
            ).all()
            return tuple(self._policy_response(row) for row in rows)

    def trust_policy_from_record(self, row: NmapDeploymentPolicy) -> NmapTrustPolicyV1:
        """Reconstruct the exact core trust contract from canonical persistence."""
        request = self._policy_request_from_record(row)
        if request.provider_mode is not NmapProviderMode.INTERNAL_OPERATOR_MANAGED:
            raise NmapPolicyStateError("The current mode has no process trust policy.")
        return NmapTrustPolicyV1(
            permitted_publishers=request.permitted_publishers,
            permitted_versions=request.permitted_versions,
            permitted_signer_sha256=request.permitted_signer_sha256,
            permitted_executable_sha256=request.permitted_executable_sha256,
            permitted_npsl_versions=request.permitted_npsl_versions,
            reviewed_scripts=request.reviewed_scripts,
            max_data_files=request.max_data_files,
            max_file_bytes=request.max_file_bytes,
            max_manifest_bytes=request.max_manifest_bytes,
        )

    def scopes_from_policy_record(self, row: NmapDeploymentPolicy) -> tuple[NmapProjectSiteScope, ...]:
        return self._policy_request_from_record(row).permitted_project_sites

    def fingerprint_from_confirmation(self, row: NmapInstallationConfirmation) -> NmapInstallationFingerprintV1:
        """Reconstruct and revalidate the exact protected installation identity."""
        try:
            fingerprint = NmapInstallationFingerprintV1(
                install_root=row.install_root,
                executable_path=row.executable_path,
                data_directory=row.data_directory,
                publisher=row.publisher,
                signer_sha256=row.signer_sha256,
                version=row.version,
                executable_sha256=row.executable_sha256,
                data_manifest_sha256=row.data_manifest_sha256,
                data_file_count=row.data_file_count,
                data_total_bytes=row.data_total_bytes,
                licence_relative_path=row.licence_relative_path,
                licence_sha256=row.licence_sha256,
                npsl_version=row.npsl_version,
                reviewed_scripts=_load_reviewed_scripts(row.reviewed_scripts_json),
                fingerprint_sha256=row.fingerprint_sha256,
            )
        except ValidationError as error:
            raise NmapPolicyStateError("Stored installation confirmation is invalid.") from error
        return fingerprint

    def detection_candidate_from_confirmation(self, row: NmapInstallationConfirmation) -> NmapDetectionCandidateV1:
        """Reconstruct the exact protected HKLM uninstall candidate snapshot."""
        value = _load_canonical_json(
            row.detection_candidate_json,
            label="Nmap detection candidate",
        )
        if not isinstance(value, dict):
            raise NmapPolicyStateError("Stored Nmap detection candidate is invalid.")
        try:
            candidate = NmapDetectionCandidateV1.model_validate(value)
        except ValidationError as error:
            raise NmapPolicyStateError("Stored Nmap detection candidate is invalid.") from error
        payload = candidate.model_dump(mode="json")
        if (
            _canonical_json(payload) != row.detection_candidate_json
            or canonical_sha256(payload) != row.detection_candidate_sha256
        ):
            raise NmapPolicyStateError("Stored Nmap detection candidate digest is invalid.")
        return candidate

    def inspect_current(self) -> tuple[NmapDetectedInstallationResponse, ...]:
        """Inspect only after an internal process policy has been validated."""
        policy = self._current_policy()
        if policy is None:
            raise NmapPolicyStateError("Nmap policy is not configured for this executor.")
        request = self._policy_request_from_record(policy)
        if request.provider_mode is not NmapProviderMode.INTERNAL_OPERATOR_MANAGED:
            raise NmapPolicyStateError("Nmap detection is unavailable in the current mode.")
        trust_policy = self.trust_policy_from_record(policy)
        return tuple(self._detected_response(item) for item in self._probe.inspect(trust_policy))

    def confirm_installation(
        self,
        request: NmapInstallationConfirmationRequest,
        *,
        principal: AuthPrincipal,
    ) -> NmapInstallationConfirmationResponse:
        """Append one exact global-admin confirmation from current probe evidence."""
        policy = self._current_policy()
        if policy is None:
            raise NmapPolicyStateError("Nmap policy is not configured for this executor.")
        policy_request = self._policy_request_from_record(policy)
        if policy_request.provider_mode is not NmapProviderMode.INTERNAL_OPERATOR_MANAGED:
            raise NmapPolicyStateError("Installation confirmation requires process mode.")
        trust_policy = self.trust_policy_from_record(policy)
        matching = [
            item
            for item in self._probe.inspect(trust_policy)
            if item.trust.available
            and item.trust.fingerprint is not None
            and item.trust.fingerprint.fingerprint_sha256 == request.fingerprint_sha256
        ]
        if len(matching) != 1:
            raise NmapInstallationNotFoundError("No single trusted installation matches that exact fingerprint.")
        observation = matching[0]
        fingerprint = observation.trust.fingerprint
        candidate = observation.candidate
        assert fingerprint is not None and candidate is not None
        if observation.machine_executor_identity is None:
            raise NmapInstallationNotFoundError("The local machine executor identity is unavailable.")
        _validate_detection_fingerprint(candidate, fingerprint)
        if (
            fingerprint.data_manifest_sha256 not in policy_request.permitted_data_manifest_sha256
            or fingerprint.licence_sha256 not in policy_request.permitted_licence_sha256
            or fingerprint.data_file_count > policy_request.max_data_files
            or fingerprint.data_total_bytes > policy_request.max_manifest_bytes
        ):
            raise NmapInstallationNotFoundError("The installation does not match the reviewed data and licence policy.")
        now = datetime.now(UTC)
        scopes_payload = [item.model_dump(mode="json") for item in policy_request.permitted_project_sites]
        candidate_payload = candidate.model_dump(mode="json")
        row = NmapInstallationConfirmation(
            confirmation_id=str(uuid4()),
            policy_id=policy.policy_id,
            deployment_id=self.deployment_id,
            network_executor_id=self.network_executor_id,
            policy_revision=policy.revision,
            deployment_lane=policy.deployment_lane,
            provider_mode=policy.provider_mode,
            permitted_project_sites_json=_canonical_json(scopes_payload),
            permitted_project_sites_sha256=canonical_sha256(scopes_payload),
            detection_candidate_json=_canonical_json(candidate_payload),
            detection_candidate_sha256=canonical_sha256(candidate_payload),
            machine_executor_identity=observation.machine_executor_identity,
            publisher=fingerprint.publisher,
            version=fingerprint.version,
            install_root=fingerprint.install_root,
            executable_path=fingerprint.executable_path,
            data_directory=fingerprint.data_directory,
            executable_sha256=fingerprint.executable_sha256,
            data_manifest_sha256=fingerprint.data_manifest_sha256,
            data_file_count=fingerprint.data_file_count,
            data_total_bytes=fingerprint.data_total_bytes,
            licence_relative_path=fingerprint.licence_relative_path,
            licence_sha256=fingerprint.licence_sha256,
            npsl_version=fingerprint.npsl_version,
            reviewed_scripts_json=_canonical_json(
                [item.model_dump(mode="json") for item in fingerprint.reviewed_scripts]
            ),
            signer_sha256=fingerprint.signer_sha256,
            fingerprint_sha256=fingerprint.fingerprint_sha256,
            npcap_version=observation.npcap_version,
            npcap_state=observation.npcap_state.value,
            raw_capable=observation.raw_capable,
            confirmed_by=_actor(principal),
            reason=request.reason,
            confirmed_at=now,
        )
        with self._session_factory.begin() as session:
            latest_confirmed_at = session.scalar(
                select(func.max(NmapInstallationConfirmation.confirmed_at)).where(
                    NmapInstallationConfirmation.policy_id == policy.policy_id,
                    NmapInstallationConfirmation.deployment_id == self.deployment_id,
                    NmapInstallationConfirmation.network_executor_id == self.network_executor_id,
                )
            )
            if latest_confirmed_at is not None and row.confirmed_at <= latest_confirmed_at:
                row.confirmed_at = latest_confirmed_at + timedelta(microseconds=1)
            session.add(row)
            session.flush()
            response = self._confirmation_response(row)
        return response

    def capability(self, *, project_id: str, site_id: str) -> NmapCapabilityResponse:
        """Return the sanitized local capability for one explicitly permitted site."""
        policy = self._current_policy()
        if policy is None:
            return self._disabled("policy_not_configured")
        try:
            request = self._policy_request_from_record(policy)
        except NmapPolicyStateError:
            return self._unavailable("policy_invalid", policy=policy)
        if (project_id, site_id) not in {(item.project_id, item.site_id) for item in request.permitted_project_sites}:
            return self._disabled("site_not_permitted", policy=policy)
        if request.deployment_lane is NmapDeploymentLane.EXTERNAL_CUSTOMER:
            return self._disabled("external_deployment", policy=policy)
        if request.provider_mode is NmapProviderMode.DISABLED:
            return self._disabled("provider_disabled", policy=policy)
        if request.provider_mode is NmapProviderMode.OPERATOR_XML_IMPORT:
            return NmapCapabilityResponse(
                state=NmapCapabilityState.XML_IMPORT_ONLY,
                reason="xml_import_allowed",
                provider_mode=request.provider_mode,
                policy_id=policy.policy_id,
                policy_revision=policy.revision,
                xml_import_allowed=True,
            )
        try:
            trust_policy = self.trust_policy_from_record(policy)
            confirmation = self._latest_confirmation(policy)
            if confirmation is None:
                return NmapCapabilityResponse(
                    state=NmapCapabilityState.CONFIRMATION_REQUIRED,
                    reason="confirmation_required",
                    provider_mode=request.provider_mode,
                    policy_id=policy.policy_id,
                    policy_revision=policy.revision,
                    permitted_profiles=_runtime_permitted_profiles(
                        request.profile_policy.permitted_profiles,
                        raw_capable=False,
                    ),
                )
            self._validate_confirmation_binding(confirmation, policy, request)
            candidate = self.detection_candidate_from_confirmation(confirmation)
            fingerprint = self.fingerprint_from_confirmation(confirmation)
            _validate_detection_fingerprint(candidate, fingerprint)
        except NmapPolicyStateError:
            return self._unavailable("policy_invalid", policy=policy)
        try:
            observation = NmapProbeObservationV1.model_validate(
                self._probe.revalidate(candidate, fingerprint, trust_policy)
            )
        except Exception:
            return self._unavailable("capability_probe_failed", policy=policy)
        if not observation.trust.available or observation.trust.fingerprint is None:
            return NmapCapabilityResponse(
                state=NmapCapabilityState.UNAVAILABLE,
                reason=observation.trust.reason.value,
                provider_mode=request.provider_mode,
                policy_id=policy.policy_id,
                policy_revision=policy.revision,
                publisher=fingerprint.publisher,
                version=fingerprint.version,
                fingerprint_sha256=fingerprint.fingerprint_sha256,
                npcap_version=observation.npcap_version,
                npcap_state=observation.npcap_state,
                raw_capable=observation.raw_capable,
                permitted_profiles=_runtime_permitted_profiles(
                    request.profile_policy.permitted_profiles,
                    raw_capable=observation.raw_capable,
                ),
            )
        if observation.machine_executor_identity != confirmation.machine_executor_identity:
            return self._invalidated_capability(
                reason="executor_mismatch",
                policy=policy,
                request=request,
                fingerprint=fingerprint,
                observation=observation,
            )
        if observation.trust.fingerprint != fingerprint:
            return self._invalidated_capability(
                reason=NmapTrustReason.FINGERPRINT_DRIFT.value,
                policy=policy,
                request=request,
                fingerprint=fingerprint,
                observation=observation,
            )
        if observation.candidate != candidate:
            return self._invalidated_capability(
                reason=NmapTrustReason.FINGERPRINT_DRIFT.value,
                policy=policy,
                request=request,
                fingerprint=fingerprint,
                observation=observation,
            )
        return NmapCapabilityResponse(
            state=NmapCapabilityState.AVAILABLE,
            reason="available",
            provider_mode=request.provider_mode,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            publisher=fingerprint.publisher,
            version=fingerprint.version,
            fingerprint_sha256=fingerprint.fingerprint_sha256,
            npcap_version=observation.npcap_version,
            npcap_state=observation.npcap_state,
            raw_capable=observation.raw_capable,
            process_selection_allowed=True,
            permitted_profiles=_runtime_permitted_profiles(
                request.profile_policy.permitted_profiles,
                raw_capable=observation.raw_capable,
            ),
        )

    def assert_process_selection_allowed(self, *, project_id: str, site_id: str) -> NmapCapabilityResponse:
        """Reject before provider selection unless exact local authority is available."""
        capability = self.capability(project_id=project_id, site_id=site_id)
        if not capability.process_selection_allowed:
            raise NmapCapabilityDeniedError(capability)
        return capability

    def execution_authority(self, *, project_id: str, site_id: str) -> NmapExecutionAuthorityV1:
        """Return protected exact launch inputs after the scoped capability gate."""
        capability = self.assert_process_selection_allowed(project_id=project_id, site_id=site_id)
        policy = self._current_policy()
        if policy is None or policy.policy_id != capability.policy_id or policy.revision != capability.policy_revision:
            changed = (
                self._disabled("policy_not_configured")
                if policy is None
                else self._unavailable("policy_changed", policy=policy)
            )
            raise NmapCapabilityDeniedError(changed)
        confirmation = self._latest_confirmation(policy)
        if confirmation is None:
            raise NmapCapabilityDeniedError(self._unavailable("confirmation_changed", policy=policy))
        request = self._policy_request_from_record(policy)
        self._validate_confirmation_binding(confirmation, policy, request)
        candidate = self.detection_candidate_from_confirmation(confirmation)
        fingerprint = self.fingerprint_from_confirmation(confirmation)
        _validate_detection_fingerprint(candidate, fingerprint)
        authority = NmapExecutionAuthorityV1(
            deployment_id=self.deployment_id,
            network_executor_id=self.network_executor_id,
            machine_executor_identity=confirmation.machine_executor_identity,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            confirmation_id=confirmation.confirmation_id,
            capability=capability,
            candidate=candidate,
            fingerprint=fingerprint,
            trust_policy=self.trust_policy_from_record(policy),
        )
        trust = self.revalidate_execution_authority(authority)
        if not trust.available:
            raise NmapCapabilityDeniedError(
                capability.model_copy(
                    update={
                        "state": NmapCapabilityState.UNAVAILABLE,
                        "reason": trust.reason.value,
                        "raw_capable": False,
                        "process_selection_allowed": False,
                    }
                )
            )
        return authority

    def revalidate_execution_authority(
        self,
        authority: NmapExecutionAuthorityV1,
    ) -> NmapTrustResultV1:
        """Revalidate a protected launch authority for the runner preflight."""
        rejected = NmapTrustResultV1(
            available=False,
            reason=NmapTrustReason.FINGERPRINT_DRIFT,
            fingerprint=None,
        )
        if authority.deployment_id != self.deployment_id or authority.network_executor_id != self.network_executor_id:
            return rejected
        policy = self._current_policy()
        if policy is None or policy.policy_id != authority.policy_id or policy.revision != authority.policy_revision:
            return rejected
        confirmation = self._latest_confirmation(policy)
        if confirmation is None or confirmation.confirmation_id != authority.confirmation_id:
            return rejected
        try:
            request = self._policy_request_from_record(policy)
            self._validate_confirmation_binding(confirmation, policy, request)
            candidate = self.detection_candidate_from_confirmation(confirmation)
            fingerprint = self.fingerprint_from_confirmation(confirmation)
            trust_policy = self.trust_policy_from_record(policy)
            _validate_detection_fingerprint(candidate, fingerprint)
        except NmapPolicyStateError:
            return NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.INSPECTION_FAILED,
                fingerprint=None,
            )
        capability = authority.capability
        if (
            authority.candidate != candidate
            or authority.fingerprint != fingerprint
            or authority.trust_policy != trust_policy
            or authority.machine_executor_identity != confirmation.machine_executor_identity
            or capability.state is not NmapCapabilityState.AVAILABLE
            or capability.provider_mode is not NmapProviderMode.INTERNAL_OPERATOR_MANAGED
            or not capability.process_selection_allowed
            or capability.policy_id != policy.policy_id
            or capability.policy_revision != policy.revision
            or capability.publisher != fingerprint.publisher
            or capability.version != fingerprint.version
            or capability.fingerprint_sha256 != fingerprint.fingerprint_sha256
            or capability.permitted_profiles
            != _runtime_permitted_profiles(
                request.profile_policy.permitted_profiles,
                raw_capable=capability.raw_capable,
            )
        ):
            return rejected
        try:
            observation = NmapProbeObservationV1.model_validate(
                self._probe.revalidate(candidate, fingerprint, trust_policy)
            )
        except Exception:
            return NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.INSPECTION_FAILED,
                fingerprint=None,
            )
        if not observation.trust.available or observation.trust.fingerprint is None:
            return observation.trust
        if (
            observation.candidate != candidate
            or observation.machine_executor_identity != authority.machine_executor_identity
            or observation.trust.fingerprint != fingerprint
            or observation.raw_capable != capability.raw_capable
            or capability.permitted_profiles
            != _runtime_permitted_profiles(
                request.profile_policy.permitted_profiles,
                raw_capable=observation.raw_capable,
            )
        ):
            return rejected
        return observation.trust

    def assert_xml_import_allowed(self, *, project_id: str, site_id: str) -> NmapCapabilityResponse:
        """Reject before reading or parsing an operator-provided XML artifact."""
        capability = self.capability(project_id=project_id, site_id=site_id)
        if not capability.xml_import_allowed:
            raise NmapCapabilityDeniedError(capability)
        return capability

    def _current_policy(self) -> NmapDeploymentPolicy | None:
        with self._session_factory() as session:
            return session.scalar(
                select(NmapDeploymentPolicy)
                .where(
                    NmapDeploymentPolicy.deployment_id == self.deployment_id,
                    NmapDeploymentPolicy.network_executor_id == self.network_executor_id,
                )
                .order_by(NmapDeploymentPolicy.revision.desc())
                .limit(1)
            )

    def _latest_confirmation(self, policy: NmapDeploymentPolicy) -> NmapInstallationConfirmation | None:
        with self._session_factory() as session:
            return session.scalar(
                select(NmapInstallationConfirmation)
                .where(
                    NmapInstallationConfirmation.policy_id == policy.policy_id,
                    NmapInstallationConfirmation.deployment_id == self.deployment_id,
                    NmapInstallationConfirmation.network_executor_id == self.network_executor_id,
                )
                .order_by(
                    NmapInstallationConfirmation.confirmed_at.desc(),
                    NmapInstallationConfirmation.confirmation_id.desc(),
                )
                .limit(1)
            )

    def _policy_request_from_record(self, row: NmapDeploymentPolicy) -> NmapDeploymentPolicyCreateRequest:
        if row.deployment_id != self.deployment_id or row.network_executor_id != self.network_executor_id:
            raise NmapPolicyStateError("Stored Nmap policy belongs to a different executor.")
        raw_fields = {
            "permitted_project_sites": _load_canonical_json(
                row.permitted_project_sites_json, label="Nmap project/site scope"
            ),
            "permitted_publishers": _load_canonical_json(row.permitted_publishers_json, label="Nmap publisher policy"),
            "permitted_versions": _load_canonical_json(row.permitted_versions_json, label="Nmap version policy"),
            "permitted_signer_sha256": _load_canonical_json(
                row.permitted_signer_sha256_json, label="Nmap signer policy"
            ),
            "permitted_executable_sha256": _load_canonical_json(
                row.permitted_executable_sha256_json, label="Nmap executable policy"
            ),
            "permitted_data_manifest_sha256": _load_canonical_json(
                row.permitted_data_manifest_sha256_json, label="Nmap data policy"
            ),
            "permitted_licence_sha256": _load_canonical_json(
                row.permitted_licence_sha256_json, label="Nmap licence policy"
            ),
            "permitted_npsl_versions": _load_canonical_json(row.permitted_npsl_versions_json, label="Nmap NPSL policy"),
            "reviewed_scripts": _load_canonical_json(row.reviewed_scripts_json, label="Nmap reviewed-script policy"),
            "profile_policy": _load_canonical_json(row.profile_policy_json, label="Nmap profile policy"),
        }
        try:
            request = NmapDeploymentPolicyCreateRequest.model_validate(
                {
                    "deployment_lane": row.deployment_lane,
                    "provider_mode": row.provider_mode,
                    "deployment_owner": row.deployment_owner,
                    "operator_install_responsibility": row.operator_install_responsibility,
                    "update_owner": row.update_owner,
                    "reviewed_version_policy": row.reviewed_version_policy,
                    "max_data_files": row.max_data_files,
                    "max_file_bytes": row.max_file_bytes,
                    "max_manifest_bytes": row.max_manifest_bytes,
                    "acknowledged_no_redistribution": row.acknowledged_no_redistribution,
                    "reason": row.reason,
                    **raw_fields,
                }
            )
        except ValidationError as error:
            raise NmapPolicyStateError("Stored Nmap policy is invalid.") from error
        scopes_payload = [item.model_dump(mode="json") for item in request.permitted_project_sites]
        profile_payload = request.profile_policy.model_dump(mode="json")
        if (
            row.organization_internal != (request.deployment_lane is NmapDeploymentLane.INTERNAL_SAME_ORGANIZATION)
            or row.permitted_project_sites_sha256 != canonical_sha256(scopes_payload)
            or row.profile_policy_sha256 != canonical_sha256(profile_payload)
        ):
            raise NmapPolicyStateError("Stored Nmap policy digest or lane binding is invalid.")
        return request

    def _validate_confirmation_binding(
        self,
        confirmation: NmapInstallationConfirmation,
        policy: NmapDeploymentPolicy,
        request: NmapDeploymentPolicyCreateRequest,
    ) -> None:
        scopes_value = _load_canonical_json(
            confirmation.permitted_project_sites_json,
            label="Nmap confirmation project/site scope",
        )
        try:
            scopes = tuple(NmapProjectSiteScope.model_validate(item) for item in scopes_value)  # type: ignore[arg-type]
        except (TypeError, ValidationError) as error:
            raise NmapPolicyStateError("Stored confirmation scope is invalid.") from error
        expected = request.permitted_project_sites
        payload = [item.model_dump(mode="json") for item in scopes]
        if (
            confirmation.policy_id != policy.policy_id
            or confirmation.deployment_id != self.deployment_id
            or confirmation.network_executor_id != self.network_executor_id
            or confirmation.policy_revision != policy.revision
            or confirmation.deployment_lane != policy.deployment_lane
            or confirmation.provider_mode != policy.provider_mode
            or scopes != expected
            or confirmation.permitted_project_sites_sha256 != canonical_sha256(payload)
            or confirmation.permitted_project_sites_sha256 != policy.permitted_project_sites_sha256
        ):
            raise NmapPolicyStateError("Stored confirmation has a mismatched authority binding.")

    def _policy_response(
        self,
        row: NmapDeploymentPolicy,
        *,
        request: NmapDeploymentPolicyCreateRequest | None = None,
    ) -> NmapDeploymentPolicyResponse:
        resolved = request if request is not None else self._policy_request_from_record(row)
        return NmapDeploymentPolicyResponse(
            policy_id=row.policy_id,
            deployment_id=row.deployment_id,
            network_executor_id=row.network_executor_id,
            revision=row.revision,
            deployment_lane=resolved.deployment_lane,
            provider_mode=resolved.provider_mode,
            organization_internal=row.organization_internal,
            deployment_owner=resolved.deployment_owner,
            operator_install_responsibility=resolved.operator_install_responsibility,
            permitted_project_sites=resolved.permitted_project_sites,
            update_owner=resolved.update_owner,
            reviewed_version_policy=resolved.reviewed_version_policy,
            permitted_publishers=resolved.permitted_publishers,
            permitted_versions=resolved.permitted_versions,
            permitted_signer_sha256=resolved.permitted_signer_sha256,
            permitted_executable_sha256=resolved.permitted_executable_sha256,
            permitted_data_manifest_sha256=resolved.permitted_data_manifest_sha256,
            permitted_licence_sha256=resolved.permitted_licence_sha256,
            permitted_npsl_versions=resolved.permitted_npsl_versions,
            reviewed_scripts=resolved.reviewed_scripts,
            max_data_files=resolved.max_data_files,
            max_file_bytes=resolved.max_file_bytes,
            max_manifest_bytes=resolved.max_manifest_bytes,
            profile_policy=resolved.profile_policy,
            reviewed_at=row.reviewed_at,
            acknowledged_no_redistribution=resolved.acknowledged_no_redistribution,
            created_by=row.created_by,
            reason=row.reason,
            created_at=row.created_at,
            supersedes_policy_id=row.supersedes_policy_id,
        )

    @staticmethod
    def _confirmation_response(
        row: NmapInstallationConfirmation,
    ) -> NmapInstallationConfirmationResponse:
        return NmapInstallationConfirmationResponse(
            confirmation_id=row.confirmation_id,
            policy_id=row.policy_id,
            deployment_id=row.deployment_id,
            network_executor_id=row.network_executor_id,
            policy_revision=row.policy_revision,
            state=NmapCapabilityState.AVAILABLE,
            reason="available",
            publisher=row.publisher,
            version=row.version,
            fingerprint_sha256=row.fingerprint_sha256,
            signer_sha256=row.signer_sha256,
            executable_sha256=row.executable_sha256,
            data_manifest_sha256=row.data_manifest_sha256,
            data_file_count=row.data_file_count,
            data_total_bytes=row.data_total_bytes,
            licence_sha256=row.licence_sha256,
            npsl_version=row.npsl_version,
            reviewed_scripts=_load_reviewed_scripts(row.reviewed_scripts_json),
            npcap_version=row.npcap_version,
            npcap_state=NmapNpcapState(row.npcap_state),
            raw_capable=row.raw_capable,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
        )

    @staticmethod
    def _detected_response(item: NmapProbeObservationV1) -> NmapDetectedInstallationResponse:
        fingerprint = item.trust.fingerprint
        candidate = item.candidate
        return NmapDetectedInstallationResponse(
            state=(NmapCapabilityState.AVAILABLE if item.trust.available else NmapCapabilityState.UNAVAILABLE),
            reason=item.trust.reason.value,
            publisher=(
                fingerprint.publisher
                if fingerprint is not None
                else None
                if candidate is None
                else candidate.registry_publisher or None
            ),
            version=(
                fingerprint.version
                if fingerprint is not None
                else None
                if candidate is None
                else candidate.version or None
            ),
            registry_view=None if candidate is None else candidate.registry_view,
            display_name=None if candidate is None else candidate.display_name,
            fingerprint_sha256=(None if fingerprint is None else fingerprint.fingerprint_sha256),
            signer_sha256=None if fingerprint is None else fingerprint.signer_sha256,
            executable_sha256=(None if fingerprint is None else fingerprint.executable_sha256),
            data_manifest_sha256=(None if fingerprint is None else fingerprint.data_manifest_sha256),
            data_file_count=None if fingerprint is None else fingerprint.data_file_count,
            data_total_bytes=None if fingerprint is None else fingerprint.data_total_bytes,
            licence_sha256=None if fingerprint is None else fingerprint.licence_sha256,
            npsl_version=None if fingerprint is None else fingerprint.npsl_version,
            reviewed_scripts=() if fingerprint is None else fingerprint.reviewed_scripts,
            npcap_version=item.npcap_version,
            npcap_state=item.npcap_state,
            raw_capable=item.raw_capable,
        )

    @staticmethod
    def _disabled(reason: str, *, policy: NmapDeploymentPolicy | None = None) -> NmapCapabilityResponse:
        return NmapCapabilityResponse(
            state=NmapCapabilityState.DISABLED,
            reason=reason,
            provider_mode=(NmapProviderMode.DISABLED if policy is None else NmapProviderMode(policy.provider_mode)),
            policy_id=None if policy is None else policy.policy_id,
            policy_revision=None if policy is None else policy.revision,
        )

    @staticmethod
    def _unavailable(reason: str, *, policy: NmapDeploymentPolicy) -> NmapCapabilityResponse:
        try:
            mode = NmapProviderMode(policy.provider_mode)
        except ValueError:
            mode = NmapProviderMode.DISABLED
        return NmapCapabilityResponse(
            state=NmapCapabilityState.UNAVAILABLE,
            reason=reason,
            provider_mode=mode,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
        )

    @staticmethod
    def _invalidated_capability(
        *,
        reason: str,
        policy: NmapDeploymentPolicy,
        request: NmapDeploymentPolicyCreateRequest,
        fingerprint: NmapInstallationFingerprintV1,
        observation: NmapProbeObservationV1,
    ) -> NmapCapabilityResponse:
        return NmapCapabilityResponse(
            state=NmapCapabilityState.UNAVAILABLE,
            reason=reason,
            provider_mode=request.provider_mode,
            policy_id=policy.policy_id,
            policy_revision=policy.revision,
            publisher=fingerprint.publisher,
            version=fingerprint.version,
            fingerprint_sha256=fingerprint.fingerprint_sha256,
            npcap_version=observation.npcap_version,
            npcap_state=observation.npcap_state,
            raw_capable=observation.raw_capable,
            permitted_profiles=_runtime_permitted_profiles(
                request.profile_policy.permitted_profiles,
                raw_capable=observation.raw_capable,
            ),
        )
