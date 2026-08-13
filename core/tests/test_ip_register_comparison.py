from __future__ import annotations

import unittest
from copy import deepcopy

from pydantic import ValidationError
from smart_commissioning_core.engines.ip import (
    IPHostComparisonInputV1,
    IPRegisterAuthorityRowV1,
    IPRegisterAuthorityV1,
    IPRegisterComparisonV1,
    ProviderIdentityEvidenceV1,
    build_ip_register_authority,
    compare_ip_register,
)
from smart_commissioning_core.run_context import canonical_sha256


def _authority(*rows: IPRegisterAuthorityRowV1) -> IPRegisterAuthorityV1:
    return IPRegisterAuthorityV1(
        import_id="imp_ip_register_001",
        accepted_rows_sha256="a" * 64,
        accepted_count=len(rows),
        rows=rows,
    )


def _row(
    row_number: int,
    device_key: str,
    expected_ip: str,
    *,
    asset_id: str | None = None,
    hostname: str | None = None,
    mac_address: str | None = None,
) -> IPRegisterAuthorityRowV1:
    return IPRegisterAuthorityRowV1(
        row_key=f"row:{row_number}",
        device_key=device_key,
        expected_ip=expected_ip,
        asset_id=asset_id,
        hostname=hostname,
        mac_address=mac_address,
    )


def _host(
    observed_ip: str,
    reachability_state: str,
    identity: ProviderIdentityEvidenceV1 | None = None,
) -> IPHostComparisonInputV1:
    return IPHostComparisonInputV1(
        observed_ip=observed_ip,
        reachability_state=reachability_state,
        identity_evidence=identity,
    )


class IPRegisterComparisonTests(unittest.TestCase):
    def test_import_rows_use_asset_id_then_asset_name_without_dropping_rows(self) -> None:
        rows = (
            {
                "Asset ID": "AHU-1",
                "Asset name": "Level 1 AHU",
                "Expected IP address": "192.0.2.10",
            },
            {
                "Asset ID": "",
                "Asset name": "Legacy panel",
                "Expected IP address": "192.0.2.20",
            },
        )
        authority = build_ip_register_authority(
            rows,
            import_id="imp_ip_register_001",
            accepted_rows_sha256=canonical_sha256(list(rows)),
        )

        self.assertEqual(
            tuple(row.device_key for row in authority.rows),
            ("AHU-1", "Legacy panel"),
        )
        self.assertEqual(authority.accepted_count, 2)
        self.assertIsNone(authority.rows[1].mac_address)
        legacy_identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="high",
            asset_id="legacy panel",
        )
        legacy_result = compare_ip_register(
            _host("192.0.2.99", "reachable", legacy_identity),
            authority=authority,
        )
        self.assertEqual(legacy_result.register_match, "wrong_ip_review")
        self.assertEqual(legacy_result.expected_ip, "192.0.2.20")
        self.assertEqual(legacy_result.matched_device_key, "Legacy panel")

    def test_selected_empty_authority_remains_configured(self) -> None:
        authority = IPRegisterAuthorityV1(
            import_id="imp_ip_register_empty",
            accepted_rows_sha256="0" * 64,
            accepted_count=0,
            rows=(),
        )

        result = compare_ip_register(
            _host("192.0.2.9", "reachable"),
            authority=authority,
        )

        self.assertEqual(result.register_match, "unregistered")
        self.assertEqual(result.authority_import_id, "imp_ip_register_empty")
        self.assertEqual(authority.expected_device_keys, ())

    def test_no_authority_is_explicitly_not_configured(self) -> None:
        result = compare_ip_register(
            _host("192.0.2.9", "reachable"),
            authority=None,
        )

        self.assertEqual(result.register_match, "not_configured")
        self.assertEqual(result.observed_ip, "192.0.2.9")
        self.assertEqual(result.expected_ips, ())
        self.assertEqual(result.candidate_device_keys, ())
        self.assertEqual(result.match_basis, ())

    def test_duplicate_expected_ip_requires_ambiguous_review(self) -> None:
        authority = _authority(
            _row(1, "AHU-1", "192.0.2.10", asset_id="AHU-1"),
            _row(2, "AHU-2", "192.0.2.10", asset_id="AHU-2"),
        )
        conflicting_identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="low",
            asset_id="AHU-2",
        )

        result = compare_ip_register(
            _host("192.0.2.10", "reachable", conflicting_identity),
            authority=authority,
        )

        self.assertEqual(result.register_match, "ambiguous_review")
        self.assertEqual(result.expected_ip, "192.0.2.10")
        self.assertEqual(result.match_basis, ("expected_ip",))
        self.assertEqual(result.candidate_device_keys, ("AHU-1", "AHU-2"))
        self.assertIsNone(result.matched_device_key)
        self.assertEqual(len(authority.rows), 2)
        with self.assertRaises(ValidationError):
            authority.accepted_count = 1  # type: ignore[misc]

    def test_private_indexes_retain_duplicate_source_order_without_serializing(self) -> None:
        authority = _authority(
            _row(1, "AHU-2", "192.0.2.10", asset_id="shared"),
            _row(2, "AHU-1", "192.0.2.10", asset_id="SHARED"),
            _row(3, "AHU-2", "192.0.2.11", asset_id="AHU-2"),
        )

        self.assertEqual(
            tuple(row.row_key for row in authority._rows_by_expected_ip["192.0.2.10"]),
            ("row:1", "row:2"),
        )
        self.assertEqual(
            tuple(row.row_key for row in authority._rows_by_device_key["AHU-2"]),
            ("row:1", "row:3"),
        )
        self.assertEqual(
            tuple(row.row_key for row in authority._rows_by_identity["asset_id"]["shared"]),
            ("row:1", "row:2"),
        )
        self.assertNotIn("_rows_by_expected_ip", authority.model_dump())
        self.assertEqual(deepcopy(authority), authority)
        self.assertEqual(authority.model_copy(deep=True), authority)

        result = compare_ip_register(
            _host("192.0.2.10", "reachable"),
            authority=authority,
        )
        self.assertEqual(result.candidate_device_keys, ("AHU-1", "AHU-2"))

    def test_unique_high_confidence_identity_creates_wrong_ip_review(self) -> None:
        authority = _authority(
            _row(
                1,
                "AHU-1",
                "192.0.2.10",
                asset_id="AHU-1",
                hostname="ahu-l03-017",
                mac_address="00:11:22:33:44:55",
            ),
        )
        evidence = ProviderIdentityEvidenceV1(
            evidence_kind="protocol_identity",
            confidence="high",
            asset_id="ahu-1",
            hostname="AHU-L03-017.",
            mac_address="00-11-22-33-44-55",
            corroborating_fields=("asset_id", "hostname", "mac_address"),
        )

        result = compare_ip_register(
            _host("192.0.2.99", "reachable", evidence),
            authority=authority,
        )

        self.assertEqual(result.register_match, "wrong_ip_review")
        self.assertEqual(result.observed_ip, "192.0.2.99")
        self.assertEqual(result.expected_ip, "192.0.2.10")
        self.assertEqual(result.expected_ips, ("192.0.2.10",))
        self.assertEqual(result.matched_device_key, "AHU-1")
        self.assertEqual(
            result.match_basis,
            ("asset_id", "hostname", "mac_address"),
        )

    def test_weak_duplicate_and_conflicting_identity_require_review(self) -> None:
        authority = _authority(
            _row(1, "AHU-1", "192.0.2.10", asset_id="AHU-1", hostname="shared"),
            _row(2, "AHU-2", "192.0.2.20", asset_id="AHU-2", hostname="shared"),
        )
        cases = (
            ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="medium",
                asset_id="AHU-1",
            ),
            ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                hostname="shared",
            ),
            ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                asset_id="AHU-1",
                hostname="shared",
                corroborating_fields=("asset_id", "hostname"),
            ),
            ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                asset_id="AHU-1",
                hostname="missing-from-register",
                corroborating_fields=("asset_id", "hostname"),
            ),
        )

        for evidence in cases:
            with self.subTest(evidence=evidence):
                result = compare_ip_register(
                    _host("192.0.2.99", "reachable", evidence),
                    authority=authority,
                )
                self.assertEqual(result.register_match, "ambiguous_review")
                self.assertEqual(result.observed_ip, "192.0.2.99")

    def test_unconfirmed_target_never_claims_wrong_ip(self) -> None:
        authority = _authority(
            _row(1, "AHU-1", "192.0.2.10", asset_id="AHU-1"),
        )
        identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="high",
            asset_id="AHU-1",
        )

        result = compare_ip_register(
            _host("192.0.2.99", "unconfirmed", identity),
            authority=authority,
        )

        self.assertEqual(result.register_match, "unregistered")
        self.assertIsNone(result.expected_ip)
        self.assertEqual(result.candidate_device_keys, ())

    def test_unknown_high_confidence_identity_remains_unregistered(self) -> None:
        authority = _authority(
            _row(1, "AHU-1", "192.0.2.10", asset_id="AHU-1"),
        )
        identity = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="high",
            asset_id="UNKNOWN-9",
        )

        result = compare_ip_register(
            _host("192.0.2.99", "reachable", identity),
            authority=authority,
        )

        self.assertEqual(result.register_match, "unregistered")

    def test_authority_rejects_duplicate_row_keys_and_extra_fields(self) -> None:
        row = _row(1, "AHU-1", "192.0.2.10", asset_id="AHU-1")
        with self.assertRaisesRegex(ValidationError, "row_key"):
            _authority(row, row)
        with self.assertRaises(ValidationError):
            IPRegisterAuthorityRowV1.model_validate(
                {
                    **row.model_dump(),
                    "unexpected": "discarding this would weaken the authority",
                }
            )

    def test_authority_digest_binds_the_exact_raw_row_snapshot(self) -> None:
        rows = [
            {"Asset ID": "AHU-1", "Expected IP address": "192.0.2.10"},
            {"Asset ID": "AHU-2", "Expected IP address": "192.0.2.20"},
        ]
        digest = canonical_sha256(rows)

        self.assertEqual(
            build_ip_register_authority(
                rows,
                import_id="imp_ip_register_001",
                accepted_rows_sha256=digest,
            ).accepted_rows_sha256,
            digest,
        )
        for changed in (
            list(reversed(rows)),
            rows[:1],
            [{**rows[0], "Asset ID": "AHU-X"}, rows[1]],
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                ValueError,
                "accepted-row digest verification failed",
            ):
                build_ip_register_authority(
                    changed,
                    import_id="imp_ip_register_001",
                    accepted_rows_sha256=digest,
                )

        empty = build_ip_register_authority(
            [],
            import_id="imp_ip_register_empty",
            accepted_rows_sha256=canonical_sha256([]),
        )
        self.assertEqual(empty.rows, ())

    def test_identity_and_comparison_strings_use_viewer_safe_4096_char_contract(
        self,
    ) -> None:
        long_identity = "A" * 4_096
        rows = [
            {
                "Asset ID": long_identity,
                "Asset name": "Plant room controller",
                "Expected IP address": "192.0.2.10",
            }
        ]
        authority = build_ip_register_authority(
            rows,
            import_id="imp_ip_register_long",
            accepted_rows_sha256=canonical_sha256(rows),
        )
        evidence = ProviderIdentityEvidenceV1(
            evidence_kind="approved_provider",
            confidence="high",
            asset_id=long_identity,
        )

        comparison = compare_ip_register(
            _host("192.0.2.99", "reachable", evidence),
            authority=authority,
        )
        self.assertEqual(comparison.matched_device_key, long_identity)
        self.assertEqual(comparison.candidate_device_keys, (long_identity,))

        malicious_values = (
            r"C:\Users\operator\scan.xml",
            "/tmp/scan.xml",
            "password=hunter2",
            "<device secret='x'/>",
            "line\x00break",
        )
        for malicious in malicious_values:
            with self.subTest(malicious=malicious), self.assertRaises(ValidationError):
                ProviderIdentityEvidenceV1(
                    evidence_kind="approved_provider",
                    confidence="high",
                    asset_id=malicious,
                )
            with self.assertRaises(ValidationError):
                IPRegisterAuthorityRowV1(
                    row_key="row:1",
                    device_key=malicious,
                    expected_ip="192.0.2.10",
                    asset_id=malicious,
                )

        with self.assertRaises(ValidationError):
            ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                asset_id="A" * 4_097,
            )
        with self.assertRaises(ValidationError):
            IPRegisterComparisonV1(
                register_match="ambiguous_review",
                observed_ip="192.0.2.99",
                candidate_device_keys=tuple(f"device-{index}" for index in range(4_097)),
                reason_code="identity_duplicate",
                authority_import_id="imp_ip_register_many",
                authority_rows_sha256="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
