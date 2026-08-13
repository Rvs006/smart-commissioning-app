from __future__ import annotations

import unittest

from pydantic import ValidationError
from smart_commissioning_core.engines.ip import (
    IPHeadlineMetricScopeV1,
    IPHostComparisonInputV1,
    IPHostMetricStateV1,
    IPRegisterAuthorityRowV1,
    IPRegisterAuthorityV1,
    ProviderIdentityEvidenceV1,
    compare_ip_register,
    reduce_ip_headline_metrics,
)


def _authority(*rows: IPRegisterAuthorityRowV1) -> IPRegisterAuthorityV1:
    return IPRegisterAuthorityV1(
        import_id="imp_ip_register_001",
        accepted_rows_sha256="b" * 64,
        accepted_count=len(rows),
        rows=rows,
    )


def _row(row_number: int, device_key: str, expected_ip: str) -> IPRegisterAuthorityRowV1:
    return IPRegisterAuthorityRowV1(
        row_key=f"row:{row_number}",
        device_key=device_key,
        expected_ip=expected_ip,
        asset_id=device_key,
    )


def _comparison(
    target: str,
    *,
    authority: IPRegisterAuthorityV1 | None,
    reachability: str = "reachable",
    identity_asset_id: str | None = None,
):
    identity = None
    if identity_asset_id is not None:
        identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="high",
            asset_id=identity_asset_id,
        )
    return compare_ip_register(
        IPHostComparisonInputV1(
            observed_ip=target,
            reachability_state=reachability,
            identity_evidence=identity,
        ),
        authority=authority,
    )


def _state(
    target: str,
    version: int,
    lifecycle: str,
    outcomes: tuple[str, ...],
    *,
    authority: IPRegisterAuthorityV1 | None,
    comparison_target_reachability: str | None = None,
    identity_asset_id: str | None = None,
) -> IPHostMetricStateV1:
    if comparison_target_reachability is None:
        comparison_target_reachability = (
            "reachable"
            if lifecycle == "finalized"
            and any(item in {"connected", "connection_refused"} for item in outcomes)
            else "unconfirmed"
        )
    return IPHostMetricStateV1(
        target_ip=target,
        entity_version=version,
        lifecycle_state=lifecycle,
        probe_outcomes=outcomes,
        comparison=_comparison(
            target,
            authority=authority,
            reachability=comparison_target_reachability,
            identity_asset_id=identity_asset_id,
        ),
    )


class IPHeadlineMetricsTests(unittest.TestCase):
    def test_selected_empty_authority_is_configured_with_zero_denominators(self) -> None:
        authority = IPRegisterAuthorityV1(
            import_id="imp_ip_register_empty",
            accepted_rows_sha256="0" * 64,
            accepted_count=0,
            rows=(),
        )
        scope = IPHeadlineMetricScopeV1(
            frozen_targets=("192.0.2.1",),
            authority=authority,
        )

        result = reduce_ip_headline_metrics(scope, ())

        for metric in (result.expected_devices, result.register_matches):
            self.assertTrue(metric.configured)
            self.assertEqual(metric.value, 0)
            self.assertEqual(metric.denominator, 0)
            self.assertIsNone(metric.percentage)
            self.assertEqual(metric.pending_count, 0)
            self.assertEqual(metric.finalized_count, 0)
        self.assertTrue(result.unexpected_unregistered_hosts.configured)
        self.assertEqual(result.unexpected_unregistered_hosts.denominator, 1)

    def test_no_authority_keeps_only_reachability_configured(self) -> None:
        scope = IPHeadlineMetricScopeV1(
            frozen_targets=("192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"),
            authority=None,
        )
        states = (
            _state("192.0.2.1", 1, "finalized", ("connected",), authority=None),
            _state("192.0.2.2", 1, "finalized", ("timed_out",), authority=None),
            _state("192.0.2.3", 1, "cancelled", ("cancelled",), authority=None),
        )

        result = reduce_ip_headline_metrics(scope, states)

        self.assertEqual(
            tuple(metric.heading for metric in result.metrics),
            (
                "Expected Devices",
                "Reachable Devices",
                "Register Matches",
                "Unexpected / Unregistered Hosts",
            ),
        )
        expected, reachable, matches, unexpected = result.metrics
        for metric in (expected, matches, unexpected):
            self.assertFalse(metric.configured)
            self.assertIsNone(metric.value)
            self.assertIsNone(metric.denominator)
            self.assertIsNone(metric.percentage)
            self.assertIsNone(metric.pending_count)
            self.assertIsNone(metric.finalized_count)
        self.assertTrue(reachable.configured)
        self.assertEqual(reachable.value, 1)
        self.assertEqual(reachable.denominator, 4)
        self.assertEqual(reachable.percentage, 25.0)
        self.assertEqual(reachable.pending_count, 1)
        self.assertEqual(reachable.finalized_count, 3)

    def test_latest_final_host_state_controls_reachability(self) -> None:
        targets = tuple(f"192.0.2.{value}" for value in range(1, 7))
        scope = IPHeadlineMetricScopeV1(frozen_targets=targets, authority=None)
        states = (
            _state("192.0.2.1", 1, "pending", ("connected",), authority=None),
            _state("192.0.2.1", 2, "finalized", ("connected",), authority=None),
            _state(
                "192.0.2.2",
                1,
                "finalized",
                ("connection_refused",),
                authority=None,
            ),
            _state("192.0.2.3", 1, "finalized", ("timed_out",), authority=None),
            _state(
                "192.0.2.4",
                1,
                "finalized",
                ("network_unreachable",),
                authority=None,
            ),
            _state("192.0.2.5", 1, "cancelled", ("cancelled",), authority=None),
            _state("192.0.2.6", 1, "not_attempted", (), authority=None),
        )

        result = reduce_ip_headline_metrics(scope, tuple(reversed(states)))
        reachable = result.reachable_devices

        self.assertEqual(reachable.value, 1)
        self.assertEqual(reachable.denominator, 6)
        self.assertEqual(reachable.percentage, 16.67)
        self.assertEqual(reachable.pending_count, 0)
        self.assertEqual(reachable.finalized_count, 6)

    def test_authority_metrics_dedupe_devices_and_count_review_hosts(self) -> None:
        authority = _authority(
            _row(1, "DEVICE-A", "192.0.2.10"),
            _row(2, "DEVICE-A", "192.0.2.11"),
            _row(3, "DEVICE-B", "192.0.2.20"),
        )
        targets = (
            "192.0.2.10",
            "192.0.2.11",
            "192.0.2.20",
            "192.0.2.97",
            "192.0.2.98",
            "192.0.2.99",
        )
        scope = IPHeadlineMetricScopeV1(frozen_targets=targets, authority=authority)
        ambiguous_authority = _authority(
            *authority.rows,
            _row(4, "DEVICE-C", "192.0.2.30"),
        )
        ambiguous_identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="medium",
            asset_id="DEVICE-A",
        )
        ambiguous = compare_ip_register(
            IPHostComparisonInputV1(
                observed_ip="192.0.2.97",
                reachability_state="reachable",
                identity_evidence=ambiguous_identity,
            ),
            authority=ambiguous_authority,
        )
        states = (
            _state("192.0.2.10", 1, "finalized", ("connected",), authority=authority),
            _state(
                "192.0.2.11",
                1,
                "finalized",
                ("connection_refused",),
                authority=authority,
            ),
            _state("192.0.2.20", 1, "finalized", ("timed_out",), authority=authority),
            IPHostMetricStateV1(
                target_ip="192.0.2.97",
                entity_version=1,
                lifecycle_state="finalized",
                probe_outcomes=("connected",),
                comparison=ambiguous,
            ),
            _state(
                "192.0.2.98",
                1,
                "finalized",
                ("connected",),
                authority=authority,
                identity_asset_id="DEVICE-B",
            ),
            _state("192.0.2.99", 1, "finalized", ("connected",), authority=authority),
        )

        result = reduce_ip_headline_metrics(scope, states)

        self.assertEqual(result.expected_devices.value, 2)
        self.assertEqual(result.expected_devices.denominator, 2)
        self.assertEqual(result.expected_devices.percentage, 100.0)
        self.assertEqual(result.register_matches.value, 1)
        self.assertEqual(result.register_matches.denominator, 2)
        self.assertEqual(result.register_matches.percentage, 50.0)
        self.assertEqual(result.reachable_devices.value, 4)
        self.assertEqual(result.reachable_devices.percentage, 66.67)
        self.assertEqual(result.unexpected_unregistered_hosts.value, 3)
        self.assertEqual(result.unexpected_unregistered_hosts.percentage, 50.0)

    def test_pending_and_finalized_counts_use_frozen_denominators(self) -> None:
        authority = _authority(
            _row(1, "DEVICE-A", "192.0.2.10"),
            _row(2, "DEVICE-B", "192.0.2.20"),
        )
        scope = IPHeadlineMetricScopeV1(
            frozen_targets=("192.0.2.10", "192.0.2.20", "192.0.2.99"),
            authority=authority,
        )
        states = (
            _state("192.0.2.10", 1, "finalized", ("connected",), authority=authority),
            _state("192.0.2.20", 1, "pending", (), authority=authority),
        )

        result = reduce_ip_headline_metrics(scope, states)

        self.assertEqual(result.expected_devices.pending_count, 0)
        self.assertEqual(result.expected_devices.finalized_count, 2)
        self.assertEqual(result.register_matches.pending_count, 1)
        self.assertEqual(result.register_matches.finalized_count, 1)
        self.assertEqual(result.reachable_devices.pending_count, 2)
        self.assertEqual(result.reachable_devices.finalized_count, 1)
        self.assertEqual(result.unexpected_unregistered_hosts.pending_count, 2)
        self.assertEqual(result.unexpected_unregistered_hosts.finalized_count, 1)

    def test_device_progress_deduplicates_in_scope_targets_once(self) -> None:
        authority = _authority(
            _row(1, "DEVICE-A", "192.0.2.10"),
            _row(2, "DEVICE-A", "192.0.2.10"),
            _row(3, "DEVICE-A", "192.0.2.11"),
            _row(4, "DEVICE-B", "192.0.2.20"),
        )
        scope = IPHeadlineMetricScopeV1(
            frozen_targets=("192.0.2.10", "192.0.2.99"),
            authority=authority,
        )
        states = (
            _state("192.0.2.10", 1, "finalized", ("connected",), authority=authority),
        )

        result = reduce_ip_headline_metrics(scope, states)

        self.assertEqual(result.expected_devices.value, 2)
        self.assertEqual(result.register_matches.value, 1)
        self.assertEqual(result.register_matches.finalized_count, 2)
        self.assertEqual(result.register_matches.pending_count, 0)

    def test_conflicting_latest_state_and_invalid_not_attempted_state_fail(self) -> None:
        scope = IPHeadlineMetricScopeV1(frozen_targets=("192.0.2.1",), authority=None)
        first = _state("192.0.2.1", 1, "finalized", ("connected",), authority=None)
        conflict = _state("192.0.2.1", 1, "finalized", ("timed_out",), authority=None)

        with self.assertRaisesRegex(ValueError, "conflicting metric state"):
            reduce_ip_headline_metrics(scope, (first, conflict))
        with self.assertRaises(ValidationError):
            IPHostMetricStateV1(
                target_ip="192.0.2.1",
                entity_version=1,
                lifecycle_state="not_attempted",
                probe_outcomes=("connected",),
                comparison=_comparison("192.0.2.1", authority=None),
            )


if __name__ == "__main__":
    unittest.main()
