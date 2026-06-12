"""BACnet <-> MQTT mapping comparison engine — deterministic, network-free.

For each row of an imported ``mapping`` register (a BACnet source object mapped
to an MQTT translated field) this engine compares the OBSERVED BACnet source
value against the OBSERVED MQTT translated value, within tolerance, and emits a
:class:`~smart_commissioning_core.records.ValidationIssueRecord` for:

* ``missing_bacnet_source`` — the BACnet side was not observed
* ``missing_mqtt_target``   — the MQTT side was not observed
* ``out_of_tolerance``      — both observed but the values differ beyond tolerance
* ``value_mismatch``        — both observed, non-numeric, and not equal
* ``unit_mismatch``         — declared BACnet vs MQTT units differ

HONESTY / ON-SITE VALIDATION
----------------------------
This module performs ZERO I/O. The BACnet and MQTT observed values are captured
ELSEWHERE (BACnet/MQTT discovery runs, or inline values for testing) and handed
to this engine as plain rows. It does not — and cannot — confirm that a real
translation gateway is correctly mapping a live controller to a live broker;
that requires on-site validation by the discovery engines that actually read
the two networks. This comparison is fully unit-testable against canned data
and is exercised by ``core/tests/test_comparison.py``.

LOADER INJECTION mirrors :mod:`point_validation`: ``import_loader`` for the
``mapping`` + ``tolerances`` registers, ``discovery_loader`` for the BACnet and
MQTT observed-value rows. Any of these may instead be supplied inline through
``parameters`` so the engine runs end to end with no loaders.
"""

from collections.abc import Callable, Mapping, Sequence
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
    build_tolerance_index,
    coerce_number,
    extract_observed_scalar,
    normalise_unit,
    parse_tolerance,
    within_tolerance,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_store import RunStore

_CANCEL_CHECK_CHUNK = 200
_ISSUE_PREFIX = "MAP"  # mapping comparison


def _issue(
    issues: Sequence[ValidationIssueRecord],
    *,
    asset_id: str | None,
    issue_type: str,
    severity: str,
    description: str,
    point_name: str | None = None,
    topic: str | None = None,
    expected_value: str | None = None,
    observed_value: str | None = None,
    match_basis: str | None = None,
    suggested_action: str | None = None,
) -> ValidationIssueRecord:
    return ValidationIssueRecord(
        issue_id=f"{_ISSUE_PREFIX}-{len(issues) + 1:04d}",
        asset_id=asset_id,
        issue_type=issue_type,
        severity=severity,
        description=description,
        point_name=point_name,
        topic=topic,
        expected_value=expected_value,
        observed_value=observed_value,
        match_basis=match_basis,
        suggested_action=suggested_action,
    )


def _bacnet_observed_key(row: Mapping[str, Any]) -> str:
    """Identity for an observed BACnet source value.

    A mapping row identifies its BACnet source by object name; observed BACnet
    rows (DiscoveredPoint) carry ``point_name``/``point_id``.
    """
    for key in ("point_name", "point_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _mqtt_observed_key(row: Mapping[str, Any]) -> str:
    """Identity for an observed MQTT translated value.

    A mapping row identifies its MQTT target by ``MQTT field/path``; observed
    MQTT rows may carry ``field`` / ``path`` / ``json_path`` / ``point_name``.
    """
    for key in ("field", "path", "json_path", "point_name", "point_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def validate_mapping(
    *,
    mapping_rows: Sequence[Mapping[str, Any]],
    bacnet_observed_rows: Sequence[Mapping[str, Any]],
    mqtt_observed_rows: Sequence[Mapping[str, Any]],
    tolerance_rows: Sequence[Mapping[str, Any]] = (),
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[list[ValidationIssueRecord], dict[str, Any]]:
    """Compare each mapping row's BACnet source vs MQTT target value.

    Pure / side-effect free. Returns ``(issues, summary)``.

    Cancellation: at chunk boundaries, if ``is_cancelled`` returns True we stop
    processing further mapping rows; the summary records ``cancelled: True`` and
    the count processed. Already-emitted issues are returned.
    """
    cancelled = is_cancelled or (lambda: False)
    tolerance_index = build_tolerance_index(tolerance_rows)

    bacnet_index = _index_observed(bacnet_observed_rows, _bacnet_observed_key)
    mqtt_index = _index_observed(mqtt_observed_rows, _mqtt_observed_key)

    issues: list[ValidationIssueRecord] = []
    counts = {
        "ok": 0,
        "missing_bacnet": 0,
        "missing_mqtt": 0,
        "out_of_tolerance": 0,
        "value_mismatch": 0,
        "unit_mismatch": 0,
    }
    processed = 0
    was_cancelled = False

    for index, mapping in enumerate(mapping_rows):
        if index % _CANCEL_CHECK_CHUNK == 0 and index > 0 and cancelled():
            was_cancelled = True
            break

        asset_id = str(mapping.get("Asset ID") or "").strip() or None
        bacnet_name = str(mapping.get("BACnet object name") or "").strip()
        mqtt_field = str(mapping.get("MQTT field/path") or "").strip()
        mqtt_topic = str(mapping.get("MQTT topic") or "").strip() or None
        bacnet_units = normalise_unit(mapping.get("BACnet units"))
        mqtt_units = normalise_unit(mapping.get("MQTT units"))
        required = _is_required(mapping.get("Mapping required flag"))
        # Row-level tolerance from the mapping itself, falling back to the
        # tolerances register (per point/per type) keyed on the BACnet name.
        row_tolerance = parse_tolerance(mapping.get("Tolerance"))
        tolerance = row_tolerance or _resolve_tolerance(
            tolerance_index, asset_id=asset_id, point_name=bacnet_name
        )

        bacnet_observed = bacnet_index.get(bacnet_name) if bacnet_name else None
        mqtt_observed = mqtt_index.get(mqtt_field) if mqtt_field else None

        missing = False
        if bacnet_observed is None:
            missing = True
            counts["missing_bacnet"] += 1
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="missing_bacnet_source",
                    severity="high" if required else "low",
                    description=(
                        f"BACnet source '{bacnet_name or '(unnamed)'}' for mapping to "
                        f"'{mqtt_field or '(unspecified MQTT field)'}' was not observed."
                    ),
                    point_name=bacnet_name or None,
                    topic=mqtt_topic,
                    expected_value="present",
                    observed_value="missing",
                    match_basis="point_name",
                    suggested_action=(
                        "Confirm the BACnet object was discovered, or correct the "
                        "BACnet object name in the mapping."
                    ),
                )
            )
        if mqtt_observed is None:
            missing = True
            counts["missing_mqtt"] += 1
            issues.append(
                _issue(
                    issues,
                    asset_id=asset_id,
                    issue_type="missing_mqtt_target",
                    severity="high" if required else "low",
                    description=(
                        f"MQTT target '{mqtt_field or '(unspecified)'}' for mapping from "
                        f"BACnet '{bacnet_name or '(unnamed)'}' was not observed."
                    ),
                    point_name=bacnet_name or None,
                    topic=mqtt_topic,
                    expected_value="present",
                    observed_value="missing",
                    match_basis="field_path",
                    suggested_action=(
                        "Confirm the translation gateway publishes this field, or "
                        "correct the MQTT field/path in the mapping."
                    ),
                )
            )

        if missing:
            processed += 1
            continue

        row_issues = _compare_pair(
            issues_so_far=issues,
            asset_id=asset_id,
            bacnet_name=bacnet_name,
            mqtt_field=mqtt_field,
            mqtt_topic=mqtt_topic,
            bacnet_observed=bacnet_observed,
            mqtt_observed=mqtt_observed,
            bacnet_units=bacnet_units,
            mqtt_units=mqtt_units,
            tolerance=tolerance,
            counts=counts,
        )
        issues.extend(row_issues)
        if not row_issues:
            counts["ok"] += 1
        processed += 1

    summary: dict[str, Any] = {
        "total": len(mapping_rows),
        "ok": counts["ok"],
        "missing_bacnet": counts["missing_bacnet"],
        "missing_mqtt": counts["missing_mqtt"],
        "out_of_tolerance": counts["out_of_tolerance"],
        "value_mismatch": counts["value_mismatch"],
        "unit_mismatch": counts["unit_mismatch"],
        "bacnet_observed_total": len(bacnet_index),
        "mqtt_observed_total": len(mqtt_index),
        "mappings_processed": processed,
        "issue_count": len(issues),
        "cancelled": was_cancelled,
    }
    return issues, summary


def _compare_pair(
    *,
    issues_so_far: Sequence[ValidationIssueRecord],
    asset_id: str | None,
    bacnet_name: str,
    mqtt_field: str,
    mqtt_topic: str | None,
    bacnet_observed: Mapping[str, Any],
    mqtt_observed: Mapping[str, Any],
    bacnet_units: str | None,
    mqtt_units: str | None,
    tolerance: Tolerance | None,
    counts: dict[str, int],
) -> list[ValidationIssueRecord]:
    """Compare one observed BACnet value against one observed MQTT value."""
    issues: list[ValidationIssueRecord] = []

    if bacnet_units and mqtt_units and bacnet_units != mqtt_units:
        counts["unit_mismatch"] += 1
        issues.append(
            _issue(
                [*issues_so_far, *issues],
                asset_id=asset_id,
                issue_type="unit_mismatch",
                severity="medium",
                description=(
                    f"Mapping '{bacnet_name}' -> '{mqtt_field}' unit mismatch: "
                    f"BACnet '{bacnet_units}' vs MQTT '{mqtt_units}'."
                ),
                point_name=bacnet_name or None,
                topic=mqtt_topic,
                expected_value=bacnet_units,
                observed_value=mqtt_units,
                match_basis="units",
                suggested_action=(
                    "Align the translation gateway's published units with the "
                    "source units, or correct the mapping's declared units."
                ),
            )
        )

    bacnet_scalar, bacnet_raw = extract_observed_scalar(bacnet_observed.get("observed_value"))
    mqtt_scalar, mqtt_raw = extract_observed_scalar(_mqtt_observed_value(mqtt_observed))

    bacnet_number = coerce_number(bacnet_scalar)
    mqtt_number = coerce_number(mqtt_scalar)

    if bacnet_number is not None and mqtt_number is not None:
        ok, basis = within_tolerance(bacnet_number, mqtt_number, tolerance)
        if not ok:
            counts["out_of_tolerance"] += 1
            issues.append(
                _issue(
                    [*issues_so_far, *issues],
                    asset_id=asset_id,
                    issue_type="out_of_tolerance",
                    severity="high",
                    description=(
                        f"Mapping '{bacnet_name}' -> '{mqtt_field}': MQTT value "
                        f"{mqtt_number} differs from BACnet source {bacnet_number} "
                        f"beyond tolerance ({basis})."
                    ),
                    point_name=bacnet_name or None,
                    topic=mqtt_topic,
                    expected_value=_stringify(bacnet_raw if bacnet_raw is not None else bacnet_scalar),
                    observed_value=_stringify(mqtt_raw if mqtt_raw is not None else mqtt_scalar),
                    match_basis=basis,
                    suggested_action=(
                        "Investigate the translation gateway's scaling/offset or "
                        "widen the configured tolerance if the deviation is acceptable."
                    ),
                )
            )
    else:
        # Non-numeric on at least one side: exact string comparison.
        if _stringify(bacnet_scalar).strip() != _stringify(mqtt_scalar).strip():
            counts["value_mismatch"] += 1
            issues.append(
                _issue(
                    [*issues_so_far, *issues],
                    asset_id=asset_id,
                    issue_type="value_mismatch",
                    severity="high",
                    description=(
                        f"Mapping '{bacnet_name}' -> '{mqtt_field}' value mismatch: "
                        f"BACnet '{_stringify(bacnet_scalar)}' vs MQTT "
                        f"'{_stringify(mqtt_scalar)}'."
                    ),
                    point_name=bacnet_name or None,
                    topic=mqtt_topic,
                    expected_value=_stringify(bacnet_raw if bacnet_raw is not None else bacnet_scalar),
                    observed_value=_stringify(mqtt_raw if mqtt_raw is not None else mqtt_scalar),
                    match_basis="exact",
                    suggested_action="Confirm the translation gateway maps the value correctly.",
                )
            )

    return issues


def _mqtt_observed_value(row: Mapping[str, Any]) -> Any:
    """Pull the observed-value payload from an MQTT observed row.

    MQTT observed rows may carry the value under ``observed_value`` (matching the
    DiscoveredPoint shape) or under ``value`` directly; we prefer the former and
    fall back to the latter.
    """
    if "observed_value" in row:
        return row.get("observed_value")
    if "value" in row:
        return row.get("value")
    return None


def _index_observed(
    rows: Sequence[Mapping[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], str],
) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key:
            index[key] = row
    return index


def _resolve_tolerance(
    tolerance_index: Mapping[tuple[str, str], Tolerance],
    *,
    asset_id: str | None,
    point_name: str,
) -> Tolerance | None:
    if asset_id is not None and point_name:
        specific = tolerance_index.get((asset_id.casefold(), point_name.casefold()))
        if specific is not None:
            return specific
    if point_name:
        by_point = tolerance_index.get(("", point_name.casefold()))
        if by_point is not None:
            return by_point
    return None


def _is_required(flag: Any) -> bool:
    text = str(flag or "").strip().casefold()
    return text in {"required", "req", "mandatory", "true", "yes", "1"}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


# -- engine + processor wiring ---------------------------------------------


def _load_rows_inline_or_import(
    parameters: Mapping[str, Any],
    *,
    inline_key: str,
    import_id_key: str,
    import_loader: ImportLoader | None,
) -> list[dict[str, Any]]:
    inline = parameters.get(inline_key)
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, Mapping)]
    import_id = parameters.get(import_id_key)
    if import_id and import_loader is not None:
        return [dict(row) for row in import_loader(str(import_id)) if isinstance(row, Mapping)]
    return []


def _load_rows_inline_or_discovery(
    parameters: Mapping[str, Any],
    *,
    inline_key: str,
    run_id_keys: tuple[str, ...],
    discovery_loader: DiscoveryLoader | None,
) -> list[dict[str, Any]]:
    inline = parameters.get(inline_key)
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, Mapping)]
    for run_id_key in run_id_keys:
        run_id = parameters.get(run_id_key)
        if run_id and discovery_loader is not None:
            return [dict(row) for row in discovery_loader(str(run_id)) if isinstance(row, Mapping)]
    return []


def build_mapping_validation_engine(
    *,
    import_loader: ImportLoader | None = None,
    discovery_loader: DiscoveryLoader | None = None,
):
    """Return an engine callable for the framework :func:`run_engine` wrapper.

    Reads the mapping/tolerance registers and BACnet/MQTT observed values from
    ``ctx.parameters`` (inline) or via the injected loaders, runs
    :func:`validate_mapping`, and packs the result. Cancellation-aware, performs
    NO network I/O, so no scan authorization is required.
    """

    def engine(ctx: EngineContext) -> EngineResult:
        mapping_rows = _load_rows_inline_or_import(
            ctx.parameters,
            inline_key="mapping_rows",
            import_id_key="mapping_import_id",
            import_loader=import_loader,
        )
        # Allow the generic ``import_id`` to refer to the mapping register too.
        if not mapping_rows and ctx.parameters.get("import_id") and import_loader is not None:
            mapping_rows = [
                dict(row)
                for row in import_loader(str(ctx.parameters["import_id"]))
                if isinstance(row, Mapping)
            ]
        tolerance_rows = _load_rows_inline_or_import(
            ctx.parameters,
            inline_key="tolerances",
            import_id_key="tolerances_import_id",
            import_loader=import_loader,
        )
        bacnet_rows = _load_rows_inline_or_discovery(
            ctx.parameters,
            inline_key="bacnet_observed",
            run_id_keys=("bacnet_discovery_run_id", "bacnet_run_id"),
            discovery_loader=discovery_loader,
        )
        mqtt_rows = _load_rows_inline_or_discovery(
            ctx.parameters,
            inline_key="mqtt_observed",
            run_id_keys=("mqtt_discovery_run_id", "mqtt_run_id"),
            discovery_loader=discovery_loader,
        )

        issues, summary = validate_mapping(
            mapping_rows=mapping_rows,
            bacnet_observed_rows=bacnet_rows,
            mqtt_observed_rows=mqtt_rows,
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


def process_mapping_validation_run(
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
    """``process_*_run`` entrypoint for BACnet<->MQTT mapping validation.

    Mirrors ``process_udmi_validation_run`` / ``process_bacnet_validation_run``:
    builds an :class:`EngineContext`, runs the engine through :func:`run_engine`,
    returns the terminal run record. No network I/O => no scan authorization;
    cancellation-aware for large mapping registers.
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
    engine = build_mapping_validation_engine(
        import_loader=import_loader,
        discovery_loader=discovery_loader,
    )
    return run_engine(ctx, engine)
