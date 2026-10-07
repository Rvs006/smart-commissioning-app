"""Reproducibility and normalization checks for the pinned DBO vocabulary."""

import hashlib
import unittest
from pathlib import Path

from smart_commissioning_core import dbo_units
from smart_commissioning_core.dbo_units import (
    DBO_UNIT_NAMES,
    KNOWN_CANONICAL_UNITS,
    NUMERIC_CANONICAL_UNITS,
    canonical_unit,
    custom_canonical_units,
    custom_unit_names,
)


class DboUnitVocabularyTests(unittest.TestCase):
    def test_bundled_license_matches_pinned_upstream_bytes(self) -> None:
        license_path = (
            Path(dbo_units.__file__).resolve().parent / "schemas" / "dbo" / "LICENSE"
        )
        self.assertEqual(
            hashlib.sha256(license_path.read_bytes()).hexdigest(),
            "2faa193b8d0f280023bb378bf2808f3b4cbff64607c6c0d05093b4b2578b108e",
        )

    def test_pinned_derived_name_set_has_expected_shape(self) -> None:
        ordered = sorted(DBO_UNIT_NAMES)
        self.assertEqual(len(ordered), 191)
        self.assertEqual(ordered[0], "ampere_square_meters")
        self.assertEqual(ordered[-1], "weeks")
        self.assertIn("parts_per_billion", DBO_UNIT_NAMES)

    def test_ppb_alias_preserves_the_billion_scale(self) -> None:
        self.assertEqual(canonical_unit("ppb"), "parts-per-billion")
        self.assertEqual(canonical_unit("parts_per_billion"), "parts-per-billion")
        self.assertEqual(canonical_unit("ppm"), "parts-per-million")
        self.assertNotEqual(canonical_unit("ppb"), canonical_unit("ppm"))
        self.assertIn("parts-per-billion", NUMERIC_CANONICAL_UNITS)

    def test_custom_unit_names_split_strip_and_dedupe_in_order(self) -> None:
        self.assertEqual(
            custom_unit_names(" milligrams_per_liter,\r\nntu; milligrams_per_liter ,, "),
            ["milligrams_per_liter", "ntu"],
        )
        self.assertEqual(custom_unit_names(["ntu", " ntu ", ""]), ["ntu"])
        self.assertEqual(custom_unit_names(None), [])
        self.assertEqual(custom_unit_names(""), [])

    def test_custom_units_share_the_canonical_unit_form(self) -> None:
        self.assertEqual(
            custom_canonical_units("Milligrams Per Liter\nmilligrams-per-liter"),
            frozenset({"milligrams-per-liter"}),
        )
        # The reason the setting exists: DBO itself has no mg/L unit.
        self.assertNotIn("milligrams-per-liter", KNOWN_CANONICAL_UNITS)


if __name__ == "__main__":
    unittest.main()
