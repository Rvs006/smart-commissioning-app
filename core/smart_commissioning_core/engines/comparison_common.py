"""Shared, network-free helpers for the validation/comparison engines.

These are pure functions used by both :mod:`point_validation` and
:mod:`comparison`. Nothing here opens a socket or imports a transport — they
are deterministic data transforms over imported register rows + observed
values, and are exercised directly by the engine tests.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Loader callables injected into the engines (mirrors udmi_validation's
# ``LiveCapture`` injection style). They return plain row dicts; the engines
# never assume a database is present.
#
#   import_loader(import_id)    -> list of an import batch's accepted_rows
#   discovery_loader(run_id)    -> list of a discovery run's row dicts
ImportLoader = Callable[[str], "Sequence[Mapping[str, Any]]"]
DiscoveryLoader = Callable[[str], "Sequence[Mapping[str, Any]]"]

# Keys we look under, in order, to pull a scalar present-value out of a
# DiscoveredPoint's ``observed_value`` JSON dict. ``DiscoveredPoint.observed_value``
# is a JSON object (see DiscoveryRepository), so a discovery engine may store
# ``{"value": 21.4}`` or the UDMI-style ``{"present_value": 21.4}``.
_SCALAR_KEYS = ("value", "present_value", "present-value", "observed_value", "reading")


@dataclass(frozen=True, slots=True)
class Tolerance:
    """A resolved tolerance for a point comparison.

    Exactly one of ``absolute`` / ``percent`` is populated. ``percent`` is a
    percentage of the EXPECTED value (e.g. ``percent=5.0`` means +-5%).
    """

    absolute: float | None = None
    percent: float | None = None

    def describe(self) -> str:
        if self.percent is not None:
            return f"percent +-{self.percent}%"
        if self.absolute is not None:
            return f"absolute +-{self.absolute}"
        return "exact"


def coerce_number(value: Any) -> float | None:
    """Best-effort numeric coercion. Returns ``None`` for non-numeric input.

    Booleans are deliberately NOT treated as numbers here: a boolean point and
    a numeric point are different value types, and silently coercing ``True`` to
    ``1.0`` would hide real type mismatches.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def extract_observed_scalar(observed_value: Any) -> tuple[Any, Any]:
    """Pull a comparable scalar out of an observed value.

    ``DiscoveredPoint.observed_value`` is a JSON dict; inline test data may also
    pass a bare scalar. Returns ``(scalar, raw)`` where ``raw`` is the original
    object (handy for surfacing the full payload in an issue) and ``scalar`` is
    the value to compare.

    Resolution order for a dict: the first present key in :data:`_SCALAR_KEYS`.
    If none are present, the dict itself is returned as the scalar (callers will
    typically stringify it for an exact comparison).
    """
    if isinstance(observed_value, Mapping):
        for key in _SCALAR_KEYS:
            if key in observed_value:
                return observed_value[key], observed_value
        return observed_value, observed_value
    return observed_value, observed_value


def normalise_unit(value: Any) -> str | None:
    """Normalise a unit string for comparison.

    Lower-cases and collapses common separator variants (``degrees_celsius`` ~
    ``degrees-celsius``) so a UDMI-style underscore unit matches the
    register's hyphenated unit. Empty / ``no-units`` reads as no unit (``None``)
    so an unspecified unit never trips a false mismatch.
    """
    if value is None:
        return None
    text = str(value).strip().casefold()
    if not text or text in {"no-units", "no_units", "none", "unitless"}:
        return None
    return text.replace("_", "-").replace(" ", "-")


def parse_tolerance(raw: Any) -> Tolerance | None:
    """Parse a tolerance cell into a :class:`Tolerance`.

    Accepted forms (strings from imported registers, or numbers from inline
    test data):

        ""               -> None (no tolerance => exact match required)
        "0.5"  / 0.5     -> absolute +-0.5
        "5%"             -> percent +-5%
        "abs:0.5"        -> absolute +-0.5
        "percent:5"      -> percent +-5%
        {"absolute": x}  -> absolute  (inline dict form)
        {"percent": x}   -> percent
    """
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        absolute = coerce_number(raw.get("absolute"))
        percent = coerce_number(raw.get("percent"))
        if percent is not None:
            return Tolerance(percent=abs(percent))
        if absolute is not None:
            return Tolerance(absolute=abs(absolute))
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Tolerance(absolute=abs(float(raw)))

    text = str(raw).strip().casefold()
    if not text:
        return None
    if text.endswith("%"):
        number = coerce_number(text[:-1])
        return Tolerance(percent=abs(number)) if number is not None else None
    for prefix in ("percent:", "pct:", "%:"):
        if text.startswith(prefix):
            number = coerce_number(text[len(prefix):])
            return Tolerance(percent=abs(number)) if number is not None else None
    for prefix in ("abs:", "absolute:"):
        if text.startswith(prefix):
            number = coerce_number(text[len(prefix):])
            return Tolerance(absolute=abs(number)) if number is not None else None
    number = coerce_number(text)
    return Tolerance(absolute=abs(number)) if number is not None else None


def build_tolerance_index(
    tolerance_rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Tolerance]:
    """Build a lookup of tolerances from ``tolerances`` accepted rows.

    Keyed by ``(asset_id_casefold, point_name_casefold)``. A row whose Asset ID
    is empty becomes an asset-agnostic ``("", point)`` entry. A row whose point
    name begins with ``type:`` (e.g. ``type:number``) registers a per-type
    tolerance under ``("", "type:number")``. Rows with an unparseable tolerance
    are skipped.
    """
    index: dict[tuple[str, str], Tolerance] = {}
    for row in tolerance_rows:
        point = str(row.get("Point name") or "").strip()
        if not point:
            continue
        tolerance = parse_tolerance(row.get("Tolerance"))
        if tolerance is None:
            continue
        asset = str(row.get("Asset ID") or "").strip().casefold()
        index[(asset, point.casefold())] = tolerance
    return index


def within_tolerance(
    expected: float,
    observed: float,
    tolerance: Tolerance | None,
) -> tuple[bool, str]:
    """Return ``(ok, basis)`` for a numeric comparison under ``tolerance``.

    ``basis`` describes how the comparison was made (for the issue's
    ``match_basis``): ``"exact"``, ``"absolute"`` or ``"percent"``. With no
    tolerance, an exact equality is required.
    """
    if tolerance is None:
        return (expected == observed, "exact")
    if tolerance.percent is not None:
        # Percent of the expected magnitude. Guard expected==0 (any deviation
        # from zero is out of a percentage band, so require exact match there).
        if expected == 0:
            return (observed == 0, "percent")
        allowed = abs(expected) * (tolerance.percent / 100.0)
        return (abs(observed - expected) <= allowed, "percent")
    if tolerance.absolute is not None:
        return (abs(observed - expected) <= tolerance.absolute, "absolute")
    return (expected == observed, "exact")
