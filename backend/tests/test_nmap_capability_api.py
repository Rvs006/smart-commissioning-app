from __future__ import annotations

import uuid

from app.core.db import get_engine
from app.services.nmap_capability_service import NmapProbeObservationV1
from harness import ApiTestCase
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import Project, Site
from smart_commissioning_core.engines.ip.nmap_detection import NmapDetectionCandidateV1
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapInstallationFingerprintV1,
    NmapTrustPolicyV1,
    NmapTrustReason,
    NmapTrustResultV1,
)
from smart_commissioning_core.run_context import canonical_sha256

_ROOT_KEY = "nmap-capability-root-key"
_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64


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
        "data_total_bytes": 400_000_000,
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


class _Probe:
    def __init__(self) -> None:
        self.fingerprint = _fingerprint()
        self.candidate = _candidate()
        self.inspect_calls = 0
        self.revalidate_calls = 0

    def _observation(self) -> NmapProbeObservationV1:
        return NmapProbeObservationV1(
            candidate=self.candidate,
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

    def inspect(self, policy: NmapTrustPolicyV1) -> tuple[NmapProbeObservationV1, ...]:
        self.inspect_calls += 1
        return (self._observation(),)

    def revalidate(
        self,
        candidate: NmapDetectionCandidateV1,
        confirmed: NmapInstallationFingerprintV1,
        policy: NmapTrustPolicyV1,
    ) -> NmapProbeObservationV1:
        self.revalidate_calls += 1
        assert confirmed == self.fingerprint
        assert candidate == self.candidate
        return self._observation()


def _policy_payload() -> dict[str, object]:
    return {
        "deployment_lane": "internal_same_organization",
        "provider_mode": "internal_operator_managed",
        "deployment_owner": "Facilities IT",
        "operator_install_responsibility": "Facilities IT installs and services Nmap.",
        "permitted_project_sites": [{"project_id": "nmap-project", "site_id": "nmap-site"}],
        "update_owner": "Facilities IT",
        "reviewed_version_policy": "Nmap 7.98 and NPSL 1.1 are reviewed.",
        "permitted_publishers": ["Insecure.Com LLC"],
        "permitted_versions": ["7.98"],
        "permitted_signer_sha256": [_A],
        "permitted_executable_sha256": [_B],
        "permitted_data_manifest_sha256": [_C],
        "permitted_licence_sha256": [_D],
        "permitted_npsl_versions": ["1.1"],
        "max_data_files": 8192,
        "max_file_bytes": 64 * 1024 * 1024,
        "max_manifest_bytes": 512 * 1024 * 1024,
        "profile_policy": {
            "schema_version": "1.0",
            "permitted_profiles": ["tcp_connect_inventory"],
        },
        "acknowledged_no_redistribution": True,
        "reason": "Approve the exact operator-managed installation lane.",
    }


class NmapCapabilityApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _ROOT_KEY,
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
        "SMART_COMMISSIONING_DEPLOYMENT_ID": "nmap-api-deployment",
        "SMART_COMMISSIONING_NETWORK_EXECUTOR_ID": "nmap-api-executor",
    }
    client_headers = {"X-API-Key": _ROOT_KEY}

    def setUp(self) -> None:
        from app.api.routes.nmap import get_nmap_installation_probe

        self.probe = _Probe()
        self.app.dependency_overrides[get_nmap_installation_probe] = lambda: self.probe
        factory = session_factory(get_engine())
        with factory.begin() as session:
            if session.get(Project, "nmap-project") is None:
                session.add(Project(id="nmap-project", name="Nmap project"))
            if session.get(Site, "nmap-site") is None:
                session.add(
                    Site(
                        id="nmap-site",
                        project_id="nmap-project",
                        name="Nmap site",
                    )
                )

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _create_policy(self):
        response = self.client.post("/api/v1/nmap/policies", json=_policy_payload())
        self.assertEqual(response.status_code, 201, response.text)
        return response

    def test_global_admin_detects_confirms_and_reads_only_sanitized_capability(self) -> None:
        policy = self._create_policy().json()
        self.assertEqual(policy["network_executor_id"], "nmap-api-executor")
        self.assertEqual(policy["created_by"], "shared_key")

        detected = self.client.get("/api/v1/nmap/installations/detected")
        self.assertEqual(detected.status_code, 200, detected.text)
        self.assertEqual(detected.json()[0]["fingerprint_sha256"], self.probe.fingerprint.fingerprint_sha256)
        self.assertNotIn("path", detected.text.lower())
        self.assertNotIn("acl", detected.text.lower())

        confirmed = self.client.post(
            "/api/v1/nmap/installations/confirm",
            json={
                "fingerprint_sha256": self.probe.fingerprint.fingerprint_sha256,
                "reason": "Bind the exact inspected installation.",
            },
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        self.assertEqual(confirmed.json()["confirmed_by"], "shared_key")

        capability = self.client.get(
            "/api/v1/nmap/capability",
            params={"project_id": "nmap-project", "site_id": "nmap-site"},
        )
        self.assertEqual(capability.status_code, 200, capability.text)
        body = capability.json()
        self.assertEqual(body["provider"], "nmap")
        self.assertEqual(body["state"], "available")
        self.assertEqual(body["publisher"], "Insecure.Com LLC")
        self.assertEqual(body["version"], "7.98")
        self.assertEqual(body["fingerprint_sha256"], self.probe.fingerprint.fingerprint_sha256)
        self.assertNotIn("path", capability.text.lower())
        self.assertNotIn("acl", capability.text.lower())

    def test_disabled_deployment_reports_no_capability_without_local_detection(self) -> None:
        from app.core.config import get_settings

        self._create_policy()
        settings = get_settings()
        original = settings.nmap_internal_provider_enabled
        settings.nmap_internal_provider_enabled = False
        self.probe.inspect_calls = 0
        self.probe.revalidate_calls = 0
        try:
            capability = self.client.get(
                "/api/v1/nmap/capability",
                params={"project_id": "nmap-project", "site_id": "nmap-site"},
            )
            detected = self.client.get("/api/v1/nmap/installations/detected")
            history = self.client.get("/api/v1/nmap/policies")
            mutation = self.client.post("/api/v1/nmap/policies", json=_policy_payload())
        finally:
            settings.nmap_internal_provider_enabled = original

        self.assertEqual(capability.status_code, 200, capability.text)
        self.assertEqual(capability.json()["state"], "disabled")
        self.assertEqual(capability.json()["reason"], "deployment_feature_disabled")
        self.assertEqual(detected.status_code, 409, detected.text)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertGreaterEqual(len(history.json()), 1)
        self.assertEqual(mutation.status_code, 409, mutation.text)
        self.assertEqual(self.probe.inspect_calls, 0)
        self.assertEqual(self.probe.revalidate_calls, 0)

    def test_engineer_cannot_edit_authority_and_scope_is_concealment_safe(self) -> None:
        self._create_policy()
        suffix = uuid.uuid4().hex[:10]
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"nmap-engineer-{suffix}", "role": "engineer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        user = created.json()
        grant = self.client.post(
            f"/api/v1/users/{user['user']['id']}/scope-grants",
            json={
                "project_id": "nmap-project",
                "site_id": "nmap-site",
                "reason": "Nmap capability read scope.",
            },
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        headers = {"X-API-Key": user["api_key"]}

        denied = self.client.post(
            "/api/v1/nmap/policies",
            headers=headers,
            json=_policy_payload(),
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        allowed = self.client.get(
            "/api/v1/nmap/capability",
            headers=headers,
            params={"project_id": "nmap-project", "site_id": "nmap-site"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        concealed = self.client.get(
            "/api/v1/nmap/capability",
            headers=headers,
            params={"project_id": "nmap-project", "site_id": "another-site"},
        )
        self.assertEqual(concealed.status_code, 404, concealed.text)

    def test_confirmation_request_cannot_claim_protected_paths(self) -> None:
        self._create_policy()
        response = self.client.post(
            "/api/v1/nmap/installations/confirm",
            json={
                "fingerprint_sha256": self.probe.fingerprint.fingerprint_sha256,
                "reason": "Client must not choose paths.",
                "executable_path": r"C:\\Temp\\nmap.exe",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
