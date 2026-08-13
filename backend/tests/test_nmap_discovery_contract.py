from __future__ import annotations

import copy
import json
import unittest

from app.services.discovery_contract_service import (
    refresh_nmap_scan_plan_packet_digest,
    resolve_ip_discovery_parameters,
)
from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    NmapScanPlanV1,
)
from smart_commissioning_core.run_context import canonical_sha256


class _NoImports:
    def list(self, **_filters: object) -> list[dict[str, object]]:
        return []

    def get(self, _import_id: str) -> dict[str, object]:
        raise FileNotFoundError


_SOURCE = {
    "schema_version": "1.0",
    "selection": "explicit",
    "executor_scope": "inline:test-deployment",
    "interface_id": "windows-guid:00000000-0000-0000-0000-000000000001",
    "interface_name": "Field NIC",
    "source_ip": "192.0.2.5",
    "prefix_length": 24,
    "local_address": "192.0.2.5/24",
    "default_route_metric": None,
}

_AUTHORITY = {
    "deployment_id": "test-deployment",
    "network_executor_id": "inline:test-deployment",
    "confirmation_id": "confirmation-1",
    "capability": {
        "provider": "nmap",
        "state": "available",
        "reason": "available",
        "provider_mode": "internal_operator_managed",
        "policy_id": "policy-1",
        "policy_revision": 3,
        "publisher": "Insecure.Com LLC",
        "version": "7.98",
        "fingerprint_sha256": "b" * 64,
        "npcap_version": "1.83",
        "npcap_state": "raw_capable",
        "raw_capable": True,
        "process_selection_allowed": True,
        "xml_import_allowed": False,
        "permitted_profiles": [
            "selected_udp",
            "tcp_connect_inventory",
            "host_discovery",
        ],
    },
    "machine_executor_identity": "machine-guid:00000000-0000-0000-0000-000000000002",
    "reviewed_scripts": [],
}


def _parameters(**values: object) -> dict[str, object]:
    parameters: dict[str, object] = {
        "dry_run": True,
        "addresses": ["192.0.2.10"],
        "ports": [443],
        "provider": "operator_managed_nmap",
        "nmap_profile": "tcp_connect_inventory",
        "source_ip": "192.0.2.5",
        "local_address": "192.0.2.5/24",
        "source_interface_identity_v1": copy.deepcopy(_SOURCE),
    }
    parameters.update(values)
    return parameters


class NmapDiscoveryContractTests(unittest.TestCase):
    def test_process_selection_requires_sanitized_execution_authority(self) -> None:
        with self.assertRaisesRegex(ValueError, "Nmap execution authority"):
            resolve_ip_discovery_parameters(
                _parameters(),
                project_id="project-a",
                site_id="site-a",
                import_repository=_NoImports(),
            )

    def test_available_authority_freezes_exact_path_free_plan(self) -> None:
        resolved = resolve_ip_discovery_parameters(
            _parameters(),
            project_id="project-a",
            site_id="site-a",
            import_repository=_NoImports(),
            nmap_execution_authority=_AUTHORITY,
        )

        plan = NmapScanPlanV1.model_validate(resolved["nmap_scan_plan_v1"])
        contract = resolved["scan_contract_v1"]
        self.assertEqual(plan.profile, NmapProfileName.TCP_CONNECT_INVENTORY)
        self.assertEqual(plan.packet_plan_sha256, contract["packet_plan_sha256"])
        self.assertEqual(plan.targets, ("192.0.2.10",))
        self.assertEqual(plan.tcp_ports, (443,))
        provider_state = contract["ip"]["provider_state"]
        self.assertTrue(provider_state["execution_enabled"])
        self.assertEqual(
            provider_state["expected_executor_identity"],
            _AUTHORITY["machine_executor_identity"],
        )
        self.assertEqual(provider_state["network_executor_id"], "inline:test-deployment")
        self.assertEqual(provider_state["confirmation_id"], "confirmation-1")
        serialized = json.dumps(resolved, sort_keys=True).casefold()
        self.assertNotIn("program files", serialized)
        self.assertNotIn("executable_path", serialized)
        self.assertNotIn("data_directory", serialized)

    def test_plan_digest_refreshes_after_control_identity_is_frozen(self) -> None:
        resolved = resolve_ip_discovery_parameters(
            _parameters(),
            project_id="project-a",
            site_id="site-a",
            import_repository=_NoImports(),
            nmap_execution_authority=_AUTHORITY,
        )
        contract = resolved["scan_contract_v1"]
        contract["initiating_user_id"] = "user-1"
        contract["packet_plan_sha256"] = canonical_sha256(
            {key: value for key, value in contract.items() if key != "packet_plan_sha256"}
        )

        refresh_nmap_scan_plan_packet_digest(resolved)

        plan = NmapScanPlanV1.model_validate(resolved["nmap_scan_plan_v1"])
        self.assertEqual(plan.packet_plan_sha256, contract["packet_plan_sha256"])

    def test_selected_udp_is_typed_and_profile_permitted(self) -> None:
        resolved = resolve_ip_discovery_parameters(
            _parameters(
                nmap_profile="selected_udp",
                ports=[{"port": 161, "protocol": "udp"}],
            ),
            project_id="project-a",
            site_id="site-a",
            import_repository=_NoImports(),
            nmap_execution_authority=_AUTHORITY,
        )

        plan = NmapScanPlanV1.model_validate(resolved["nmap_scan_plan_v1"])
        self.assertEqual(plan.tcp_ports, ())
        self.assertEqual(plan.udp_ports, (161,))

    def test_raw_profile_is_rejected_when_runtime_is_connect_only(self) -> None:
        authority = copy.deepcopy(_AUTHORITY)
        capability = authority["capability"]
        assert isinstance(capability, dict)
        capability["npcap_state"] = "connect_only"
        capability["raw_capable"] = False

        with self.assertRaisesRegex(ValueError, "requires raw packet capability"):
            resolve_ip_discovery_parameters(
                _parameters(
                    nmap_profile="selected_udp",
                    ports=[{"port": 161, "protocol": "udp"}],
                ),
                project_id="project-a",
                site_id="site-a",
                import_repository=_NoImports(),
                nmap_execution_authority=authority,
            )

    def test_connect_only_runtime_keeps_tcp_connect_profile_available(self) -> None:
        authority = copy.deepcopy(_AUTHORITY)
        capability = authority["capability"]
        assert isinstance(capability, dict)
        capability["npcap_state"] = "connect_only"
        capability["raw_capable"] = False
        capability["permitted_profiles"] = ["tcp_connect_inventory"]

        resolved = resolve_ip_discovery_parameters(
            _parameters(),
            project_id="project-a",
            site_id="site-a",
            import_repository=_NoImports(),
            nmap_execution_authority=authority,
        )

        plan = NmapScanPlanV1.model_validate(resolved["nmap_scan_plan_v1"])
        self.assertEqual(plan.profile, NmapProfileName.TCP_CONNECT_INVENTORY)
        self.assertFalse(plan.raw_capability_required)

    def test_unpermitted_named_profile_fails_before_run_creation(self) -> None:
        with self.assertRaisesRegex(ValueError, "not permitted"):
            resolve_ip_discovery_parameters(
                _parameters(nmap_profile="os_inventory"),
                project_id="project-a",
                site_id="site-a",
                import_repository=_NoImports(),
                nmap_execution_authority=_AUTHORITY,
            )

    def test_host_discovery_has_no_port_observation_rows(self) -> None:
        resolved = resolve_ip_discovery_parameters(
            _parameters(nmap_profile="host_discovery", ports=[]),
            project_id="project-a",
            site_id="site-a",
            import_repository=_NoImports(),
            nmap_execution_authority=_AUTHORITY,
        )

        plan = NmapScanPlanV1.model_validate(resolved["nmap_scan_plan_v1"])
        self.assertEqual(plan.tcp_ports, ())
        self.assertEqual(plan.udp_ports, ())
        self.assertEqual(
            resolved["scan_contract_v1"]["ip"]["observation_budget"]["planned_observation_rows"],
            5,
        )


if __name__ == "__main__":
    unittest.main()
