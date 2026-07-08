"""BACnet point validation engine — deterministic, network-free comparison.

This engine compares an EXPECTED BACnet point register (an imported
``bacnet_points`` batch) against OBSERVED BACnet point values, and emits a
:class:`~smart_commissioning_core.records.ValidationIssueRecord` for every
problem it finds (missing point, unexpected point, value mismatch,
out-of-tolerance, value-type mismatch, unit mismatch).

HONESTY / ON-SITE VALIDATION
----------------------------
There is NO real BACnet device, building network, or live transport in this
module. It performs ZERO I/O: it is a pure function over (a) imported register
rows and (b) observed values that were captured elsewhere (a BACnet discovery
run's ``DiscoveredPoint`` rows, or inline observed values supplied for testing).
Reading observed values off a live controller is the responsibility of a
separate active-scan discovery engine; THAT engine requires on-site validation
and must be listed in its own ``live_untested`` output. This engine is fully
unit-testable against canned data and is exercised by
``core/tests/test_point_validation.py``.

LOADER INJECTION (mirrors ``udmi_validation`` injection style)
--------------------------------------------------------------
The two data sources are pulled in through injected callables so tests can pass
canned data with no database:

* ``import_loader(import_id: str) -> list[dict]`` returns the imported
  ``accepted_rows`` for an import batch (the production wiring backs this with
  ``ImportRepository.get_accepted_rows``). Used for the expected ``bacnet_points``
  register and the optional ``tolerances`` register.
* ``discovery_loader(run_id: str) -> list[dict]`` returns the
  ``DiscoveredPoint`` row dicts for a discovery run (production wiring backs this
  with ``DiscoveryRepository.list_points``).

Either source may instead be supplied INLINE via ``parameters`` (see
:func:`process_bacnet_validation_run`) so the engine is runnable end to end with
no loaders at all — which is exactly how the tests drive it.

This engine is registered with the framework ``run_engine`` wrapper via
:func:`build_bacnet_validation_engine`; the standalone
:func:`process_bacnet_validation_run` is the ``process_*_run`` entrypoint that
the API/worker call (matching ``process_udmi_validation_run``).
"""

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    ThrottleConfig,
    run_engine,
)
from smart_commissioning_core.engines.comparison_common import (
    DiscoveryLoader,
    ImportLoader,
    Tolerance,
    _is_required,
    _stringify,
    build_tolerance_index,
    coerce_number,
    extract_observed_scalar,
    make_issue,
    normalise_unit,
    within_tolerance,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_store import RunStore

# How many expected points to process before re-checking cooperative
# cancellation. Keeps a huge register responsive without per-row overhead.
_CANCEL_CHECK_CHUNK = 200

_ISSUE_PREFIX = "BPV"  # BACnet Point Validation


# Sequential "BPV-0001"-style issue builder (shared numbering helper).
_issue = partial(make_issue, prefix=_ISSUE_PREFIX)


def _expected_point_key(row: Mapping[str, Any]) -> str:
    """Stable identity for an expected point: its expected point name."""
    return str(row.get("Expected point name") or "").strip()


def _observed_point_key(row: Mapping[str, Any]) -> str:
    """Stable identity for an observed point.

    A ``DiscoveredPoint`` carries both ``point_name`` and ``point_id``; we match
    on ``point_name`` (what the register's "Expected point name" refers to),
    falling back to ``point_id`` so inline test data may use either.
    """
    name = str(row.get("point_name") or "").strip()
    if name:
        return name
    return str(row.get("point_id") or "").strip()


def validate_bacnet_points(
    *,
    expected_rows: Sequence[Mapping[str, Any]],
    observed_rows: Sequence[Mapping[str, Any]],
    tolerance_rows: Sequence[Mapping[str, Any]] = (),
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[list[ValidationIssueRecord], dict[str, Any]]:
    """Compare expected vs observed BACnet points; return (issues, summary).

    Pure / side-effect free. ``expected_rows`` are ``bacnet_points`` accepted
    rows; ``observed_rows`` are ``DiscoveredPoint`` dicts (or inline equivalents
    carrying ``point_name``/``observed_value``/``units``); ``tolerance_rows`` are
    ``tolerances`` accepted rows.

    Cancellation: if ``is_cancelled`` returns True at a chunk boundary, we stop
    processing further expected points and the summary records ``cancelled: True``
    with the number actually processed. Already-emitted issues are returned.
    """
    cancelled = is_cancelled or (lambda: False)
    tolerance_index = build_tolerance_index(tolerance_rows)

    # Index observed points by their key so lookups + the "unexpected" pass are
    # both O(1)-per-point. Later duplicates win (last observation), which is the
    # safest default for a re-scan.
    observed_index: dict[str, Mapping[str, Any]] = {}
    for observed in observed_rows:
        key = _observed_point_key(observed)
        if key:
            observed_index[key] = observed

    issues: list[ValidationIssueRecord] = []
    counts = {
        "ok": 0,
        "missing": 0,
        "value_mismatch": 0,
        "out_of_tolerance": 0,
        "type_mismatch": 0,
        "unit_mismatch": 0,
    }

    matched_observed_keys: set[str] = set()
    processed = 0
    was_cancelled = False

    for index, expected in enumerate(expected_rows):
        if index % _CANCEL_CHECK_CHUNK == 0 and index > 0 and cancelled():
            was_cancelled = True
            break

        point_name = _expected_point_key(expected)
        if not point_name:
            # An expected register row with no point name is unusable; skip it
            # but do not blow up the whole run.
            continue
        asset_id = str(expected.get("Asset ID") or "").strip() or None
        expected_units = normalise_unit(expected.get("Expected units"))
        expected_value_type = str(expected.get("Expected value type") or "").strip().casefold()
        required = _is_required(expected.get("Required/optional flag"))

        observed = observed_index.get(point_name)
        if observed is None:
            # Missing point. Optional points are a lower severity than required.
            counts["missing"] += 1
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="missing_point",
                    severity="high" if required else "low",
                    description=(
                        f"Expected BACnet point '{point_name}' was not observed "
                        f"on {asset_id or 'the device'}."
                    ),
                    point_name=point_name,
                    expected_value="present",
                    observed_value="missing",
                    match_basis="point_name",
                    suggested_action=(
                        "Confirm the BACnet object exists and was discovered, "
                        "or correct the expected point name in the register."
                    ),
                )
            )
            continue

        matched_observed_keys.add(point_name)
        observed_scalar, observed_raw = extract_observed_scalar(observed.get("observed_value"))
        observed_units = normalise_unit(observed.get("units"))

        point_issues = _compare_one_point(
            issues_so_far=issues,
            asset_id=asset_id,
            point_name=point_name,
            expected=expected,
            expected_units=expected_units,
            expected_value_type=expected_value_type,
            observed_scalar=observed_scalar,
            observed_raw=observed_raw,
            observed_units=observed_units,
            tolerance=_resolve_tolerance(
                tolerance_index,
                asset_id=asset_id,
                point_name=point_name,
                value_type=expected_value_type,
            ),
            counts=counts,
        )
        issues.extend(point_issues)
        if not point_issues:
            counts["ok"] += 1
        processed += 1

    # Unexpected observed points: observed but never matched to an expected row.
    # Only meaningful when we processed the whole expected register (otherwise a
    # cancelled run would spuriously flag everything as unexpected).
    unexpected = 0
    if not was_cancelled:
        for key, observed in observed_index.items():
            if key in matched_observed_keys:
                continue
            unexpected += 1
            asset_id = (
                str(observed.get("device_ref") or "").strip()
                or _observed_asset_id(observed)
                or None
            )
            observed_scalar, observed_raw = extract_observed_scalar(observed.get("observed_value"))
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="unexpected_point",
                    severity="medium",
                    description=(
                        f"Observed BACnet point '{key}' is not present in the "
                        "expected point register."
                    ),
                    point_name=key,
                    expected_value="absent",
                    observed_value=_stringify(observed_raw if observed_raw is not None else observed_scalar),
                    match_basis="point_name",
                    suggested_action=(
                        "Add the point to the expected register if it is valid, "
                        "or investigate why an unexpected object is present."
                    ),
                )
            )

    summary: dict[str, Any] = {
        "total": len(expected_rows),
        "ok": counts["ok"],
        "missing": counts["missing"],
        "unexpected": unexpected,
        "value_mismatch": counts["value_mismatch"],
        "out_of_tolerance": counts["out_of_tolerance"],
        "type_mismatch": counts["type_mismatch"],
        "unit_mismatch": counts["unit_mismatch"],
        "observed_total": len(observed_index),
        "expected_processed": processed,
        "issue_count": len(issues),
        "cancelled": was_cancelled,
    }
    return issues, summary


def _compare_one_point(
    *,
    issues_so_far: Sequence[ValidationIssueRecord],
    asset_id: str | None,
    point_name: str,
    expected: Mapping[str, Any],
    expected_units: str | None,
    expected_value_type: str,
    observed_scalar: Any,
    observed_raw: Any,
    observed_units: str | None,
    tolerance: Tolerance | None,
    counts: dict[str, int],
) -> list[ValidationIssueRecord]:
    """Run all per-point checks; return the issues for THIS point only.

    A point with no problems returns ``[]`` (counts.ok handled by the caller).
    """
    issues: list[ValidationIssueRecord] = []

    # 1. Unit mismatch (only when both sides declare a unit and they differ).
    if expected_units and observed_units and expected_units != observed_units:
        counts["unit_mismatch"] += 1
        issues.append(
            _issue(
                [*issues_so_far, *issues],
                asset_id=asset_id,
                issue_type="unit_mismatch",
                severity="medium",
                description=(
                    f"Point '{point_name}' unit mismatch: expected "
                    f"'{expected_units}', observed '{observed_units}'."
                ),
                point_name=point_name,
                expected_value=expected_units,
                observed_value=observed_units,
                match_basis="units",
                suggested_action=(
                    "Align the controller's engineering units with the register, "
                    "or correct the expected units."
                ),
            )
        )

    expected_value_raw = expected.get("Expected value")
    has_expected_value = expected_value_raw is not None and str(expected_value_raw).strip() != ""

    # 2. Value-type expectation. For numeric points, the observed scalar must be
    #    numeric; if it isn't, that's a type mismatch (and value/tolerance checks
    #    cannot run).
    numeric_expected = expected_value_type in {"number", "numeric", "float", "real", "analog"}
    observed_number = coerce_number(observed_scalar)
    if numeric_expected and observed_scalar is not None and observed_number is None:
        counts["type_mismatch"] += 1
        issues.append(
            _issue(
                [*issues_so_far, *issues],
                asset_id=asset_id,
                issue_type="value_type_mismatch",
                severity="high",
                description=(
                    f"Point '{point_name}' expected a numeric value but observed "
                    f"a non-numeric value."
                ),
                point_name=point_name,
                expected_value=f"numeric ({expected_value_type})",
                observed_value=_stringify(observed_raw if observed_raw is not None else observed_scalar),
                match_basis="value_type",
                suggested_action="Fix the publisher/controller so the present value type matches.",
            )
        )
        return issues

    # 3. Value comparison (only when the register declares an expected value).
    if has_expected_value:
        expected_number = coerce_number(expected_value_raw)
        if expected_number is not None and observed_number is not None:
            ok, basis = within_tolerance(expected_number, observed_number, tolerance)
            if not ok:
                counts["out_of_tolerance"] += 1
                issues.append(
                    _issue(
                        [*issues_so_far, *issues],
                        asset_id=asset_id,
                        issue_type="out_of_tolerance",
                        severity="high",
                        description=(
                            f"Point '{point_name}' value {observed_number} is outside "
                            f"tolerance of expected {expected_number} ({basis})."
                        ),
                        point_name=point_name,
                        expected_value=_stringify(expected_value_raw),
                        observed_value=_stringify(observed_raw if observed_raw is not None else observed_scalar),
                        match_basis=basis,
                        suggested_action=(
                            "Investigate the sensor/controller calibration or widen "
                            "the configured tolerance if the deviation is acceptable."
                        ),
                    )
                )
        else:
            # Non-numeric expected value: exact string comparison.
            if _stringify(expected_value_raw).strip() != _stringify(observed_scalar).strip():
                counts["value_mismatch"] += 1
                issues.append(
                    _issue(
                        [*issues_so_far, *issues],
                        asset_id=asset_id,
                        issue_type="value_mismatch",
                        severity="high",
                        description=(
                            f"Point '{point_name}' value mismatch: expected "
                            f"'{_stringify(expected_value_raw)}', observed "
                            f"'{_stringify(observed_scalar)}'."
                        ),
                        point_name=point_name,
                        expected_value=_stringify(expected_value_raw),
                        observed_value=_stringify(observed_raw if observed_raw is not None else observed_scalar),
                        match_basis="exact",
                        suggested_action="Confirm the expected value or investigate the observed reading.",
                    )
                )

    return issues


def _resolve_tolerance(
    tolerance_index: Mapping[tuple[str, str], Tolerance],
    *,
    asset_id: str | None,
    point_name: str,
    value_type: str,
) -> Tolerance | None:
    """Resolve the tolerance for a point: per-point first, then per-type."""
    # Per (asset, point) is the most specific.
    if asset_id is not None:
        specific = tolerance_index.get((asset_id.casefold(), point_name.casefold()))
        if specific is not None:
            return specific
    # Per point name across any asset.
    by_point = tolerance_index.get(("", point_name.casefold()))
    if by_point is not None:
        return by_point
    # Per value type (a "type" tolerance row stores its key under point name).
    if value_type:
        by_type = tolerance_index.get(("", f"type:{value_type}"))
        if by_type is not None:
            return by_type
    return None


def _observed_asset_id(observed: Mapping[str, Any]) -> str | None:
    attributes = observed.get("attributes")
    if isinstance(attributes, Mapping):
        value = attributes.get("asset_id")
        if value:
            return str(value)
    return None


# -- engine + processor wiring ---------------------------------------------


def _load_expected_rows(
    parameters: Mapping[str, Any],
    import_loader: ImportLoader | None,
) -> list[dict[str, Any]]:
    inline = parameters.get("expected_points")
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, Mapping)]
    import_id = parameters.get("import_id") or parameters.get("expected_points_import_id")
    if import_id and import_loader is not None:
        return [dict(row) for row in import_loader(str(import_id)) if isinstance(row, Mapping)]
    return []


def _load_observed_rows(
    parameters: Mapping[str, Any],
    discovery_loader: DiscoveryLoader | None,
) -> list[dict[str, Any]]:
    inline = parameters.get("observed_points")
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, Mapping)]
    discovery_run_id = parameters.get("discovery_run_id") or parameters.get("bacnet_discovery_run_id")
    if discovery_run_id and discovery_loader is not None:
        return [dict(row) for row in discovery_loader(str(discovery_run_id)) if isinstance(row, Mapping)]
    return []


def _load_tolerance_rows(
    parameters: Mapping[str, Any],
    import_loader: ImportLoader | None,
) -> list[dict[str, Any]]:
    inline = parameters.get("tolerances")
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, Mapping)]
    tolerances_import_id = parameters.get("tolerances_import_id")
    if tolerances_import_id and import_loader is not None:
        return [dict(row) for row in import_loader(str(tolerances_import_id)) if isinstance(row, Mapping)]
    return []


def build_bacnet_validation_engine(
    *,
    import_loader: ImportLoader | None = None,
    discovery_loader: DiscoveryLoader | None = None,
):
    """Return an engine callable for the framework :func:`run_engine` wrapper.

    The returned callable reads its inputs from ``ctx.parameters`` (inline) or
    via the injected loaders, runs :func:`validate_bacnet_points`, and packs the
    result into an :class:`EngineResult`. It is cancellation-aware (checks
    ``ctx.is_cancelled`` between chunks) and performs NO network I/O, so no scan
    authorization is required.
    """

    def engine(ctx: EngineContext) -> EngineResult:
        expected_rows = _load_expected_rows(ctx.parameters, import_loader)
        observed_rows = _load_observed_rows(ctx.parameters, discovery_loader)
        tolerance_rows = _load_tolerance_rows(ctx.parameters, import_loader)

        issues, summary = validate_bacnet_points(
            expected_rows=expected_rows,
            observed_rows=observed_rows,
            tolerance_rows=tolerance_rows,
            is_cancelled=ctx.is_cancelled,
        )
        status_override = "cancelled" if summary.get("cancelled") else None
        return EngineResult(
            issues=list(issues),
            result_summary_extra=summary,
            status_override=status_override,
        )

    return engine


def process_bacnet_validation_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: RunStore,
    execution_mode: str,
    import_loader: ImportLoader | None = None,
    discovery_loader: DiscoveryLoader | None = None,
    throttle: ThrottleConfig | None = None,
    dry_run: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> Any:
    """``process_*_run`` entrypoint for BACnet point validation.

    Mirrors ``process_udmi_validation_run``: builds an :class:`EngineContext`,
    runs the engine through the framework :func:`run_engine` wrapper, and returns
    the terminal run record. This validation does NOT touch the network, so no
    scan authorization is required; it IS cancellation-aware for large registers.

    ``import_loader`` / ``discovery_loader`` are injected (default ``None``) so
    callers/tests can supply canned data; production wiring backs them with
    ``ImportRepository.get_accepted_rows`` / ``DiscoveryRepository.list_points``.
    """
    ctx = EngineContext(
        run_id=run_id,
        parameters=dict(parameters),
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle or ThrottleConfig(),
        dry_run=dry_run,
        _is_cancelled=is_cancelled or (lambda: False),
    )
    engine = build_bacnet_validation_engine(
        import_loader=import_loader,
        discovery_loader=discovery_loader,
    )
    return run_engine(ctx, engine)
