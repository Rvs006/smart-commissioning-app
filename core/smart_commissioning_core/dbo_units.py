"""Pinned Digital Buildings Ontology unit vocabulary.

The names below are derived from Google Digital Buildings' Apache-2.0
``ontology/yaml/resources/units/units.yaml`` at commit
``ad1e627de2b05aa7c0b9ee226ab16069d0d78ad5``.  Keeping the vocabulary in a
small Python module makes validation deterministic and network-free.  The
upstream metadata and license are packaged under ``schemas/dbo``.

The source vocabulary uses underscores.  Public helpers return the app's
existing hyphenated comparison form so register CSVs and UDMI payloads can use
either separator without changing a unit's meaning.

Copyright 2020 Google LLC. Licensed under the Apache License, Version 2.0.
This derived file adds aliases used by the Smart Commissioning Tool.
"""

from __future__ import annotations

import re

DBO_UNIT_NAMES = frozenset(
    {
        "amperes",
        "amperes_per_meter",
        "amperes_per_square_meter",
        "ampere_square_meters",
        "atmospheres",
        "awair",
        "bars",
        "btus",
        "btus_per_hour",
        "btus_per_pound",
        "btus_per_pound_dry_air",
        "candelas",
        "candelas_per_square_meter",
        "centimeters",
        "centimeters_of_mercury",
        "centimeters_of_water",
        "cubic_feet",
        "cubic_feet_per_hour",
        "cubic_feet_per_minute",
        "cubic_feet_per_second",
        "cubic_meters",
        "cubic_meters_per_hour",
        "cubic_meters_per_minute",
        "cubic_meters_per_second",
        "cycles_per_hour",
        "cycles_per_minute",
        "days",
        "decibels",
        "degree_days_celsius",
        "degree_days_fahrenheit",
        "degree_days_kelvin",
        "degrees",
        "degrees_celsius",
        "degrees_fahrenheit",
        "degrees_phase",
        "eea",
        "epa",
        "farads",
        "feet",
        "feet_per_minute",
        "feet_per_second",
        "feet_per_square_second",
        "foot_candles",
        "grams",
        "grams_per_cubic_meter",
        "grams_per_minute",
        "grams_per_second",
        "gravity",
        "hectopascals",
        "henrys",
        "hertz",
        "horsepower",
        "hours",
        "hundredths_seconds",
        "imperial_gallons",
        "imperial_gallons_per_minute",
        "inches",
        "inches_of_mercury",
        "inches_of_water",
        "inv_second",
        "joules",
        "joules_per_kelvin",
        "joules_per_kilogram",
        "joules_per_kilogram_dry_air",
        "joules_per_kilogram_kelvin",
        "joule_seconds",
        "kelvin",
        "kilograms",
        "kilograms_per_cubic_meter",
        "kilograms_per_hour",
        "kilograms_per_minute",
        "kilograms_per_second",
        "kilohertz",
        "kilobtus",
        "kilobtus_per_hour",
        "kilojoules",
        "kilojoules_per_kelvin",
        "kilojoules_per_kilogram",
        "kilojoules_per_kilogram_dry_air",
        "kiloohm_centimeters",
        "kiloohms",
        "kilometers_per_hour",
        "kilopascals",
        "kilovolt_amperes",
        "kilovolt_amperes_apparent",
        "kilovolt_amperes_reactive",
        "kilovolt_ampere_apparent_hours",
        "kilovolt_ampere_hours",
        "kilovolt_ampere_reactive_hours",
        "kilovolts",
        "kilowatt_hours",
        "kilowatts",
        "kilowatts_per_square_meter_irradiance",
        "kilowatts_per_tons_of_refrigeration",
        "liters",
        "liters_per_hour",
        "liters_per_minute",
        "liters_per_second",
        "lumens",
        "lux",
        "megabtus",
        "megabtus_per_hour",
        "megahertz",
        "megajoules",
        "megajoules_per_kelvin",
        "megajoules_per_kilogram_dry_air",
        "megaohms",
        "megavolt_amperes_apparent",
        "megavolt_amperes_reactive",
        "megavolts",
        "megawatt_hours",
        "megawatts",
        "meters",
        "meters_per_hour",
        "meters_per_minute",
        "meters_per_second",
        "meters_per_square_second",
        "metric_tons",
        "metric_tons_per_hour",
        "micrograms_per_cubic_meter",
        "micrometers",
        "microsiemens_per_centimeter",
        "milliamperes",
        "millibar",
        "milligrams_per_cubic_meter",
        "milligravity",
        "millimeters",
        "millimeters_of_mercury",
        "millimeters_per_minute",
        "millimeters_per_second",
        "millimeters_per_square_second",
        "milliohms",
        "milliseconds",
        "millivolts",
        "milliwatts",
        "miles_per_hour",
        "minutes",
        "newton",
        "newton_meters",
        "newton_seconds",
        "no_units",
        "ohms",
        "ohm_meters",
        "pascals",
        "parts_per_billion",
        "parts_per_cubic_centimeter",
        "parts_per_million",
        "parts_per_unit",
        "percent",
        "percent_obscuration_per_foot",
        "percent_obscuration_per_meter",
        "percent_relative_humidity",
        "pounds_force_per_square_inch",
        "pounds_mass",
        "pounds_mass_per_hour",
        "pounds_mass_per_minute",
        "pounds_mass_per_second",
        "revolutions_per_minute",
        "seconds",
        "siemens",
        "siemens_per_meter",
        "square_centimeters",
        "square_feet",
        "square_inches",
        "square_meters",
        "teslas",
        "therms",
        "therms_per_hour",
        "tons_of_refrigeration",
        "ton_hours",
        "uba",
        "uk_tons",
        "uk_tons_per_hour",
        "us_gallons",
        "us_gallons_per_hour",
        "us_gallons_per_minute",
        "us_tons",
        "us_tons_per_hour",
        "volt_amperes",
        "volt_amperes_apparent",
        "volt_amperes_reactive",
        "volts",
        "volts_per_meter",
        "watt_hours",
        "watts",
        "watts_per_meter_per_kelvin",
        "watts_per_square_meter_irradiance",
        "watts_per_square_meter_kelvin",
        "watts_per_watts",
        "webers",
        "weeks",
    }
)

# UDMI payloads in the field also use these non-numeric point classifications.
# They are app extensions, kept separate from the pinned DBO names.
APPLICATION_UNIT_NAMES = frozenset({"boolean", "enum"})
KNOWN_UNIT_NAMES = DBO_UNIT_NAMES | APPLICATION_UNIT_NAMES
NUMERIC_UNIT_NAMES = DBO_UNIT_NAMES - {"no_units"}

UNIT_ALIASES = {
    "%": "percent",
    "a": "amperes",
    "amp": "amperes",
    "amps": "amperes",
    "celsius": "degrees-celsius",
    "cfm": "cubic-feet-per-minute",
    "deg-c": "degrees-celsius",
    "degc": "degrees-celsius",
    "hz": "hertz",
    "kva": "kilovolt-amperes",
    "kvar": "kilovolt-amperes-reactive",
    "kw": "kilowatts",
    "kwh": "kilowatt-hours",
    "pa": "pascals",
    "ppb": "parts-per-billion",
    "ppm": "parts-per-million",
    "v": "volts",
}

KNOWN_CANONICAL_UNITS = frozenset(name.replace("_", "-") for name in KNOWN_UNIT_NAMES)
NUMERIC_CANONICAL_UNITS = frozenset(name.replace("_", "-") for name in NUMERIC_UNIT_NAMES)

_SEPARATORS = re.compile(r"[\s_]+")
_EXPLICIT_NO_UNIT = frozenset({"no-unit", "no-units", "none", "unitless"})


def canonical_unit(value: object) -> str | None:
    """Return a canonical hyphenated unit, or ``None`` for a blank value."""
    if value is None:
        return None
    text = str(value).strip().casefold()
    if not text:
        return None
    normalised = _SEPARATORS.sub("-", text)
    if normalised in _EXPLICIT_NO_UNIT:
        return "no-units"
    return UNIT_ALIASES.get(normalised, normalised)


def is_known_unit(value: object, *, allow_blank: bool = False) -> bool:
    """Whether ``value`` resolves to the pinned DBO/app unit vocabulary."""
    canonical = canonical_unit(value)
    return allow_blank if canonical is None else canonical in KNOWN_CANONICAL_UNITS
