"""The downloadable templates must import cleanly into their own profile.

Each profile ships an example row that the template download writes into the
sheet, so an operator's first action is often to upload that file unchanged. If
a profile's validators reject its own example, the tool rejects the very row it
told the operator to use -- the failure mode this suite guards against.

Pure validation over the profile tables: no database, no HTTP client (CI runs
these under stdlib ``unittest``, which does not load ``conftest.py``).
"""

import unittest

from app.services.import_service import EXAMPLE_ROWS, PROFILES
from smart_commissioning_core.engines.comparison_common import parse_tolerance


class ExampleRowTests(unittest.TestCase):
    def test_every_example_row_passes_its_own_profile(self) -> None:
        for import_type, profile in PROFILES.items():
            with self.subTest(import_type=import_type):
                example = EXAMPLE_ROWS[import_type]
                errors = profile.validate_row(dict(example), row_number=2)
                self.assertEqual(
                    [],
                    [(error.field, error.code, error.message) for error in errors],
                    f"the {import_type} template's example row must import unchanged",
                )

    def test_tolerances_example_row_is_accepted(self) -> None:
        # The regression: "Tolerance" was checked with the integer-only
        # _validate_numeric (value.isdigit()), so the example "0.5" was
        # rejected as invalid_numeric.
        profile = PROFILES["tolerances"]
        example = EXAMPLE_ROWS["tolerances"]
        self.assertEqual("0.5", example["Tolerance"], "example tolerance should stay a decimal")
        self.assertEqual([], profile.validate_row(dict(example), row_number=2))


class ToleranceValidationTests(unittest.TestCase):
    """The import gate must accept exactly what the comparison engine parses."""

    def _errors(self, value: str) -> list[tuple[str, str]]:
        row = {"Asset ID": "AHU-L03-017", "Point name": "supply_air_temperature_sensor", "Tolerance": value}
        return [(error.field, error.code) for error in PROFILES["tolerances"].validate_row(row, row_number=2)]

    def test_engine_parseable_tolerances_are_accepted(self) -> None:
        for value in ("0.5", "2", "5%", "abs:0.5", "percent:5", "0"):
            with self.subTest(tolerance=value):
                self.assertIsNotNone(parse_tolerance(value), "precondition: the engine parses this form")
                self.assertEqual([], self._errors(value))

    def test_unparseable_tolerance_is_rejected(self) -> None:
        for value in ("abc", "0.5 degrees", "%"):
            with self.subTest(tolerance=value):
                self.assertIsNone(parse_tolerance(value), "precondition: the engine cannot parse this form")
                self.assertEqual([("Tolerance", "invalid_tolerance")], self._errors(value))

    def test_blank_tolerance_reports_only_the_required_field_error(self) -> None:
        # Blank is the required-column check's business, not the validator's:
        # one empty cell should not produce two errors for the same field.
        self.assertEqual([("Tolerance", "empty_required_field")], self._errors(""))


class NumericValidationTests(unittest.TestCase):
    def test_integer_fields_still_reject_decimals(self) -> None:
        # Guards the fix's blast radius: BACnet device instance is an integer
        # identity, and must not inherit the tolerance field's decimal grammar.
        row = {
            "Asset ID": "AHU-L03-017",
            "BACnet device instance": "1532117.5",
            "BACnet object type": "analogInput",
            "BACnet object instance": "300001",
            "BACnet object name": "supply_air_temperature",
            "BACnet units": "degrees-celsius",
            "MQTT topic": "electracom/sct/1532/ahu/l03/events/pointset",
            "MQTT field/path": "pointset.points.supply_air_temperature_sensor.present_value",
            "MQTT units": "degrees-celsius",
            "Tolerance": "0.5",
            "Mapping required flag": "required",
        }
        errors = [(error.field, error.code) for error in PROFILES["mapping"].validate_row(row, row_number=2)]
        self.assertEqual([("BACnet device instance", "invalid_numeric")], errors)


if __name__ == "__main__":
    unittest.main()
