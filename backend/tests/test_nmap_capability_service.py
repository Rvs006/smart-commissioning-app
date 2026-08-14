from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.core.auth import AuthPrincipal
from app.schemas.nmap import (
    NmapDeploymentLane,
    NmapDeploymentPolicyCreateRequest,
    NmapInstallationConfirmationRequest,
    NmapProfilePolicyV1,
    NmapProjectSiteScope,
    NmapProviderMode,
)
from app.services.nmap_capability_service import (
    NmapCapabilityDeniedError,
    NmapCapabilityService,
    NmapInstallationNotFoundError,
    NmapNpcapState,
    NmapPolicyStateError,
    NmapProbeObservationV1,
    WindowsNmapInstallationProbe,
)
from pydantic import ValidationError
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    Base,
    NmapDeploymentPolicy,
    NmapInstallationConfirmation,
)
from smart_commissioning_core.engines.ip.nmap_detection import NmapDetectionCandidateV1
from smart_commissioning_core.engines.ip.nmap_profiles import NmapProfileName
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapTrustPolicyV1,
    NmapTrustReason,
    NmapTrustResultV1,
)
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.run_context import canonical_sha256
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.pool import StaticPool

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64


def _fingerprint() -> NmapInstallationFingerprintV1:
    payload: dict[str, object] = {
        "install_root": r"C:\\Program Files\\Nmap",
        "executable_path": r"C:\\Program Files\\Nmap\\nmap.exe",
        "data_directory": r"C:\\Program Files\\Nmap",
        "publisher": "Insecure.Com LLC",
        "signer_sha256": _A,
        "version": "7.98",
        "executable_sha256": _B,
        "data_manifest_sha256": _C,
        "data_file_count": 17,
        # Keep this within the bootstrap inspection's bounded 512 MiB manifest
        # allowance. A real one-click approval can only record an installation
        # it was able to inspect under that same bound.
        "data_total_bytes": 123_456_789,
        "licence_relative_path": "LICENSE.txt",
        "licence_sha256": _D,
        "npsl_version": "1.1",
        "reviewed_scripts": [],
    }
    return NmapInstallationFingerprintV1(
        **payload,
        fingerprint_sha256=canonical_sha256(payload),
    )


def _candidate() -> NmapDetectionCandidateV1:
    return NmapDetectionCandidateV1(
        registry_view="64",
        registry_key="Nmap",
        display_name="Nmap 7.98",
        registry_publisher="Insecure.Com LLC",
        version="7.98",
        install_root=r"C:\\Program Files\\Nmap",
        executable_path=r"C:\\Program Files\\Nmap\\nmap.exe",
        data_directory=r"C:\\Program Files\\Nmap",
    )


def _policy_request(
    *,
    lane: NmapDeploymentLane = NmapDeploymentLane.INTERNAL_SAME_ORGANIZATION,
    mode: NmapProviderMode = NmapProviderMode.INTERNAL_OPERATOR_MANAGED,
    profiles: tuple[NmapProfileName, ...] | None = None,
) -> NmapDeploymentPolicyCreateRequest:
    enabled = mode is not NmapProviderMode.DISABLED
    return NmapDeploymentPolicyCreateRequest(
        deployment_lane=lane,
        provider_mode=mode,
        deployment_owner="Facilities IT",
        operator_install_responsibility="The deployment owner installs and services Nmap.",
        permitted_project_sites=(NmapProjectSiteScope(project_id="project-a", site_id="site-a"),),
        update_owner="Facilities IT",
        reviewed_version_policy="Nmap 7.98 and NPSL 1.1 were reviewed for this lane.",
        permitted_publishers=("Insecure.Com LLC",) if enabled else (),
        permitted_versions=("7.98",) if enabled else (),
        permitted_signer_sha256=(_A,) if enabled else (),
        permitted_executable_sha256=(_B,) if enabled else (),
        permitted_data_manifest_sha256=(_C,) if enabled else (),
        permitted_licence_sha256=(_D,) if enabled else (),
        permitted_npsl_versions=("1.1",) if enabled else (),
        max_data_files=8192,
        max_file_bytes=64 * 1024 * 1024,
        max_manifest_bytes=1024 * 1024 * 1024,
        profile_policy=NmapProfilePolicyV1(
            permitted_profiles=(
                profiles
                if enabled and profiles is not None
                else (NmapProfileName.TCP_CONNECT_INVENTORY,)
                if enabled
                else ()
            ),
        ),
        acknowledged_no_redistribution=enabled,
        reason="Approve this exact deployment lane and trust policy.",
    )


class _Probe:
    def __init__(self, fingerprint: NmapInstallationFingerprintV1 | None = None) -> None:
        self.fingerprint = fingerprint
        self.candidate = _candidate()
        self.inspect_calls = 0
        self.revalidate_calls = 0
        self.revalidation_error: Exception | None = None
        self.revalidation = self._available(fingerprint) if fingerprint is not None else None
        self.bootstrap_observations: tuple[NmapProbeObservationV1, ...] | None = None
        self.inspect_observations: tuple[NmapProbeObservationV1, ...] | None = None

    @staticmethod
    def _available(fingerprint: NmapInstallationFingerprintV1 | None) -> NmapProbeObservationV1:
        assert fingerprint is not None
        return NmapProbeObservationV1(
            candidate=_candidate(),
            trust=NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=fingerprint,
            ),
            machine_executor_identity=("machine-guid:12345678-1234-1234-1234-123456789abc"),
            npcap_version="1.82",
            npcap_state="raw_capable",
            raw_capable=True,
        )

    def inspect(self, policy: NmapTrustPolicyV1) -> tuple[NmapProbeObservationV1, ...]:
        self.inspect_calls += 1
        if self.inspect_observations is not None:
            return self.inspect_observations
        if self.fingerprint is None:
            return ()
        return (self._available(self.fingerprint),)

    def bootstrap_inspect(self) -> tuple[NmapProbeObservationV1, ...]:
        if self.bootstrap_observations is not None:
            return self.bootstrap_observations
        if self.fingerprint is None:
            return ()
        return (self._available(self.fingerprint),)

    def revalidate(
        self,
        candidate: NmapDetectionCandidateV1,
        confirmed: NmapInstallationFingerprintV1,
        policy: NmapTrustPolicyV1,
    ) -> NmapProbeObservationV1:
        self.revalidate_calls += 1
        if self.revalidation_error is not None:
            raise self.revalidation_error
        assert candidate == self.candidate
        assert confirmed == self.fingerprint
        assert self.revalidation is not None
        return self.revalidation


class _BlockingBootstrapProbe(_Probe):
    def __init__(self, fingerprint: NmapInstallationFingerprintV1) -> None:
        super().__init__(fingerprint)
        self.bootstrap_started = threading.Event()
        self.release_bootstrap = threading.Event()
        self.bootstrap_calls = 0
        self._bootstrap_lock = threading.Lock()

    def bootstrap_inspect(self) -> tuple[NmapProbeObservationV1, ...]:
        with self._bootstrap_lock:
            self.bootstrap_calls += 1
            first_call = self.bootstrap_calls == 1
        if first_call:
            self.bootstrap_started.set()
            if not self.release_bootstrap.wait(3.0):
                raise TimeoutError("concurrent approval test did not release bootstrap inspection")
        return super().bootstrap_inspect()


class NmapCapabilityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        event.listen(
            self.engine,
            "connect",
            lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
        )
        Base.metadata.create_all(self.engine)
        self.principal = AuthPrincipal("admin-1", "admin", Role.ADMIN, "user_key")
        self.fingerprint = _fingerprint()
        self.probe = _Probe(self.fingerprint)
        self.service = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-a",
            network_executor_id="executor-a",
            probe=self.probe,
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_connect_only_runtime_filters_raw_profiles_but_keeps_tcp_connect(self) -> None:
        self.service.create_policy(
            _policy_request(
                profiles=tuple(
                    sorted(NmapProfileName, key=lambda profile: profile.value)
                )
            ),
            principal=self.principal,
        )
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm exact trusted files.",
            ),
            principal=self.principal,
        )
        self.probe.revalidation = NmapProbeObservationV1(
            candidate=self.probe.candidate,
            trust=NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=self.fingerprint,
            ),
            machine_executor_identity=(
                "machine-guid:12345678-1234-1234-1234-123456789abc"
            ),
            npcap_version="1.82",
            npcap_state="connect_only",
            raw_capable=False,
        )

        capability = self.service.capability(project_id="project-a", site_id="site-a")

        self.assertTrue(capability.process_selection_allowed)
        self.assertFalse(capability.raw_capable)
        self.assertEqual(
            capability.permitted_profiles,
            (
                NmapProfileName.REVIEWED_SCRIPT_INVENTORY,
                NmapProfileName.SERVICE_VERSION_INVENTORY,
                NmapProfileName.TCP_CONNECT_INVENTORY,
            ),
        )
        authority = self.service.execution_authority(
            project_id="project-a",
            site_id="site-a",
        )
        self.assertEqual(
            authority.capability.permitted_profiles,
            (
                NmapProfileName.REVIEWED_SCRIPT_INVENTORY,
                NmapProfileName.SERVICE_VERSION_INVENTORY,
                NmapProfileName.TCP_CONNECT_INVENTORY,
            ),
        )

    def test_one_click_approval_persists_the_detected_installation_and_is_idempotent(self) -> None:
        first = self.service.approve_detected_installation(
            project_id="project-a",
            site_id="site-a",
            principal=self.principal,
        )
        second = self.service.approve_detected_installation(
            project_id="project-a",
            site_id="site-a",
            principal=self.principal,
        )

        self.assertTrue(first.process_selection_allowed)
        self.assertEqual(first.permitted_profiles, (NmapProfileName.TCP_CONNECT_INVENTORY,))
        self.assertEqual(second.policy_id, first.policy_id)
        self.assertEqual(second.policy_revision, first.policy_revision)
        factory = session_factory(self.engine)
        with factory() as session:
            self.assertEqual(session.scalar(select(func.count(NmapDeploymentPolicy.policy_id))), 1)
            self.assertEqual(session.scalar(select(func.count(NmapInstallationConfirmation.confirmation_id))), 1)

    def test_concurrent_one_click_approvals_converge_on_one_policy_identity(self) -> None:
        probe = _BlockingBootstrapProbe(self.fingerprint)
        first_service = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-concurrent",
            network_executor_id="executor-concurrent",
            probe=probe,
        )
        second_service = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-concurrent",
            network_executor_id="executor-concurrent",
            probe=probe,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                first_service.approve_detected_installation,
                project_id="project-a",
                site_id="site-a",
                principal=self.principal,
            )
            self.assertTrue(probe.bootstrap_started.wait(1.0))
            second_future = pool.submit(
                second_service.approve_detected_installation,
                project_id="project-a",
                site_id="site-a",
                principal=self.principal,
            )
            probe.release_bootstrap.set()
            first = first_future.result(timeout=3.0)
            second = second_future.result(timeout=3.0)

        self.assertEqual((first.policy_id, first.policy_revision), (second.policy_id, second.policy_revision))
        self.assertEqual(probe.bootstrap_calls, 1)
        self.assertEqual(len(first_service.list_policy_history()), 1)

    def test_one_click_approval_rejects_zero_or_multiple_trusted_installations_without_writes(self) -> None:
        factory = session_factory(self.engine)
        for observations in ((), (self.probe._available(self.fingerprint),) * 2):
            with self.subTest(observations=len(observations)):
                self.probe.bootstrap_observations = observations
                with self.assertRaises(NmapInstallationNotFoundError):
                    self.service.approve_detected_installation(
                        project_id="project-a",
                        site_id="site-a",
                        principal=self.principal,
                    )
                with factory() as session:
                    self.assertEqual(session.scalar(select(func.count(NmapDeploymentPolicy.policy_id))), 0)
                    self.assertEqual(
                        session.scalar(select(func.count(NmapInstallationConfirmation.confirmation_id))),
                        0,
                    )

    def test_one_click_approval_preserves_a_working_policy_when_reapproval_cannot_confirm(self) -> None:
        first = self.service.approve_detected_installation(
            project_id="project-a",
            site_id="site-a",
            principal=self.principal,
        )
        self.probe.inspect_observations = ()

        with self.assertRaises(NmapInstallationNotFoundError):
            self.service.approve_detected_installation(
                project_id="project-a",
                site_id="site-b",
                principal=self.principal,
            )

        still_approved = self.service.capability(project_id="project-a", site_id="site-a")
        self.assertTrue(still_approved.process_selection_allowed)
        self.assertEqual(still_approved.policy_id, first.policy_id)
        factory = session_factory(self.engine)
        with factory() as session:
            self.assertEqual(session.scalar(select(func.count(NmapDeploymentPolicy.policy_id))), 1)
            self.assertEqual(session.scalar(select(func.count(NmapInstallationConfirmation.confirmation_id))), 1)

    def test_one_click_approval_rejects_replacement_of_active_xml_import_policy(self) -> None:
        created = self.service.create_policy(
            _policy_request(mode=NmapProviderMode.OPERATOR_XML_IMPORT),
            principal=self.principal,
        )

        with self.assertRaisesRegex(
            NmapPolicyStateError,
            "cannot replace an active operator XML import policy",
        ):
            self.service.approve_detected_installation(
                project_id="project-a",
                site_id="site-b",
                principal=self.principal,
            )

        capability = self.service.capability(project_id="project-a", site_id="site-a")
        self.assertTrue(capability.xml_import_allowed)
        self.assertEqual(capability.policy_id, created.policy_id)

    def test_persist_reload_reconstructs_exact_typed_policy_and_fingerprint(self) -> None:
        created = self.service.create_policy(_policy_request(), principal=self.principal)
        expected_policy = NmapTrustPolicyV1(
            permitted_publishers=("Insecure.Com LLC",),
            permitted_versions=("7.98",),
            permitted_signer_sha256=(_A,),
            permitted_executable_sha256=(_B,),
            permitted_npsl_versions=("1.1",),
            max_data_files=8192,
            max_file_bytes=64 * 1024 * 1024,
            max_manifest_bytes=1024 * 1024 * 1024,
        )
        factory = session_factory(self.engine)
        with factory() as session:
            stored_policy = session.scalar(
                select(NmapDeploymentPolicy).where(NmapDeploymentPolicy.policy_id == created.policy_id)
            )
            assert stored_policy is not None
            self.assertEqual(
                self.service.trust_policy_from_record(stored_policy),
                expected_policy,
            )
            self.assertEqual(
                self.service.scopes_from_policy_record(stored_policy),
                (NmapProjectSiteScope(project_id="project-a", site_id="site-a"),),
            )

        confirmed = self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Bind the inspected installation to this exact policy revision.",
            ),
            principal=self.principal,
        )
        with factory() as session:
            stored_confirmation = session.scalar(
                select(NmapInstallationConfirmation).where(
                    NmapInstallationConfirmation.confirmation_id == confirmed.confirmation_id
                )
            )
            assert stored_confirmation is not None
            self.assertEqual(
                self.service.fingerprint_from_confirmation(stored_confirmation),
                self.fingerprint,
            )
            self.assertEqual(
                self.service.detection_candidate_from_confirmation(stored_confirmation),
                self.probe.candidate,
            )
            self.assertEqual(stored_confirmation.data_total_bytes, 123_456_789)
            self.assertEqual(stored_confirmation.network_executor_id, "executor-a")
            self.assertEqual(
                stored_confirmation.machine_executor_identity,
                "machine-guid:12345678-1234-1234-1234-123456789abc",
            )

        launch = self.service.execution_authority(project_id="project-a", site_id="site-a")
        self.assertEqual(launch.confirmation_id, confirmed.confirmation_id)
        self.assertEqual(launch.candidate, self.probe.candidate)
        self.assertEqual(launch.fingerprint, self.fingerprint)
        self.assertEqual(launch.trust_policy, expected_policy)
        self.assertEqual(launch.network_executor_id, "executor-a")
        self.assertEqual(self.probe.revalidate_calls, 2)
        self.assertEqual(
            self.service.revalidate_execution_authority(launch),
            self.probe.revalidation.trust,
        )

    def test_malformed_persisted_json_fails_closed_without_probing(self) -> None:
        created = self.service.create_policy(_policy_request(), principal=self.principal)
        factory = session_factory(self.engine)
        with factory.begin() as session:
            row = session.get(NmapDeploymentPolicy, created.policy_id)
            assert row is not None
            row.permitted_publishers_json = '["Insecure.Com LLC", ]'

        capability = self.service.capability(project_id="project-a", site_id="site-a")
        self.assertEqual(capability.state, "unavailable")
        self.assertEqual(capability.reason, "policy_invalid")
        self.assertEqual(self.probe.inspect_calls, 0)
        self.assertEqual(self.probe.revalidate_calls, 0)

    def test_wrong_site_and_wrong_executor_reject_before_probe(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        denied = self.service.capability(project_id="project-a", site_id="site-b")
        self.assertEqual(denied.reason, "site_not_permitted")

        other_executor = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-a",
            network_executor_id="executor-b",
            probe=self.probe,
        )
        absent = other_executor.capability(project_id="project-a", site_id="site-a")
        self.assertEqual(absent.reason, "policy_not_configured")
        self.assertEqual(self.probe.inspect_calls, 0)
        self.assertEqual(self.probe.revalidate_calls, 0)

    def test_external_and_default_lanes_reject_before_detection_or_parsing(self) -> None:
        default_service = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-unconfigured",
            network_executor_id="executor-a",
            probe=self.probe,
        )
        with self.assertRaises(NmapCapabilityDeniedError) as process_error:
            default_service.assert_process_selection_allowed(project_id="project-a", site_id="site-a")
        self.assertEqual(process_error.exception.capability.reason, "policy_not_configured")
        with self.assertRaises(NmapCapabilityDeniedError) as xml_error:
            default_service.assert_xml_import_allowed(project_id="project-a", site_id="site-a")
        self.assertEqual(xml_error.exception.capability.reason, "policy_not_configured")

        external = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-external",
            network_executor_id="executor-a",
            probe=self.probe,
        )
        external.create_policy(
            _policy_request(
                lane=NmapDeploymentLane.EXTERNAL_CUSTOMER,
                mode=NmapProviderMode.DISABLED,
            ),
            principal=self.principal,
        )
        self.assertEqual(
            external.capability(project_id="project-a", site_id="site-a").reason,
            "external_deployment",
        )
        self.assertEqual(self.probe.inspect_calls, 0)
        self.assertEqual(self.probe.revalidate_calls, 0)

    def test_confirmation_is_invalidated_when_revalidation_reports_drift(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm exact trusted files.",
            ),
            principal=self.principal,
        )
        self.probe.revalidation = NmapProbeObservationV1(
            candidate=self.probe.candidate,
            trust=NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.FINGERPRINT_DRIFT,
                fingerprint=None,
            ),
            machine_executor_identity=("machine-guid:12345678-1234-1234-1234-123456789abc"),
            npcap_state="not_checked",
            raw_capable=False,
        )
        capability = self.service.capability(project_id="project-a", site_id="site-a")
        self.assertEqual(capability.state, "unavailable")
        self.assertEqual(capability.reason, "fingerprint_drift")
        self.assertFalse(capability.process_selection_allowed)
        self.assertNotIn("path", capability.model_dump_json().lower())
        self.assertNotIn("acl", capability.model_dump_json().lower())

        changed_candidate = self.probe.candidate.model_copy(update={"registry_view": "32"})
        self.probe.revalidation = NmapProbeObservationV1(
            candidate=changed_candidate,
            trust=NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=self.fingerprint,
            ),
            machine_executor_identity=("machine-guid:12345678-1234-1234-1234-123456789abc"),
            npcap_version="1.82",
            npcap_state="raw_capable",
            raw_capable=True,
        )
        candidate_drift = self.service.capability(project_id="project-a", site_id="site-a")
        self.assertEqual(candidate_drift.reason, "fingerprint_drift")
        self.assertFalse(candidate_drift.process_selection_allowed)

    def test_capability_probe_failure_returns_sanitized_unavailable_response(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm exact trusted files.",
            ),
            principal=self.principal,
        )
        self.probe.revalidation_error = OSError(
            r"C:\Program Files\Nmap\nmap.exe could not be inspected"
        )

        capability = self.service.capability(
            project_id="project-a",
            site_id="site-a",
        )

        self.assertEqual(capability.state, "unavailable")
        self.assertEqual(capability.reason, "capability_probe_failed")
        self.assertFalse(capability.process_selection_allowed)
        self.assertNotIn("program files", capability.model_dump_json().casefold())

    def test_confirmation_is_invalidated_on_machine_executor_drift(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm the installation on this exact machine executor.",
            ),
            principal=self.principal,
        )
        self.probe.revalidation = NmapProbeObservationV1(
            candidate=self.probe.candidate,
            trust=NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=self.fingerprint,
            ),
            machine_executor_identity=("machine-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            npcap_version="1.82",
            npcap_state="raw_capable",
            raw_capable=True,
        )

        capability = self.service.capability(project_id="project-a", site_id="site-a")

        self.assertEqual(capability.state, "unavailable")
        self.assertEqual(capability.reason, "executor_mismatch")
        self.assertFalse(capability.process_selection_allowed)
        with self.assertRaises(NmapCapabilityDeniedError) as denied:
            self.service.execution_authority(project_id="project-a", site_id="site-a")
        self.assertEqual(denied.exception.capability.reason, "executor_mismatch")

    def test_execution_authority_rejects_policy_change_after_revalidation(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm the current policy revision.",
            ),
            principal=self.principal,
        )
        original_revalidate = self.probe.revalidate

        def revalidate_then_replace_policy(
            candidate: NmapDetectionCandidateV1,
            confirmed: NmapInstallationFingerprintV1,
            policy: NmapTrustPolicyV1,
        ) -> NmapProbeObservationV1:
            observation = original_revalidate(candidate, confirmed, policy)
            self.service.create_policy(_policy_request(), principal=self.principal)
            return observation

        self.probe.revalidate = revalidate_then_replace_policy  # type: ignore[method-assign]

        with self.assertRaises(NmapCapabilityDeniedError) as denied:
            self.service.execution_authority(project_id="project-a", site_id="site-a")

        self.assertEqual(denied.exception.capability.reason, "policy_changed")

    def test_execution_authority_fails_closed_when_second_probe_raises(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Confirm exact trusted files.",
            ),
            principal=self.principal,
        )
        original_revalidate = self.probe.revalidate
        calls = 0

        def fail_second_revalidation(
            candidate: NmapDetectionCandidateV1,
            confirmed: NmapInstallationFingerprintV1,
            policy: NmapTrustPolicyV1,
        ) -> NmapProbeObservationV1:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("protected installation disappeared")
            return original_revalidate(candidate, confirmed, policy)

        self.probe.revalidate = fail_second_revalidation  # type: ignore[method-assign]

        with self.assertRaises(NmapCapabilityDeniedError) as denied:
            self.service.execution_authority(project_id="project-a", site_id="site-a")

        self.assertEqual(denied.exception.capability.reason, "inspection_failed")
        self.assertEqual(calls, 2)

    def test_revalidate_execution_authority_rejects_confirmation_and_live_drift(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Create the protected execution authority.",
            ),
            principal=self.principal,
        )
        authority = self.service.execution_authority(project_id="project-a", site_id="site-a")
        calls_before_drift = self.probe.revalidate_calls
        self.probe.revalidation = NmapProbeObservationV1(
            candidate=self.probe.candidate,
            trust=NmapTrustResultV1(
                available=True,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=self.fingerprint,
            ),
            machine_executor_identity=("machine-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            npcap_version="1.82",
            npcap_state="raw_capable",
            raw_capable=True,
        )

        live_drift = self.service.revalidate_execution_authority(authority)

        self.assertFalse(live_drift.available)
        self.assertEqual(live_drift.reason, NmapTrustReason.FINGERPRINT_DRIFT)
        self.assertEqual(self.probe.revalidate_calls, calls_before_drift + 1)

        self.probe.revalidation = self.probe._available(self.fingerprint)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Supersede the prior confirmation with a new audit event.",
            ),
            principal=self.principal,
        )
        calls_before_confirmation_change = self.probe.revalidate_calls

        stale_confirmation = self.service.revalidate_execution_authority(authority)

        self.assertFalse(stale_confirmation.available)
        self.assertEqual(stale_confirmation.reason, NmapTrustReason.FINGERPRINT_DRIFT)
        self.assertEqual(self.probe.revalidate_calls, calls_before_confirmation_change)

    def test_revalidate_execution_authority_rejects_executor_and_policy_change_before_probe(self) -> None:
        self.service.create_policy(_policy_request(), principal=self.principal)
        self.service.confirm_installation(
            NmapInstallationConfirmationRequest(
                fingerprint_sha256=self.fingerprint.fingerprint_sha256,
                reason="Create the protected execution authority.",
            ),
            principal=self.principal,
        )
        authority = self.service.execution_authority(project_id="project-a", site_id="site-a")
        calls_before_checks = self.probe.revalidate_calls
        wrong_executor = NmapCapabilityService(
            self.engine,
            deployment_id="deployment-a",
            network_executor_id="executor-b",
            probe=self.probe,
        )

        executor_mismatch = wrong_executor.revalidate_execution_authority(authority)

        self.assertFalse(executor_mismatch.available)
        self.assertEqual(executor_mismatch.reason, NmapTrustReason.FINGERPRINT_DRIFT)
        self.assertEqual(self.probe.revalidate_calls, calls_before_checks)

        self.service.create_policy(_policy_request(), principal=self.principal)
        policy_mismatch = self.service.revalidate_execution_authority(authority)

        self.assertFalse(policy_mismatch.available)
        self.assertEqual(policy_mismatch.reason, NmapTrustReason.FINGERPRINT_DRIFT)
        self.assertEqual(self.probe.revalidate_calls, calls_before_checks)

    def test_schema_forbids_client_paths_and_noncanonical_policy_lists(self) -> None:
        with self.assertRaises(ValidationError):
            NmapInstallationConfirmationRequest.model_validate(
                {
                    "fingerprint_sha256": _E,
                    "reason": "Attempt to claim a local path.",
                    "executable_path": r"C:\\Temp\\nmap.exe",
                }
            )
        with self.assertRaises(ValidationError):
            NmapDeploymentPolicyCreateRequest.model_validate(
                {
                    **_policy_request().model_dump(mode="json"),
                    "permitted_versions": ["7.99", "7.98"],
                }
            )


class WindowsNmapInstallationProbeTests(unittest.TestCase):
    def test_missing_runtime_identity_returns_fail_closed_observations(self) -> None:
        fingerprint = _fingerprint()
        available = NmapTrustResultV1(
            available=True,
            reason=NmapTrustReason.AVAILABLE,
            fingerprint=fingerprint,
        )
        probe = WindowsNmapInstallationProbe()
        with (
            patch.object(
                WindowsNmapInstallationProbe,
                "_runtime_capability",
                return_value=(None, None, NmapNpcapState.NOT_CHECKED, False),
            ),
            patch(
                "app.services.nmap_capability_service.CtypesNmapTrustBackend",
            ),
            patch(
                "app.services.nmap_capability_service.nmap_uninstall_registry",
                return_value=object(),
            ),
            patch(
                "app.services.nmap_capability_service.detect_nmap_candidates",
                return_value=(_candidate(),),
            ),
            patch(
                "app.services.nmap_capability_service.inspect_nmap_installation_for_administrator_approval",
                return_value=available,
            ),
            patch(
                "app.services.nmap_capability_service.inspect_nmap_installation",
                return_value=available,
            ),
            patch(
                "app.services.nmap_capability_service.revalidate_nmap_installation",
                return_value=available,
            ),
        ):
            bootstrap = probe.bootstrap_inspect()
            inspected = probe.inspect(
                NmapTrustPolicyV1(
                    permitted_publishers=(fingerprint.publisher,),
                    permitted_versions=(fingerprint.version,),
                    permitted_signer_sha256=(fingerprint.signer_sha256,),
                    permitted_executable_sha256=(fingerprint.executable_sha256,),
                    permitted_npsl_versions=(fingerprint.npsl_version,),
                    max_data_files=8192,
                    max_file_bytes=64 * 1024 * 1024,
                    max_manifest_bytes=1024 * 1024 * 1024,
                )
            )
            revalidated = probe.revalidate(
                _candidate(),
                fingerprint,
                NmapTrustPolicyV1(
                    permitted_publishers=(fingerprint.publisher,),
                    permitted_versions=(fingerprint.version,),
                    permitted_signer_sha256=(fingerprint.signer_sha256,),
                    permitted_executable_sha256=(fingerprint.executable_sha256,),
                    permitted_npsl_versions=(fingerprint.npsl_version,),
                    max_data_files=8192,
                    max_file_bytes=64 * 1024 * 1024,
                    max_manifest_bytes=1024 * 1024 * 1024,
                ),
            )

        for observation in (*bootstrap, *inspected, revalidated):
            self.assertFalse(observation.trust.available)
            self.assertEqual(observation.trust.reason, NmapTrustReason.INSPECTION_FAILED)
            self.assertIsNone(observation.trust.fingerprint)


if __name__ == "__main__":
    unittest.main()
