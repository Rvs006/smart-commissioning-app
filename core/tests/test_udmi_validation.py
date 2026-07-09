"""UDMI workbench validation: schema-version match, structural checks, unit
equality, and metadata point coverage.

Honesty contract: the validator never fabricates results — a version mismatch,
missing version, or unknown ruleset is reported as an explicit issue, and a
skipped structural check is never presented as a pass.
"""

import unittest

from smart_commissioning_core.udmi_schema import declared_version, structural_issues, versions_match
from smart_commissioning_core.udmi_validation import _capture_topics, validate_udmi_full_report


def _issues(parameters: dict) -> list:
    return validate_udmi_full_report(parameters, live_capture=None).issues


def _descriptions(parameters: dict) -> str:
    return " ".join(issue.description for issue in _issues(parameters))


def _schedule(**overrides: object) -> dict:
    schedule: dict[str, object] = {
        "asset_id": "EM-1",
        "udmi_version": "1.5.2",
        "units": {"phase_1_line_current_sensor": "amperes"},
    }
    schedule.update(overrides)
    return schedule


def _state(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "version": "1.5.2",
        "timestamp": "2026-07-09T10:00:00Z",
        "system": {},
    }
    payload.update(overrides)
    return payload


def _metadata(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "version": "1.5.2",
        "timestamp": "2026-07-09T10:00:00Z",
        "system": {},
        "pointset": {"points": {"phase_1_line_current_sensor": {"units": "amperes"}}},
    }
    payload.update(overrides)
    return payload


def _pointset(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "version": "1.5.2",
        "timestamp": "2026-07-09T10:00:00Z",
        "points": {"phase_1_line_current_sensor": {"present_value": 1.2}},
    }
    payload.update(overrides)
    return payload


class SchemaVersionMatchTests(unittest.TestCase):
    def test_conformant_payload_set_yields_no_issues(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(),
            }
        )
        self.assertEqual([issue.description for issue in issues], [])

    def test_version_mismatch_is_a_critical_issue_and_blocks_structure_checks(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                # Wrong version AND missing 'points': only the version mismatch
                # may be reported — structure must not be judged against 1.5.2.
                "pointset_payload": {"version": "1.4.0", "timestamp": "2026-07-09T10:00:00Z"},
            }
        )
        mismatches = [issue for issue in issues if "Expected schema version does not match" in issue.description]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].severity, "critical")
        self.assertEqual(mismatches[0].expected_value, "1.5.2")
        self.assertEqual(mismatches[0].observed_value, "1.4.0")
        self.assertNotIn("Required field", " ".join(issue.description for issue in issues))

    def test_payload_without_version_is_flagged_when_register_expects_one(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "state_payload": {"timestamp": "2026-07-09T10:00:00Z", "system": {}},
            }
        )
        flagged = [issue for issue in issues if "does not declare a UDMI version" in issue.description]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].severity, "high")

    def test_unknown_declared_version_reports_skipped_structural_checks(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(udmi_version="1.4.1"),
                "state_payload": _state(version="1.4.1"),
            }
        )
        self.assertIn("structural checks were skipped", descriptions)

    def test_version_match_tolerates_leading_v_and_numbers(self) -> None:
        self.assertTrue(versions_match("v1.5.2", "1.5.2"))
        self.assertTrue(versions_match("1.5.2", " 1.5.2 "))
        self.assertFalse(versions_match("1.5.2", "1.5.1"))
        self.assertEqual(declared_version({"version": 1}), "1")
        self.assertIsNone(declared_version({"version": ""}))
        self.assertIsNone(declared_version({}))


class StructuralCheckTests(unittest.TestCase):
    def test_missing_required_fields_are_flagged(self) -> None:
        findings = structural_issues("pointset", {"version": "1.5.2"})
        described = " ".join(finding.description for finding in findings)
        self.assertIn("Required field 'timestamp' is missing", described)
        self.assertIn("Required field 'points' is missing", described)

    def test_point_entry_without_present_value_is_flagged(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "pointset_payload": _pointset(points={"phase_1_line_current_sensor": {}}),
            }
        )
        self.assertIn("missing 'present_value'", descriptions)

    def test_bad_point_name_is_flagged(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(units={}),
                "pointset_payload": _pointset(points={"Phase1Current": {"present_value": 3}}),
            }
        )
        self.assertIn("does not match the UDMI point-name pattern", descriptions)

    def test_non_object_system_and_points_are_flagged_without_crashing(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(system=["not-an-object"]),
                "pointset_payload": _pointset(points=["not-an-object"]),
            }
        )
        self.assertIn("'system' in the state payload must be an object", descriptions)
        self.assertIn("'points' field must be an object", descriptions)

    def test_malformed_timestamp_is_flagged(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(timestamp="last tuesday"),
            }
        )
        self.assertIn("not an RFC 3339 date-time string", descriptions)

    def test_timestamp_check_is_rfc3339_strict_both_ways(self) -> None:
        # Date-only parses with fromisoformat but is NOT an RFC 3339 date-time.
        self.assertIn(
            "not an RFC 3339 date-time string",
            _descriptions({"expected_schedule": _schedule(), "state_payload": _state(timestamp="2026-07-09")}),
        )
        # Lowercase t/z separators ARE valid RFC 3339 and must not be flagged.
        self.assertNotIn(
            "not an RFC 3339 date-time string",
            _descriptions(
                {"expected_schedule": _schedule(), "state_payload": _state(timestamp="2026-07-09t10:00:00z")}
            ),
        )

    def test_required_field_present_but_null_is_flagged(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(system=None),
            }
        )
        flagged = [issue for issue in issues if "Required field 'system' is missing" in issue.description]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].observed_value, "null")

    def test_legacy_nested_pointset_shape_reports_shape_not_missing_points(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "pointset_payload": {
                    "version": "1.5.2",
                    "timestamp": "2026-07-09T10:00:00Z",
                    "pointset": {"points": {"phase_1_line_current_sensor": {"present_value": 1.2}}},
                },
            }
        )
        self.assertIn("nests its points under 'pointset.points'", descriptions)
        self.assertNotIn("Required field 'points' is missing", descriptions)
        # The nested points are still matched against the register.
        self.assertNotIn("was not received in the pointset payload", descriptions)

    def test_metadata_point_names_are_pattern_checked(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(units={}),
                "metadata_payload": _metadata(
                    pointset={"points": {"Zone_Temp": {"units": "degrees_celsius"}}}
                ),
            }
        )
        self.assertIn("Metadata point name 'Zone_Temp' does not match", descriptions)


class UnitMatchTests(unittest.TestCase):
    def test_metadata_unit_must_match_expected_register_unit(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(units={"phase_1_line_current_sensor": "volts"}),
                "metadata_payload": _metadata(),
            }
        )
        mismatches = [issue for issue in issues if "does not match the expected register unit" in issue.description]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].expected_value, "volts")
        self.assertEqual(mismatches[0].observed_value, "amperes")

    def test_unit_aliases_and_separators_do_not_trip_false_mismatches(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(units={"energy_sensor": "kwh"}),
                "metadata_payload": _metadata(
                    pointset={"points": {"energy_sensor": {"units": "kilowatt_hours"}}}
                ),
                "pointset_payload": _pointset(points={"energy_sensor": {"present_value": 12.5}}),
            }
        )
        self.assertNotIn("does not match the expected register unit", descriptions)
        self.assertNotIn("not a supported UDMI unit", descriptions)

    def test_unknown_units_are_still_flagged(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(units={"temp_sensor": "dagrees_celsius"}),
            }
        )
        self.assertIn("not a supported UDMI unit", descriptions)

    def test_explicit_no_units_metadata_still_mismatches_a_numeric_register_unit(self) -> None:
        # "no_units" is a real observed declaration, not an absent unit: a
        # register expecting kwh must be told it does not match.
        issues = _issues(
            {
                "expected_schedule": _schedule(units={"status_flag": "kwh"}),
                "metadata_payload": _metadata(
                    pointset={"points": {"status_flag": {"units": "no_units"}}}
                ),
                # String present_value must NOT trip a numeric-unit critical:
                # the device declared the point unit-less.
                "pointset_payload": _pointset(points={"status_flag": {"present_value": "OK"}}),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn("does not match the expected register unit", descriptions)
        self.assertNotIn("should be numeric", descriptions)


class MetadataPointCoverageTests(unittest.TestCase):
    def test_expected_point_missing_from_metadata_pointset(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(
                    units={"phase_1_line_current_sensor": "amperes", "phase_2_line_current_sensor": "amperes"}
                ),
                "metadata_payload": _metadata(),
            }
        )
        missing = [issue for issue in issues if "is not defined in the metadata pointset" in issue.description]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].point_name, "phase_2_line_current_sensor")

    def test_extra_metadata_point_is_flagged(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={
                        "points": {
                            "phase_1_line_current_sensor": {"units": "amperes"},
                            "surprise_sensor": {"units": "volts"},
                        }
                    }
                ),
            }
        )
        self.assertIn("Metadata defines point surprise_sensor", descriptions)

    def test_no_metadata_payload_means_no_per_point_metadata_issues(self) -> None:
        descriptions = _descriptions({"expected_schedule": _schedule()})
        self.assertNotIn("metadata pointset", descriptions)


class RegisterDrivenAssetsTests(unittest.TestCase):
    def test_assets_only_parameters_use_inline_report_not_fixture(self) -> None:
        result = validate_udmi_full_report(
            {"assets": [{"expected_schedule": _schedule()}]},
            live_capture=None,
        )
        self.assertEqual(result.result_summary["source"], "schedule_payload_inputs")
        self.assertEqual(result.result_summary["expected_devices"], 1)

    def test_not_publishing_claimed_only_when_capture_was_attempted(self) -> None:
        # No broker capture requested: no observation happened, so no
        # publishing/not-publishing claim may be fabricated.
        no_capture = validate_udmi_full_report(
            {"assets": [{"expected_schedule": _schedule()}]},
            live_capture=None,
        )
        self.assertEqual(no_capture.result_summary["not_publishing"], 0)

        # Capture attempted and delivered nothing for the asset: honestly
        # reported as not publishing.
        def empty_capture(_settings: object, **_kwargs: object) -> list:
            return []

        captured_nothing = validate_udmi_full_report(
            {
                "use_live_broker": True,
                "broker_host": "203.0.113.10",
                "assets": [{"expected_schedule": _schedule(), "state_topic": "a/b/state"}],
            },
            live_capture=empty_capture,
        )
        self.assertEqual(captured_nothing.result_summary["not_publishing"], 1)
        descriptions = " ".join(issue.description for issue in captured_nothing.issues)
        self.assertIn("did not publish during the validation window", descriptions)


class CaptureTopicTests(unittest.TestCase):
    def test_extra_capture_topics_are_included_and_deduplicated(self) -> None:
        topics = _capture_topics(
            {
                "state_topic": "a/b/state",
                "metadata_topic": "a/b/metadata",
                "pointset_topic": "a/b/events/pointset",
                "extra_capture_topics": ["a/b/event/pointset", "a/b/state"],
            }
        )
        self.assertEqual(
            topics,
            ["a/b/state", "a/b/metadata", "a/b/events/pointset", "a/b/event/pointset"],
        )


if __name__ == "__main__":
    unittest.main()
