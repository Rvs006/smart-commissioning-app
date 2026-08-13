"""Versioned discovery input contracts shared by API, worker, and engines."""

from __future__ import annotations

import unittest

from pydantic import ValidationError
from smart_commissioning_core.scan_contract import (
    BacnetExpectedDeviceV1,
    BacnetScanParametersV1,
    DiscoveryPolicyV1,
    EffectiveThrottleV1,
    ImportAuthorityReferenceV1,
    IPNotAttemptedPortV1,
    IPScanParametersV1,
    IPv4TargetExpressionV1,
    ProtocolPortV1,
    estimate_discovery_work,
    normalize_ipv4_targets,
    normalize_protocol_ports,
    parse_protocol_port_spec,
)


class IPv4TargetContractTests(unittest.TestCase):
    def test_multiple_shapes_overlap_exclusions_and_sort_numerically(self) -> None:
        plan = normalize_ipv4_targets(
            expressions=(
                IPv4TargetExpressionV1(kind="cidr", cidr="10.0.0.0/30"),
                IPv4TargetExpressionV1(
                    kind="range", start="10.0.0.2", end="10.0.0.4"
                ),
                IPv4TargetExpressionV1(kind="address", address="10.0.0.10"),
                IPv4TargetExpressionV1(kind="address", address="10.0.0.3"),
            ),
            exclusions=(
                IPv4TargetExpressionV1(kind="address", address="10.0.0.2"),
                IPv4TargetExpressionV1(kind="range", start="10.0.0.4", end="10.0.0.4"),
            ),
            max_hosts=16,
        )

        self.assertEqual(plan.expanded_addresses, ("10.0.0.1", "10.0.0.3", "10.0.0.10"))
        self.assertEqual(plan.target_count, 3)
        self.assertEqual(plan.excluded_count, 2)
        self.assertEqual(
            tuple(item.model_dump(mode="json") for item in plan.grouped_ranges),
            (
                {"start": "10.0.0.1", "end": "10.0.0.1", "count": 1},
                {"start": "10.0.0.3", "end": "10.0.0.3", "count": 1},
                {"start": "10.0.0.10", "end": "10.0.0.10", "count": 1},
            ),
        )
        self.assertEqual(len(plan.expanded_target_sha256), 64)
        self.assertEqual(
            plan,
            type(plan).model_validate(plan.model_dump(mode="json")),
            "the serializable frozen preview must round-trip without hidden required fields",
        )

    def test_equivalent_input_order_has_the_same_digest(self) -> None:
        left = normalize_ipv4_targets(
            expressions=(
                IPv4TargetExpressionV1(kind="address", address="10.0.0.2"),
                IPv4TargetExpressionV1(kind="address", address="10.0.0.1"),
            ),
            max_hosts=4,
        )
        right = normalize_ipv4_targets(
            expressions=(
                IPv4TargetExpressionV1(kind="range", start="10.0.0.1", end="10.0.0.2"),
            ),
            max_hosts=4,
        )

        self.assertEqual(left.expanded_addresses, right.expanded_addresses)
        self.assertEqual(left.expanded_target_sha256, right.expanded_target_sha256)

    def test_cidr_drops_network_and_broadcast_but_keeps_slash_31(self) -> None:
        ordinary = normalize_ipv4_targets(
            expressions=(IPv4TargetExpressionV1(kind="cidr", cidr="192.0.2.0/30"),),
            max_hosts=8,
        )
        point_to_point = normalize_ipv4_targets(
            expressions=(IPv4TargetExpressionV1(kind="cidr", cidr="192.0.2.4/31"),),
            max_hosts=8,
        )

        self.assertEqual(ordinary.expanded_addresses, ("192.0.2.1", "192.0.2.2"))
        self.assertEqual(point_to_point.expanded_addresses, ("192.0.2.4", "192.0.2.5"))

    def test_ipv6_reversed_range_and_limited_broadcast_are_rejected(self) -> None:
        invalid = (
            {"kind": "address", "address": "2001:db8::1"},
            {"kind": "range", "start": "10.0.0.2", "end": "10.0.0.1"},
            {"kind": "address", "address": "255.255.255.255"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                IPv4TargetExpressionV1.model_validate(value)

    def test_host_cap_is_rejected_without_silent_truncation(self) -> None:
        with self.assertRaisesRegex(ValueError, "expands to 5 hosts.*max_hosts=4"):
            normalize_ipv4_targets(
                expressions=(
                    IPv4TargetExpressionV1(
                        kind="range", start="10.0.0.1", end="10.0.0.5"
                    ),
                ),
                max_hosts=4,
            )

    def test_empty_effective_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero hosts"):
            normalize_ipv4_targets(
                expressions=(IPv4TargetExpressionV1(kind="address", address="10.0.0.1"),),
                exclusions=(IPv4TargetExpressionV1(kind="address", address="10.0.0.1"),),
                max_hosts=4,
            )


class ProtocolPortContractTests(unittest.TestCase):
    def test_not_attempted_port_records_are_strict_and_capability_bound(self) -> None:
        self.assertEqual(
            IPNotAttemptedPortV1(
                port=47808,
                protocol="udp",
                source="expected",
                reason="unsupported_protocol",
                capability="use_bacnet_discovery",
            ).model_dump(mode="json", exclude_none=True),
            {
                "port": 47808,
                "protocol": "udp",
                "source": "expected",
                "reason": "unsupported_protocol",
                "capability": "use_bacnet_discovery",
            },
        )
        invalid = (
            {
                "port": 47808,
                "protocol": "udp",
                "source": "expected",
                "reason": "unsupported_protocol",
            },
            {
                "port": 80,
                "protocol": "tcp",
                "source": "expected",
                "reason": "profile_port_cap",
                "capability": "use_bacnet_discovery",
            },
            {
                "port": 80,
                "protocol": "tcp",
                "source": "operator",
                "reason": "profile_port_cap",
            },
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                IPNotAttemptedPortV1.model_validate(value)

    def test_text_spec_preserves_protocol_and_expands_ranges(self) -> None:
        self.assertEqual(
            parse_protocol_port_spec("443/tcp, 47808/udp, 8000-8002"),
            (
                ProtocolPortV1(port=443, protocol="tcp"),
                ProtocolPortV1(port=8000, protocol="tcp"),
                ProtocolPortV1(port=8001, protocol="tcp"),
                ProtocolPortV1(port=8002, protocol="tcp"),
                ProtocolPortV1(port=47808, protocol="udp"),
            ),
        )

    def test_text_spec_rejects_reversed_range_and_unknown_protocol(self) -> None:
        for value in ("10-1/tcp", "47808/sctp"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_protocol_port_spec(value)

    def test_protocol_is_preserved_and_output_is_deterministic(self) -> None:
        ports = normalize_protocol_ports(
            (
                ProtocolPortV1(port=47808, protocol="udp"),
                ProtocolPortV1(port=443, protocol="tcp"),
                ProtocolPortV1(port=443, protocol="tcp"),
            ),
            max_ports=8,
        )

        self.assertEqual(
            ports,
            (
                ProtocolPortV1(port=443, protocol="tcp"),
                ProtocolPortV1(port=47808, protocol="udp"),
            ),
        )

    def test_builtin_provider_rejects_udp_instead_of_scanning_it_as_tcp(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "use the BACnet discovery workflow for BACnet/IP UDP/47808",
        ):
            IPScanParametersV1(
                target_expressions=(
                    IPv4TargetExpressionV1(kind="address", address="10.0.0.1"),
                ),
                ports=(ProtocolPortV1(port=47808, protocol="udp"),),
            )

    def test_port_cap_and_invalid_port_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "2 protocol ports.*max_ports=1"):
            normalize_protocol_ports(
                (
                    ProtocolPortV1(port=80, protocol="tcp"),
                    ProtocolPortV1(port=443, protocol="tcp"),
                ),
                max_ports=1,
            )
        with self.assertRaises(ValidationError):
            ProtocolPortV1(port=0, protocol="tcp")


class DiscoveryRuntimePolicyContractTests(unittest.TestCase):
    def _gentle_policy(self) -> DiscoveryPolicyV1:
        return DiscoveryPolicyV1(
            profile="gentle",
            max_targets=256,
            max_protocol_ports_per_target=64,
            total_dispatch_attempt_ceiling=6_000,
            profile_max_concurrency=8,
            per_target_concurrency=1,
            profile_max_rate_limit_per_sec=10,
            min_target_spacing_ms=100,
            retries=0,
            retry_backoff_ms=250,
            dispatch_phase_seconds=2_400,
            cleanup_margin_seconds=300,
            run_deadline_seconds=2_700,
        )

    def test_effective_throttle_is_finite_positive_and_exactly_shaped(self) -> None:
        throttle = EffectiveThrottleV1(
            max_concurrency=8,
            rate_limit_per_sec=10,
            connect_timeout_s=1.5,
        )
        self.assertEqual(
            throttle.model_dump(mode="json"),
            {
                "max_concurrency": 8,
                "rate_limit_per_sec": 10.0,
                "connect_timeout_s": 1.5,
            },
        )

        invalid = (
            {"max_concurrency": 0, "rate_limit_per_sec": 1, "connect_timeout_s": 1},
            {"max_concurrency": 1, "rate_limit_per_sec": float("nan"), "connect_timeout_s": 1},
            {"max_concurrency": 1, "rate_limit_per_sec": 1, "connect_timeout_s": float("inf")},
            {"max_concurrency": 1, "rate_limit_per_sec": "1", "connect_timeout_s": 1},
            {"max_concurrency": 1, "rate_limit_per_sec": 1, "connect_timeout_s": 1, "extra": 1},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EffectiveThrottleV1.model_validate(value)

    def test_work_estimate_uses_rate_concurrency_timeout_and_target_spacing(self) -> None:
        estimate = estimate_discovery_work(
            base_dispatch_units_by_target={
                "10.0.0.1": 64,
                "10.0.0.2": 64,
            },
            policy=self._gentle_policy(),
            effective_throttle=EffectiveThrottleV1(
                max_concurrency=8,
                rate_limit_per_sec=10,
                connect_timeout_s=1.5,
            ),
            estimate_basis="exact_cartesian",
            register_added_dispatch_units=2,
        )

        self.assertEqual(estimate.known_initial_dispatch_units, 128)
        self.assertEqual(estimate.planned_dispatch_attempts, 128)
        self.assertEqual(estimate.derived_executable_attempt_ceiling, 3_200)
        self.assertEqual(estimate.optimistic_dispatch_seconds, 12.7)
        self.assertEqual(estimate.conservative_dispatch_seconds, 96.0)
        self.assertEqual(estimate.conservative_run_seconds, 396.0)
        self.assertEqual(estimate.required_authorization_window_seconds, 396)
        self.assertEqual(len(estimate.dispatch_plan_sha256), 64)
        self.assertEqual(len(estimate.dispatch_samples), 2)

    def test_policy_rejects_missing_extended_risk_acknowledgement(self) -> None:
        with self.assertRaisesRegex(ValidationError, "risk acknowledgement"):
            DiscoveryPolicyV1(
                profile="planned_extended",
                max_targets=4_096,
                max_protocol_ports_per_target=4_096,
                total_dispatch_attempt_ceiling=14_000,
                profile_max_concurrency=16,
                per_target_concurrency=2,
                profile_max_rate_limit_per_sec=10,
                min_target_spacing_ms=25,
                retries=1,
                retry_backoff_ms=250,
                dispatch_phase_seconds=2_700,
                cleanup_margin_seconds=300,
                run_deadline_seconds=3_000,
                risk_acknowledgement_required=True,
                risk_acknowledged=False,
            )


class BacnetParameterContractTests(unittest.TestCase):
    def test_expected_device_identity_is_typed_and_canonical(self) -> None:
        device = BacnetExpectedDeviceV1(
            internetwork_id=" plant-a ",
            device_instance="2001",
            source_address="10.20.0.21",
            network="20",
            asset_id=" ahu-1 ",
        )

        self.assertEqual(device.internetwork_id, "plant-a")
        self.assertEqual(device.device_instance, 2001)
        self.assertEqual(device.source_address, "10.20.0.21")
        self.assertEqual(device.network, 20)
        self.assertEqual(device.asset_id, "ahu-1")

    def test_expected_device_rejects_ipv6_and_out_of_range_instance(self) -> None:
        with self.assertRaises(ValidationError):
            BacnetExpectedDeviceV1(
                internetwork_id="plant-a",
                device_instance=2001,
                source_address="2001:db8::1",
            )
        with self.assertRaises(ValidationError):
            BacnetExpectedDeviceV1(
                internetwork_id="plant-a",
                device_instance=4_194_303,
                source_address="10.20.0.21",
            )

    def test_local_address_preserves_prefix_and_rejects_bare_ip(self) -> None:
        parameters = BacnetScanParametersV1(local_address="192.168.10.23/24")
        self.assertEqual(parameters.local_address, "192.168.10.23/24")
        with self.assertRaisesRegex(ValidationError, "prefix"):
            BacnetScanParametersV1(local_address="192.168.10.23")

    def test_lane_and_read_sets_are_frozen_in_canonical_order(self) -> None:
        parameters = BacnetScanParametersV1(
            lanes=("directed_unicast", "local_broadcast", "directed_unicast"),
            base_read_set=("units", "object_name", "present_value", "units"),
            authorized_property_ceiling=(
                "description",
                "units",
                "object_name",
                "present_value",
            ),
        )

        self.assertEqual(parameters.lanes, ("local_broadcast", "directed_unicast"))
        self.assertEqual(
            parameters.base_read_set,
            ("object_name", "present_value", "units"),
        )
        self.assertEqual(
            parameters.authorized_property_ceiling,
            ("object_name", "present_value", "units", "description"),
        )

    def test_base_read_set_must_fit_inside_authorized_ceiling(self) -> None:
        with self.assertRaisesRegex(ValidationError, "base_read_set.*ceiling"):
            BacnetScanParametersV1(
                base_read_set=("present_value", "reliability"),
                authorized_property_ceiling=("present_value",),
            )

    def test_foreign_device_lane_requires_bbmd_address(self) -> None:
        with self.assertRaisesRegex(ValidationError, "bbmd_address"):
            BacnetScanParametersV1(lanes=("foreign_device",))


class ImportAuthorityReferenceTests(unittest.TestCase):
    def test_reference_carries_bounded_digest_metadata_only(self) -> None:
        reference = ImportAuthorityReferenceV1(
            import_id="imp_123",
            import_type="ip_register",
            source_filename="register.csv",
            source_sha256="a" * 64,
            accepted_rows_sha256="b" * 64,
            accepted_count=25_000,
            rejected_count=3,
            authority_schema_version="1.0",
        )

        payload = reference.model_dump(mode="json")
        self.assertEqual(payload["accepted_count"], 25_000)
        self.assertNotIn("accepted_rows", payload)
        self.assertNotIn("stored_file_path", payload)

    def test_reference_rejects_bad_digest(self) -> None:
        with self.assertRaises(ValidationError):
            ImportAuthorityReferenceV1(
                import_id="imp_123",
                import_type="ip_register",
                source_filename="register.csv",
                source_sha256="short",
                accepted_rows_sha256="b" * 64,
                accepted_count=1,
                rejected_count=0,
                authority_schema_version="1.0",
            )

    def test_legacy_reference_records_that_original_source_digest_is_unavailable(self) -> None:
        reference = ImportAuthorityReferenceV1(
            import_id="legacy-import",
            import_type="bacnet_register",
            source_filename="legacy-register.csv",
            source_digest_kind="legacy_unavailable",
            accepted_rows_sha256="b" * 64,
            accepted_count=5,
            rejected_count=0,
            authority_schema_version="legacy-accepted-snapshot-1.0",
        )

        self.assertIsNone(reference.source_sha256)
        invalid = reference.model_dump()
        invalid["source_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValidationError, "cannot claim"):
            ImportAuthorityReferenceV1(**invalid)


if __name__ == "__main__":
    unittest.main()
