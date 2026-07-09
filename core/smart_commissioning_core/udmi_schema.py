"""Structural UDMI payload checks keyed by the payload's declared schema version.

The register template carries an "Expected schema version" (e.g. ``1.5.2``) and
every UDMI payload declares its own top-level ``version``. Once those two agree,
the payload's structure is checked against the field rules of that version.

The 1.5.2 rules are grounded in the published UDMI schemas
(github.com/faucetsdn/udmi, tag ``1.5.2``):

- ``state.json`` and ``metadata.json`` require ``timestamp``, ``version`` and
  ``system``.
- ``events_pointset.json`` requires ``timestamp``, ``version`` and ``points``;
  point names match ``^[a-z][a-z0-9]*(_[a-z0-9]+)*$`` and each point entry
  (``events_pointset_point.json``) requires ``present_value``.
- ``model_pointset_point.json`` (metadata points) has no required fields, but
  ``units`` must be a string when present.

These are deliberate field-level structural checks, not a full JSON-Schema
evaluation. ponytail: vendoring the complete google/udmi schema tree plus a
JSON-Schema validator is the ceiling; until then an unknown declared version is
reported honestly as "structural checks skipped", never silently passed.
"""

import re
from dataclasses import dataclass
from datetime import datetime

_POINT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
# RFC 3339 date-time: full date, 'T' separator, full time, and an offset
# (Z or +hh:mm). fromisoformat alone is too lax — it accepts date-only and
# space-separated forms — so shape-check first, then parse for validity.
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

# Required top-level fields per payload type, keyed by declared UDMI version.
_RULES_1_5_2 = {
    "state": ("timestamp", "version", "system"),
    "metadata": ("timestamp", "version", "system"),
    "pointset": ("timestamp", "version", "points"),
}
STRUCTURAL_RULESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "1.5.2": _RULES_1_5_2,
}


@dataclass(frozen=True)
class StructuralFinding:
    description: str
    severity: str
    point_name: str | None = None
    expected_value: str | None = None
    observed_value: str | None = None
    suggested_action: str | None = None


def declared_version(payload: dict) -> str | None:
    """The payload's top-level ``version`` as a normalised string, or None.

    UDMI carries the version as a string ("1.5.2"); very old payloads used a
    bare number, so numbers are accepted and stringified. Anything else (dict,
    list, empty string) reads as "no version declared".
    """
    value = payload.get("version")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def versions_match(expected: str, declared: str) -> bool:
    """Exact version equality, tolerating a leading ``v`` and whitespace."""
    return _normalise_version(expected) == _normalise_version(declared)


def _normalise_version(value: str) -> str:
    return value.strip().lstrip("vV")


def structural_issues(payload_type: str, payload: dict) -> list[StructuralFinding]:
    """Field-level structural findings for one payload against its declared version.

    Returns a single low-severity "checks skipped" finding when no ruleset is
    pinned for the declared version, so a skipped check is never mistaken for a
    passed one. Callers ensure the payload declares a version before calling.
    """
    version = declared_version(payload)
    if version is None:
        return []
    ruleset = STRUCTURAL_RULESETS.get(_normalise_version(version))
    if ruleset is None:
        return [
            StructuralFinding(
                description=(
                    f"No structural ruleset is pinned for UDMI version {version}; "
                    "structural checks were skipped for this payload."
                ),
                severity="low",
                observed_value=version,
                expected_value=", ".join(sorted(STRUCTURAL_RULESETS)),
                suggested_action="Confirm the device's UDMI version, or add a ruleset for it.",
            )
        ]

    findings: list[StructuralFinding] = []
    for field in ruleset.get(payload_type, ()):
        if payload.get(field) is not None:
            continue
        if field == "points" and _nested_pointset_points(payload) is not None:
            # Legacy nested shape: the points exist under pointset.points and
            # the rest of this module validates them there — report the shape
            # deviation once instead of a contradictory "points missing".
            findings.append(
                StructuralFinding(
                    description=(
                        f"The pointset payload nests its points under 'pointset.points'; "
                        f"UDMI {version} expects a top-level 'points' field."
                    ),
                    severity="medium",
                    expected_value="top-level points",
                    observed_value="pointset.points",
                    suggested_action="Move the points map to the payload's top level in the publisher.",
                )
            )
            continue
        findings.append(
            StructuralFinding(
                description=(
                    f"Required field '{field}' is missing from the {payload_type} payload "
                    f"(UDMI {version} {payload_type} schema)."
                ),
                severity="high",
                expected_value="present",
                observed_value="null" if field in payload else "missing",
                suggested_action=f"Fix the publisher so the {payload_type} payload carries '{field}'.",
            )
        )

    findings.extend(_timestamp_findings(payload_type, payload))
    if payload_type in ("state", "metadata"):
        findings.extend(_object_field_findings(payload_type, payload, "system"))
    if payload_type == "pointset":
        findings.extend(_pointset_points_findings(payload))
    if payload_type == "metadata":
        findings.extend(_metadata_pointset_findings(payload))
    return findings


def _timestamp_findings(payload_type: str, payload: dict) -> list[StructuralFinding]:
    timestamp = payload.get("timestamp")
    if timestamp is None:
        return []  # absence is already reported by the required-field check
    if isinstance(timestamp, str) and _RFC3339_PATTERN.match(timestamp):
        try:
            datetime.fromisoformat(timestamp.upper().replace("Z", "+00:00"))
            return []
        except ValueError:
            pass
    return [
        StructuralFinding(
            description=(
                f"The {payload_type} payload timestamp is not an RFC 3339 date-time string."
            ),
            severity="medium",
            expected_value="RFC 3339 date-time, e.g. 2026-07-09T10:47:38Z",
            observed_value=str(timestamp),
            suggested_action="Fix the publisher's timestamp format.",
        )
    ]


def _object_field_findings(payload_type: str, payload: dict, field: str) -> list[StructuralFinding]:
    value = payload.get(field)
    if value is None or isinstance(value, dict):
        return []
    return [
        StructuralFinding(
            description=f"Field '{field}' in the {payload_type} payload must be an object.",
            severity="high",
            expected_value="object",
            observed_value=type(value).__name__,
            suggested_action=f"Fix the publisher so '{field}' is a JSON object.",
        )
    ]


def _nested_pointset_points(payload: dict) -> object | None:
    if isinstance(payload.get("pointset"), dict):
        return payload["pointset"].get("points")
    return None


def _pointset_points_findings(payload: dict) -> list[StructuralFinding]:
    points = payload.get("points")
    if points is None:
        points = _nested_pointset_points(payload)
    if points is None:
        return []
    if not isinstance(points, dict):
        return [
            StructuralFinding(
                description="The pointset payload 'points' field must be an object of point entries.",
                severity="high",
                expected_value="object",
                observed_value=type(points).__name__,
                suggested_action="Fix the publisher so 'points' maps point names to entries.",
            )
        ]

    findings: list[StructuralFinding] = []
    for name, entry in points.items():
        point_name = str(name)
        if not _POINT_NAME_PATTERN.match(point_name):
            findings.append(
                StructuralFinding(
                    description=(
                        f"Point name '{point_name}' does not match the UDMI point-name pattern "
                        "(lower-case snake_case)."
                    ),
                    severity="medium",
                    point_name=point_name,
                    expected_value="^[a-z][a-z0-9]*(_[a-z0-9]+)*$",
                    observed_value=point_name,
                    suggested_action="Rename the point to lower-case snake_case in the publisher.",
                )
            )
        if not isinstance(entry, dict):
            findings.append(
                StructuralFinding(
                    description=f"Pointset entry for '{point_name}' must be an object.",
                    severity="high",
                    point_name=point_name,
                    expected_value="object with present_value",
                    observed_value=type(entry).__name__,
                    suggested_action="Fix the publisher so each point entry is a JSON object.",
                )
            )
        elif "present_value" not in entry:
            findings.append(
                StructuralFinding(
                    description=(
                        f"Pointset entry for '{point_name}' is missing 'present_value' "
                        "(required by the UDMI pointset event schema)."
                    ),
                    severity="high",
                    point_name=point_name,
                    expected_value="present_value",
                    observed_value="missing",
                    suggested_action="Fix the publisher so every point carries present_value.",
                )
            )
    return findings


def _metadata_pointset_findings(payload: dict) -> list[StructuralFinding]:
    pointset = payload.get("pointset")
    if pointset is None:
        return []
    if not isinstance(pointset, dict):
        return [
            StructuralFinding(
                description="The metadata payload 'pointset' field must be an object.",
                severity="high",
                expected_value="object",
                observed_value=type(pointset).__name__,
                suggested_action="Fix the metadata so 'pointset' is a JSON object.",
            )
        ]
    points = pointset.get("points")
    if points is None:
        return []
    if not isinstance(points, dict):
        return [
            StructuralFinding(
                description="The metadata payload 'pointset.points' field must be an object of point entries.",
                severity="high",
                expected_value="object",
                observed_value=type(points).__name__,
                suggested_action="Fix the metadata so 'pointset.points' maps point names to entries.",
            )
        ]

    findings: list[StructuralFinding] = []
    for name, entry in points.items():
        point_name = str(name)
        if not _POINT_NAME_PATTERN.match(point_name):
            findings.append(
                StructuralFinding(
                    description=(
                        f"Metadata point name '{point_name}' does not match the UDMI point-name "
                        "pattern (lower-case snake_case)."
                    ),
                    severity="medium",
                    point_name=point_name,
                    expected_value="^[a-z][a-z0-9]*(_[a-z0-9]+)*$",
                    observed_value=point_name,
                    suggested_action="Rename the point to lower-case snake_case in the metadata.",
                )
            )
        if not isinstance(entry, dict):
            findings.append(
                StructuralFinding(
                    description=f"Metadata point entry for '{point_name}' must be an object.",
                    severity="high",
                    point_name=point_name,
                    expected_value="object",
                    observed_value=type(entry).__name__,
                    suggested_action="Fix the metadata so each point entry is a JSON object.",
                )
            )
            continue
        units = entry.get("units")
        if units is not None and not isinstance(units, str):
            findings.append(
                StructuralFinding(
                    description=f"Metadata units for '{point_name}' must be a string.",
                    severity="medium",
                    point_name=point_name,
                    expected_value="string",
                    observed_value=type(units).__name__,
                    suggested_action="Fix the metadata so units is a UDMI unit string.",
                )
            )
    return findings
