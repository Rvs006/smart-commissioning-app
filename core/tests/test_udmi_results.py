import unittest

from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_results import (
    build_validation_summary_v1,
    fault_category_for_issue,
)


def _issue(
    issue_id: str,
    asset_id: str,
    issue_type: str,
    severity: str,
    description: str,
    *,
    point_name: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
) -> ValidationIssueRecord:
    return ValidationIssueRecord(
        issue_id=issue_id,
        asset_id=asset_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
        point_name=point_name,
        expected_value=expected_value,
        observed_value=observed_value,
    )


class UdmiResultsTests(unittest.TestCase):
    def test_metrics_group_real_systems_and_keep_observation_wording(self) -> None:
        parameters = {
            "assets": [
                {
                    "expected_schedule": {"asset_id": "A-1", "system": "BMS"},
                    "state_topic": "site/a-1/state",
                    "metadata_topic": "site/a-1/metadata",
                    "pointset_topic": "site/a-1/events/pointset",
                    "state_payload": {"timestamp": "2026-07-20T10:00:00Z"},
                    "state_payload_received_at": "2026-07-20T10:00:01+00:00",
                    "metadata_payload": {"timestamp": "2026-07-20T10:00:02Z"},
                },
                {
                    "expected_schedule": {"asset_id": "A-2", "system": "Lighting"},
                    "state_topic": "site/a-2/state",
                    # A malformed body still counts as received evidence.
                    "messages": [
                        {
                            "topic": "site/a-2/state",
                            "payload": None,
                            "received_at": "2026-07-20T10:01:00+00:00",
                        }
                    ],
                },
                {
                    "expected_schedule": {"asset_id": "A-3", "system": ""},
                    "metadata_topic": "site/a-3/metadata",
                },
            ]
        }
        issues = [
            _issue(
                "UDMI-ST-1",
                "A-1",
                "state_validation",
                "high",
                "State payload has an invalid field type.",
            ),
            _issue(
                "UDMI-PS-2",
                "A-1",
                "pointset_validation",
                "high",
                "Expected point supply_temp was not received in the pointset payload.",
                point_name="supply_temp",
                expected_value="supply_temp",
                observed_value="missing",
            ),
            _issue(
                "UDMI-MD-3",
                "A-1",
                "metadata_validation",
                "low",
                "Metadata defines point spare that is not in the expected schedule.",
                point_name="spare",
                expected_value="not in register",
                observed_value="spare",
            ),
            _issue(
                "UDMI-PL-4",
                "A-2",
                "payload_error",
                "critical",
                "The state payload is not a JSON object.",
            ),
        ]

        summary = build_validation_summary_v1(parameters, issues)

        self.assertEqual(summary["schema_version"], "1.1")
        self.assertEqual(
            summary["asset_metrics"],
            {
                "expected": 3,
                "observed": 2,
                "not_observed": 1,
                "with_issues": 2,
                "successfully_validated": 0,
                "unexpected": 0,
            },
        )
        self.assertEqual(
            summary["payload_metrics"],
            {
                "expected": 5,
                "received": 3,
                "not_received": 2,
                "with_issues": 3,
                "successfully_validated": 1,
            },
        )
        self.assertEqual(
            summary["fault_metrics"],
            {
                "payload_formatting_issues": 2,
                "missing_points": 1,
                "point_naming_issues": 0,
                "additional_points": 1,
                "stale_or_cadence": 0,
                "other_issues": 0,
            },
        )
        self.assertEqual(summary["issue_metrics"], {"blocking": 3, "warning": 1})
        self.assertEqual(
            [row["system"] for row in summary["system_metrics"]],
            ["BMS", "Lighting", "Unspecified"],
        )
        asset_three = next(row for row in summary["asset_results"] if row["asset_id"] == "A-3")
        self.assertFalse(asset_three["observed"])
        self.assertNotIn("online", str(summary).casefold())
        self.assertNotIn("offline", str(summary).casefold())

    def test_empty_json_object_is_received_not_missing(self) -> None:
        summary = build_validation_summary_v1(
            {
                "expected_schedule": {"asset_id": "A-1", "system": "BMS"},
                "state_payload": {},
            },
            [],
        )
        state = next(
            row
            for row in summary["asset_results"][0]["payload_results"]
            if row["payload_type"] == "state"
        )
        self.assertTrue(state["received"])

    def test_unexpected_payload_remains_evidence_without_inflating_coverage(self) -> None:
        summary = build_validation_summary_v1(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "A-1", "system": "BMS"},
                        "state_topic": "site/a-1/state",
                        "state_payload": {"timestamp": "2026-07-20T10:00:00Z"},
                        "metadata_payload": "not-json",
                    }
                ]
            },
            [],
        )

        self.assertEqual(
            summary["payload_metrics"],
            {
                "expected": 1,
                "received": 1,
                "not_received": 0,
                "with_issues": 0,
                "successfully_validated": 1,
            },
        )
        payloads = summary["asset_results"][0]["payload_results"]
        unexpected = next(row for row in payloads if row["payload_type"] == "metadata")
        self.assertFalse(unexpected["expected"])
        self.assertTrue(unexpected["received"])

    def test_only_received_expected_payloads_make_a_registered_asset_observed(self) -> None:
        summary = build_validation_summary_v1(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "A-1", "system": "BMS"},
                        "state_topic": "site/a-1/state",
                        "metadata_payload": {"timestamp": "2026-07-23T10:00:00Z"},
                        "metadata_payload_received_at": "2026-07-23T10:00:01Z",
                    }
                ]
            },
            [],
        )

        asset = summary["asset_results"][0]
        self.assertFalse(asset["observed"])
        self.assertEqual(asset["expected_payloads"], 1)
        self.assertEqual(asset["received_payloads"], 0)
        self.assertFalse(asset["all_received_payloads_successfully_validated"])
        self.assertIsNone(asset["last_observed_at"])
        self.assertEqual(summary["asset_metrics"]["observed"], 0)
        self.assertEqual(summary["asset_metrics"]["not_observed"], 1)
        self.assertEqual(summary["payload_metrics"]["received"], 0)
        self.assertEqual(
            [
                (row["payload_type"], row["expected"], row["received"])
                for row in asset["payload_results"]
            ],
            [("state", True, False), ("metadata", False, True)],
        )

    def test_last_observed_at_uses_the_latest_rfc3339_instant(self) -> None:
        summary = build_validation_summary_v1(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "A-1", "system": "BMS"},
                        "state_topic": "site/a-1/state",
                        "metadata_topic": "site/a-1/metadata",
                        "state_payload": {"timestamp": "2026-07-23T10:00:00+01:00"},
                        "state_payload_received_at": "2026-07-23T10:00:00+01:00",
                        "metadata_payload": {"timestamp": "2026-07-23T09:30:00Z"},
                        "metadata_payload_received_at": "2026-07-23T09:30:00Z",
                    }
                ]
            },
            [],
        )

        self.assertEqual(
            summary["asset_results"][0]["last_observed_at"],
            "2026-07-23T09:30:00Z",
        )

    def test_unexpected_devices_are_separate_versioned_supporting_evidence(self) -> None:
        summary = build_validation_summary_v1(
            {
                "unexpected_devices": [
                    {
                        "id": "rogue-2",
                        "topic_root": "site/rogue-2",
                        "topics": ["site/rogue-2/state"],
                        "last_seen": "2026-07-23T10:00:00+00:00",
                    },
                    {
                        "id": "rogue-1",
                        "topic_root": "site/rogue-1",
                        "topics": ["site/rogue-1/state"],
                        "last_seen": "2026-07-23T09:00:00+00:00",
                    },
                ],
                "unexpected_devices_measured": True,
                "unexpected_devices_measurement_scope": "site/#",
            },
            [],
        )
        self.assertEqual(summary["schema_version"], "1.1")
        self.assertEqual(summary["asset_metrics"]["expected"], 0)
        self.assertEqual(summary["asset_metrics"]["unexpected"], 2)
        self.assertEqual(
            [row["id"] for row in summary["unexpected_devices"]],
            ["rogue-1", "rogue-2"],
        )
        self.assertTrue(summary["unexpected_devices_measured"])
        self.assertEqual(summary["unexpected_devices_measurement_scope"], "site/#")

    def test_legacy_unexpected_issue_cannot_enter_validation_metrics(self) -> None:
        summary = build_validation_summary_v1(
            {
                "unexpected_devices": [
                    {
                        "id": "rogue-1",
                        "topic_root": "site/rogue-1",
                        "topics": ["site/rogue-1/state"],
                        "last_seen": "2026-07-23T09:00:00+00:00",
                    }
                ],
                "unexpected_devices_measured": True,
                "unexpected_devices_measurement_scope": "site/#",
            },
            [
                _issue(
                    "UDMI-UNEXPECTED-0001",
                    "rogue-1",
                    "unexpected_device",
                    "high",
                    "Legacy unexpected publisher finding.",
                )
            ],
        )

        self.assertEqual(summary["asset_metrics"]["unexpected"], 1)
        self.assertEqual(summary["fault_rows"], [])
        self.assertEqual(summary["issue_metrics"], {"blocking": 0, "warning": 0})
        self.assertTrue(all(value == 0 for value in summary["fault_metrics"].values()))

    def test_point_fault_categories_are_distinct(self) -> None:
        misnamed = _issue(
            "one",
            "A",
            "pointset_validation",
            "high",
            "Expected point phase_1 was not received; the pointset instead carries the similarly named phase1.",
            point_name="phase_1",
            expected_value="phase_1",
            observed_value="phase1",
        )
        additional = _issue(
            "two",
            "A",
            "pointset_validation",
            "medium",
            "Received point spare was not found in the expected schedule.",
            point_name="spare",
            expected_value="not in register",
            observed_value="spare",
        )
        cadence = _issue(
            "three",
            "A",
            "pointset_timestamp",
            "high",
            "Pointset payload exceeds the expected reporting interval.",
        )
        self.assertEqual(fault_category_for_issue(misnamed), "point_naming_issues")
        self.assertEqual(fault_category_for_issue(additional), "additional_points")
        self.assertEqual(fault_category_for_issue(cadence), "stale_or_cadence")


if __name__ == "__main__":
    unittest.main()
