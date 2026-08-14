"""Pure U1 discovery-contract assembly and immutable import selection tests."""

from __future__ import annotations

import copy
import ipaddress
import unittest

from app.services.discovery_contract_service import (
    SCAN_CONTRACT_MAX_BYTES,
    resolve_bacnet_discovery_parameters,
    resolve_ip_discovery_parameters,
)
from smart_commissioning_core.run_context import canonical_json_bytes

_TEST_SOURCE_INTERFACE_IDENTITY = {
    "schema_version": "1.0",
    "selection": "explicit",
    "executor_scope": "test-network-executor",
    "interface_id": "test-if:1",
    "interface_name": "Test field NIC",
    "source_ip": "192.0.2.10",
    "prefix_length": 24,
    "local_address": "192.0.2.10/24",
    "default_route_metric": None,
}


def _source_parameters(**values: object) -> dict[str, object]:
    parameters: dict[str, object] = {
        "source_ip": "192.0.2.10",
        "local_address": "192.0.2.10/24",
    }
    parameters.update(values)
    if "source_interface_identity_v1" not in values:
        if "local_address" in values and "source_ip" not in values:
            local = ipaddress.ip_interface(str(parameters["local_address"]))
            parameters["source_ip"] = str(local.ip)
        elif "source_ip" in values and "local_address" not in values:
            parameters["local_address"] = f'{parameters["source_ip"]}/32'
        local = ipaddress.ip_interface(str(parameters["local_address"]))
        parameters["source_interface_identity_v1"] = {
            **_TEST_SOURCE_INTERFACE_IDENTITY,
            "source_ip": str(parameters["source_ip"]),
            "prefix_length": local.network.prefixlen,
            "local_address": local.with_prefixlen,
        }
    return parameters


_IP_PARAMETER_RESOLVER = resolve_ip_discovery_parameters
_BACNET_PARAMETER_RESOLVER = resolve_bacnet_discovery_parameters


def resolve_ip_discovery_parameters(
    parameters: dict[str, object],
    **options: object,
) -> dict[str, object]:
    return _IP_PARAMETER_RESOLVER(
        _source_parameters(**parameters),
        **options,
    )


def resolve_bacnet_discovery_parameters(
    parameters: dict[str, object],
    **options: object,
) -> dict[str, object]:
    return _BACNET_PARAMETER_RESOLVER(
        _source_parameters(**parameters),
        **options,
    )


def _record(
    import_id: str,
    rows: list[dict[str, object]],
    *,
    rejected_rows: int = 0,
) -> dict[str, object]:
    return {
        "import_id": import_id,
        "import_type": "ip_register",
        "project_id": "project-a",
        "site_id": "site-a",
        "original_filename": f"{import_id}.csv",
        "stored_file_path": f"C:/private/{import_id}.csv",
        "summary": {
            "import_id": import_id,
            "import_type": "ip_register",
            "accepted_rows": len(rows),
            "rejected_rows": rejected_rows,
            "file_sha256": ("a" if import_id == "imp_new" else "b") * 64,
            "authority_schema_version": "1.0",
        },
        "accepted_rows": rows,
        "errors": [],
        "created_at": "2026-08-10T12:00:00+00:00",
    }


def _bacnet_record(
    import_id: str,
    import_type: str,
    rows: list[dict[str, object]],
    *,
    site_id: str = "site-a",
) -> dict[str, object]:
    record = _record(import_id, rows)
    record["import_type"] = import_type
    record["site_id"] = site_id
    record["summary"]["import_type"] = import_type
    return record


class _ImportRepository:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = copy.deepcopy(records)

    def list(self, **filters: object) -> list[dict[str, object]]:
        return [
            copy.deepcopy(record)
            for record in self.records
            if all(filters.get(name) in (None, record.get(name)) for name in filters)
        ]

    def get(self, import_id: str) -> dict[str, object]:
        for record in self.records:
            if record["import_id"] == import_id:
                return copy.deepcopy(record)
        raise FileNotFoundError(import_id)


class ResolveIpDiscoveryParametersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.older = _record(
            "imp_old",
            [
                {
                    "Expected IP address": "10.0.0.5",
                    "Expected hostname": "old-host",
                    "Expected services/ports": "80/tcp",
                    "Ports that should not be enabled": "23/tcp",
                    "Asset ID": "asset-old",
                }
            ],
        )
        self.newer = _record(
            "imp_new",
            [
                {
                    "Expected IP address": "10.0.0.2",
                    "Expected services/ports": "443/tcp, 47808/udp",
                    "Ports that should not be enabled": "22/tcp, 161/udp",
                    "Asset ID": "asset-new",
                },
                {
                    "Expected IP address": "10.0.0.2",
                    "Expected services/ports": "443/tcp",
                    "Asset name": "duplicate-address",
                },
            ],
        )
        self.repository = _ImportRepository([self.newer, self.older])

    def test_register_targets_require_an_explicit_opt_in(self) -> None:
        for parameters in (
            {"dry_run": True},
            {"dry_run": True, "use_register_addresses": False},
            {"dry_run": True, "use_register_addresses": "false"},
        ):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(
                ValueError, "use_register_addresses=true"
            ):
                resolve_ip_discovery_parameters(
                    parameters,
                    project_id="project-a",
                    site_id="site-a",
                    import_repository=self.repository,
                )

    def test_one_newest_record_supplies_targets_and_every_optional_map(self) -> None:
        source_parameters = {
            "dry_run": True,
            "ports": [80],
            "use_register_addresses": True,
        }
        result = resolve_ip_discovery_parameters(
            source_parameters,
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )

        self.assertEqual(
            source_parameters,
            {"dry_run": True, "ports": [80], "use_register_addresses": True},
        )
        self.assertEqual(result["addresses"], ["10.0.0.2"])
        self.assertEqual(result["ip_register_import_id"], "imp_new")
        self.assertNotIn("expected_hostname_by_address", result)
        self.assertEqual(result["expected_ports_by_address"], {"10.0.0.2": "443/tcp"})
        self.assertEqual(result["forbidden_ports_by_address"], {"10.0.0.2": "22/tcp"})
        self.assertEqual(result["asset_id_by_address"], {"10.0.0.2": "asset-new"})

        contract = result["scan_contract_v1"]
        authority = contract["ip"]["authority"]
        self.assertEqual(authority["import_id"], "imp_new")
        self.assertEqual(authority["source_filename"], "imp_new.csv")
        self.assertEqual(authority["source_sha256"], "a" * 64)
        self.assertEqual(authority["accepted_count"], 2)
        self.assertEqual(len(authority["accepted_rows_sha256"]), 64)
        self.assertNotIn("accepted_rows", authority)
        self.assertNotIn("stored_file_path", authority)
        self.assertEqual(
            contract["ip"]["unsupported_register_ports_by_address"],
            {"10.0.0.2": ["161/udp", "47808/udp"]},
        )
        self.assertEqual(len(contract["packet_plan_sha256"]), 64)

    def test_explicit_older_import_is_selected_without_newer_values(self) -> None:
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "ip_register_import_id": "imp_old",
                "target_expressions": [
                    {"kind": "address", "address": "10.0.0.9"},
                ],
                "ports": [{"port": 80, "protocol": "tcp"}],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )

        self.assertEqual(result["addresses"], ["10.0.0.9"])
        self.assertEqual(result["expected_hostname_by_address"], {"10.0.0.5": "old-host"})
        self.assertEqual(result["ip_register_import_id"], "imp_old")
        self.assertNotIn("expected_ports_by_address", result)

    def test_implicit_newest_zero_row_register_remains_the_selected_authority(
        self,
    ) -> None:
        newest_empty = _record("imp_empty_newest", [])
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.10"},
                ],
                "ports": [80],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([newest_empty, self.older]),
        )

        authority = result["scan_contract_v1"]["ip"]["authority"]
        self.assertEqual(authority["import_id"], "imp_empty_newest")
        self.assertEqual(authority["accepted_count"], 0)
        self.assertEqual(
            authority["accepted_rows_sha256"],
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        )
        self.assertEqual(result["ip_register_import_id"], "imp_empty_newest")
        self.assertNotIn("expected_ports_by_address", result)

    def test_explicit_zero_row_register_can_authorize_an_explicit_target_scan(self) -> None:
        selected_empty = _record("imp_empty_selected", [])
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "ip_register_import_id": "imp_empty_selected",
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.11"},
                ],
                "ports": [443],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([self.newer, selected_empty]),
        )

        authority = result["scan_contract_v1"]["ip"]["authority"]
        self.assertEqual(authority["import_id"], "imp_empty_selected")
        self.assertEqual(authority["accepted_count"], 0)
        self.assertEqual(result["addresses"], ["192.0.2.11"])

    def test_zero_row_register_cannot_be_the_only_target_source(self) -> None:
        selected_empty = _record("imp_empty_selected", [])

        with self.assertRaisesRegex(ValueError, "no accepted scan targets"):
            resolve_ip_discovery_parameters(
                {
                    "dry_run": True,
                    "ip_register_import_id": "imp_empty_selected",
                    "use_register_addresses": True,
                    "ports": [80],
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([selected_empty]),
            )

    def test_multiple_explicit_targets_and_exclusions_are_normalized(self) -> None:
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "target_expressions": [
                    {"kind": "cidr", "cidr": "10.0.0.0/30"},
                    {"kind": "range", "start": "10.0.0.2", "end": "10.0.0.4"},
                    {"kind": "address", "address": "10.0.0.10"},
                ],
                "exclusions": [
                    {"kind": "address", "address": "10.0.0.2"},
                    {"kind": "address", "address": "10.0.0.4"},
                ],
                "ports": [80, 443],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )

        self.assertEqual(result["addresses"], ["10.0.0.1", "10.0.0.3", "10.0.0.10"])
        target = result["scan_contract_v1"]["ip"]["targets"]
        self.assertEqual(target["target_count"], 3)
        self.assertEqual(target["excluded_count"], 2)
        self.assertEqual(len(target["expanded_target_sha256"]), 64)

    def test_builtin_request_udp_is_rejected_before_run_creation(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "use the BACnet discovery workflow for BACnet/IP UDP/47808",
        ):
            resolve_ip_discovery_parameters(
                {
                    "dry_run": True,
                    "target_expressions": [
                        {"kind": "address", "address": "10.0.0.1"},
                    ],
                    "ports": [{"port": 47808, "protocol": "udp"}],
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=self.repository,
            )

    def test_profile_port_omissions_are_frozen_in_priority_and_numeric_order(self) -> None:
        register = _record(
            "imp_cap",
            [
                {
                    "Expected IP address": "10.0.0.10",
                    "Expected services/ports": "47808/udp",
                },
                {
                    "Expected IP address": "10.0.0.2",
                    "Expected services/ports": "21/tcp,24/tcp,47808/udp",
                    "Ports that should not be enabled": "22/tcp,23/tcp,161/udp",
                },
            ],
        )
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "target_expressions": [
                    {"kind": "address", "address": "10.0.0.10"},
                    {"kind": "address", "address": "10.0.0.2"},
                ],
                "ports": list(range(1_000, 1_063)),
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([register]),
        )

        expected = {
            "10.0.0.2": [
                {
                    "port": 23,
                    "protocol": "tcp",
                    "source": "forbidden",
                    "reason": "profile_port_cap",
                },
                {
                    "port": 161,
                    "protocol": "udp",
                    "source": "forbidden",
                    "reason": "unsupported_protocol",
                },
                {
                    "port": 21,
                    "protocol": "tcp",
                    "source": "expected",
                    "reason": "profile_port_cap",
                },
                {
                    "port": 24,
                    "protocol": "tcp",
                    "source": "expected",
                    "reason": "profile_port_cap",
                },
                {
                    "port": 47808,
                    "protocol": "udp",
                    "source": "expected",
                    "reason": "unsupported_protocol",
                    "capability": "use_bacnet_discovery",
                },
            ],
            "10.0.0.10": [
                {
                    "port": 47808,
                    "protocol": "udp",
                    "source": "expected",
                    "reason": "unsupported_protocol",
                    "capability": "use_bacnet_discovery",
                }
            ],
        }
        self.assertEqual(result["not_attempted_ports_by_address"], expected)
        self.assertEqual(
            result["forbidden_ports_by_address"],
            {"10.0.0.2": "22/tcp"},
        )
        self.assertEqual(result["forbidden_ports"], "22/tcp")
        self.assertNotIn("expected_ports_by_address", result)
        contract = result["scan_contract_v1"]["ip"]
        self.assertEqual(contract["not_attempted_ports_by_address"], expected)
        self.assertEqual(
            contract["work_estimate"]["known_initial_dispatch_units"],
            127,
        )
        self.assertEqual(
            contract["work_estimate"]["register_added_dispatch_units"],
            1,
        )

    def test_register_ports_over_the_hard_axis_are_rejected_not_omitted(self) -> None:
        register = _record(
            "imp_hard_cap",
            [
                {
                    "Expected IP address": "10.0.0.2",
                    "Expected services/ports": "1-4096/tcp",
                    "Ports that should not be enabled": "1/udp",
                }
            ],
        )

        with self.assertRaisesRegex(
            ValueError,
            "4,097 protocol ports.*hard maximum 4,096",
        ):
            resolve_ip_discovery_parameters(
                {
                    "dry_run": True,
                    "target_expressions": [
                        {"kind": "address", "address": "10.0.0.2"},
                    ],
                    "ports": [1],
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([register]),
            )

    def test_source_interface_is_bound_into_packet_plan_digest(self) -> None:
        common = {
            "dry_run": True,
            "target_expressions": [
                {"kind": "address", "address": "10.0.0.1"},
            ],
            "ports": [80],
        }
        first = resolve_ip_discovery_parameters(
            {
                **common,
                "source_ip": "192.168.1.10",
                "local_address": "192.168.1.10/24",
                "source_interface_identity_v1": {
                    **_TEST_SOURCE_INTERFACE_IDENTITY,
                    "interface_id": "test-if:10",
                    "interface_name": "Test field NIC 10",
                    "source_ip": "192.168.1.10",
                    "local_address": "192.168.1.10/24",
                },
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )["scan_contract_v1"]
        second = resolve_ip_discovery_parameters(
            {
                **common,
                "source_ip": "192.168.2.10",
                "local_address": "192.168.2.10/24",
                "source_interface_identity_v1": {
                    **_TEST_SOURCE_INTERFACE_IDENTITY,
                    "interface_id": "test-if:20",
                    "interface_name": "Test field NIC 20",
                    "source_ip": "192.168.2.10",
                    "local_address": "192.168.2.10/24",
                },
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )["scan_contract_v1"]

        self.assertEqual(
            first["source_interface"],
            {
                **_TEST_SOURCE_INTERFACE_IDENTITY,
                "interface_id": "test-if:10",
                "interface_name": "Test field NIC 10",
                "source_ip": "192.168.1.10",
                "local_address": "192.168.1.10/24",
            },
        )
        self.assertEqual(
            first["resource_keys"],
            [
                "nic:v1:9453e87d1ae9ef255a7a1d0554df1206beb9c746cf222957188f3db493261223"
            ],
        )
        self.assertNotEqual(first["packet_plan_sha256"], second["packet_plan_sha256"])

    def test_import_from_another_scope_or_with_tampered_count_is_rejected(self) -> None:
        wrong_scope = copy.deepcopy(self.older)
        wrong_scope["site_id"] = "site-b"
        with self.assertRaises(FileNotFoundError):
            resolve_ip_discovery_parameters(
                {
                    "dry_run": True,
                    "ip_register_import_id": "imp_old",
                    "target_expressions": [
                        {"kind": "address", "address": "10.0.0.1"},
                    ],
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([wrong_scope]),
            )

        tampered = copy.deepcopy(self.older)
        tampered["summary"]["accepted_rows"] = 99
        with self.assertRaisesRegex(ValueError, "accepted-row count"):
            resolve_ip_discovery_parameters(
                {"dry_run": True},
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([tampered]),
            )

    def test_explicit_targets_work_without_register_and_record_no_authority(self) -> None:
        result = resolve_ip_discovery_parameters(
            {"dry_run": True, "cidr": "192.0.2.1/32", "ports": [80]},
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )

        self.assertEqual(result["addresses"], ["192.0.2.1"])
        self.assertIsNone(result["scan_contract_v1"]["ip"]["authority"])
        self.assertNotIn("ip_register_import_id", result)

    def test_gentle_profile_freezes_effective_caps_work_and_provider_state(self) -> None:
        result = resolve_ip_discovery_parameters(
            {
                "dry_run": True,
                "target_expressions": [
                    {"kind": "range", "start": "192.0.2.1", "end": "192.0.2.3"},
                ],
                "ports": [80, 443],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
            effective_throttle={
                "max_concurrency": 16,
                "rate_limit_per_sec": 20,
                "connect_timeout_s": 5,
            },
        )

        contract = result["scan_contract_v1"]
        self.assertEqual(
            contract["effective_throttle"],
            {
                "max_concurrency": 8,
                "rate_limit_per_sec": 10.0,
                "connect_timeout_s": 1.5,
            },
        )
        self.assertEqual(result["scan_max_concurrency"], 8)
        self.assertEqual(result["scan_rate_limit_per_sec"], 10.0)
        self.assertEqual(result["scan_connect_timeout_s"], 1.5)
        self.assertEqual(result["scan_per_target_concurrency"], 1)
        policy = contract["ip"]["policy"]
        self.assertEqual(policy["max_targets"], 256)
        self.assertEqual(policy["max_protocol_ports_per_target"], 64)
        self.assertEqual(policy["total_dispatch_attempt_ceiling"], 6_000)
        self.assertEqual(policy["per_target_concurrency"], 1)
        self.assertEqual(policy["min_target_spacing_ms"], 100.0)
        self.assertEqual(policy["retries"], 0)
        self.assertEqual(policy["dispatch_phase_seconds"], 2_400)
        self.assertEqual(policy["run_deadline_seconds"], 2_700)
        self.assertEqual(
            contract["ip"]["provider_state"],
            {
                "provider": "builtin_tcp_connect",
                "capability_state": "available",
                "execution_boundary": "application_owned",
                "execution_enabled": True,
                "supported_protocols": ["tcp"],
            },
        )
        estimate = contract["ip"]["work_estimate"]
        self.assertEqual(estimate["known_initial_dispatch_units"], 6)
        self.assertEqual(estimate["planned_dispatch_attempts"], 6)
        self.assertLessEqual(
            estimate["planned_dispatch_attempts"],
            estimate["derived_executable_attempt_ceiling"],
        )
        self.assertGreater(estimate["conservative_run_seconds"], 300)
        self.assertEqual(len(estimate["dispatch_plan_sha256"]), 64)

    def test_gentle_uses_local_and_routed_timeout_ceilings(self) -> None:
        common = {
            "dry_run": True,
            "ports": [80],
            "local_address": "10.0.0.10/24",
        }
        throttle = {
            "max_concurrency": 4,
            "rate_limit_per_sec": 2,
            "connect_timeout_s": 5,
        }
        local = resolve_ip_discovery_parameters(
            {
                **common,
                "target_expressions": [
                    {"kind": "address", "address": "10.0.0.20"},
                ],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
            effective_throttle=throttle,
        )["scan_contract_v1"]["effective_throttle"]
        routed = resolve_ip_discovery_parameters(
            {
                **common,
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.20"},
                ],
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
            effective_throttle=throttle,
        )["scan_contract_v1"]["effective_throttle"]

        self.assertEqual(local["max_concurrency"], 4)
        self.assertEqual(local["rate_limit_per_sec"], 2.0)
        self.assertEqual(local["connect_timeout_s"], 1.5)
        self.assertEqual(routed["connect_timeout_s"], 3.0)

    def test_gentle_rejects_axis_and_register_added_cartesian_overruns(self) -> None:
        cases = (
            {
                "target_expressions": [
                    {"kind": "range", "start": "192.0.2.1", "end": "192.0.3.1"},
                ],
                "ports": [80],
            },
            {
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.1"},
                ],
                "ports": list(range(1, 66)),
            },
        )
        for parameters in cases:
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                resolve_ip_discovery_parameters(
                    {"dry_run": True, **parameters},
                    project_id="project-a",
                    site_id="site-a",
                    import_repository=_ImportRepository([]),
                )

        with self.assertRaisesRegex(ValueError, "6,002 dispatch attempts.*6,000"):
            resolve_ip_discovery_parameters(
                {
                    "dry_run": True,
                    "target_expressions": [
                        {
                            "kind": "range",
                            "start": "10.0.0.1",
                            "end": "10.0.0.100",
                        },
                    ],
                    "ports": list(range(1_000, 1_060)),
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=self.repository,
            )

    def test_planned_extended_requires_ack_and_rejects_derived_runtime_overrun(self) -> None:
        common = {
            "dry_run": True,
            "profile": "planned_extended",
            "target_expressions": [
                {"kind": "range", "start": "10.10.0.1", "end": "10.10.3.232"},
            ],
            "ports": list(range(20_000, 20_005)),
        }
        throttle = {
            "max_concurrency": 16,
            "rate_limit_per_sec": 10,
            "connect_timeout_s": 5,
        }
        with self.assertRaisesRegex(ValueError, "risk acknowledgement"):
            resolve_ip_discovery_parameters(
                common,
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
                effective_throttle=throttle,
            )

        with self.assertRaisesRegex(ValueError, "derived executable maximum"):
            resolve_ip_discovery_parameters(
                {**common, "planned_extended_risk_acknowledged": True},
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
                effective_throttle=throttle,
            )

        accepted = resolve_ip_discovery_parameters(
            {
                **common,
                "planned_extended_risk_acknowledged": True,
                "ports": list(range(20_000, 20_004)),
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
            effective_throttle=throttle,
        )["scan_contract_v1"]
        self.assertEqual(accepted["ip"]["policy"]["retries"], 1)
        self.assertEqual(
            accepted["ip"]["work_estimate"]["planned_dispatch_attempts"],
            8_000,
        )
        self.assertEqual(
            accepted["ip"]["work_estimate"]["derived_executable_attempt_ceiling"],
            8_624,
        )

    def test_planned_extended_rejects_projected_observation_payload_over_runtime_cap(
        self,
    ) -> None:
        first = int(ipaddress.IPv4Address("10.30.0.0"))
        asset_id = "\U0001f4a1" * 4_096
        rows = [
            {
                "Expected IP address": str(ipaddress.IPv4Address(first + offset)),
                "Asset ID": asset_id,
            }
            for offset in range(4_096)
        ]
        register = _record("imp_observation_payload_cap", rows)

        with self.assertRaisesRegex(
            ValueError,
            "planned IP observation payload.*67,108,864-byte ceiling",
        ):
            resolve_ip_discovery_parameters(
                _source_parameters(**{
                    "dry_run": True,
                    "profile": "planned_extended",
                    "planned_extended_risk_acknowledged": True,
                    "target_expressions": [
                        {
                            "kind": "range",
                            "start": "10.30.0.0",
                            "end": "10.30.15.255",
                        },
                    ],
                    "ports": [80],
                }),
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([register]),
            )

    def test_ip_contract_freezes_the_complete_u3_observation_budget(self) -> None:
        contract = resolve_ip_discovery_parameters(
            _source_parameters(**{
                "dry_run": True,
                "target_expressions": [
                    {"kind": "range", "start": "192.0.2.1", "end": "192.0.2.3"},
                ],
                "ports": [80, 443],
            }),
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )["scan_contract_v1"]

        budget = contract["ip"]["observation_budget"]
        self.assertEqual(
            budget,
            {
                "schema_version": "1.0",
                "estimator": "ip_u3_v1",
                "planned_observation_rows": 19,
                "planned_observation_payload_bytes": 24_462,
                "row_ceiling": 50_000,
                "canonical_payload_byte_ceiling": 67_108_864,
                "host_state_rows": 12,
                "port_attempt_rows": 6,
                "not_attempted_port_rows": 0,
                "control_diagnostic_rows": 1,
            },
        )
        self.assertGreater(budget["planned_observation_payload_bytes"], 0)
        self.assertLess(
            budget["planned_observation_payload_bytes"],
            budget["canonical_payload_byte_ceiling"],
        )

    def test_ip_port_projection_boundary_is_admitted_or_rejected_at_preview(
        self,
    ) -> None:
        common = _source_parameters(**{
            "dry_run": True,
            "profile": "planned_extended",
            "planned_extended_risk_acknowledged": True,
            "scan_connect_timeout_s": 0.1,
            "target_expressions": [
                {"kind": "address", "address": "192.0.2.10"},
            ],
        })
        accepted = resolve_ip_discovery_parameters(
            {**common, "ports": list(range(1, 514))},
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )
        self.assertEqual(len(accepted["ports"]), 513)
        self.assertLessEqual(
            accepted["scan_contract_v1"]["ip"]["observation_budget"][
                "planned_observation_payload_bytes"
            ],
            67_108_864,
        )

        with self.assertRaisesRegex(
            ValueError,
            "planned IP observation row exceeds the 65,536-byte canonical limit",
        ):
            resolve_ip_discovery_parameters(
                {**common, "ports": list(range(1, 4_097))},
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
            )

    def test_one_host_thousand_five_digit_ports_has_a_bounded_preview_budget(self) -> None:
        resolved = resolve_ip_discovery_parameters(
            _source_parameters(**{
                "dry_run": True,
                "profile": "planned_extended",
                "planned_extended_risk_acknowledged": True,
                "scan_connect_timeout_s": 0.1,
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.10"},
                ],
                "ports": list(range(10_000, 11_000)),
            }),
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )

        budget = resolved["scan_contract_v1"]["ip"]["observation_budget"]
        self.assertEqual(len(resolved["ports"]), 1_000)
        self.assertLessEqual(
            budget["planned_observation_payload_bytes"],
            budget["canonical_payload_byte_ceiling"],
        )

    def test_observation_budget_covers_long_multibyte_a7_projection_fields(
        self,
    ) -> None:
        rows = [
            {
                "Expected IP address": "192.0.2.10",
                "Expected hostname": "h" * 253,
                "Asset ID": "\U0001f4a1" * 4_096,
                "Asset name": "\U0001f680" * 4_096,
            }
        ]
        resolved = resolve_ip_discovery_parameters(
            _source_parameters(**{
                "dry_run": True,
                "target_expressions": [
                    {"kind": "address", "address": "192.0.2.99"},
                ],
                "ports": [443],
            }),
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([_record("imp_long_a7", rows)]),
        )

        budget = resolved["scan_contract_v1"]["ip"]["observation_budget"]
        self.assertGreater(budget["planned_observation_payload_bytes"], 50_000)
        self.assertLessEqual(
            budget["planned_observation_payload_bytes"],
            budget["canonical_payload_byte_ceiling"],
        )

    def test_every_authority_projection_candidate_is_viewer_safe(self) -> None:
        rows = [
            {
                "Expected IP address": "192.0.2.10",
                "Asset ID": "AHU-10",
            },
            {
                "Expected IP address": "192.0.2.200",
                "Asset name": r"C:\Users\operator\private-register.xml",
            },
        ]

        with self.assertRaisesRegex(ValueError, "raw or locally scoped evidence"):
            resolve_ip_discovery_parameters(
                _source_parameters(**{
                    "dry_run": True,
                    "target_expressions": [
                        {"kind": "address", "address": "192.0.2.10"},
                    ],
                    "ports": [443],
                }),
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([_record("imp_unsafe", rows)]),
            )

    def test_complete_resolved_ip_parameters_are_bounded_without_freezing_rows(
        self,
    ) -> None:
        first = int(ipaddress.IPv4Address("10.40.0.0"))

        def resolve(row_count: int) -> dict[str, object]:
            rows = [
                {
                    "Expected IP address": str(
                        ipaddress.IPv4Address(first + offset)
                    ),
                    "Asset ID": f"asset-{offset:04d}-" + ("x" * 40),
                }
                for offset in range(row_count)
            ]
            return resolve_ip_discovery_parameters(
                _source_parameters(**{
                    "dry_run": True,
                    "profile": "planned_extended",
                    "planned_extended_risk_acknowledged": True,
                    "target_expressions": [
                        {
                            "kind": "range",
                            "start": "10.40.0.0",
                            "end": str(ipaddress.IPv4Address(first + row_count - 1)),
                        },
                    ],
                    "ports": [443],
                }),
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository(
                    [_record(f"imp_resolved_{row_count}", rows)]
                ),
            )

        near_bound = resolve(2_500)
        encoded = canonical_json_bytes(near_bound)
        self.assertGreater(len(encoded), 196_608)
        self.assertLessEqual(len(encoded), SCAN_CONTRACT_MAX_BYTES)
        self.assertNotIn(b'"accepted_rows"', encoded)

        with self.assertRaisesRegex(
            ValueError,
            "resolved IP engine parameters.*262,144-byte ceiling",
        ):
            resolve(4_096)

    def test_planned_extended_rejects_more_than_50000_observation_rows(self) -> None:
        first = int(ipaddress.IPv4Address("10.31.0.0"))
        last = str(ipaddress.IPv4Address(first + 4_095))
        udp_ports = ",".join(f"{port}/udp" for port in range(40_000, 40_007))
        rows = [
            {
                "Expected IP address": str(ipaddress.IPv4Address(first + offset)),
                "Expected services/ports": udp_ports,
            }
            for offset in range(4_096)
        ]
        register = _record("imp_observation_row_cap", rows)

        with self.assertRaisesRegex(
            ValueError,
            "53,249 rows.*50,000-row ceiling",
        ):
            resolve_ip_discovery_parameters(
                _source_parameters(**{
                    "dry_run": True,
                    "profile": "planned_extended",
                    "planned_extended_risk_acknowledged": True,
                    "target_expressions": [
                        {"kind": "range", "start": "10.31.0.0", "end": last},
                    ],
                    "ports": [80],
                }),
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([register]),
            )

    def test_nonfinite_throttle_and_short_executor_or_authorization_window_fail_closed(self) -> None:
        request = {
            "dry_run": True,
            "target_expressions": [
                {"kind": "address", "address": "192.0.2.1"},
            ],
            "ports": [80],
        }
        for throttle in (
            {"max_concurrency": 1, "rate_limit_per_sec": float("nan"), "connect_timeout_s": 1},
            {"max_concurrency": 1, "rate_limit_per_sec": 1, "connect_timeout_s": float("inf")},
        ):
            with self.subTest(throttle=throttle), self.assertRaisesRegex(
                ValueError, "effective_throttle"
            ):
                resolve_ip_discovery_parameters(
                    request,
                    project_id="project-a",
                    site_id="site-a",
                    import_repository=_ImportRepository([]),
                    effective_throttle=throttle,
                )

        for window in ("authorization", "executor"):
            kwargs = (
                {"authorization_window_seconds": 300}
                if window == "authorization"
                else {"executor_limit_seconds": 300}
            )
            with self.subTest(window=window), self.assertRaisesRegex(
                ValueError, f"{window} window"
            ):
                resolve_ip_discovery_parameters(
                    request,
                    project_id="project-a",
                    site_id="site-a",
                    import_repository=_ImportRepository([]),
                    **kwargs,
                )

        invalid_policy_values = (
            {"scan_target_spacing_ms": float("nan")},
            {"scan_retry_backoff_ms": float("inf")},
            {"scan_dispatch_phase_seconds": float("inf")},
            {"scan_retries": 2**80},
        )
        for override in invalid_policy_values:
            with self.subTest(override=override), self.assertRaises(ValueError):
                resolve_ip_discovery_parameters(
                    {**request, **override},
                    project_id="project-a",
                    site_id="site-a",
                    import_repository=_ImportRepository([]),
                )


class ResolveBacnetDiscoveryParametersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = _bacnet_record(
            "bacnet_devices_new",
            "bacnet_register",
            [
                {
                    "Asset ID": "ahu-2",
                    "Asset name": "Second AHU",
                    "BACnet device instance": "2002",
                    "BACnet network": "20",
                    "IP address": "10.20.0.22",
                    "Internetwork ID": "plant-a",
                },
                {
                    "Asset ID": "ahu-1",
                    "Asset name": "First AHU",
                    "BACnet device instance": "2001",
                    "BACnet network": "20",
                    "IP address": "10.20.0.21",
                    "Internetwork ID": "plant-a",
                },
            ],
        )
        self.points = _bacnet_record(
            "bacnet_points_new",
            "bacnet_points",
            [
                {
                    "Asset ID": "ahu-1",
                    "Device instance": "2001",
                    "BACnet network": "20",
                    "Internetwork ID": "plant-a",
                    "Object type": "analogInput",
                    "Object instance": "1",
                    "Expected point name": "supply-air-temperature",
                }
            ],
        )
        self.repository = _ImportRepository([self.devices, self.points])

    def test_broadcast_without_register_is_valid_and_records_no_authority(self) -> None:
        result = resolve_bacnet_discovery_parameters(
            {"dry_run": True, "internetwork_id": "plant-a"},
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )

        self.assertNotIn("bacnet_targets", result)
        contract = result["scan_contract_v1"]
        self.assertEqual(contract["bacnet"]["lanes"], ["local_broadcast"])
        self.assertEqual(
            contract["bacnet"]["authorities"],
            {"devices": None, "points": None},
        )
        self.assertEqual(contract["bacnet"]["expected_device_count"], 0)

    def test_one_device_and_point_authority_are_selected_and_bounded(self) -> None:
        result = resolve_bacnet_discovery_parameters(
            {
                "dry_run": True,
                "internetwork_id": "plant-a",
                "local_address": "10.20.0.10/24",
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
        )

        self.assertEqual(result["bacnet_register_import_id"], "bacnet_devices_new")
        self.assertEqual(result["bacnet_points_import_id"], "bacnet_points_new")
        self.assertEqual(
            [(row["device_instance"], row["address"]) for row in result["bacnet_targets"]],
            [(2001, "10.20.0.21"), (2002, "10.20.0.22")],
        )
        contract = result["scan_contract_v1"]
        self.assertEqual(
            contract["bacnet"]["lanes"],
            ["local_broadcast", "directed_unicast"],
        )
        self.assertEqual(contract["bacnet"]["local_address"], "10.20.0.10/24")
        self.assertTrue(any(key.startswith("bacnet:") for key in contract["resource_keys"]))
        self.assertFalse(any(key.startswith("nic:") for key in contract["resource_keys"]))
        self.assertEqual(len(contract["resource_keys"]), 1)
        self.assertEqual(contract["bacnet"]["expected_device_count"], 2)
        self.assertEqual(
            contract["bacnet"]["authorities"]["devices"]["import_id"],
            "bacnet_devices_new",
        )
        self.assertEqual(
            contract["bacnet"]["authorities"]["points"]["import_id"],
            "bacnet_points_new",
        )
        self.assertNotIn("accepted_rows", contract["bacnet"]["authorities"]["points"])
        self.assertLess(len(canonical_json_bytes(contract)), SCAN_CONTRACT_MAX_BYTES)

    def test_bacnet_rejects_targets_above_the_execution_ceiling_before_contract_creation(self) -> None:
        targets = [
            {
                "address": f"10.20.{index // 254}.{(index % 254) + 1}",
                "device_instance": index + 1,
                "internetwork_id": "plant-a",
            }
            for index in range(1_025)
        ]

        with self.assertRaisesRegex(ValueError, "1025.*1024"):
            resolve_bacnet_discovery_parameters(
                {
                    "dry_run": True,
                    "internetwork_id": "plant-a",
                    "bacnet_targets": targets,
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
            )

    def test_foreign_lane_is_explicit_and_requires_a_bbmd(self) -> None:
        with self.assertRaisesRegex(ValueError, "bbmd_address"):
            resolve_bacnet_discovery_parameters(
                {
                    "dry_run": True,
                    "bacnet_mode": "foreign_device",
                    "internetwork_id": "plant-a",
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
            )

        result = resolve_bacnet_discovery_parameters(
            {
                "dry_run": True,
                "bacnet_mode": "foreign_device",
                "bbmd_address": "10.20.0.1",
                "bbmd_port": 47809,
                "fd_ttl": 300,
                "internetwork_id": "plant-a",
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([]),
        )
        self.assertEqual(
            result["scan_contract_v1"]["bacnet"]["lanes"],
            ["foreign_device"],
        )
        self.assertEqual(
            len(
                [
                    key
                    for key in result["scan_contract_v1"]["resource_keys"]
                    if key.startswith("bacnet:")
                ]
            ),
            2,
        )

    def test_explicit_authority_must_match_type_and_scope(self) -> None:
        wrong_scope = _bacnet_record(
            "foreign-devices",
            "bacnet_register",
            [],
            site_id="site-b",
        )
        with self.assertRaises(FileNotFoundError):
            resolve_bacnet_discovery_parameters(
                {
                    "dry_run": True,
                    "bacnet_register_import_id": "foreign-devices",
                    "internetwork_id": "plant-a",
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([wrong_scope]),
            )

    def test_large_point_manifest_is_referenced_without_copying_rows(self) -> None:
        rows = [
            {
                "Asset ID": "ahu-1",
                "Device instance": "2001",
                "BACnet network": "20",
                "Internetwork ID": "plant-a",
                "Object type": "analogInput",
                "Object instance": str(index),
                "Expected point name": f"point-{index}",
            }
            for index in range(25_000)
        ]
        points = _bacnet_record("large-points", "bacnet_points", rows)
        result = resolve_bacnet_discovery_parameters(
            {"dry_run": True, "internetwork_id": "plant-a"},
            project_id="project-a",
            site_id="site-a",
            import_repository=_ImportRepository([points]),
        )

        contract = result["scan_contract_v1"]
        point_authority = contract["bacnet"]["authorities"]["points"]
        self.assertEqual(point_authority["accepted_count"], 25_000)
        self.assertNotIn("accepted_rows", point_authority)
        self.assertLess(len(canonical_json_bytes(contract)), SCAN_CONTRACT_MAX_BYTES)

    def test_bacnet_freezes_apdu_budget_runtime_provider_and_response_policy(self) -> None:
        contract = resolve_bacnet_discovery_parameters(
            {
                "dry_run": True,
                "internetwork_id": "plant-a",
                "local_address": "10.20.0.10/24",
            },
            project_id="project-a",
            site_id="site-a",
            import_repository=self.repository,
            effective_throttle={
                "max_concurrency": 16,
                "rate_limit_per_sec": 20,
                "connect_timeout_s": 5,
            },
        )["scan_contract_v1"]

        self.assertEqual(
            contract["effective_throttle"],
            {
                "max_concurrency": 4,
                "rate_limit_per_sec": 10.0,
                "connect_timeout_s": 3.0,
            },
        )
        policy = contract["bacnet"]["policy"]
        self.assertEqual(policy["total_dispatch_attempt_ceiling"], 3_500)
        self.assertEqual(policy["per_target_concurrency"], 1)
        self.assertEqual(policy["min_target_spacing_ms"], 100.0)
        self.assertEqual(policy["retries"], 1)
        self.assertEqual(policy["dispatch_phase_seconds"], 2_640)
        self.assertEqual(policy["run_deadline_seconds"], 3_000)
        estimate = contract["bacnet"]["work_estimate"]
        self.assertEqual(estimate["planned_dispatch_attempts"], 3_500)
        self.assertEqual(estimate["derived_executable_attempt_ceiling"], 3_500)
        self.assertEqual(estimate["conservative_dispatch_seconds"], 2_625.25)
        self.assertEqual(estimate["conservative_run_seconds"], 2_985.25)
        self.assertEqual(estimate["required_authorization_window_seconds"], 2_986)
        self.assertEqual(
            contract["bacnet"]["provider_state"]["provider"],
            "bacpypes3",
        )
        self.assertEqual(
            contract["bacnet"]["response_source_policy"]["unmatched_response_action"],
            "quarantine_no_follow_on",
        )
        self.assertEqual(
            [
                row["lane"]
                for row in contract["bacnet"]["response_source_policy"]["lanes"]
            ],
            ["local_broadcast", "directed_unicast"],
        )

    def test_bacnet_rejects_apdu_cap_and_window_overruns(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_apdu_attempts.*3,500"):
            resolve_bacnet_discovery_parameters(
                {
                    "dry_run": True,
                    "internetwork_id": "plant-a",
                    "max_apdu_attempts": 3_501,
                },
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
            )

        with self.assertRaisesRegex(ValueError, "authorization window"):
            resolve_bacnet_discovery_parameters(
                {"dry_run": True, "internetwork_id": "plant-a"},
                project_id="project-a",
                site_id="site-a",
                import_repository=_ImportRepository([]),
                authorization_window_seconds=2_985,
            )


if __name__ == "__main__":
    unittest.main()
