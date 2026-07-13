"""UDMI workbench validation: schema-version match, structural checks, unit
equality, and metadata point coverage.

Honesty contract: the validator never fabricates results — a version mismatch,
missing version, or unknown ruleset is reported as an explicit issue, and a
skipped structural check is never presented as a pass.
"""

import json
import unittest
from pathlib import Path

from smart_commissioning_core import mqtt_transport, udmi_schema
from smart_commissioning_core.mqtt_transport import MqttMessage
from smart_commissioning_core.udmi_run_processor import (
    INLINE_INDEFINITE_CEILING_SECONDS,
    process_udmi_validation_run,
)
from smart_commissioning_core.udmi_schema import declared_version, structural_issues, versions_match
from smart_commissioning_core.udmi_validation import (
    DEFAULT_CAPTURE_SECONDS,
    DEFAULT_MAX_MESSAGES,
    _capture_topics,
    validate_udmi_full_report,
)


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
        "system": {
            "serial_no": "SN-1",
            "last_config": "2026-07-09T09:59:00Z",
            "hardware": {"make": "Acme", "model": "Meter"},
            "software": {},
            "operation": {"operational": True},
        },
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

    def test_pete_metadata_shape_matches_registered_point_units(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(
                    units={
                        "primary_ratio_sensor": "no_units",
                        "phase_2_power_sensor": "kilowatts",
                    }
                ),
                "metadata_payload": _metadata(
                    pointset={
                        "points": {
                            "primary_ratio_sensor": {"units": "no_units"},
                            "phase_2_power_sensor": {"units": "kilowatts"},
                        }
                    }
                ),
            }
        )
        self.assertNotIn("not defined in the metadata pointset", " ".join(issue.description for issue in issues))

    def test_expected_identity_missing_from_captured_state_is_reported(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(manufacturer="Schneider", model="PM5121"),
                "state_payload": _state(system={"hardware": {}}),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn("Expected manufacturer is missing", descriptions)
        self.assertIn("Expected model is missing", descriptions)

    def test_payload_view_uses_udmi_field_names_for_expectations(self) -> None:
        result = validate_udmi_full_report(
            {
                "expected_schedule": _schedule(manufacturer="Schneider", model="PM5121"),
                "state_payload": _state(hardware={"make": "Schneider", "model": "PM5121"}),
            },
            live_capture=None,
        )
        expected = result.result_summary["payload_views"][0]["payload_types"][0]["expected"]
        self.assertEqual(expected["version"], "1.5.2")
        self.assertEqual(expected["system"]["hardware"], {"make": "Schneider", "model": "PM5121"})
        self.assertNotIn("udmi_version", expected)
        self.assertNotIn("manufacturer", expected)


class StructuralCheckTests(unittest.TestCase):
    def test_canonical_fixtures_are_valid_and_all_local_refs_are_vendored(self) -> None:
        for payload_type, payload in (
            ("state", _state()),
            ("metadata", _metadata()),
            ("pointset", _pointset()),
        ):
            with self.subTest(payload_type=payload_type):
                self.assertEqual(structural_issues(payload_type, payload), [])

        schema_directory = (
            Path(udmi_schema.__file__).resolve().parent / "schemas" / "udmi" / "1.5.2"
        )

        def local_refs(value: object) -> list[str]:
            if isinstance(value, dict):
                refs = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
                return refs + [ref for child in value.values() for ref in local_refs(child)]
            if isinstance(value, list):
                return [ref for child in value for ref in local_refs(child)]
            return []

        for schema_path in schema_directory.glob("*.json"):
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            for ref in local_refs(schema):
                if ref.startswith("file:"):
                    target = ref.removeprefix("file:").split("#", 1)[0]
                    self.assertTrue(
                        (schema_directory / target).is_file(),
                        f"{schema_path.name} references missing vendored schema {target}",
                    )

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

    def test_canonical_schema_rejects_nested_additional_property(self) -> None:
        findings = structural_issues(
            "metadata",
            _metadata(
                pointset={
                    "points": {
                        "phase_1_line_current_sensor": {
                            "units": "amperes",
                            "not_in_udmi_schema": True,
                        }
                    }
                }
            ),
        )

        self.assertIn(
            "not allowed by the canonical UDMI 1.5.2 metadata schema",
            " ".join(finding.description for finding in findings),
        )

    def test_canonical_schema_requires_nested_state_fields(self) -> None:
        state = _state()
        del state["system"]["serial_no"]

        self.assertIn(
            "Required canonical field 'system.serial_no' is missing",
            " ".join(finding.description for finding in structural_issues("state", state)),
        )

    def test_canonical_schema_checks_nested_date_time(self) -> None:
        state = _state()
        state["system"]["last_config"] = "yesterday"

        self.assertIn(
            "Field 'system.last_config' is not an RFC 3339 date-time string",
            " ".join(finding.description for finding in structural_issues("state", state)),
        )

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

    def test_legacy_nested_pointset_does_not_hide_other_root_properties(self) -> None:
        descriptions = _descriptions(
            {
                "expected_schedule": _schedule(),
                "pointset_payload": {
                    "version": "1.5.2",
                    "timestamp": "2026-07-09T10:00:00Z",
                    "pointset": {"points": {"phase_1_line_current_sensor": {"present_value": 1.2}}},
                    "rogue": 123,
                },
            }
        )
        self.assertIn("'rogue' was unexpected", descriptions)

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

    def test_expected_metadata_unit_must_be_present(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={"points": {"phase_1_line_current_sensor": {}}}
                ),
            }
        )

        missing = [issue for issue in issues if "does not declare units" in issue.description]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].expected_value, "amperes")
        self.assertEqual(missing[0].observed_value, "missing")

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
    def test_expected_points_are_checked_independently_of_expected_units(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(
                    points=["unitless_status", "phase_1_line_current_sensor"],
                    units={"phase_1_line_current_sensor": "amperes"},
                ),
                "metadata_payload": _metadata(
                    pointset={
                        "points": {
                            "unitless_status": {},
                            "phase_1_line_current_sensor": {"units": "amperes"},
                        }
                    }
                ),
                "pointset_payload": _pointset(
                    points={
                        "unitless_status": {"present_value": "ok"},
                        "phase_1_line_current_sensor": {"present_value": 1.2},
                    }
                ),
            }
        )
        self.assertFalse([issue for issue in issues if issue.point_name == "unitless_status"])
        self.assertFalse([issue for issue in issues if "does not declare units" in issue.description])

    def test_payload_views_show_units_only_for_metadata(self) -> None:
        result = validate_udmi_full_report(
            {
                "expected_schedule": _schedule(
                    points=["unitless_status", "phase_1_line_current_sensor"],
                    units={"phase_1_line_current_sensor": "amperes"},
                ),
                "metadata_payload": _metadata(
                    pointset={
                        "points": {
                            "unitless_status": {},
                            "phase_1_line_current_sensor": {"units": "amperes"},
                        }
                    }
                ),
                "pointset_payload": _pointset(
                    points={
                        "unitless_status": {"present_value": "ok"},
                        "phase_1_line_current_sensor": {"present_value": 1.2},
                    }
                ),
            },
            live_capture=None,
        )
        expected_by_type = {
            entry["payload_type"]: entry["expected"]
            for entry in result.result_summary["payload_views"][0]["payload_types"]
        }
        self.assertEqual(
            expected_by_type["metadata"]["pointset"]["points"],
            {"unitless_status": {}, "phase_1_line_current_sensor": {"units": "amperes"}},
        )
        self.assertEqual(
            expected_by_type["pointset"]["points"],
            {
                "unitless_status": {"present_value": None},
                "phase_1_line_current_sensor": {"present_value": None},
            },
        )

    def test_payload_views_show_udmi_templates_with_register_constraints(self) -> None:
        result = validate_udmi_full_report(
            {
                "expected_schedule": _schedule(
                    asset_id="EM-1002001",
                    manufacturer="Schneider",
                    model="PM5121",
                    serial="SN-1",
                    firmware="1.2.3",
                    guid="ifc://changeMe0123",
                    site="GB-LON-IES",
                    room="METER-ROOM-R093",
                    points=["primary_ratio_sensor"],
                    units={"primary_ratio_sensor": "no_units"},
                ),
                "state_payload": _state(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(),
            },
            live_capture=None,
        )
        expected_by_type = {
            entry["payload_type"]: entry["expected"]
            for entry in result.result_summary["payload_views"][0]["payload_types"]
        }
        for payload_type, expected_payload in expected_by_type.items():
            self.assertEqual(
                structural_issues(payload_type, expected_payload),
                [],
                f"{payload_type} expected template must be structurally valid",
            )
        # Template timestamps are the build time (RFC 3339), never the epoch
        # sentinel operators read as "the tool is not pulling the correct time".
        template_timestamp = expected_by_type["state"]["timestamp"]
        self.assertRegex(template_timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertFalse(template_timestamp.startswith("1970"))
        self.assertEqual(expected_by_type["state"]["system"]["last_config"], template_timestamp)
        self.assertEqual(expected_by_type["state"]["system"]["serial_no"], "SN-1")
        self.assertEqual(expected_by_type["state"]["system"]["hardware"], {"make": "Schneider", "model": "PM5121"})
        self.assertEqual(expected_by_type["state"]["system"]["software"], {"firmware": "1.2.3"})
        self.assertEqual(expected_by_type["metadata"]["system"]["physical_tag"]["asset"], {"guid": "ifc://changeMe0123", "name": "EM-1002001"})
        self.assertEqual(expected_by_type["metadata"]["system"]["location"], {"site": "GB-LON-IES", "section": "METER-ROOM-R093"})
        self.assertEqual(expected_by_type["metadata"]["pointset"]["points"], {"primary_ratio_sensor": {"units": "no_units"}})
        self.assertEqual(expected_by_type["pointset"]["points"], {"primary_ratio_sensor": {"present_value": None}})

    def test_room_that_fits_location_room_is_embedded_without_a_note(self) -> None:
        # "METER_ROOM_R093" fails the strict section pattern (underscores) but
        # is perfectly canonical as system.location.room — real devices publish
        # either field (on-site 2026-07-13: location.room = "2-09_Meter_Room").
        result = validate_udmi_full_report(
            {"expected_schedule": _schedule(site="GB-LON-IES", room="METER_ROOM_R093")},
            live_capture=None,
        )
        descriptions = " ".join(issue.description for issue in result.issues)
        self.assertNotIn("cannot appear in canonical UDMI metadata", descriptions)
        self.assertNotIn("cannot form a valid UDMI metadata template", descriptions)
        expected_metadata = next(
            entry["expected"]
            for entry in result.result_summary["payload_views"][0]["payload_types"]
            if entry["payload_type"] == "metadata"
        )
        self.assertEqual(structural_issues("metadata", expected_metadata), [])
        self.assertEqual(
            expected_metadata["system"]["location"],
            {"site": "GB-LON-IES", "room": "METER_ROOM_R093"},
        )

    def test_register_room_matches_metadata_location_room(self) -> None:
        # A device publishing the room under location.room (not section) must
        # satisfy the register comparison — both fields are canonical UDMI.
        issues = _issues(
            {
                "expected_schedule": _schedule(room="2-09_Meter_Room"),
                "metadata_payload": _metadata(
                    system={"location": {"room": "2-09_Meter_Room"}},
                ),
            }
        )
        room_issues = [issue for issue in issues if "room" in issue.description.casefold()]
        self.assertEqual([issue.description for issue in room_issues], [])

    def test_misplaced_identity_value_names_where_it_was_found(self) -> None:
        # On-site 2026-07-13: the publisher nested a second 'system' inside
        # 'system' (system.system.location.site), so identity checks read
        # "missing" while the value was plainly visible in MQTT Explorer. The
        # issue must name the wrong path, and the structural check must call
        # out the double-nested system with the one-move fix.
        issues = _issues(
            {
                "expected_schedule": _schedule(site="GB-LON-1ES", room="2-09_Meter_Room"),
                "metadata_payload": _metadata(
                    system={
                        "last_config": {},
                        "system": {
                            "location": {"site": "GB-LON-1ES", "room": "2-09_Meter_Room"},
                        },
                    },
                ),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn(
            "Expected site is missing from the metadata payload at system.location.site.",
            descriptions,
        )
        self.assertIn("found at system.system.location.site", descriptions)
        self.assertIn("fix the publisher's payload nesting", descriptions)
        self.assertIn("found at system.system.location.room", descriptions)
        self.assertIn("nests a second 'system' object inside 'system'", descriptions)
        self.assertIn("Move the inner system's contents up one level", " ".join(
            issue.suggested_action or "" for issue in issues
        ))

    def test_numeric_asset_id_and_free_text_room_keep_template_valid(self) -> None:
        # Pete's site register: asset IDs like "2001" and free-text rooms can
        # never fit canonical UDMI patterns; each gets a named note and a
        # schema-valid placeholder in the template.
        result = validate_udmi_full_report(
            {"expected_schedule": _schedule(asset_id="2001", room="Meter Room 2")},
            live_capture=None,
        )
        descriptions = " ".join(issue.description for issue in result.issues)
        self.assertIn("Register Asset ID '2001' cannot appear in canonical UDMI metadata", descriptions)
        self.assertIn("system.physical_tag.asset.name", descriptions)
        self.assertIn("Register Room 'Meter Room 2' cannot appear in canonical UDMI metadata", descriptions)
        self.assertNotIn("cannot form a valid UDMI metadata template", descriptions)
        expected_metadata = next(
            entry["expected"]
            for entry in result.result_summary["payload_views"][0]["payload_types"]
            if entry["payload_type"] == "metadata"
        )
        self.assertEqual(structural_issues("metadata", expected_metadata), [])
        self.assertEqual(expected_metadata["system"]["physical_tag"]["asset"]["name"], "ASSET-1")
        self.assertEqual(expected_metadata["system"]["location"]["section"], "UNSPECIFIED")

    def test_expected_template_tolerates_malformed_point_constraints(self) -> None:
        result = validate_udmi_full_report({"expected_schedule": _schedule(points=42)})
        payload_types = result.result_summary["payload_views"][0]["payload_types"]
        pointset = next(entry for entry in payload_types if entry["payload_type"] == "pointset")

        self.assertEqual(pointset["expected"]["points"], {})

    def test_points_without_units_are_independently_required_in_both_payloads(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["unitless_status"], units={}),
                "metadata_payload": _metadata(pointset={"points": {}}),
                "pointset_payload": _pointset(points={}),
            }
        )
        missing = [issue for issue in issues if issue.point_name == "unitless_status"]
        self.assertEqual({issue.issue_type for issue in missing}, {"metadata_validation", "pointset_validation"})

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

    def test_rejected_register_rows_become_a_visible_issue(self) -> None:
        # A partial register import silently narrowed the expected asset list on
        # site (2026-07-13): the dropped device never appeared in any result.
        result = validate_udmi_full_report(
            {
                "assets": [{"expected_schedule": _schedule()}],
                "register_rejected_rows": 2,
                "register_rejected_details": [
                    "row 3: Expected topic — must use a fixed asset prefix",
                ],
                "register_import_filename": "register.csv",
            },
            live_capture=None,
        )
        rejections = [issue for issue in result.issues if issue.issue_type == "register_import"]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].severity, "high")
        self.assertIn("'register.csv' rejected 2 row(s)", rejections[0].description)
        self.assertIn("row 3: Expected topic", rejections[0].description)
        self.assertIn("do not appear in these results", rejections[0].description)

    def test_no_rejection_issue_without_rejected_rows(self) -> None:
        result = validate_udmi_full_report(
            {"assets": [{"expected_schedule": _schedule()}]},
            live_capture=None,
        )
        self.assertFalse([issue for issue in result.issues if issue.issue_type == "register_import"])

    def test_duplicate_asset_id_collision_is_reported(self) -> None:
        # Backend detected two register rows sharing one Asset ID but pointing
        # at different device topic roots (2026-07-13: one device looked
        # missing, its neighbour carried a doubled issue list).
        result = validate_udmi_full_report(
            {
                "assets": [{"expected_schedule": _schedule()}],
                "register_duplicate_asset_ids": [
                    {"asset_id": "EM-1002002", "topic_roots": ["MNVRHS/EM-1002001", "MNVRHS/EM-1002002"]},
                ],
            },
            live_capture=None,
        )
        collisions = [issue for issue in result.issues if issue.issue_type == "register_import"]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].severity, "high")
        self.assertIn("multiple rows with Asset ID 'EM-1002002'", collisions[0].description)
        self.assertIn("MNVRHS/EM-1002001, MNVRHS/EM-1002002", collisions[0].description)


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


class RecordingCapture:
    """Fake live_capture that records every call's kwargs and returns canned messages."""

    def __init__(self, messages: list[MqttMessage] | None = None) -> None:
        self.messages = messages or []
        self.calls: list[dict] = []

    def __call__(self, _settings: object, **kwargs: object) -> list[MqttMessage]:
        self.calls.append(kwargs)
        return self.messages


def _msg(topic: str, payload: bytes = b'{"timestamp":"2026-07-09T10:00:00Z"}') -> MqttMessage:
    return MqttMessage(topic=topic, payload=payload)


_BROKER = {"use_live_broker": True, "broker_host": "203.0.113.10"}
_TOPICS = {
    "state_topic": "a/b/state",
    "metadata_topic": "a/b/metadata",
    "pointset_topic": "a/b/events/pointset",
    "extra_capture_topics": ["a/b/event/pointset"],
}
_ALL_TOPIC_MESSAGES = [_msg("a/b/state"), _msg("a/b/metadata"), _msg("a/b/events/pointset")]


class CaptureRunTimeTests(unittest.TestCase):
    def test_blank_capture_seconds_is_indefinite_until_all_topics(self) -> None:
        # Blank run time + a cancel path => indefinite capture (timeout None)
        # that completes once every expected topic has a payload.
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        result = validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": ""},
            live_capture=capture,
            cancel_check=lambda: False,
        )
        call = capture.calls[-1]
        self.assertIsNone(call["timeout_seconds"])
        self.assertTrue(callable(call["cancel_check"]))
        self.assertEqual(call["max_messages"], DEFAULT_MAX_MESSAGES)
        self.assertEqual(result.result_summary["capture_mode"], "indefinite")
        self.assertEqual(result.result_summary["broker_status_detail"], "live_payloads_captured")

    def test_stop_when_needs_distinct_topics_not_message_count(self) -> None:
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": 0},
            live_capture=capture,
            cancel_check=lambda: False,
        )
        stop_when = capture.calls[-1]["stop_when"]
        # Duplicate publishes on one chatty topic never complete the capture.
        self.assertFalse(stop_when([_msg("a/b/state")] * 10))
        # One payload per expected topic completes it.
        self.assertTrue(stop_when(list(_ALL_TOPIC_MESSAGES)))
        # The legacy event/pointset alias satisfies the same pointset slot as
        # events/pointset — either convention completes the capture.
        self.assertTrue(stop_when([_msg("a/b/state"), _msg("a/b/metadata"), _msg("a/b/event/pointset")]))

    def test_stop_when_requires_each_topic_to_carry_a_json_object(self) -> None:
        capture = RecordingCapture(
            [
                _msg("a/b/state", b"not-json"),
                _msg("a/b/metadata", b"[]"),
                _msg("a/b/events/pointset"),
            ]
        )
        result = validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": 1},
            live_capture=capture,
            cancel_check=lambda: False,
        )

        stop_when = capture.calls[-1]["stop_when"]
        self.assertFalse(stop_when(capture.messages))
        self.assertEqual(result.result_summary["broker_status_detail"], "live_capture_timeout")
        self.assertEqual(result.result_summary["captured_topics"], ["a/b/events/pointset"])
        invalid = [issue for issue in result.issues if issue.issue_type == "payload_error"]
        self.assertEqual(len(invalid), 1)
        self.assertIn("valid JSON objects", invalid[0].description)

    def test_stale_retained_pointset_exceeds_register_reporting_interval(self) -> None:
        capture = RecordingCapture(
            [
                MqttMessage("a/b/state", json.dumps(_state(timestamp="2020-01-01T00:00:00Z")).encode(), retained=True),
                MqttMessage("a/b/metadata", json.dumps(_metadata(timestamp="2020-01-01T00:00:00Z")).encode(), retained=True),
                MqttMessage("a/b/events/pointset", json.dumps(_pointset(timestamp="2020-01-01T00:00:00Z")).encode(), retained=True),
            ]
        )
        result = validate_udmi_full_report(
            {
                **_BROKER,
                **_TOPICS,
                "capture_seconds": 1,
                "expected_schedule": _schedule(reporting_interval_seconds="20"),
            },
            live_capture=capture,
            cancel_check=lambda: False,
        )

        stale = [issue for issue in result.issues if "reporting interval" in issue.description]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].issue_type, "pointset_validation")
        self.assertIn("retained", stale[0].description)
        self.assertTrue(result.result_summary["payload_views"][0]["payload_types"][2]["retained"])

    def test_numeric_capture_seconds_is_a_bounded_window(self) -> None:
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        result = validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": 45},
            live_capture=capture,
            cancel_check=lambda: False,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], 45.0)
        self.assertEqual(result.result_summary["capture_mode"], "bounded")
        self.assertEqual(result.result_summary["capture_window_seconds"], 45.0)

    def test_indefinite_without_cancel_path_is_bounded_and_labelled(self) -> None:
        # No cancel mechanism reachable => an indefinite request would be
        # unkillable, so it is bounded to the default window and says so.
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        result = validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": 0},
            live_capture=capture,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], DEFAULT_CAPTURE_SECONDS)
        self.assertEqual(result.result_summary["capture_mode"], "indefinite_bounded_no_cancel")

    def test_partial_capture_names_the_missing_topics(self) -> None:
        # Only state arrived: the run is an honest timeout, not "captured", and
        # the issue names which expected topics never reported.
        capture = RecordingCapture([_msg("a/b/state")])
        result = validate_udmi_full_report(
            {**_BROKER, **_TOPICS, "capture_seconds": 1},
            live_capture=capture,
            cancel_check=lambda: False,
        )
        self.assertEqual(result.result_summary["broker_status_detail"], "live_capture_timeout")
        missing_issues = [issue for issue in result.issues if issue.issue_type == "not_publishing"]
        self.assertEqual(len(missing_issues), 1)
        self.assertIn("a/b/metadata", missing_issues[0].description)
        self.assertIn("a/b/events/pointset", missing_issues[0].description)
        self.assertNotIn("a/b/state", missing_issues[0].description)


class SharedMultiAssetCaptureTests(unittest.TestCase):
    def test_one_shared_capture_routes_payloads_to_each_asset(self) -> None:
        # ONE live_capture call subscribes every asset's topics; messages route
        # back to each entry's payload slots (duplicates keep the last payload).
        messages = [
            _msg("site/a1/state", b'{"system":{"hardware":{"make":"Co1"}}}'),
            _msg("site/a2/state", b'{"system":{"hardware":{"make":"stale"}}}'),
            _msg("site/a2/state", b'{"system":{"hardware":{"make":"Co2"}}}'),
        ]
        capture = RecordingCapture(messages)
        parameters = {
            **_BROKER,
            "capture_seconds": 2,
            "assets": [
                {"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state"},
                {"expected_schedule": {"asset_id": "A2"}, "state_topic": "site/a2/state"},
            ],
        }
        result = validate_udmi_full_report(parameters, live_capture=capture, cancel_check=lambda: False)
        self.assertEqual(len(capture.calls), 1)
        self.assertEqual(capture.calls[0]["topics"], ["site/a1/state", "site/a2/state"])
        self.assertEqual(result.result_summary["subscribed_topics"], ["site/a1/state", "site/a2/state"])
        entries = result.result_summary["payload_views"]
        self.assertEqual([view["asset_id"] for view in entries], ["A1", "A2"])
        self.assertEqual(result.result_summary["broker_status_detail"], "live_payloads_captured")
        self.assertEqual(result.result_summary["message_count"], 3)
        # Messages routed back per entry: A2 saw both publishes, last one wins.
        asset_entries = parameters["assets"]
        self.assertEqual(len(asset_entries[0]["messages"]), 1)
        self.assertEqual(len(asset_entries[1]["messages"]), 2)
        self.assertEqual(asset_entries[0]["state_payload"]["system"]["hardware"]["make"], "Co1")
        self.assertEqual(asset_entries[1]["state_payload"]["system"]["hardware"]["make"], "Co2")

    def test_shared_capture_stop_when_waits_for_every_asset(self) -> None:
        capture = RecordingCapture([_msg("site/a1/state"), _msg("site/a2/state")])
        validate_udmi_full_report(
            {
                **_BROKER,
                "capture_seconds": 0,
                "assets": [
                    {"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state"},
                    {"expected_schedule": {"asset_id": "A2"}, "state_topic": "site/a2/state"},
                ],
            },
            live_capture=capture,
            cancel_check=lambda: False,
        )
        call = capture.calls[-1]
        self.assertIsNone(call["timeout_seconds"])
        stop_when = call["stop_when"]
        # A chatty first asset does not end the capture while the second is quiet.
        self.assertFalse(stop_when([_msg("site/a1/state")] * 5))
        self.assertTrue(stop_when([_msg("site/a1/state"), _msg("site/a2/state")]))

    def test_shared_capture_timeout_names_missing_assets_topics(self) -> None:
        capture = RecordingCapture([_msg("site/a1/state")])
        result = validate_udmi_full_report(
            {
                **_BROKER,
                "capture_seconds": 1,
                "assets": [
                    {"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state"},
                    {"expected_schedule": {"asset_id": "A2"}, "state_topic": "site/a2/state"},
                ],
            },
            live_capture=capture,
            cancel_check=lambda: False,
        )
        self.assertEqual(result.result_summary["broker_status_detail"], "live_capture_timeout")
        missing_issues = [issue for issue in result.issues if issue.issue_type == "not_publishing"]
        self.assertTrue(any("site/a2/state" in issue.description for issue in missing_issues))

    def test_not_publishing_issue_names_subscribed_and_observed_topics(self) -> None:
        # On-site 2026-07-13: a device visible in MQTT Explorer was reported
        # "not found" with no way to see why. The per-asset issue now says
        # which topics were subscribed and what actually arrived: A2's wildcard
        # saw traffic on an unrecognised topic; A3 saw nothing at all.
        capture = RecordingCapture(
            [
                _msg("site/a1/state"),
                _msg("site/a2/events/system"),
            ]
        )
        result = validate_udmi_full_report(
            {
                **_BROKER,
                "capture_seconds": 1,
                "assets": [
                    {"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state"},
                    {
                        "expected_schedule": {"asset_id": "A2"},
                        "state_topic": "site/a2/state",
                        "register_topic_filter": "site/a2/#",
                    },
                    {"expected_schedule": {"asset_id": "A3"}, "state_topic": "site/a3/state"},
                ],
            },
            live_capture=capture,
            cancel_check=lambda: False,
        )
        by_asset = {
            issue.asset_id: issue.description
            for issue in result.issues
            if issue.issue_type == "not_publishing" and issue.asset_id in {"A1", "A2", "A3"}
        }
        self.assertEqual(set(by_asset), {"A2", "A3"})
        self.assertIn("site/a2/events/system", by_asset["A2"])
        self.assertIn("none is a recognised UDMI payload topic", by_asset["A2"])
        self.assertIn("Nothing arrived on the subscribed topics", by_asset["A3"])
        self.assertIn("site/a3/state", by_asset["A3"])

    def test_register_wildcard_is_subscribed_alongside_derived_topics(self) -> None:
        capture = RecordingCapture([_msg("site/a1/state")])
        validate_udmi_full_report(
            {**_BROKER, "capture_seconds": 1, "assets": [{"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state", "register_topic_filter": "site/a1/#"}]},
            live_capture=capture,
            cancel_check=lambda: False,
        )
        self.assertIn("site/a1/#", capture.calls[0]["topics"])


class _FakeRunStore:
    """Minimal cancellable run store for exercising the processor without a DB."""

    def __init__(self, *, cancel: bool = False, cancellable: bool = True) -> None:
        self.cancel = cancel
        self.status_calls: list[dict] = []
        self.summaries: list[dict] = []
        if not cancellable:
            # Hide the cancel API entirely: the processor must then pass no
            # cancel_check and the engine bounds indefinite captures itself.
            self.is_cancel_requested = None  # type: ignore[assignment]

    def update_run_status(self, run_id: str, **kwargs: object) -> dict:
        record = {"run_id": run_id, **kwargs}
        self.status_calls.append(record)
        return record

    def update_result_summary(self, run_id: str, summary: dict, merge: bool = True) -> None:
        self.summaries.append(summary)

    def replace_issues(self, run_id: str, issues: list) -> None:
        self.issues = issues

    def is_cancel_requested(self, run_id: str) -> bool:
        return self.cancel


_PROCESSOR_PARAMS = {**_BROKER, **_TOPICS, "capture_seconds": 0}


class UdmiProcessorCancelAndInlineGuardTests(unittest.TestCase):
    def test_worker_mode_honours_indefinite_and_wires_cancel(self) -> None:
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-1", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="dramatiq_worker", live_capture=capture,
        )
        call = capture.calls[-1]
        self.assertIsNone(call["timeout_seconds"])
        self.assertTrue(callable(call["cancel_check"]))
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["indefinite_bounded_inline"])

    def test_inline_mode_bounds_indefinite_to_the_ceiling(self) -> None:
        # Inline runs execute inside the API request with no Cancel button
        # available, so an indefinite request is bounded and flagged honestly.
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-2", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="inline_local_fallback", live_capture=capture,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], INLINE_INDEFINITE_CEILING_SECONDS)
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(store.summaries[-1]["indefinite_bounded_inline"])

    def test_explicit_window_is_untouched_by_the_inline_guard(self) -> None:
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        process_udmi_validation_run(
            "run-3", {**_PROCESSOR_PARAMS, "capture_seconds": 7}, run_store=store,
            execution_mode="inline_local_fallback", live_capture=capture,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], 7.0)
        self.assertFalse(store.summaries[-1]["indefinite_bounded_inline"])

    def test_live_capture_timeout_marks_the_run_failed(self) -> None:
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-timeout",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=RecordingCapture([]),
        )
        self.assertEqual(record["status"], "failed")
        self.assertIn("did not complete", record["error_message"])
        self.assertEqual(store.summaries[-1]["broker_status_detail"], "live_capture_timeout")
        self.assertTrue(any(issue.issue_type == "not_publishing" for issue in store.issues))

    def test_live_broker_error_marks_the_run_failed(self) -> None:
        store = _FakeRunStore()

        def unavailable(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise OSError("connection refused")

        record = process_udmi_validation_run(
            "run-broker-error",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=unavailable,
        )
        self.assertEqual(record["status"], "failed")
        self.assertIn("broker_unreachable", record["error_message"])
        self.assertEqual(store.summaries[-1]["broker_status_detail"], "broker_unreachable")

    def test_unexpected_failure_does_not_expose_exception_text(self) -> None:
        store = _FakeRunStore()

        def unexpected(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise RuntimeError("broker password=hunter2")

        record = process_udmi_validation_run(
            "run-unexpected-error",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=unexpected,
        )

        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_message"], "UDMI validation failed; see server logs.")
        self.assertNotIn("hunter2", str(record))

    def test_mid_capture_broker_drop_fails_and_keeps_partial_evidence(self) -> None:
        store = _FakeRunStore()
        partial = [_msg("a/b/state")]

        def interrupted(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise mqtt_transport.MqttCaptureInterrupted(
                partial,
                ConnectionResetError("broker dropped password=hunter2"),
            )

        record = process_udmi_validation_run(
            "run-partial-drop",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=interrupted,
        )

        self.assertEqual(record["status"], "failed")
        self.assertEqual(store.summaries[-1]["broker_status_detail"], "authentication_error")
        self.assertEqual(store.summaries[-1]["captured_topics"], ["a/b/state"])
        self.assertNotIn("hunter2", str(store.summaries[-1]))
        self.assertNotIn("hunter2", str(store.issues))

    def test_multi_asset_broker_drop_fails_and_keeps_partial_evidence(self) -> None:
        store = _FakeRunStore()
        partial = [_msg("site/a1/state")]
        parameters = {
            **_BROKER,
            "capture_seconds": 1,
            "assets": [
                {"expected_schedule": {"asset_id": "A1"}, "state_topic": "site/a1/state"},
                {"expected_schedule": {"asset_id": "A2"}, "state_topic": "site/a2/state"},
            ],
        }

        def interrupted(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise mqtt_transport.MqttCaptureInterrupted(partial, ConnectionResetError("broker dropped"))

        record = process_udmi_validation_run(
            "run-multi-partial-drop",
            parameters,
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=interrupted,
        )

        summary = store.summaries[-1]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(summary["broker_status_detail"], "broker_unreachable")
        self.assertEqual(summary["captured_topics"], ["site/a1/state"])
        self.assertEqual(summary["message_count"], 1)
        self.assertEqual(summary["payload_view_source"], "live_capture")

    def test_valid_pasted_payloads_still_succeed(self) -> None:
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-pasted",
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(),
            },
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=None,
        )
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["broker_capture_attempted"])

    def test_cancel_observed_marks_the_run_cancelled(self) -> None:
        store = _FakeRunStore(cancel=True)
        capture = RecordingCapture([_msg("a/b/state")])
        record = process_udmi_validation_run(
            "run-4", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="dramatiq_worker", live_capture=capture,
        )
        # The capture received a live checker reflecting the store's flag …
        self.assertTrue(capture.calls[-1]["cancel_check"]())
        # … and the run finishes under a real cancelled status, not succeeded.
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(store.summaries[-1]["captured_topics"], ["a/b/state"])
        self.assertTrue(any(issue.issue_type == "not_publishing" for issue in store.issues))

    def test_store_without_cancel_api_falls_back_to_bounded(self) -> None:
        store = _FakeRunStore(cancellable=False)
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-5", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="dramatiq_worker", live_capture=capture,
        )
        call = capture.calls[-1]
        self.assertIsNone(call["cancel_check"])
        self.assertEqual(call["timeout_seconds"], DEFAULT_CAPTURE_SECONDS)
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(store.summaries[-1]["capture_mode"], "indefinite_bounded_no_cancel")


if __name__ == "__main__":
    unittest.main()
