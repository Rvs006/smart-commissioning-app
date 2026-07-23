"""Pure projection of persisted UDMI validation summaries into report data.

The worker owns the validation facts. Report renderers only aggregate the
versioned ``result_summary.validation_summary_v1`` contract and never infer a
device's connection state from silence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

ASSET_METRIC_LABELS = (
    ("expected", "Expected Assets"),
    ("observed", "Observed Assets"),
    ("not_observed", "Not Observed Assets"),
    ("with_issues", "Assets With Issues"),
    ("successfully_validated", "Successfully Validated Assets"),
)

PAYLOAD_METRIC_LABELS = (
    ("expected", "Expected Payloads"),
    ("received", "Received Payloads"),
    ("with_issues", "Payloads With Issues"),
    ("successfully_validated", "Successfully Validated Payloads"),
)

FAULT_METRIC_LABELS = (
    ("payload_formatting_issues", "Payload Formatting Issues"),
    ("missing_points", "Missing Points"),
    ("point_naming_issues", "Point Naming Issues"),
    ("additional_points", "Additional Points"),
    ("stale_or_cadence", "Stale or Cadence Issues"),
    ("other_issues", "Other Issues"),
)

ISSUE_METRIC_LABELS = (
    ("blocking", "Blocking Issues"),
    ("warning", "Warning Issues"),
)

METRIC_DEFINITIONS = (
    {
        "metric": "Expected Assets",
        "definition": "Assets in the retained validation schedule for the selected runs.",
    },
    {
        "metric": "Observed Assets",
        "definition": "Expected assets with at least one retained expected payload during validation.",
    },
    {
        "metric": "Not Observed Assets",
        "definition": (
            "Expected assets with no retained expected payload during the validation window. "
            "This does not prove the asset's connection state."
        ),
    },
    {
        "metric": "Assets With Issues",
        "definition": "Observed or expected assets linked to one or more retained validation issues.",
    },
    {
        "metric": "Successfully Validated Assets",
        "definition": "Assets whose expected retained payloads passed the recorded validation checks.",
    },
    {
        "metric": "Unexpected Devices",
        "definition": (
            "Not measured by this run because validation subscribes to the expected register "
            "topics. A separate discovery scope is required to prove that no unscheduled "
            "devices are publishing."
        ),
    },
    {
        "metric": "System Completion",
        "definition": (
            "Successfully validated assets divided by expected assets for that system; "
            "reported as N/A when the system has no expected assets."
        ),
    },
    {
        "metric": "Expected Payloads",
        "definition": "Expected payload types recorded in the retained validation schedule.",
    },
    {
        "metric": "Received Payloads",
        "definition": "Expected payload types for which retained evidence was received.",
    },
    {
        "metric": "Payloads With Issues",
        "definition": "Retained payloads linked to one or more validation issues.",
    },
    {
        "metric": "Successfully Validated Payloads",
        "definition": "Received expected payloads that passed the recorded validation checks.",
    },
    {
        "metric": "Payload Formatting Issues",
        "definition": "Payloads that could not be validated as the expected structure or data form.",
    },
    {
        "metric": "Missing Points",
        "definition": "Expected points absent from retained payload evidence.",
    },
    {
        "metric": "Point Naming Issues",
        "definition": "Retained points whose names did not match the expected register or schema.",
    },
    {
        "metric": "Additional Points",
        "definition": "Retained points present in a payload but absent from the expected definition.",
    },
    {
        "metric": "Stale or Cadence Issues",
        "definition": "Recorded timing issues based only on the configured validation rule and evidence.",
    },
    {
        "metric": "Blocking Issues",
        "definition": "Retained issues classified as blocking for acceptance.",
    },
    {
        "metric": "Warning Issues",
        "definition": "Retained non-blocking issues that require review.",
    },
    {
        "metric": "Overall Compliance",
        "definition": (
            "Successfully validated assets divided by expected assets; N/A when no assets "
            "were expected."
        ),
    },
    {
        "metric": "Payloads Correct %",
        "definition": (
            "Successfully validated expected payloads divided by expected payloads; N/A when "
            "no payloads were expected."
        ),
    },
    {
        "metric": "Payloads Incorrect %",
        "definition": (
            "Expected payloads minus successfully validated expected payloads, divided by "
            "expected payloads; this includes missing and received-but-invalid expected "
            "payloads, and is N/A when no payloads were expected."
        ),
    },
    {
        "metric": "Last Validation Run",
        "definition": "Latest stored update time among the selected UDMI validation runs.",
    },
)

_ASSET_KEYS = tuple(key for key, _label in ASSET_METRIC_LABELS)
_PAYLOAD_KEYS = tuple(key for key, _label in PAYLOAD_METRIC_LABELS)
_FAULT_KEYS = tuple(key for key, _label in FAULT_METRIC_LABELS)
_ISSUE_KEYS = tuple(key for key, _label in ISSUE_METRIC_LABELS)

_FAULT_CATEGORY_ALIASES = {
    "payload_formatting": "payload_formatting_issues",
    "payload_formatting_issue": "payload_formatting_issues",
    "payload_formatting_issues": "payload_formatting_issues",
    "missing_point": "missing_points",
    "missing_points": "missing_points",
    "point_naming": "point_naming_issues",
    "point_naming_issue": "point_naming_issues",
    "point_naming_issues": "point_naming_issues",
    "additional_point": "additional_points",
    "additional_points": "additional_points",
    "stale": "stale_or_cadence",
    "cadence": "stale_or_cadence",
    "stale_or_cadence": "stale_or_cadence",
    "other": "other_issues",
    "other_issue": "other_issues",
    "other_issues": "other_issues",
}


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _metric_group(value: object, keys: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for key in keys:
        number = _non_negative_int(value.get(key))
        if number is None:
            return None
        result[key] = number
    return result


def _text(value: object, default: str = "") -> str:
    return str(value) if value is not None else default


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _normalise_fault_category(value: object) -> str:
    key = _text(value, "other_issues").strip().casefold().replace("-", "_").replace(" ", "_")
    return _FAULT_CATEGORY_ALIASES.get(key, "other_issues")


def _timestamp(value: object) -> tuple[datetime, str] | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        return parsed, parsed.isoformat()
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        return parsed, parsed.isoformat()
    return None


def _contract(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        return None
    asset_metrics = _metric_group(value.get("asset_metrics"), _ASSET_KEYS)
    payload_metrics = _metric_group(value.get("payload_metrics"), _PAYLOAD_KEYS)
    fault_metrics = _metric_group(value.get("fault_metrics"), _FAULT_KEYS)
    issue_metrics = _metric_group(value.get("issue_metrics"), _ISSUE_KEYS)
    system_metrics = value.get("system_metrics")
    asset_results = value.get("asset_results")
    fault_rows = value.get("fault_rows")
    if (
        asset_metrics is None
        or payload_metrics is None
        or fault_metrics is None
        or issue_metrics is None
        or not isinstance(system_metrics, list)
        or not isinstance(asset_results, list)
        or not isinstance(fault_rows, list)
    ):
        return None
    return {
        "asset_metrics": asset_metrics,
        "payload_metrics": payload_metrics,
        "fault_metrics": fault_metrics,
        "issue_metrics": issue_metrics,
        "system_metrics": system_metrics,
        "asset_results": asset_results,
        "fault_rows": fault_rows,
    }


def _empty_metrics(keys: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


def _add_metrics(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += source[key]


def build_udmi_report_model(sources: list[object]) -> dict[str, Any] | None:
    """Aggregate source contracts, or return ``None`` for the legacy renderer.

    If any selected UDMI run predates or corrupts the v1 contract, callers use
    the existing report summary instead of mixing precise and inferred metrics.
    """

    udmi_sources = [source for source in sources if getattr(source, "job_type", None) == "udmi_validation"]
    contracts: list[tuple[object, dict[str, Any]]] = []
    source_rows: list[dict[str, Any]] = []
    incomplete_source_runs: list[dict[str, Any]] = []
    latest: tuple[datetime, str] | None = None
    for source in udmi_sources:
        source_id = _text(getattr(source, "run_id", ""))
        status = _text(getattr(source, "status", ""))
        observed_at = _timestamp(getattr(source, "updated_at", None))
        if observed_at is not None and (latest is None or observed_at[0] > latest[0]):
            latest = observed_at
        summary = source.result_summary if isinstance(source.result_summary, dict) else {}
        contract = _contract(summary.get("validation_summary_v1"))
        if contract is None:
            # A succeeded pre-contract run still belongs to the legacy renderer.
            # Failed/cancelled runs may legitimately have no final metrics; keep
            # them in the v1 model as excluded evidence so the report can state
            # that its validation scope is incomplete instead of hiding them.
            if status == "succeeded":
                return None
        else:
            contracts.append((source, contract))

        source_row = {
            "run_id": source_id,
            "status": status,
            "updated_at": observed_at[1] if observed_at is not None else "",
            "schema_version": "1.0" if contract is not None else None,
            "metrics_included": contract is not None,
        }
        source_rows.append(source_row)
        if status != "succeeded" or contract is None:
            incomplete_source_runs.append(dict(source_row))

    asset_metrics = _empty_metrics(_ASSET_KEYS)
    payload_metrics = _empty_metrics(_PAYLOAD_KEYS)
    fault_metrics = _empty_metrics(_FAULT_KEYS)
    issue_metrics = _empty_metrics(_ISSUE_KEYS)
    systems: dict[str, dict[str, Any]] = {}
    asset_results: list[dict[str, Any]] = []
    fault_rows: list[dict[str, Any]] = []
    for source, contract in contracts:
        source_id = _text(getattr(source, "run_id", ""))
        _add_metrics(asset_metrics, contract["asset_metrics"])
        _add_metrics(payload_metrics, contract["payload_metrics"])
        _add_metrics(fault_metrics, contract["fault_metrics"])
        _add_metrics(issue_metrics, contract["issue_metrics"])

        for raw_system in contract["system_metrics"]:
            if not isinstance(raw_system, dict):
                return None
            system_name = _text(raw_system.get("system"), "Unspecified").strip() or "Unspecified"
            system_asset = _metric_group(raw_system.get("asset_metrics"), _ASSET_KEYS)
            system_payload = _metric_group(raw_system.get("payload_metrics"), _PAYLOAD_KEYS)
            system_fault = _metric_group(raw_system.get("fault_metrics"), _FAULT_KEYS)
            system_issue = _metric_group(raw_system.get("issue_metrics"), _ISSUE_KEYS)
            if None in (system_asset, system_payload, system_fault, system_issue):
                return None
            target = systems.setdefault(
                system_name,
                {
                    "system": system_name,
                    "asset_metrics": _empty_metrics(_ASSET_KEYS),
                    "payload_metrics": _empty_metrics(_PAYLOAD_KEYS),
                    "fault_metrics": _empty_metrics(_FAULT_KEYS),
                    "issue_metrics": _empty_metrics(_ISSUE_KEYS),
                },
            )
            _add_metrics(target["asset_metrics"], system_asset)
            _add_metrics(target["payload_metrics"], system_payload)
            _add_metrics(target["fault_metrics"], system_fault)
            _add_metrics(target["issue_metrics"], system_issue)

        for raw_asset in contract["asset_results"]:
            if not isinstance(raw_asset, dict):
                return None
            asset_results.append(
                {
                    "source_run_id": source_id,
                    "asset_id": _text(raw_asset.get("asset_id"), "Unspecified asset"),
                    "system": _text(raw_asset.get("system"), "Unspecified") or "Unspecified",
                    "observed": raw_asset.get("observed") is True,
                    "expected_payloads": _non_negative_int(raw_asset.get("expected_payloads")) or 0,
                    "received_payloads": _non_negative_int(raw_asset.get("received_payloads")) or 0,
                    "all_expected_payloads_received": (
                        raw_asset.get("all_expected_payloads_received") is True
                    ),
                    "all_received_payloads_successfully_validated": (
                        raw_asset.get("all_received_payloads_successfully_validated") is True
                    ),
                    "successfully_validated": raw_asset.get("successfully_validated") is True,
                    "issue_count": _non_negative_int(raw_asset.get("issue_count")) or 0,
                    "blocking_issue_count": (
                        _non_negative_int(raw_asset.get("blocking_issue_count")) or 0
                    ),
                    "last_observed_at": _optional_text(raw_asset.get("last_observed_at")),
                }
            )

        for raw_fault in contract["fault_rows"]:
            if not isinstance(raw_fault, dict):
                return None
            fault_rows.append(
                {
                    "source_run_id": source_id,
                    "issue_id": _text(raw_fault.get("issue_id")),
                    "asset_id": _text(raw_fault.get("asset_id"), "Unspecified asset"),
                    "system": _text(raw_fault.get("system"), "Unspecified") or "Unspecified",
                    "payload_type": _text(raw_fault.get("payload_type")),
                    "category": _normalise_fault_category(raw_fault.get("category")),
                    "severity": _text(raw_fault.get("severity")),
                    "description": _text(raw_fault.get("description")),
                    "point_name": _optional_text(raw_fault.get("point_name")),
                    "expected_value": _optional_text(raw_fault.get("expected_value")),
                    "observed_value": _optional_text(raw_fault.get("observed_value")),
                    "suggested_action": _optional_text(raw_fault.get("suggested_action")),
                    "raw_evidence_uri": _optional_text(raw_fault.get("raw_evidence_uri")),
                }
            )

    asset_results.sort(
        key=lambda row: (
            row["source_run_id"],
            row["system"].casefold(),
            row["asset_id"].casefold(),
        )
    )
    fault_rows.sort(
        key=lambda row: (
            row["source_run_id"],
            row["asset_id"].casefold(),
            row["issue_id"],
        )
    )

    matrix: dict[tuple[str, str, str], dict[str, Any]] = {}
    for asset in asset_results:
        key = (asset["source_run_id"], asset["asset_id"], asset["system"])
        matrix[key] = {
            "source_run_id": asset["source_run_id"],
            "asset_id": asset["asset_id"],
            "system": asset["system"],
            **{category: False for category in _FAULT_KEYS},
        }
    for fault in fault_rows:
        key = (fault["source_run_id"], fault["asset_id"], fault["system"])
        row = matrix.setdefault(
            key,
            {
                "source_run_id": fault["source_run_id"],
                "asset_id": fault["asset_id"],
                "system": fault["system"],
                **{category: False for category in _FAULT_KEYS},
            },
        )
        row[fault["category"]] = True

    notes = [
        "Observed means retained expected payload evidence was seen during the validation window.",
        "Not observed does not prove an asset's connection state.",
        "Asset timestamps are shown only when retained evidence contains one.",
    ]
    if not udmi_sources:
        notes.append("No UDMI validation source runs were selected.")
    excluded_sources = [row for row in source_rows if row["metrics_included"] is False]
    if excluded_sources:
        excluded_ids = ", ".join(str(row["run_id"]) for row in excluded_sources)
        notes.append(
            "Metrics exclude source runs without a retained validation_summary_v1 contract: "
            f"{excluded_ids}."
        )

    scope_complete = bool(udmi_sources) and not incomplete_source_runs
    if scope_complete:
        scope_summary = (
            f"Complete - all {len(udmi_sources)} selected UDMI validation source run(s) succeeded."
        )
    elif not udmi_sources:
        scope_summary = "INCOMPLETE - no UDMI validation source runs were selected."
    else:
        incomplete_labels = ", ".join(
            f"{row['run_id']} ({row['status']})" for row in incomplete_source_runs
        )
        scope_summary = (
            "INCOMPLETE - failed or cancelled source runs make this a partial validation scope: "
            f"{incomplete_labels}."
        )

    return {
        "schema_version": "1.0",
        "source_runs": source_rows,
        "scope_complete": scope_complete,
        "scope_status": "complete" if scope_complete else "incomplete",
        "scope_summary": scope_summary,
        "incomplete_source_runs": incomplete_source_runs,
        "last_validation_run_at": latest[1] if latest is not None else None,
        "asset_metrics": asset_metrics,
        "payload_metrics": payload_metrics,
        "fault_metrics": fault_metrics,
        "issue_metrics": issue_metrics,
        "system_metrics": sorted(systems.values(), key=lambda row: row["system"].casefold()),
        "asset_results": asset_results,
        "fault_matrix": [matrix[key] for key in sorted(matrix, key=lambda item: (item[0], item[2].casefold(), item[1].casefold()))],
        "fault_rows": fault_rows,
        "metric_definitions": list(METRIC_DEFINITIONS),
        "notes": notes,
    }
