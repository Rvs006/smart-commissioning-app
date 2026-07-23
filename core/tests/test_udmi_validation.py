"""UDMI workbench validation: schema-version match, structural checks, unit
equality, and metadata point coverage.

Honesty contract: the validator never fabricates results — a version mismatch,
missing version, or unknown ruleset is reported as an explicit issue, and a
skipped structural check is never presented as a pass.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_commissioning_core import mqtt_transport, udmi_schema
from smart_commissioning_core.mqtt_settings import INDEFINITE_BACKSTOP_SECONDS
from smart_commissioning_core.mqtt_transport import MqttMessage
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run
from smart_commissioning_core.udmi_schema import (
    declared_version,
    is_nonpub_version,
    nonpub_version_key,
    structural_issues,
    versions_match,
)
from smart_commissioning_core.udmi_validation import (
    DEFAULT_CAPTURE_SECONDS,
    DEFAULT_MAX_MESSAGES,
    _capture_topics,
    _conformance_fields,
    _pointset_freshness_issue,
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

    def test_field_metadata_shape_matches_registered_point_units(self) -> None:
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


def _nonpub_state_schema() -> dict:
    """A deliberately tiny operator schema: state requires a 'site_code' string."""
    return {
        "type": "object",
        "required": ["version", "site_code"],
        "properties": {
            "version": {"type": "string"},
            "site_code": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _nonpub_set(state_schema: dict | None = None) -> dict[str, dict]:
    return {
        "state.json": state_schema or _nonpub_state_schema(),
        "metadata.json": {"type": "object"},
        "events_pointset.json": {"type": "object"},
    }


class NonPubSchemaTests(unittest.TestCase):
    """Non-published version labels resolve against operator-uploaded schema sets."""

    def test_nonpub_version_detection_and_key(self) -> None:
        self.assertTrue(is_nonpub_version("nonpub.1"))
        self.assertTrue(is_nonpub_version(" NonPub-siteA "))
        self.assertTrue(is_nonpub_version("nonpub"))
        self.assertFalse(is_nonpub_version("1.5.2"))
        self.assertFalse(is_nonpub_version("nonpublished"))
        self.assertEqual(nonpub_version_key(" NonPub.1 "), "nonpub.1")

    def test_nonpub_labels_match_case_insensitively(self) -> None:
        self.assertTrue(versions_match("NonPub.1", "nonpub.1"))
        self.assertFalse(versions_match("nonpub.1", "nonpub.2"))
        # Published versions keep exact matching.
        self.assertFalse(versions_match("1.5.2", "1.5.20"))

    def test_missing_uploaded_set_is_one_high_finding_with_upload_action(self) -> None:
        findings = structural_issues("state", {"version": "nonpub.1", "site_code": "X"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertIn("no schema set with that label has been uploaded", findings[0].description)
        self.assertIn("Upload the 'nonpub.1' schema set", findings[0].suggested_action)

    def test_uploaded_set_validates_payload_and_names_the_set(self) -> None:
        sets = {"nonpub.1": _nonpub_set()}
        conforming = structural_issues(
            "state", {"version": "nonpub.1", "site_code": "GB-LON"}, sets
        )
        self.assertEqual(conforming, [])
        violating = structural_issues("state", {"version": "nonpub.1"}, sets)
        self.assertEqual(len(violating), 1)
        self.assertEqual(violating[0].severity, "high")
        self.assertIn("site_code", violating[0].description)
        self.assertIn("uploaded 'nonpub.1' schema set", violating[0].suggested_action)

    def test_focused_152_checks_do_not_run_for_nonpub_payloads(self) -> None:
        # No timestamp, no system: fails 1.5.2's focused checks, but the
        # operator's schema does not require them — must be a clean pass.
        findings = structural_issues(
            "state", {"version": "nonpub.1", "site_code": "GB-LON"}, {"nonpub.1": _nonpub_set()}
        )
        self.assertEqual(findings, [])

    def test_reuploaded_set_takes_effect_without_restart(self) -> None:
        payload = {"version": "nonpub.1", "site_code": "GB-LON"}
        strict = _nonpub_set(
            {
                "type": "object",
                "required": ["site_code", "extra_field"],
                "properties": {"site_code": {"type": "string"}},
            }
        )
        self.assertEqual(len(structural_issues("state", payload, {"nonpub.1": strict})), 1)
        # Corrected re-upload under the SAME label must not serve stale results.
        self.assertEqual(structural_issues("state", payload, {"nonpub.1": _nonpub_set()}), [])

    def test_reupload_evicts_the_superseded_validator(self) -> None:
        # The cache is keyed by (label, payload type): a re-upload rebuilds and
        # replaces the entry instead of accumulating one validator per digest.
        payload = {"version": "nonpub.evict", "site_code": "GB-LON"}
        strict = _nonpub_set(
            {"type": "object", "required": ["site_code", "extra_field"]}
        )
        structural_issues("state", payload, {"nonpub.evict": strict})
        structural_issues("state", payload, {"nonpub.evict": _nonpub_set()})
        keys = [
            key for key in udmi_schema._uploaded_validator_cache if key[0] == "nonpub.evict"
        ]
        self.assertEqual(keys, [("nonpub.evict", "state")])

    def test_broken_uploaded_set_is_one_high_finding_not_an_exception(self) -> None:
        # A dangling $ref must degrade to a single finding, never escape and
        # kill the run through the sanitized failure path.
        broken = {
            "state.json": {"$ref": "file:missing.json#/definitions/nope"},
            "metadata.json": {"type": "object"},
            "events_pointset.json": {"type": "object"},
        }
        findings = structural_issues(
            "state", {"version": "nonpub.1", "site_code": "GB-LON"}, {"nonpub.1": broken}
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertIn("could not be applied to the state payload", findings[0].description)
        self.assertIn("file:<name>#", findings[0].suggested_action)

    def test_uploaded_set_judges_the_payload_as_published(self) -> None:
        # The legacy pointset.points hoist is a published-version concession;
        # the operator's own schema must see the payload shape as published.
        nested_payload = {
            "version": "nonpub.1",
            "pointset": {"points": {"flow_sensor": {"present_value": 1}}},
        }
        requires_nested = {
            "state.json": {"type": "object"},
            "metadata.json": {"type": "object"},
            "events_pointset.json": {
                "type": "object",
                "required": ["pointset"],
                "properties": {"pointset": {"type": "object"}},
            },
        }
        requires_top_level = {
            "state.json": {"type": "object"},
            "metadata.json": {"type": "object"},
            "events_pointset.json": {"type": "object", "required": ["points"]},
        }
        self.assertEqual(
            structural_issues("pointset", nested_payload, {"nonpub.1": requires_nested}), []
        )
        flagged = structural_issues("pointset", nested_payload, {"nonpub.1": requires_top_level})
        self.assertEqual(len(flagged), 1)
        self.assertIn("points", flagged[0].description)

    def test_full_report_missing_set_reported_once_per_payload_not_per_template(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(udmi_version="nonpub.1"),
                "state_payload": {"version": "nonpub.1", "site_code": "GB-LON"},
            }
        )
        missing = [
            issue
            for issue in issues
            if "no schema set with that label has been uploaded" in issue.description
        ]
        self.assertEqual(len(missing), 1, [issue.description for issue in issues])
        self.assertEqual(missing[0].severity, "high")

    def test_full_report_with_uploaded_set_checks_payload_against_it(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(udmi_version="nonpub.1"),
                "state_payload": {"version": "nonpub.1"},
                "nonpub_schema_sets": {"NonPub.1": _nonpub_set()},
            }
        )
        site_code = [issue for issue in issues if "site_code" in issue.description]
        self.assertEqual(len(site_code), 1, [issue.description for issue in issues])

    def test_register_driven_assets_use_run_level_uploaded_sets(self) -> None:
        # Run creation embeds nonpub_schema_sets at the TOP level of the run
        # parameters only; register rows become per-asset entries that must
        # still resolve the uploaded sets rather than reporting them missing.
        issues = _issues(
            {
                "assets": [
                    {
                        "expected_schedule": _schedule(udmi_version="nonpub.1"),
                        "state_payload": {"version": "nonpub.1"},
                    }
                ],
                "nonpub_schema_sets": {"NonPub.1": _nonpub_set()},
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertNotIn("no schema set with that label has been uploaded", descriptions)
        site_code = [issue for issue in issues if "site_code" in issue.description]
        self.assertEqual(len(site_code), 1, [issue.description for issue in issues])

    def test_nonpub_template_facets_are_never_judged_against_uploaded_sets(self) -> None:
        # Template facets are canonical-1.5.2-shaped with placeholders (no
        # site_code); judging them against the operator's schema would fabricate
        # "cannot form a valid UDMI template" findings for a conforming device.
        issues = _issues(
            {
                "expected_schedule": _schedule(udmi_version="nonpub.1", units={}),
                "state_payload": {"version": "nonpub.1", "site_code": "GB-LON"},
                "nonpub_schema_sets": {"nonpub.1": _nonpub_set()},
            }
        )
        self.assertEqual([issue.description for issue in issues], [])


class ConformanceScoreTests(unittest.TestCase):
    """The hero score is fed by validation outcomes, never publishing liveness."""

    def _summary(self, parameters: dict) -> dict:
        return validate_udmi_full_report(parameters, live_capture=None).result_summary

    def test_conformant_run_scores_100(self) -> None:
        summary = self._summary(
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(),
            }
        )
        self.assertEqual(summary["blocking_issue_count"], 0)
        self.assertEqual(summary["payload_conformance_percent"], 100)

    def test_blocking_issue_caps_score_below_100(self) -> None:
        # Critical version mismatch on a pasted payload: the old liveness ratio
        # read 100% here (nothing is "not publishing" in inline mode) — the
        # score must now be capped strictly below 100.
        summary = self._summary(
            {
                "expected_schedule": _schedule(),
                "pointset_payload": {"version": "1.4.0", "timestamp": "2026-07-09T10:00:00Z"},
            }
        )
        self.assertGreaterEqual(summary["blocking_issue_count"], 1)
        self.assertLess(summary["payload_conformance_percent"], 100)

    def test_no_expected_devices_yields_null_score(self) -> None:
        fields = _conformance_fields([], [], [])
        self.assertIsNone(fields["payload_conformance_percent"])
        self.assertEqual(fields["blocking_issue_count"], 0)

    def test_run_scoped_blocking_issue_clamps_score_to_99(self) -> None:
        # A blocking issue that names no device (asset_id=None) means devices
        # were not fully verified: per-device math would still say 100, so the
        # clamp must make 100% impossible.
        issue = ValidationIssueRecord(
            issue_id="i1",
            issue_type="payload_error",
            severity="high",
            description="run-scoped failure",
        )
        fields = _conformance_fields(["EM-1", "EM-2"], [], [issue])
        self.assertEqual(fields["payload_conformance_percent"], 99)
        self.assertEqual(fields["blocking_issue_count"], 1)

    def test_low_severity_notes_do_not_block_100(self) -> None:
        note = ValidationIssueRecord(
            issue_id="i1",
            issue_type="payload_error",
            severity="low",
            description="informational note",
        )
        fields = _conformance_fields(["EM-1"], [], [note])
        self.assertEqual(fields["payload_conformance_percent"], 100)
        self.assertEqual(fields["blocking_issue_count"], 0)

    def test_device_scoped_blocking_issue_subtracts_only_that_device(self) -> None:
        issue = ValidationIssueRecord(
            issue_id="i1",
            asset_id="EM-2",
            issue_type="state_validation",
            severity="critical",
            description="device-scoped failure",
        )
        fields = _conformance_fields(["EM-1", "EM-2", "EM-3", "EM-4"], [], [issue])
        self.assertEqual(fields["payload_conformance_percent"], 75)
        self.assertEqual(fields["blocking_issue_count"], 1)

    def test_silent_device_depresses_score_without_any_issue_records(self) -> None:
        # Pins the not_publishing subtraction independently of issue records.
        fields = _conformance_fields(["EM-1", "EM-2"], ["EM-2"], [])
        self.assertEqual(fields["payload_conformance_percent"], 50)
        self.assertEqual(fields["blocking_issue_count"], 0)

    def test_silent_device_issues_are_not_blocking_but_still_depress_the_score(self) -> None:
        # Silent devices are "neither validated nor failed": their liveness
        # issues stay out of the blocking count, yet the score cannot pretend
        # they conformed.
        silent = ValidationIssueRecord(
            issue_id="i1",
            asset_id="EM-2",
            issue_type="not_publishing",
            severity="high",
            description="Expected device EM-2 did not publish during the validation window.",
        )
        fields = _conformance_fields(["EM-1", "EM-2"], ["EM-2"], [silent])
        self.assertEqual(fields["blocking_issue_count"], 0)
        self.assertEqual(fields["payload_conformance_percent"], 50)

    def test_run_scoped_silence_alone_keeps_100_impossible(self) -> None:
        # Only silence exists (no blocking issue) and it names no expected
        # device: the clamp must still make 100% impossible.
        silent = ValidationIssueRecord(
            issue_id="i1",
            asset_id="UDMI assets",
            issue_type="not_publishing",
            severity="high",
            description="Capture ended before every expected topic reported.",
        )
        fields = _conformance_fields(["EM-1"], [], [silent])
        self.assertEqual(fields["blocking_issue_count"], 0)
        self.assertEqual(fields["payload_conformance_percent"], 99)


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
        # An ABSENT units key is the "does not declare units" case only — the
        # present-but-empty message must NOT also fire.
        self.assertNotIn(
            "carries an empty units value", " ".join(issue.description for issue in issues)
        )

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


class EmptyValueTests(unittest.TestCase):
    """Field review 2026-07-20 (item 9): units/present_value present-but-blank.

    A field that EXISTS but is empty ("", null, or whitespace) is a real fault
    (a point never linked to its data source), reported distinctly from the
    absent-field checks, scoped to units + present_value across both payloads.
    """

    def _empty(self, issues: list, fragment: str) -> list:
        return [issue for issue in issues if f"carries an empty {fragment} value" in issue.description]

    def test_metadata_empty_units_string_is_flagged_with_register_expectation(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={"points": {"phase_1_line_current_sensor": {"units": ""}}}
                ),
            }
        )
        empty = self._empty(issues, "units")
        self.assertEqual(len(empty), 1)
        self.assertTrue(empty[0].issue_id.startswith("UDMI-MD"))
        self.assertEqual(empty[0].severity, "high")
        self.assertEqual(empty[0].point_name, "phase_1_line_current_sensor")
        self.assertEqual(empty[0].observed_value, '""')
        self.assertEqual(empty[0].expected_value, "amperes")
        self.assertIn("The register expects amperes.", empty[0].description)
        self.assertNotIn(
            "does not declare units", " ".join(issue.description for issue in issues)
        )

    def test_metadata_null_units_is_flagged_and_observed_reads_null(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={"points": {"phase_1_line_current_sensor": {"units": None}}}
                ),
            }
        )
        empty = self._empty(issues, "units")
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].observed_value, "null")
        self.assertNotIn(
            "does not declare units", " ".join(issue.description for issue in issues)
        )

    def test_metadata_whitespace_units_is_flagged(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={"points": {"phase_1_line_current_sensor": {"units": "   "}}}
                ),
            }
        )
        empty = self._empty(issues, "units")
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].observed_value, '"   "')

    def test_unlinked_sensor_empty_units_without_register_expectation_is_flagged(self) -> None:
        # A point the register carries no expected unit for (a sensor never
        # linked to the register) still trips the empty-value check.
        issues = _issues(
            {
                "expected_schedule": _schedule(units={}),
                "metadata_payload": _metadata(
                    pointset={"points": {"spare_sensor": {"units": ""}}}
                ),
            }
        )
        empty = self._empty(issues, "units")
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].point_name, "spare_sensor")
        self.assertEqual(empty[0].expected_value, "a non-empty value")
        self.assertNotIn("The register expects", empty[0].description)

    def test_pointset_null_present_value_is_flagged(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(
                    points={"phase_1_line_current_sensor": {"present_value": None}}
                ),
            }
        )
        empty = self._empty(issues, "present_value")
        self.assertEqual(len(empty), 1)
        self.assertTrue(empty[0].issue_id.startswith("UDMI-PS"))
        self.assertEqual(empty[0].severity, "high")
        self.assertEqual(empty[0].observed_value, "null")

    def test_pointset_empty_present_value_reroutes_away_from_numeric_complaint(self) -> None:
        # "" against a numeric metadata unit must report as the precise
        # empty-value fault, NOT the confusing critical "should be numeric".
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(
                    points={"phase_1_line_current_sensor": {"present_value": ""}}
                ),
            }
        )
        empty = self._empty(issues, "present_value")
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].observed_value, '""')
        self.assertNotIn(
            "should be numeric", " ".join(issue.description for issue in issues)
        )

    def test_pointset_whitespace_present_value_is_flagged(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(),
                "pointset_payload": _pointset(
                    points={"phase_1_line_current_sensor": {"present_value": "   "}}
                ),
            }
        )
        empty = self._empty(issues, "present_value")
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].observed_value, '"   "')
        self.assertNotIn(
            "should be numeric", " ".join(issue.description for issue in issues)
        )

    def test_real_present_values_are_never_read_as_empty(self) -> None:
        # 0, 0.0, False and a non-blank string are real observations, not empty.
        for real_value in (0, 0.0, False, "ok"):
            with self.subTest(value=real_value):
                issues = _issues(
                    {
                        "expected_schedule": _schedule(),
                        "metadata_payload": _metadata(),
                        "pointset_payload": _pointset(
                            points={"phase_1_line_current_sensor": {"present_value": real_value}}
                        ),
                    }
                )
                self.assertFalse(self._empty(issues, "present_value"))

    def test_empty_value_checks_apply_to_multi_asset_entries(self) -> None:
        issues = _issues(
            {
                "assets": [
                    {
                        "expected_schedule": _schedule(),
                        "metadata_payload": _metadata(),
                        "pointset_payload": _pointset(
                            points={"phase_1_line_current_sensor": {"present_value": None}}
                        ),
                    }
                ]
            }
        )
        self.assertEqual(len(self._empty(issues, "present_value")), 1)

    def test_illegal_field_blank_is_not_double_flagged_by_the_empty_sweep(self) -> None:
        # units in a POINTSET event point and present_value in a METADATA point
        # are illegal per canonical UDMI 1.5.2 (additionalProperties:false); the
        # structural pass already flags them. The empty-value sweep must NOT add
        # a second issue with the opposite (register/publisher) remediation.
        issues = _issues(
            {
                "expected_schedule": _schedule(),
                "metadata_payload": _metadata(
                    pointset={"points": {"phase_1_line_current_sensor": {"units": "amperes", "present_value": ""}}}
                ),
                "pointset_payload": _pointset(
                    points={"phase_1_line_current_sensor": {"present_value": 1.2, "units": ""}}
                ),
            }
        )
        # No empty-value issue on the illegal side of either payload.
        self.assertFalse(self._empty(issues, "present_value"))  # illegal in metadata
        self.assertFalse(self._empty(issues, "units"))  # illegal in pointset
        # The structural pass still reports the stray fields as not allowed.
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn("is not allowed", descriptions)


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
                    asset_id="DEMO-1000001",
                    manufacturer="Schneider",
                    model="PM5121",
                    serial="SN-1",
                    firmware="1.2.3",
                    guid="ifc://changeMe0123",
                    site="ZZ-DEMO-01",
                    room="DEMO-ROOM-01",
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
        self.assertEqual(expected_by_type["metadata"]["system"]["physical_tag"]["asset"], {"guid": "ifc://changeMe0123", "name": "DEMO-1000001"})
        self.assertEqual(expected_by_type["metadata"]["system"]["location"], {"site": "ZZ-DEMO-01", "section": "DEMO-ROOM-01"})
        self.assertEqual(expected_by_type["metadata"]["pointset"]["points"], {"primary_ratio_sensor": {"units": "no_units"}})
        self.assertEqual(expected_by_type["pointset"]["points"], {"primary_ratio_sensor": {"present_value": None}})

    def test_room_that_fits_location_room_is_embedded_without_a_note(self) -> None:
        # "DEMO_ROOM_01" fails the strict section pattern (underscores) but
        # is perfectly canonical as system.location.room — real devices publish
        # either field (on-site 2026-07-13: location.room = "2-09_Meter_Room").
        result = validate_udmi_full_report(
            {"expected_schedule": _schedule(site="ZZ-DEMO-01", room="DEMO_ROOM_01")},
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
            {"site": "ZZ-DEMO-01", "room": "DEMO_ROOM_01"},
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

    def test_register_room_matches_when_section_holds_a_different_subdivision(self) -> None:
        # A device may populate BOTH fields (section = building subdivision,
        # room = the register's room): matching either must pass — comparing
        # against section alone would emit a false mismatch.
        issues = _issues(
            {
                "expected_schedule": _schedule(room="2-09_Meter_Room"),
                "metadata_payload": _metadata(
                    system={"location": {"section": "LEVEL-2", "room": "2-09_Meter_Room"}},
                ),
            }
        )
        room_issues = [issue for issue in issues if "room" in issue.description.casefold()]
        self.assertEqual([issue.description for issue in room_issues], [])

    def test_register_room_matching_neither_field_is_a_single_mismatch(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(room="2-09_Meter_Room"),
                "metadata_payload": _metadata(
                    system={"location": {"section": "LEVEL-2", "room": "OTHER_ROOM"}},
                ),
            }
        )
        mismatches = [issue for issue in issues if "does not match the asset register" in issue.description and "room" in issue.description.casefold()]
        self.assertEqual(len(mismatches), 1)
        # Both observed candidates are shown so the operator sees what the
        # device actually published in each field.
        self.assertEqual(mismatches[0].observed_value, "LEVEL-2 / OTHER_ROOM")

    def test_misplaced_metadata_pointset_reported_once_and_content_still_checked(self) -> None:
        # On-site 2026-07-13 (afternoon): metadata nested the whole pointset
        # under 'system' — every register point read "not defined in the
        # metadata pointset" while plainly visible in MQTT Explorer, and a real
        # device-side typo (phas2_line_current_sensor) hid among the noise.
        issues = _issues(
            {
                "expected_schedule": _schedule(
                    points=["phase1_power_sensor", "phase2_line_current_sensor"],
                    units={"phase1_power_sensor": "kilowatts"},
                ),
                "metadata_payload": {
                    "version": "1.5.2",
                    "timestamp": "2026-07-13T16:44:19Z",
                    "system": {
                        "pointset": {
                            "points": {
                                "phase1_power_sensor": {"units": "kilowatts"},
                                "phas2_line_current_sensor": {"units": "amperes"},
                            }
                        },
                    },
                },
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        misplaced = [
            issue for issue in issues
            if "nests its pointset at system.pointset.points" in issue.description
        ]
        self.assertEqual(len(misplaced), 1)
        self.assertEqual(misplaced[0].severity, "high")
        # Content is compared against the nested copy: the point present there
        # is NOT falsely missing, while the device's typo IS reported both ways.
        self.assertNotIn("Expected point phase1_power_sensor is not defined", descriptions)
        self.assertIn("Expected point phase2_line_current_sensor is not defined", descriptions)
        self.assertIn("Metadata defines point phas2_line_current_sensor", descriptions)

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
        # field engineer's site register: asset IDs like "2001" and free-text rooms can
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


class PointsetMisnamePairingTests(unittest.TestCase):
    """A typo point name is one root cause, not two pointset faults.

    A missing expected point whose spelling nearly matches an unexpected
    received point is reported ONCE, naming both spellings, instead of a
    separate UDMI-PS 'not received' plus 'not in the expected schedule' pair.
    Genuinely different names still report as two issues, and the metadata
    point-coverage checks stay independent per the AGENTS.md contract.
    """

    _MERGED = "probably a single misnamed"
    _MISSING = "was not received in the pointset payload"
    _UNEXPECTED = "was not found in the expected schedule"

    def test_single_typo_reports_one_pointset_issue_naming_both_spellings(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["phase2_line_current_sensor"], units={}),
                "pointset_payload": _pointset(points={"phas2_line_current_sensor": {"present_value": 1.2}}),
            }
        )
        merged = [issue for issue in issues if self._MERGED in issue.description]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].issue_type, "pointset_validation")
        self.assertTrue(merged[0].issue_id.startswith("UDMI-PS"))
        self.assertEqual(merged[0].severity, "high")
        self.assertEqual(merged[0].point_name, "phase2_line_current_sensor")
        self.assertEqual(merged[0].expected_value, "phase2_line_current_sensor")
        self.assertEqual(merged[0].observed_value, "phas2_line_current_sensor")
        self.assertIn("phase2_line_current_sensor", merged[0].description)
        self.assertIn("phas2_line_current_sensor", merged[0].description)
        # Neither single-sided message survives for the paired names.
        descriptions = " ".join(issue.description for issue in issues)
        self.assertNotIn(self._MISSING, descriptions)
        self.assertNotIn(self._UNEXPECTED, descriptions)

    def test_dissimilar_missing_and_received_points_stay_two_issues(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["supply_air_temperature"], units={}),
                "pointset_payload": _pointset(points={"totally_different_flow": {"present_value": 3}}),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn(
            "Expected point supply_air_temperature was not received in the pointset payload.",
            descriptions,
        )
        self.assertIn(
            "Received point totally_different_flow was not found in the expected schedule.",
            descriptions,
        )
        self.assertNotIn(self._MERGED, descriptions)

    def test_digit_indexed_points_stay_two_independent_issues(self) -> None:
        # phase1 vs phase2 score ~0.96 but are distinct physical measurements,
        # not one misspelling. Merging them would drop two faults to one and
        # steer the operator to rename phase-2 current onto the phase-1 row.
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["phase1_line_current_sensor"], units={}),
                "pointset_payload": _pointset(points={"phase2_line_current_sensor": {"present_value": 2}}),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertNotIn(self._MERGED, descriptions)
        self.assertIn(
            "Expected point phase1_line_current_sensor was not received in the pointset payload.",
            descriptions,
        )
        self.assertIn(
            "Received point phase2_line_current_sensor was not found in the expected schedule.",
            descriptions,
        )

    def test_indexed_siblings_do_not_arbitrarily_merge_one(self) -> None:
        # expected [phase1_x], observed [phase2_x, phase3_x]: the two candidates
        # tie on ratio, so a merge would arbitrarily rename one sibling and leave
        # the other unexpected. All three stay independent faults.
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["phase1_flow_sensor"], units={}),
                "pointset_payload": _pointset(
                    points={
                        "phase2_flow_sensor": {"present_value": 1},
                        "phase3_flow_sensor": {"present_value": 2},
                    }
                ),
            }
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertNotIn(self._MERGED, descriptions)
        self.assertIn(
            "Expected point phase1_flow_sensor was not received in the pointset payload.",
            descriptions,
        )
        self.assertIn(
            "Received point phase2_flow_sensor was not found in the expected schedule.",
            descriptions,
        )
        self.assertIn(
            "Received point phase3_flow_sensor was not found in the expected schedule.",
            descriptions,
        )

    def test_two_simultaneous_typos_pair_one_to_one(self) -> None:
        issues = _issues(
            {
                "expected_schedule": _schedule(
                    points=["phase1_power_sensor", "phase2_line_current_sensor"], units={}
                ),
                "pointset_payload": _pointset(
                    points={
                        "phas1_power_sensor": {"present_value": 1},
                        "phas2_line_current_sensor": {"present_value": 2},
                    }
                ),
            }
        )
        merged = [issue for issue in issues if self._MERGED in issue.description]
        self.assertEqual(len(merged), 2)
        pairs = {(issue.expected_value, issue.observed_value) for issue in merged}
        self.assertEqual(
            pairs,
            {
                ("phase1_power_sensor", "phas1_power_sensor"),
                ("phase2_line_current_sensor", "phas2_line_current_sensor"),
            },
        )
        descriptions = " ".join(issue.description for issue in issues)
        self.assertNotIn(self._MISSING, descriptions)
        self.assertNotIn(self._UNEXPECTED, descriptions)

    def test_offline_device_with_no_pointset_payload_is_not_a_rename(self) -> None:
        # No pointset payload => nothing 'unexpected' to pair against, so a
        # missing expected point stays the plain 'not received' fault.
        descriptions = _descriptions(
            {"expected_schedule": _schedule(points=["phase2_line_current_sensor"], units={})}
        )
        self.assertIn(
            "Expected point phase2_line_current_sensor was not received in the pointset payload.",
            descriptions,
        )
        self.assertNotIn(self._MERGED, descriptions)

    def test_pointset_typo_merges_while_metadata_typo_stays_independent(self) -> None:
        # AGENTS.md: schema/register checks are independent. The pointset side
        # pairs the typo into one issue; the metadata point-coverage check still
        # reports the same typo both ways (missing-expected + extra-metadata).
        issues = _issues(
            {
                "expected_schedule": _schedule(points=["phase2_line_current_sensor"], units={}),
                "metadata_payload": _metadata(
                    pointset={"points": {"phas2_line_current_sensor": {"units": "amperes"}}}
                ),
                "pointset_payload": _pointset(points={"phas2_line_current_sensor": {"present_value": 2}}),
            }
        )
        pointset_issues = [issue for issue in issues if issue.issue_type == "pointset_validation"]
        self.assertEqual(len(pointset_issues), 1)
        self.assertIn(self._MERGED, pointset_issues[0].description)
        descriptions = " ".join(issue.description for issue in issues)
        self.assertIn(
            "Expected point phase2_line_current_sensor is not defined in the metadata pointset.",
            descriptions,
        )
        self.assertIn("Metadata defines point phas2_line_current_sensor", descriptions)


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
                    {"asset_id": "DEMO-1000002", "topic_roots": ["demo-site/DEMO-1000001", "demo-site/DEMO-1000002"]},
                ],
            },
            live_capture=None,
        )
        collisions = [issue for issue in result.issues if issue.issue_type == "register_import"]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].severity, "high")
        self.assertIn("multiple rows with Asset ID 'DEMO-1000002'", collisions[0].description)
        self.assertIn("demo-site/DEMO-1000001, demo-site/DEMO-1000002", collisions[0].description)


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


class ProgressRecordingCapture(RecordingCapture):
    """Capture fake that mirrors the transport's per-message callback."""

    def __call__(self, _settings: object, **kwargs: object) -> list[MqttMessage]:
        self.calls.append(kwargs)
        on_message = kwargs.get("on_message")
        for message in self.messages:
            if callable(on_message):
                on_message(message)
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
        # The summary stays "indefinite" but the transport is handed the 48h
        # backstop so a never-publishing device can't hang the capture forever.
        self.assertEqual(call["timeout_seconds"], INDEFINITE_BACKSTOP_SECONDS)
        self.assertTrue(callable(call["cancel_check"]))
        self.assertEqual(call["max_messages"], DEFAULT_MAX_MESSAGES)
        self.assertEqual(result.result_summary["capture_mode"], "indefinite")
        self.assertIsNone(result.result_summary["capture_window_seconds"])
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
        self.assertEqual(stale[0].issue_type, "pointset_timestamp")
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
        self.assertEqual(call["timeout_seconds"], INDEFINITE_BACKSTOP_SECONDS)
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


class PointsetTimestampDiagnosisTests(unittest.TestCase):
    """Freshness issues carry the pointset_timestamp category (UDMI-TS) so they
    filter apart from data faults, and name a whole-hour clock-labelling cause
    (a device stamping LOCAL wall time, e.g. BST, but labelling it 'Z') when the
    age is a near-exact hour — while a genuine non-whole-hour stale still reads
    as a cadence fault with no misleading clock hint.
    """

    def _freshness(self, *, payload_ts: str, observed_ts: str, interval: str):
        return _pointset_freshness_issue(
            parameters={"pointset_payload_received_at": observed_ts},
            expected={"reporting_interval_seconds": interval},
            pointset_payload={"timestamp": payload_ts},
            issues=[],
            asset_id="EM-1",
            raw_evidence_uri="mqtt://capture/pointset",
        )

    def test_whole_hour_future_names_clock_labelling_offset(self) -> None:
        # Stamp reads one hour AHEAD of the capture clock (age -3600s): the future
        # trigger fires and the whole-hour cause is named, but it is still reported.
        issue = self._freshness(
            payload_ts="2026-07-09T11:00:00Z",
            observed_ts="2026-07-09T10:00:00Z",
            interval="60",
        )
        assert issue is not None
        self.assertEqual(issue.issue_type, "pointset_timestamp")
        self.assertTrue(issue.issue_id.startswith("UDMI-TS-"))
        self.assertIn("too far in the future", issue.description)
        self.assertIn("whole-hour clock-labelling offset, about 1 hour", issue.description)

    def test_whole_hour_past_keeps_clock_and_cadence_diagnoses(self) -> None:
        # A payload exactly one hour old could be a local-clock label or a device
        # that genuinely stopped publishing. The evidence cannot choose between
        # them, so the finding must tell the operator to check both.
        issue = self._freshness(
            payload_ts="2026-07-09T09:00:00Z",
            observed_ts="2026-07-09T10:00:00Z",
            interval="60",
        )
        assert issue is not None
        self.assertEqual(issue.issue_type, "pointset_timestamp")
        self.assertTrue(issue.issue_id.startswith("UDMI-TS-"))
        self.assertIn("reporting interval", issue.description)
        self.assertIn("clock-labelling error or genuine stale publishing", issue.description)
        self.assertIn("publish cadence", issue.description)

    def test_genuine_stale_reads_as_cadence_fault_without_clock_hint(self) -> None:
        # 45s old against a 20s cadence is a real freshness miss, not a whole-hour
        # offset: it keeps the new category but must NOT claim a clock-labelling cause.
        issue = self._freshness(
            payload_ts="2026-07-09T09:59:15Z",
            observed_ts="2026-07-09T10:00:00Z",
            interval="20",
        )
        assert issue is not None
        self.assertEqual(issue.issue_type, "pointset_timestamp")
        self.assertIn("reporting interval", issue.description)
        self.assertNotIn("clock-labelling", issue.description)

    def test_long_cadence_stale_omits_clock_labelling_hint(self) -> None:
        # A >=30-min cadence makes the whole-hour residual window (max 1800s) span
        # every possible age, so an uncapped tolerance would stamp the clock hint
        # onto a genuinely dead device. A half-hourly register 45 min stale must
        # read as a plain cadence fault, with no clock-labelling claim.
        issue = self._freshness(
            payload_ts="2026-07-09T09:15:00Z",
            observed_ts="2026-07-09T10:00:00Z",
            interval="1800",
        )
        assert issue is not None
        self.assertEqual(issue.issue_type, "pointset_timestamp")
        self.assertIn("reporting interval", issue.description)
        self.assertNotIn("clock-labelling", issue.description)


class _FakeRunStore:
    """Minimal cancellable run store for exercising the processor without a DB."""

    def __init__(self, *, cancel: bool = False, cancellable: bool = True) -> None:
        self.cancel = cancel
        self.status_calls: list[dict] = []
        self.summaries: list[dict] = []
        self.issue_snapshots: list[list] = []
        self.current_summary: dict = {}
        if not cancellable:
            # Hide the cancel API entirely: the processor must then pass no
            # cancel_check and the engine bounds indefinite captures itself.
            self.is_cancel_requested = None  # type: ignore[assignment]

    def update_run_status(self, run_id: str, **kwargs: object) -> dict:
        record = {"run_id": run_id, **kwargs}
        self.status_calls.append(record)
        return record

    def update_result_summary(self, run_id: str, summary: dict, merge: bool = True) -> None:
        if merge:
            self.current_summary.update(summary)
        else:
            self.current_summary = dict(summary)
        self.summaries.append(dict(self.current_summary))

    def replace_issues(self, run_id: str, issues: list) -> None:
        self.issues = issues
        self.issue_snapshots.append(list(issues))

    def is_cancel_requested(self, run_id: str) -> bool:
        return self.cancel


_PROCESSOR_PARAMS = {**_BROKER, **_TOPICS, "capture_seconds": 0}


class UdmiProcessorCancelAndInlineGuardTests(unittest.TestCase):
    def test_live_messages_persist_provisional_results_before_terminal(self) -> None:
        store = _FakeRunStore()
        parameters = {
            **_BROKER,
            "capture_seconds": 1,
            "assets": [
                {
                    "expected_schedule": {"asset_id": "A1", "system": "BMS"},
                    "state_topic": "site/a1/state",
                    "metadata_topic": "site/a1/metadata",
                },
                {
                    "expected_schedule": {"asset_id": "A2", "system": "Lighting"},
                    "state_topic": "site/a2/state",
                },
            ],
        }

        record = process_udmi_validation_run(
            "run-progress",
            parameters,
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=ProgressRecordingCapture([_msg("site/a1/state")]),
        )

        provisional = [summary for summary in store.summaries if summary.get("provisional") is True]
        self.assertEqual(len(provisional), 1)
        snapshot = provisional[0]
        self.assertEqual(snapshot["broker_status_detail"], "live_capture_in_progress")
        self.assertEqual(snapshot["message_count"], 1)
        self.assertEqual(snapshot["publishing_seen"], 1)
        self.assertEqual(snapshot["not_publishing_devices"], [])
        self.assertEqual(snapshot["validation_summary_v1"]["asset_metrics"]["observed"], 1)
        self.assertEqual(snapshot["validation_summary_v1"]["payload_metrics"]["received"], 1)
        self.assertEqual(snapshot["payload_views"][0]["system"], "BMS")
        # Silence is not a fault until the window closes. A2 can still publish.
        self.assertFalse(
            any(issue.issue_type == "not_publishing" for issue in store.issue_snapshots[0])
        )

        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["provisional"])
        self.assertTrue(any(issue.issue_type == "not_publishing" for issue in store.issues))
        self.assertTrue(
            any(call.get("stage") == "capturing_live_mqtt" for call in store.status_calls)
        )

    def test_progress_persistence_is_throttled_but_terminal_write_is_not(self) -> None:
        store = _FakeRunStore()
        capture = ProgressRecordingCapture(
            [
                _msg("a/b/state", b'{"n":1}'),
                _msg("a/b/state", b'{"n":2}'),
                _msg("a/b/state", b'{"n":3}'),
            ]
        )

        with patch(
            "smart_commissioning_core.udmi_run_processor.time.monotonic",
            side_effect=[0.0, 0.2, 1.2],
        ):
            record = process_udmi_validation_run(
                "run-throttle",
                {**_BROKER, "capture_seconds": 1, "state_topic": "a/b/state"},
                run_store=store,
                execution_mode="dramatiq_worker",
                live_capture=capture,
            )

        provisional = [summary for summary in store.summaries if summary.get("provisional") is True]
        self.assertEqual([summary["message_count"] for summary in provisional], [1, 3])
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["provisional"])

    def test_progress_persistence_failure_does_not_abort_capture(self) -> None:
        class FailingProgressStore(_FakeRunStore):
            def update_result_summary(
                self, run_id: str, summary: dict, merge: bool = True
            ) -> None:
                if summary.get("provisional") is True:
                    raise OSError("database unavailable secret=do-not-log")
                super().update_result_summary(run_id, summary, merge)

        store = FailingProgressStore()
        with self.assertLogs(
            "smart_commissioning_core.udmi_run_processor", level="WARNING"
        ) as logs:
            record = process_udmi_validation_run(
                "run-progress-store-failure",
                {**_BROKER, "capture_seconds": 1, "state_topic": "a/b/state"},
                run_store=store,
                execution_mode="dramatiq_worker",
                live_capture=ProgressRecordingCapture([_msg("a/b/state")]),
            )

        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["provisional"])
        self.assertEqual(store.summaries[-1]["message_count"], 1)
        self.assertIn("OSError", logs.output[0])
        self.assertNotIn("do-not-log", logs.output[0])

    def test_outer_validation_failure_preserves_last_partial_snapshot(self) -> None:
        store = _FakeRunStore()

        def fail_after_message(_settings: object, **kwargs: object) -> list[MqttMessage]:
            on_message = kwargs["on_message"]
            on_message(_msg("a/b/state"))
            raise RuntimeError("unexpected validator failure")

        record = process_udmi_validation_run(
            "run-fail-after-progress",
            {**_BROKER, "capture_seconds": 1, "state_topic": "a/b/state"},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=fail_after_message,
        )

        self.assertEqual(record["status"], "failed")
        self.assertTrue(store.current_summary["provisional"])
        self.assertEqual(store.current_summary["message_count"], 1)
        self.assertEqual(store.current_summary["execution_mode"], "dramatiq_worker")
        self.assertEqual(store.current_summary["payload_views"][0]["asset_id"], "UDMI asset")

    def test_worker_mode_honours_indefinite_and_wires_cancel(self) -> None:
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-1", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="dramatiq_worker", live_capture=capture,
        )
        call = capture.calls[-1]
        # Indefinite honoured (summary stays "indefinite"); the transport gets the
        # 48h backstop so a silent device cannot hang the capture forever.
        self.assertEqual(call["timeout_seconds"], INDEFINITE_BACKSTOP_SECONDS)
        self.assertTrue(callable(call["cancel_check"]))
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["indefinite_bounded_inline"])

    def test_inline_mode_honours_indefinite_when_cancel_path_exists(self) -> None:
        # The store advertises is_cancel_requested (every real RunService does),
        # so a blank request is honoured as indefinite on the inline path too — the
        # run is backgrounded and Stop run can end it. No downgrade is flagged.
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-2", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="inline_local_fallback", live_capture=capture,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], INDEFINITE_BACKSTOP_SECONDS)
        self.assertEqual(record["status"], "succeeded")
        self.assertFalse(store.summaries[-1]["indefinite_bounded_inline"])

    def test_inline_mode_bounds_indefinite_when_no_cancel_path(self) -> None:
        # A store with NO cancel path cannot be stopped, so a blank request is
        # bounded to the default window (by udmi_validation._capture_window) and
        # the downgrade is flagged honestly in the summary.
        store = _FakeRunStore()
        store.is_cancel_requested = None  # simulate a store with no cancel path
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-2b", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="inline_local_fallback", live_capture=capture,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], DEFAULT_CAPTURE_SECONDS)
        self.assertEqual(record["status"], "succeeded")
        self.assertTrue(store.summaries[-1]["indefinite_bounded_inline"])

    def test_sync_inline_bounds_indefinite_even_with_cancel_path(self) -> None:
        # A cancel path exists, but the run is NOT backgrounded: a synchronous
        # inline run blocks the HTTP request until it finishes, so the client never
        # receives a run_id and cannot reach Stop run. A blank window is bounded to
        # the default and the downgrade is flagged — a silent broker cannot hold the
        # request thread up to the 48h backstop.
        store = _FakeRunStore()
        capture = RecordingCapture(list(_ALL_TOPIC_MESSAGES))
        record = process_udmi_validation_run(
            "run-2c", dict(_PROCESSOR_PARAMS), run_store=store,
            execution_mode="inline_local_fallback", live_capture=capture,
            run_is_backgrounded=False,
        )
        self.assertEqual(capture.calls[-1]["timeout_seconds"], DEFAULT_CAPTURE_SECONDS)
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

    def test_live_capture_timeout_succeeds_with_silent_devices(self) -> None:
        # Field ask 2026-07-15: "it can't fail the whole validation just because
        # one device isn't responding." A completed capture window with a silent
        # device ends succeeded (distinct stage) and lands on Results; the
        # not_publishing issue carries the story, so no error_message is set.
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-timeout",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=RecordingCapture([]),
        )
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["stage"], "udmi_validation_complete_with_silent_devices")
        self.assertNotIn("error_message", record)
        self.assertEqual(store.summaries[-1]["broker_status_detail"], "live_capture_timeout")
        self.assertTrue(any(issue.issue_type == "not_publishing" for issue in store.issues))

    def test_partial_capture_succeeds_and_names_silent_topics(self) -> None:
        # field engineer's "expected 4 / publishing 3" shape: one topic reports, the rest
        # are silent. The window completed, so the run succeeds and the silent
        # topics are named in a not_publishing issue.
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-partial",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=RecordingCapture([_msg("a/b/state")]),
        )
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["stage"], "udmi_validation_complete_with_silent_devices")
        self.assertEqual(store.summaries[-1]["captured_topics"], ["a/b/state"])
        not_publishing = "\n".join(
            issue.description for issue in store.issues if issue.issue_type == "not_publishing"
        )
        self.assertIn("a/b/metadata", not_publishing)

    def test_full_capture_keeps_the_plain_complete_stage(self) -> None:
        # A complete capture must never leak the silent-device stage.
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-full",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=RecordingCapture(list(_ALL_TOPIC_MESSAGES)),
        )
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["stage"], "udmi_fixture_validation_complete")

    def test_capture_unavailable_still_fails(self) -> None:
        # Allowlist boundary: a config/context failure (no live_capture wired)
        # is NOT a silent device — it must stay failed.
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-unavailable",
            {**_PROCESSOR_PARAMS, "capture_seconds": 1},
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=None,
        )
        self.assertEqual(record["status"], "failed")
        self.assertIn("live_capture_unavailable", record["error_message"])

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

    def test_offset_less_timestamp_does_not_crash_the_run(self) -> None:
        # A single controller stamping an offset-less (naive) timestamp used to
        # make max() compare a naive sort key against an aware one, raising a
        # TypeError that failed the WHOLE run with the sanitized message. The key
        # now ranks valid aware timestamps ahead of invalid wall-clock values, so
        # the run succeeds without inventing UTC for the naive stamp.
        store = _FakeRunStore()
        record = process_udmi_validation_run(
            "run-naive-ts",
            {
                "expected_schedule": _schedule(),
                "state_payload": _state(timestamp="2026-07-09T10:45:00Z"),
                "metadata_payload": _metadata(timestamp="2026-07-09T10:00:00Z"),
                "pointset_payload": _pointset(timestamp="2026-07-09T11:30:00"),
            },
            run_store=store,
            execution_mode="dramatiq_worker",
            live_capture=None,
        )
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(store.summaries[-1]["payload_last_seen"], "2026-07-09T10:45:00Z")
        self.assertTrue(
            any("not an RFC 3339 date-time string" in issue.description for issue in store.issues)
        )

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
