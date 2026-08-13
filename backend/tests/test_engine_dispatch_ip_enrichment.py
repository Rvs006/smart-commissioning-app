"""Unit tests for the first-release IP enrichment boundary.

HONESTY: no sockets, DNS, cache reads, or processes are permitted by this helper.
Hostname and MAC values must come from frozen register or scan-protocol evidence.
"""

import unittest

from app.services.engine_dispatch import build_throttle, resolve_ip_enrichment


class IpProviderExecutionBoundaryTests(unittest.TestCase):
    def test_operator_managed_provider_reaches_its_own_runtime_gate(self) -> None:
        parameters = {
            "scan_contract_v1": {
                "job_type": "ip_discovery",
                "ip": {
                    "provider": "operator_managed_nmap",
                    "provider_state": {"provider": "operator_managed_nmap"},
                },
            }
        }
        build_throttle(
            parameters,
            max_concurrency=8,
            rate_limit_per_sec=10,
            connect_timeout_s=3,
        )

    def test_disabled_builtin_provider_is_rejected_before_dispatch(self) -> None:
        parameters = {
            "scan_contract_v1": {
                "job_type": "ip_discovery",
                "ip": {
                    "provider": "builtin_tcp_connect",
                    "provider_state": {"provider": "builtin_tcp_connect"},
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "builtin_tcp_connect.*execution is disabled"):
            build_throttle(
                parameters,
                max_concurrency=8,
                rate_limit_per_sec=10,
                connect_timeout_s=3,
            )


class ResolveIpEnrichmentTests(unittest.TestCase):
    def test_authorized_real_run_disables_reverse_dns(self) -> None:
        parameters: dict = {"authorized": True}
        resolve_ip_enrichment(parameters)
        self.assertFalse(parameters["reverse_dns"])

    def test_dry_run_freezes_reverse_dns_off(self) -> None:
        for value in (True, "true", "1", "yes", "on"):
            parameters: dict = {"dry_run": value}
            resolve_ip_enrichment(parameters)
            self.assertFalse(parameters["reverse_dns"])

    def test_explicit_reverse_dns_false_override_is_preserved(self) -> None:
        parameters: dict = {"authorized": True, "reverse_dns": False}
        resolve_ip_enrichment(parameters)
        self.assertFalse(parameters["reverse_dns"])

    def test_explicit_reverse_dns_true_is_overridden(self) -> None:
        parameters: dict = {"authorized": True, "reverse_dns": True}
        resolve_ip_enrichment(parameters)
        self.assertFalse(parameters["reverse_dns"])

    def test_no_other_parameters_are_added(self) -> None:
        parameters: dict = {"authorized": True}
        resolve_ip_enrichment(parameters)
        self.assertEqual(parameters, {"authorized": True, "reverse_dns": False})


if __name__ == "__main__":
    unittest.main()
