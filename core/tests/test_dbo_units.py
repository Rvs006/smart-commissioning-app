"""Reproducibility and normalization checks for the pinned DBO vocabulary."""

import hashlib
import unittest
from pathlib import Path

from smart_commissioning_core import dbo_units
from smart_commissioning_core.dbo_units import (
    DBO_UNIT_NAMES,
    NUMERIC_CANONICAL_UNITS,
    canonical_unit,
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


if __name__ == "__main__":
    unittest.main()
