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

        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(
            summary["asset_metrics"],
            {
                "expected": 3,
                "observed": 2,
                "not_observed": 1,
                "with_issues": 2,
                "successfully_validated": 0,
            },
        )
        self.assertEqual(
            summary["payload_metrics"],
            {
                "expected": 5,
                "received": 3,
                "with_issues": 4,
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
                "with_issues": 0,
                "successfully_validated": 1,
            },
        )
        payloads = summary["asset_results"][0]["payload_results"]
        unexpected = next(row for row in payloads if row["payload_type"] == "metadata")
        self.assertFalse(unexpected["expected"])
        self.assertTrue(unexpected["received"])

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
