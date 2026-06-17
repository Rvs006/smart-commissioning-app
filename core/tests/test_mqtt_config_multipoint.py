"""Unit tests for multi-point config-publish confirmation.

HONESTY: there is NO real MQTT broker here. These tests drive
validate_and_publish_config against a FAKE captured pointset supplied via
parameters['next_pointset_payload'] (the same local-verification path the
engine uses when no live broker is contacted). The live multi-point broker
confirmation is on-site-untested and listed in the task's live_untested output.
"""

import json
import unittest

from smart_commissioning_core.mqtt_config_publish import validate_and_publish_config


def _captured_pointset(points: dict[str, object]) -> dict[str, object]:
    return {"pointset": {"points": {name: {"present_value": value} for name, value in points.items()}}}


class ExpectedPointsListTests(unittest.TestCase):
    def test_all_points_match_succeeds(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_points": [
                    {"point": "sat", "value": 18},
                    {"point": "fan", "value": "on"},
                ],
                "next_pointset_payload": _captured_pointset({"sat": 18, "fan": "on"}),
            }
        )
        self.assertEqual(result.issues, [])
        summary = result.result_summary
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["expected_point_count"], 2)
        self.assertEqual(summary["matched_point_count"], 2)
        self.assertFalse(summary["partial_confirm"])
        checks = {c["point"]: c for c in summary["point_checks"]}
        self.assertTrue(checks["sat"]["matched"])
        self.assertTrue(checks["fan"]["matched"])

    def test_one_mismatch_reports_that_points_issue_and_partial(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_points": [
                    {"point": "sat", "value": 18},
                    {"point": "fan", "value": "on"},
                ],
                "next_pointset_payload": _captured_pointset({"sat": 18, "fan": "off"}),
            }
        )
        # Exactly one issue, naming the mismatched point only.
        override_issues = [i for i in result.issues if i.issue_type == "config_override_not_observed"]
        self.assertEqual(len(override_issues), 1)
        self.assertEqual(override_issues[0].point_name, "fan")
        self.assertEqual(override_issues[0].observed_value, "off")
        self.assertEqual(override_issues[0].expected_value, "on")

        summary = result.result_summary
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["expected_point_count"], 2)
        self.assertEqual(summary["matched_point_count"], 1)
        self.assertTrue(summary["partial_confirm"])

    def test_missing_point_in_pointset_is_a_mismatch(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_points": [{"point": "sat", "value": 18}, {"point": "ghost", "value": 5}],
                "next_pointset_payload": _captured_pointset({"sat": 18}),
            }
        )
        ghost = next(i for i in result.issues if i.point_name == "ghost")
        self.assertEqual(ghost.observed_value, "missing")
        self.assertEqual(result.result_summary["status"], "failed")


class DerivedFromPayloadTests(unittest.TestCase):
    def test_expected_derived_from_published_set_values(self) -> None:
        payload = json.dumps(
            {"pointset": {"points": {"sat": {"set_value": 21}, "fan": {"set_value": "on"}}}}
        )
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": payload,
                "confirmed": True,
                # No explicit expected_points: derive from the published set_values.
                "next_pointset_payload": _captured_pointset({"sat": 21, "fan": "on"}),
            }
        )
        self.assertEqual(result.issues, [])
        summary = result.result_summary
        self.assertEqual(summary["expected_point_count"], 2)
        self.assertEqual(summary["matched_point_count"], 2)

    def test_derived_mismatch_flags_the_point(self) -> None:
        payload = json.dumps(
            {"pointset": {"points": {"sat": {"set_value": 21}, "fan": {"set_value": "on"}}}}
        )
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": payload,
                "confirmed": True,
                "next_pointset_payload": _captured_pointset({"sat": 21, "fan": "off"}),
            }
        )
        mismatches = [i for i in result.issues if i.issue_type == "config_override_not_observed"]
        self.assertEqual([m.point_name for m in mismatches], ["fan"])
        self.assertEqual(result.result_summary["status"], "failed")


class SinglePointBackCompatTests(unittest.TestCase):
    def test_single_point_match_unchanged(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_point": "sat",
                "expected_value": 18,
                "next_pointset_payload": _captured_pointset({"sat": 18}),
            }
        )
        self.assertEqual(result.issues, [])
        summary = result.result_summary
        # Legacy summary fields preserved.
        self.assertEqual(summary["expected_point"], "sat")
        self.assertEqual(summary["expected_value"], 18)
        self.assertEqual(summary["observed_value"], 18)
        self.assertEqual(summary["expected_point_count"], 1)
        self.assertEqual(summary["matched_point_count"], 1)
        self.assertFalse(summary["partial_confirm"])

    def test_single_point_mismatch_unchanged(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_point": "sat",
                "expected_value": 18,
                "next_pointset_payload": _captured_pointset({"sat": 25}),
            }
        )
        override = [i for i in result.issues if i.issue_type == "config_override_not_observed"]
        self.assertEqual(len(override), 1)
        self.assertEqual(override[0].point_name, "sat")
        summary = result.result_summary
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["expected_point"], "sat")
        self.assertEqual(summary["observed_value"], 25)

    def test_no_expectation_no_pointset_produces_no_override_issue(self) -> None:
        # A fire-and-forget publish with set_values but no captured pointset
        # must NOT manufacture spurious mismatches (back-compat).
        payload = json.dumps({"pointset": {"points": {"sat": {"set_value": 21}}}})
        result = validate_and_publish_config(
            {"topic": "site/ahu-1/config", "payload": payload, "confirmed": True}
        )
        self.assertEqual(
            [i for i in result.issues if i.issue_type == "config_override_not_observed"], []
        )
        self.assertEqual(result.result_summary["expected_point_count"], 0)


class NoCredentialLeakTests(unittest.TestCase):
    def test_point_names_and_values_only_no_credentials(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "username": "admin",
                "password": "hunter2",
                "expected_points": [{"point": "sat", "value": 18}],
                "next_pointset_payload": _captured_pointset({"sat": 99}),
            }
        )
        serialized = json.dumps(
            {"summary": result.result_summary, "issues": [i.__dict__ for i in result.issues]},
            default=str,
        )
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("admin", serialized)


if __name__ == "__main__":
    unittest.main()
