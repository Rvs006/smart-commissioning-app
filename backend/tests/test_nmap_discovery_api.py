from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.nmap import (
    NmapCapabilityResponse,
    NmapCapabilityState,
    NmapNpcapState,
    NmapProviderMode,
)
from harness import ApiTestCase
from smart_commissioning_core.engines.ip import NmapProviderDependencies
from smart_commissioning_core.engines.ip.nmap_profiles import NmapProfileName

_API_KEY = "nmap-discovery-api-key"
_MACHINE = "machine-guid:12345678-1234-1234-1234-123456789abc"


class _CapabilityService:
    def __init__(self) -> None:
        self.execution_authority_calls = 0
        capability = NmapCapabilityResponse(
            state=NmapCapabilityState.AVAILABLE,
            reason="available",
            provider_mode=NmapProviderMode.INTERNAL_OPERATOR_MANAGED,
            policy_id="policy-1",
            policy_revision=3,
            publisher="Insecure.Com LLC",
            version="7.98",
            fingerprint_sha256="b" * 64,
            npcap_version="1.83",
            npcap_state=NmapNpcapState.RAW_CAPABLE,
            raw_capable=True,
            process_selection_allowed=True,
            permitted_profiles=("tcp_connect_inventory",),
        )
        self.authority = SimpleNamespace(
            deployment_id="smart-commissioning-local",
            network_executor_id="inline:smart-commissioning-local",
            machine_executor_identity=_MACHINE,
            policy_id="policy-1",
            policy_revision=3,
            confirmation_id="confirmation-1",
            capability=capability,
            fingerprint=SimpleNamespace(
                reviewed_scripts=(),
                fingerprint_sha256="b" * 64,
                publisher="Insecure.Com LLC",
                version="7.98",
            ),
        )

    def execution_authority(self, *, project_id: str, site_id: str) -> object:
        self.execution_authority_calls += 1
        assert (project_id, site_id) == ("demo-project", "demo-site")
        return self.authority

    def revalidate_execution_authority(self, authority: object) -> object:
        assert authority is self.authority
        return object()


def _freeze_source(_project_id: str, _site_id: str, parameters: dict) -> None:
    identity = {
        "schema_version": "1.0",
        "selection": "explicit",
        "executor_scope": "inline:smart-commissioning-local",
        "interface_id": "windows-guid:00000000-0000-0000-0000-000000000001",
        "interface_name": "Test source interface",
        "source_ip": "192.0.2.5",
        "prefix_length": 24,
        "local_address": "192.0.2.5/24",
        "default_route_metric": None,
    }
    parameters.update(
        source_ip="192.0.2.5",
        local_address="192.0.2.5/24",
        source_interface_identity_v1=identity,
    )


class NmapDiscoveryApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
        "JOB_EXECUTION_MODE": "inline",
        "SMART_COMMISSIONING_DEPLOYMENT_ID": "smart-commissioning-local",
    }
    client_headers = {"X-API-Key": _API_KEY}

    def setUp(self) -> None:
        super().setUp()
        from app.api.routes import discovery as discovery_routes

        self.discovery_routes = discovery_routes
        self.capability = _CapabilityService()
        self.app.dependency_overrides[
            self.discovery_routes._nmap_discovery_capability_service
        ] = lambda: self.capability

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        super().tearDown()

    def _preview(
        self,
        *,
        profile: str = "tcp_connect_inventory",
        ports: list[object] | None = None,
    ):
        with patch.object(
            self.discovery_routes, "_resolve_source_interface", side_effect=_freeze_source
        ):
            return self.client.post(
                "/api/v1/discovery/ip/runs",
                json={
                    "project_id": "demo-project",
                    "site_id": "demo-site",
                    "job_type": "ip_discovery",
                    "parameters": {
                        "dry_run": True,
                        "provider": "operator_managed_nmap",
                        "nmap_profile": profile,
                        "addresses": ["192.0.2.10"],
                        "ports": [443] if ports is None else ports,
                    },
                },
                headers=self.client_headers,
            )

    def _run_ids(self) -> set[str]:
        response = self.client.get(
            "/api/v1/discovery/runs",
            headers=self.client_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {item["run_id"] for item in response.json()["runs"]}

    def test_dry_preview_freezes_the_named_plan_without_runtime_dependencies(self) -> None:
        preview = self._preview()

        self.assertEqual(preview.status_code, 200, preview.text)
        run_id = preview.json()["run_id"]
        result = self.client.get(
            f"/api/v1/discovery/runs/{run_id}/results", headers=self.client_headers
        )
        self.assertEqual(result.status_code, 200, result.text)
        plan = result.json()["result_summary"]["dry_run_plan"]
        self.assertEqual(plan["provider"], "operator_managed_nmap")
        self.assertEqual(plan["profile"], "tcp_connect_inventory")

    def test_raw_profile_preview_rejects_before_run_creation(self) -> None:
        self.capability.authority.capability = self.capability.authority.capability.model_copy(
            update={
                "npcap_state": NmapNpcapState.CONNECT_ONLY,
                "raw_capable": False,
                "permitted_profiles": (
                    NmapProfileName.SELECTED_UDP,
                    NmapProfileName.TCP_CONNECT_INVENTORY,
                ),
            }
        )
        before = self._run_ids()

        response = self._preview(
            profile="selected_udp",
            ports=[{"port": 161, "protocol": "udp"}],
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("requires raw packet capability", response.json()["detail"])
        self.assertEqual(self._run_ids(), before)

    def test_external_deployment_rejects_nmap_before_authority_or_run_creation(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        original = settings.nmap_internal_provider_enabled
        settings.nmap_internal_provider_enabled = False
        try:
            response = self._preview()
        finally:
            settings.nmap_internal_provider_enabled = original

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "Operator-managed Nmap is disabled in this deployment.")
        self.assertEqual(self.capability.execution_authority_calls, 0)

    def test_runtime_dependency_factory_rechecks_the_deployment_gate(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        original = settings.nmap_internal_provider_enabled
        settings.nmap_internal_provider_enabled = False
        try:
            with self.assertRaisesRegex(ValueError, "disabled in this deployment"):
                self.discovery_routes._nmap_live_dependencies(
                    run_id="run_external_gate",
                    project_id="demo-project",
                    site_id="demo-site",
                    parameters={"scan_contract_v1": {"ip": {"provider_state": {}}}},
                    capability_service=self.capability,
                )
        finally:
            settings.nmap_internal_provider_enabled = original

        self.assertEqual(self.capability.execution_authority_calls, 0)

    def test_linked_live_run_receives_only_preview_bound_runtime_dependencies(self) -> None:
        preview = self._preview()
        self.assertEqual(preview.status_code, 200, preview.text)
        now = datetime.now(UTC)
        authorization = self.client.post(
            "/api/v1/discovery/scan-authorizations",
            json={
                "preview_run_id": preview.json()["run_id"],
                "ticket": "CHG-NMAP-API",
                "purpose": "Nmap runtime dependency proof",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
            headers=self.client_headers,
        )
        self.assertEqual(authorization.status_code, 201, authorization.text)
        captured: dict[str, object] = {}

        def processor(run_id: str, _parameters: dict, *, run_store, **kwargs: object):
            captured.update(kwargs)
            return run_store.update_run_status(
                run_id,
                status="failed",
                stage="engine_failed",
                error_message="Stubbed after dependency binding.",
            )

        artifacts = SimpleNamespace(
            for_nmap_run=lambda **_kwargs: object(),
            read_for_normalization=lambda **_kwargs: (object(), b""),
        )
        with (
            patch.object(
                self.discovery_routes, "process_ip_discovery_run", side_effect=processor
            ),
            patch.object(self.discovery_routes, "_runtime_source_interface_guard"),
            patch.object(
                self.discovery_routes, "RawEvidenceArtifactStore", return_value=artifacts
            ),
            patch.object(
                self.discovery_routes, "CtypesNmapWindowsProcessApi", return_value=object()
            ),
            patch.object(
                self.discovery_routes,
                "CtypesNmapRuntimeCapabilityProbe",
                return_value=object(),
            ),
        ):
            live = self.client.post(
                "/api/v1/discovery/ip/runs",
                json={
                    "project_id": "demo-project",
                    "site_id": "demo-site",
                    "job_type": "ip_discovery",
                    "preview_run_id": preview.json()["run_id"],
                    "scan_authorization_id": authorization.json()["authorization_id"],
                    "parameters": {},
                },
                headers=self.client_headers,
            )

        self.assertEqual(live.status_code, 200, live.text)
        self.assertIsInstance(captured["nmap_dependencies"], NmapProviderDependencies)
        record = self.client.get(
            f"/api/v1/discovery/runs/{live.json()['run_id']}", headers=self.client_headers
        )
        self.assertEqual(record.json()["status"], "failed")

    def test_raw_capability_loss_rejects_live_start_before_run_or_child_persistence(self) -> None:
        self.capability.authority.capability = self.capability.authority.capability.model_copy(
            update={
                "permitted_profiles": (
                    NmapProfileName.SELECTED_UDP,
                    NmapProfileName.TCP_CONNECT_INVENTORY,
                ),
            }
        )
        preview = self._preview(
            profile="selected_udp",
            ports=[{"port": 161, "protocol": "udp"}],
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        now = datetime.now(UTC)
        authorization = self.client.post(
            "/api/v1/discovery/scan-authorizations",
            json={
                "preview_run_id": preview.json()["run_id"],
                "ticket": "CHG-NMAP-RAW-LOSS",
                "purpose": "Reject a raw profile after runtime capability loss",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
            headers=self.client_headers,
        )
        self.assertEqual(authorization.status_code, 201, authorization.text)
        self.capability.authority.capability = self.capability.authority.capability.model_copy(
            update={
                "npcap_state": NmapNpcapState.CONNECT_ONLY,
                "raw_capable": False,
                "permitted_profiles": (NmapProfileName.TCP_CONNECT_INVENTORY,),
            }
        )
        before = self._run_ids()

        with (
            patch.object(self.discovery_routes, "process_ip_discovery_run") as process,
            patch.object(self.discovery_routes, "_runtime_source_interface_guard"),
            patch.object(self.discovery_routes, "RawEvidenceArtifactStore") as artifacts,
            patch.object(self.discovery_routes, "CtypesNmapWindowsProcessApi") as windows,
        ):
            live = self.client.post(
                "/api/v1/discovery/ip/runs",
                json={
                    "project_id": "demo-project",
                    "site_id": "demo-site",
                    "job_type": "ip_discovery",
                    "preview_run_id": preview.json()["run_id"],
                    "scan_authorization_id": authorization.json()["authorization_id"],
                    "parameters": {},
                },
                headers=self.client_headers,
            )

        self.assertEqual(live.status_code, 409, live.text)
        self.assertIn("not permitted by deployment policy", live.json()["detail"])
        self.assertEqual(self._run_ids(), before)
        process.assert_not_called()
        artifacts.assert_not_called()
        windows.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
