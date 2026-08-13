"""Focused compatibility tests for typed IP discovery result rows."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from app.schemas.jobs import DiscoveryAssetObservation, ObservedPort
from pydantic import ValidationError


def _provenance() -> dict[str, object]:
    return {
        "profile": "gentle",
        "source_ip": "192.0.2.10",
        "source_interface": "Ethernet 2",
        "packet_plan_sha256": "a" * 64,
        "register_import_id": "imp_01JTEST",
        "register_rows_sha256": "b" * 64,
    }


class IPObservationSchemaTests(unittest.TestCase):
    def test_typed_ip_result_fields_round_trip_without_losing_legacy_service(self) -> None:
        legacy = ObservedPort(port=443, protocol="tcp", service="https")
        self.assertEqual(legacy.model_dump()["service"], "https")

        last_packet_at = datetime(2026, 8, 11, 9, 15, 30, tzinfo=UTC)
        asset = DiscoveryAssetObservation(
            ip_address="192.0.2.20",
            schema_version="1.0",
            target="192.0.2.20",
            coverage_state="attempted",
            reachability_state="reachable",
            register_match="expected_match",
            policy_verdict="forbidden_open",
            provider="builtin_tcp_connect",
            provider_version="0.1.41",
            provider_contract_version="1.0",
            provenance=_provenance(),
            reason="TCP/23 accepted a connection.",
            attempts=1,
            elapsed_ms=8.25,
            control_reason=None,
            last_packet_dispatched_at=last_packet_at,
            observed_ports=[
                {
                    "schema_version": "1.0",
                    "port": 23,
                    "protocol": "tcp",
                    "service": None,
                    "coverage_state": "attempted",
                    "reachability_state": "reachable",
                    "probe_outcome": "connected",
                    "policy_verdict": "forbidden_open",
                    "provider": "builtin_tcp_connect",
                    "provider_version": "0.1.41",
                    "provider_contract_version": "1.0",
                    "provenance": _provenance(),
                    "reason": "TCP connection accepted.",
                    "attempts": 1,
                    "elapsed_ms": 8.25,
                    "port_hint": "telnet",
                    "detected_service": None,
                    "detected_version": None,
                    "control_reason": None,
                    "last_packet_dispatched_at": last_packet_at,
                }
            ],
        )

        serialized = asset.model_dump(mode="json")
        self.assertEqual(serialized["coverage_state"], "attempted")
        self.assertEqual(serialized["provenance"]["profile"], "gentle")
        self.assertEqual(serialized["observed_ports"][0]["probe_outcome"], "connected")
        self.assertEqual(serialized["observed_ports"][0]["port_hint"], "telnet")
        self.assertIsNone(serialized["observed_ports"][0]["detected_service"])

        with self.assertRaises(ValidationError):
            ObservedPort(
                port=23,
                protocol="tcp",
                probe_outcome="closed",
            )

    def test_udp_47808_capability_action_is_exposed_without_probe_evidence(self) -> None:
        omitted = ObservedPort(
            port=47808,
            protocol="udp",
            coverage_state="not_attempted",
            reachability_state="not_applicable",
            policy_verdict="not_attempted",
            attempts=0,
            capability_action="use_bacnet_discovery",
        )

        self.assertEqual(
            omitted.model_dump()["capability_action"],
            "use_bacnet_discovery",
        )


if __name__ == "__main__":
    unittest.main()
