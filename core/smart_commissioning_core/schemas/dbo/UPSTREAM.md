# Pinned Digital Buildings Ontology units

`smart_commissioning_core.dbo_units` is derived from the unit names in Google's
Digital Buildings repository:

- upstream file: `ontology/yaml/resources/units/units.yaml`
- repository: <https://github.com/google/digitalbuildings>
- pinned commit: `ad1e627de2b05aa7c0b9ee226ab16069d0d78ad5`
- raw file SHA-256: `e12a590b982e6e0063719f3d18a77a79291a7a9b12d1b195f7ed19cbed0e6e6b`
- derived unit-name count: `191`
- raw source: <https://raw.githubusercontent.com/google/digitalbuildings/ad1e627de2b05aa7c0b9ee226ab16069d0d78ad5/ontology/yaml/resources/units/units.yaml>

The Python module changes underscore separators to the app's established
hyphenated comparison form and adds documented field shorthand such as `ppm`,
`ppb`, `kWh`, and `cfm`. It does not change conversion factors or treat ppm and
ppb as equivalent.

The adjacent `LICENSE` is the upstream Apache License 2.0.
