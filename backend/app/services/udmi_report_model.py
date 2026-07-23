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
    ("unexpected", "Unexpected Devices"),
)

PAYLOAD_METRIC_LABELS = (
    ("expected", "Expected Payloads"),
    ("received", "Received Payloads"),
    ("not_received", "Not Received Payloads"),
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
    ("blocking", "Issues"),
    ("warning", "Warnings"),
)

_PAYLOAD_TYPES = ("state", "metadata", "pointset")
_PAYLOAD_ORDER = {payload_type: index for index, payload_type in enumerate(_PAYLOAD_TYPES)}
_BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium", "blocking"})

METRIC_DEFINITIONS = (
    {
        "metric": "Expected Assets",
        "definition": "Assets in the retained validation schedule for the selected runs.",
    },
    {
        "metric": "Observed Assets",
        "definition": (
            "Expected register assets with at least one retained expected payload during validation. "
            "Unexpected devices are counted separately and are not included."
        ),
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
            "Distinct publishers observed inside the run's measured discovery scope but absent "
            "from the expected register. They are excluded from expected, observed, compliance, "
            "fault-matrix, and validation-detail totals."
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
        "metric": "Not Received Payloads",
        "definition": "Expected payload types for which no retained evidence was received.",
    },
    {
        "metric": "Payloads With Issues",
        "definition": (
            "Received expected payloads linked to one or more retained validation issues. "
            "Expected payloads that were not received remain in Not Received and Payloads "
            "Incorrect, not in this count."
        ),
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
        "metric": "Issues",
        "definition": "Retained issues classified as blocking for acceptance.",
    },
    {
        "metric": "Warnings",
        "definition": "Retained warnings that require review.",
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

_ASSET_KEYS = tuple(key for key, _label in ASSET_METRIC_LABELS if key != "unexpected")
_PAYLOAD_KEYS = tuple(key for key, _label in PAYLOAD_METRIC_LABELS if key != "not_received")
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
    if not isinstance(value, dict) or value.get("schema_version") not in {"1.0", "1.1"}:
        return None
    raw_asset_metrics = value.get("asset_metrics")
    asset_metrics = _metric_group(raw_asset_metrics, _ASSET_KEYS)
    payload_metrics = _metric_group(value.get("payload_metrics"), _PAYLOAD_KEYS)
    fault_metrics = _metric_group(value.get("fault_metrics"), _FAULT_KEYS)
    issue_metrics = _metric_group(value.get("issue_metrics"), _ISSUE_KEYS)
    system_metrics = value.get("system_metrics")
    asset_results = value.get("asset_results")
    fault_rows = value.get("fault_rows")
    raw_unexpected = value.get("unexpected_devices", [])
    unexpected_devices = raw_unexpected if isinstance(raw_unexpected, list) else None
    if (
        asset_metrics is None
        or payload_metrics is None
        or fault_metrics is None
        or issue_metrics is None
        or not isinstance(system_metrics, list)
        or not isinstance(asset_results, list)
        or not isinstance(fault_rows, list)
        or unexpected_devices is None
    ):
        return None
    if value.get("schema_version") == "1.0":
        payload_metrics = _received_only_payload_metrics(payload_metrics, asset_results)
    unexpected_count = (
        _non_negative_int(raw_asset_metrics.get("unexpected"))
        if isinstance(raw_asset_metrics, dict)
        else None
    )
    if unexpected_count is None:
        unexpected_count = len([row for row in unexpected_devices if isinstance(row, dict)])
    asset_metrics["unexpected"] = unexpected_count
    return {
        "schema_version": str(value["schema_version"]),
        "asset_metrics": asset_metrics,
        "payload_metrics": payload_metrics,
        "fault_metrics": fault_metrics,
        "issue_metrics": issue_metrics,
        "system_metrics": system_metrics,
        "asset_results": asset_results,
        "fault_rows": fault_rows,
        "unexpected_devices": unexpected_devices,
        "unexpected_devices_measured": value.get("unexpected_devices_measured") is True,
        "unexpected_devices_measurement_scope": value.get(
            "unexpected_devices_measurement_scope"
        ),
    }


def _empty_metrics(keys: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(keys, 0)


def _add_metrics(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += source[key]


def _normalise_payload_results(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    rows: list[dict[str, Any]] = []
    seen_payload_types: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return None
        payload_type = _text(raw.get("payload_type")).strip().casefold()
        if payload_type not in _PAYLOAD_TYPES or payload_type in seen_payload_types:
            return None
        if any(
            not isinstance(raw.get(field), bool)
            for field in (
                "expected",
                "received",
                "has_issues",
                "successfully_validated",
            )
        ):
            return None
        blocking_issue_count = _non_negative_int(raw.get("blocking_issue_count"))
        if blocking_issue_count is None:
            return None
        if any(
            raw.get(field) is not None and not isinstance(raw.get(field), str)
            for field in ("topic", "received_at")
        ):
            return None
        seen_payload_types.add(payload_type)
        rows.append(
            {
                "payload_type": payload_type,
                "expected": raw["expected"],
                "received": raw["received"],
                "has_issues": raw["has_issues"],
                "blocking_issue_count": blocking_issue_count,
                "successfully_validated": raw["successfully_validated"],
                "topic": _optional_text(raw.get("topic")),
                "received_at": _optional_text(raw.get("received_at")),
            }
        )
    rows.sort(key=lambda row: _PAYLOAD_ORDER[row["payload_type"]])
    return rows


def _received_only_payload_metrics(
    metrics: dict[str, int],
    raw_assets: object = None,
) -> dict[str, int]:
    """Translate schema-1.0 payload counts to the received-only definition.

    Version 1.0 counted a missing expected payload with an attached validation
    issue in ``with_issues``. Version 1.1 separates that row into
    ``not_received``. When the legacy contract retains complete payload rows we
    recompute exactly; older compact snapshots without those rows are clamped
    conservatively so ``with_issues`` can never exceed ``received``.
    """

    normalised = dict(metrics)
    expected_rows: list[dict[str, Any]] = []
    if isinstance(raw_assets, list):
        for raw_asset in raw_assets:
            if not isinstance(raw_asset, dict):
                continue
            payloads = _normalise_payload_results(raw_asset.get("payload_results"))
            if payloads is not None:
                expected_rows.extend(payload for payload in payloads if payload["expected"] is True)
    received_rows = [payload for payload in expected_rows if payload["received"] is True]
    if (
        len(expected_rows) == normalised["expected"]
        and len(received_rows) == normalised["received"]
    ):
        normalised["with_issues"] = sum(
            payload["has_issues"] is True for payload in received_rows
        )
    else:
        normalised["with_issues"] = min(
            normalised["with_issues"], normalised["received"]
        )
    normalised["successfully_validated"] = min(
        normalised["successfully_validated"], normalised["received"]
    )
    return normalised


def _normalise_unexpected_device(
    value: object,
    *,
    source_run_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    device_id = _text(value.get("id")).strip()
    if not device_id:
        return None
    raw_topics = value.get("topics")
    topics = (
        sorted(
            {_text(topic).strip() for topic in raw_topics if _text(topic).strip()},
            key=str.casefold,
        )
        if isinstance(raw_topics, list)
        else []
    )
    return {
        "source_run_id": source_run_id,
        "id": device_id,
        "topic_root": _text(value.get("topic_root")).strip(),
        "topics": topics,
        "last_seen": _optional_text(value.get("last_seen")),
    }


def _unexpected_issue_ids(source: object) -> set[str]:
    """Issue IDs used by the retired unexpected-device fault representation."""

    issue_ids: set[str] = set()
    raw_issues = getattr(source, "issues", None)
    for issue in raw_issues if isinstance(raw_issues, list) else []:
        issue_type = (
            issue.get("issue_type")
            if isinstance(issue, dict)
            else getattr(issue, "issue_type", None)
        )
        issue_id = (
            issue.get("issue_id")
            if isinstance(issue, dict)
            else getattr(issue, "issue_id", None)
        )
        if (
            _text(issue_type).strip().casefold() == "unexpected_device"
            and _text(issue_id).strip()
        ):
            issue_ids.add(_text(issue_id).strip())
    return issue_ids


def _without_retired_unexpected_faults(
    fault_metrics: dict[str, int],
    issue_metrics: dict[str, int],
    faults: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Remove old unexpected-publisher rows from copied aggregate metrics."""

    adjusted_faults = dict(fault_metrics)
    adjusted_issues = dict(issue_metrics)
    for fault in faults:
        category = _normalise_fault_category(fault.get("category"))
        adjusted_faults[category] = max(0, adjusted_faults[category] - 1)
        severity_group = (
            "blocking"
            if _text(fault.get("severity")).strip().casefold() in _BLOCKING_SEVERITIES
            else "warning"
        )
        adjusted_issues[severity_group] = max(
            0, adjusted_issues[severity_group] - 1
        )
    return adjusted_faults, adjusted_issues


def _project_site_values(
    source: object,
    *,
    retained_asset_ids: set[str] | None = None,
) -> list[str]:
    parameters = getattr(source, "parameters", None)
    if not isinstance(parameters, dict):
        return []
    schedules: list[object] = [parameters.get("expected_schedule")]
    raw_assets = parameters.get("assets")
    if isinstance(raw_assets, list):
        schedules.extend(
            raw.get("expected_schedule")
            for raw in raw_assets
            if isinstance(raw, dict)
        )
    values = set()
    for schedule in schedules:
        if not isinstance(schedule, dict):
            continue
        asset_id = _text(schedule.get("asset_id")).strip()
        if retained_asset_ids is not None and asset_id not in retained_asset_ids:
            continue
        project_site = _text(schedule.get("project_site")).strip()
        if project_site:
            values.add(project_site)
    return sorted(values, key=str.casefold)


def _filter_summary(filters: dict[str, Any]) -> str:
    labels: list[str] = []
    text = _text(filters.get("text")).strip()
    topic = _text(filters.get("topic_contains")).strip()
    system = _text(filters.get("system"), "all").strip() or "all"
    verdict = _text(filters.get("verdict"), "all").strip() or "all"
    observation = _text(filters.get("observation"), "all").strip() or "all"
    category = _text(filters.get("category"), "all").strip() or "all"
    if text:
        labels.append(f'text contains "{text}"')
    if topic:
        labels.append(f'topic contains "{topic}"')
    if system != "all":
        labels.append(f"system {system}")
    if verdict != "all":
        labels.append(f"verdict {verdict}")
    if observation != "all":
        labels.append(f"observation {observation}")
    if category != "all":
        labels.append(f"category {category}")
    return ", ".join(labels) if labels else "All Results filters"


def normalise_udmi_report_scope(
    scope: object,
    sources: list[object],
) -> dict[str, Any]:
    """Validate and deterministically order an exact filtered-view selection."""

    if not isinstance(scope, dict) or scope.get("schema_version") != "1.0":
        raise ValueError("UDMI report scope must use schema version 1.0.")

    source_order: dict[str, int] = {}
    available_payloads: set[tuple[str, str, str]] = set()
    non_expected_payloads: set[tuple[str, str, str]] = set()
    all_payloads: set[tuple[str, str, str]] = set()
    available_unexpected_ids: set[str] = set()
    unexpected_sources: dict[str, set[str]] = {}
    for index, source in enumerate(sources):
        source_id = _text(getattr(source, "run_id", "")).strip()
        if not source_id:
            raise ValueError("A selected source run has no run ID.")
        source_order.setdefault(source_id, index)
        if getattr(source, "job_type", None) != "udmi_validation":
            raise ValueError(f"Source run '{source_id}' is not a UDMI validation run.")
        if _text(getattr(source, "status", "")) not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Source run '{source_id}' is not terminal.")
        summary = getattr(source, "result_summary", None)
        summary = summary if isinstance(summary, dict) else {}
        contract = _contract(summary.get("validation_summary_v1"))
        if contract is None:
            raise ValueError(
                f"Source run '{source_id}' cannot be filtered because it has no supported "
                "validation_summary_v1 contract. Run validation again before exporting."
            )
        for raw_asset in contract["asset_results"]:
            if not isinstance(raw_asset, dict):
                raise ValueError(f"Source run '{source_id}' has an invalid asset result.")
            asset_id = _text(raw_asset.get("asset_id")).strip()
            payloads = _normalise_payload_results(raw_asset.get("payload_results"))
            if not asset_id:
                raise ValueError(f"Source run '{source_id}' has an invalid asset result.")
            if payloads is None:
                if contract["schema_version"] == "1.0" and "payload_results" not in raw_asset:
                    raise ValueError(
                        f"Source run '{source_id}' predates exact payload filtering. "
                        "Run validation again before exporting a filtered report."
                    )
                raise ValueError(
                    f"Source run '{source_id}' contains malformed payload results for "
                    f"asset '{asset_id}'. Run validation again before exporting."
                )
            for payload in payloads:
                key = (source_id, asset_id, payload["payload_type"])
                if key in all_payloads:
                    raise ValueError(
                        f"Source run '{source_id}' contains a duplicate payload result for "
                        f"asset '{asset_id}' and type '{payload['payload_type']}'."
                    )
                all_payloads.add(key)
                if payload["expected"] is True:
                    available_payloads.add(key)
                else:
                    non_expected_payloads.add(key)
        for raw_device in contract["unexpected_devices"]:
            device = _normalise_unexpected_device(raw_device, source_run_id=source_id)
            if device is None:
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed unexpected-device row."
                )
            available_unexpected_ids.add(device["id"])
            unexpected_sources.setdefault(device["id"], set()).add(source_id)

    raw_selected = scope.get("selected_payloads")
    if not isinstance(raw_selected, list):
        raise ValueError("UDMI report scope selected_payloads must be a list.")
    selected: list[dict[str, str]] = []
    seen_payloads: set[tuple[str, str, str]] = set()
    for raw in raw_selected:
        if not isinstance(raw, dict):
            raise ValueError("UDMI report scope contains an invalid payload selection.")
        key = (
            _text(raw.get("source_run_id")).strip(),
            _text(raw.get("asset_id")).strip(),
            _text(raw.get("payload_type")).strip().casefold(),
        )
        if key in seen_payloads:
            raise ValueError("UDMI report scope contains a duplicate selected payload.")
        if key in non_expected_payloads:
            raise ValueError(
                "UDMI report scope may select expected payloads only; the referenced payload "
                f"was received but was not expected by the retained schedule: "
                f"{key[0]}/{key[1]}/{key[2]}."
            )
        if key not in available_payloads:
            raise ValueError(
                "UDMI report scope references a payload that is not present in its selected "
                f"source contract: {key[0]}/{key[1]}/{key[2]}."
            )
        seen_payloads.add(key)
        selected.append(
            {
                "source_run_id": key[0],
                "asset_id": key[1],
                "payload_type": key[2],
            }
        )
    selected.sort(
        key=lambda row: (
            source_order[row["source_run_id"]],
            row["asset_id"].casefold(),
            _PAYLOAD_ORDER[row["payload_type"]],
        )
    )

    raw_unexpected_ids = scope.get("unexpected_device_ids")
    if not isinstance(raw_unexpected_ids, list):
        raise ValueError("UDMI report scope unexpected_device_ids must be a list.")
    unexpected_ids = [_text(value).strip() for value in raw_unexpected_ids]
    if len(unexpected_ids) != len(set(unexpected_ids)):
        raise ValueError("UDMI report scope contains a duplicate unexpected device ID.")
    unknown_unexpected = sorted(
        set(unexpected_ids).difference(available_unexpected_ids),
        key=str.casefold,
    )
    if unknown_unexpected:
        raise ValueError(
            "UDMI report scope references unexpected devices that are not present in the "
            f"selected source contracts: {', '.join(unknown_unexpected)}."
        )
    ambiguous_unexpected = sorted(
        {
            device_id
            for device_id in unexpected_ids
            if len(unexpected_sources.get(device_id, set())) > 1
        },
        key=str.casefold,
    )
    if ambiguous_unexpected:
        raise ValueError(
            "UDMI report scope cannot identify these unexpected devices uniquely across the "
            f"selected source runs: {', '.join(ambiguous_unexpected)}."
        )

    raw_filters = scope.get("filters")
    raw_filters = raw_filters if isinstance(raw_filters, dict) else {}
    filters = {
        "text": _text(raw_filters.get("text")).strip(),
        "verdict": _text(raw_filters.get("verdict"), "all").strip() or "all",
        "topic_contains": _text(raw_filters.get("topic_contains")).strip(),
        "system": _text(raw_filters.get("system"), "all").strip() or "all",
        "observation": _text(raw_filters.get("observation"), "all").strip() or "all",
        "category": _text(raw_filters.get("category"), "all").strip() or "all",
    }
    return {
        "schema_version": "1.0",
        "selected_payloads": selected,
        "unexpected_device_ids": sorted(unexpected_ids, key=str.casefold),
        "filters": filters,
    }


def _computed_metrics(
    assets: list[dict[str, Any]],
    faults: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    asset_metrics = _empty_metrics(_ASSET_KEYS)
    asset_metrics["expected"] = len(assets)
    asset_metrics["observed"] = sum(row["observed"] is True for row in assets)
    asset_metrics["not_observed"] = asset_metrics["expected"] - asset_metrics["observed"]
    asset_metrics["with_issues"] = sum(row["issue_count"] > 0 for row in assets)
    asset_metrics["successfully_validated"] = sum(
        row["successfully_validated"] is True for row in assets
    )

    expected_payloads = [
        payload
        for asset in assets
        for payload in asset["payload_results"]
        if payload["expected"] is True
    ]
    payload_metrics = _empty_metrics(_PAYLOAD_KEYS)
    payload_metrics["expected"] = len(expected_payloads)
    payload_metrics["received"] = sum(row["received"] is True for row in expected_payloads)
    payload_metrics["not_received"] = (
        payload_metrics["expected"] - payload_metrics["received"]
    )
    payload_metrics["with_issues"] = sum(
        row["received"] is True and row["has_issues"] is True
        for row in expected_payloads
    )
    payload_metrics["successfully_validated"] = sum(
        row["successfully_validated"] is True for row in expected_payloads
    )

    fault_metrics = _empty_metrics(_FAULT_KEYS)
    issue_metrics = _empty_metrics(_ISSUE_KEYS)
    for fault in faults:
        fault_metrics[fault["category"]] += 1
        if fault["severity"].strip().casefold() in _BLOCKING_SEVERITIES:
            issue_metrics["blocking"] += 1
        else:
            issue_metrics["warning"] += 1
    return asset_metrics, payload_metrics, fault_metrics, issue_metrics


def _computed_system_metrics(
    assets: list[dict[str, Any]],
    faults: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for system in sorted({row["system"] for row in assets}, key=str.casefold):
        system_assets = [row for row in assets if row["system"] == system]
        asset_keys = {(row["source_run_id"], row["asset_id"]) for row in system_assets}
        system_faults = [
            row
            for row in faults
            if row["asset_id"] is not None
            and (row["source_run_id"], row["asset_id"]) in asset_keys
        ]
        asset, payload, fault, issue = _computed_metrics(system_assets, system_faults)
        rows.append(
            {
                "system": system,
                "asset_metrics": asset,
                "payload_metrics": payload,
                "fault_metrics": fault,
                "issue_metrics": issue,
            }
        )
    return rows


def build_udmi_report_model(
    sources: list[object],
    scope: object = None,
) -> dict[str, Any] | None:
    """Aggregate source contracts, or return ``None`` for the legacy renderer.

    Only a succeeded run with no ``validation_summary_v1`` key is treated as a
    pre-contract legacy run. A present but malformed/unsupported contract fails
    closed so an evidence report cannot silently discard its precise metrics.
    """

    udmi_sources = [
        source for source in sources if getattr(source, "job_type", None) == "udmi_validation"
    ]
    canonical_scope = (
        normalise_udmi_report_scope(scope, udmi_sources) if scope is not None else None
    )
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
        raw_summary = getattr(source, "result_summary", None)
        summary = raw_summary if isinstance(raw_summary, dict) else {}
        contract_key_present = "validation_summary_v1" in summary
        contract = _contract(summary.get("validation_summary_v1"))
        if contract is None:
            if contract_key_present:
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed or unsupported "
                    "validation_summary_v1 contract. Run validation again before exporting."
                )
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
            "schema_version": contract["schema_version"] if contract is not None else None,
            "metrics_included": contract is not None,
        }
        source_rows.append(source_row)
        if status != "succeeded" or contract is None:
            incomplete_source_runs.append(dict(source_row))

    asset_metrics = _empty_metrics(_ASSET_KEYS)
    asset_metrics["unexpected"] = 0
    payload_metrics = _empty_metrics(_PAYLOAD_KEYS)
    fault_metrics = _empty_metrics(_FAULT_KEYS)
    issue_metrics = _empty_metrics(_ISSUE_KEYS)
    systems: dict[str, dict[str, Any]] = {}
    asset_results: list[dict[str, Any]] = []
    fault_rows: list[dict[str, Any]] = []
    unexpected_devices: list[dict[str, Any]] = []
    unexpected_devices_measured = bool(contracts)
    unexpected_measurement_scopes: set[str] = set()
    project_sites: set[str] = set()
    for source, contract in contracts:
        source_id = _text(getattr(source, "run_id", ""))
        retired_issue_ids = _unexpected_issue_ids(source)
        source_fault_rows: list[dict[str, Any]] = []
        retired_fault_rows: list[dict[str, Any]] = []
        for raw_fault in contract["fault_rows"]:
            if not isinstance(raw_fault, dict):
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed validation fault row."
                )
            if _text(raw_fault.get("issue_id")).strip() in retired_issue_ids:
                retired_fault_rows.append(raw_fault)
            else:
                source_fault_rows.append(raw_fault)

        source_fault_metrics, source_issue_metrics = _without_retired_unexpected_faults(
            contract["fault_metrics"],
            contract["issue_metrics"],
            retired_fault_rows,
        )
        _add_metrics(asset_metrics, contract["asset_metrics"])
        _add_metrics(payload_metrics, contract["payload_metrics"])
        _add_metrics(fault_metrics, source_fault_metrics)
        _add_metrics(issue_metrics, source_issue_metrics)
        unexpected_devices_measured = (
            unexpected_devices_measured and contract["unexpected_devices_measured"]
        )
        measurement_scope = _optional_text(contract["unexpected_devices_measurement_scope"])
        if measurement_scope:
            unexpected_measurement_scopes.add(measurement_scope)
        for raw_device in contract["unexpected_devices"]:
            device = _normalise_unexpected_device(raw_device, source_run_id=source_id)
            if device is None:
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed unexpected-device row."
                )
            unexpected_devices.append(device)

        for raw_system in contract["system_metrics"]:
            if not isinstance(raw_system, dict):
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed system-metrics row."
                )
            system_name = _text(raw_system.get("system"), "Unspecified").strip() or "Unspecified"
            system_asset = _metric_group(raw_system.get("asset_metrics"), _ASSET_KEYS)
            system_payload = _metric_group(raw_system.get("payload_metrics"), _PAYLOAD_KEYS)
            system_fault = _metric_group(raw_system.get("fault_metrics"), _FAULT_KEYS)
            system_issue = _metric_group(raw_system.get("issue_metrics"), _ISSUE_KEYS)
            if (
                system_asset is None
                or system_payload is None
                or system_fault is None
                or system_issue is None
            ):
                raise ValueError(
                    f"Source run '{source_id}' contains malformed metrics for system "
                    f"'{system_name}'."
                )
            if contract["schema_version"] == "1.0":
                raw_system_assets = [
                    raw_asset
                    for raw_asset in contract["asset_results"]
                    if isinstance(raw_asset, dict)
                    and (
                        _text(raw_asset.get("system"), "Unspecified").strip()
                        or "Unspecified"
                    )
                    == system_name
                ]
                system_payload = _received_only_payload_metrics(
                    system_payload,
                    raw_system_assets,
                )
            retired_system_faults = [
                fault
                for fault in retired_fault_rows
                if (
                    _text(fault.get("system"), "Unspecified").strip()
                    or "Unspecified"
                )
                == system_name
            ]
            system_fault, system_issue = _without_retired_unexpected_faults(
                system_fault,
                system_issue,
                retired_system_faults,
            )
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
                raise ValueError(
                    f"Source run '{source_id}' contains a malformed asset-result row."
                )
            if "payload_results" not in raw_asset and contract["schema_version"] == "1.0":
                payload_results: list[dict[str, Any]] = []
            else:
                normalised_payloads = _normalise_payload_results(
                    raw_asset.get("payload_results")
                )
                if normalised_payloads is None:
                    raise ValueError(
                        f"Source run '{source_id}' contains malformed payload results for "
                        f"asset '{_text(raw_asset.get('asset_id'), 'Unspecified asset')}'."
                    )
                payload_results = normalised_payloads
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
                    "payload_results": payload_results,
                }
            )

        for raw_fault in source_fault_rows:
            fault_rows.append(
                {
                    "source_run_id": source_id,
                    "issue_id": _text(raw_fault.get("issue_id")),
                    "asset_id": _optional_text(raw_fault.get("asset_id")),
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

    unexpected_devices.sort(
        key=lambda row: (row["source_run_id"], row["id"].casefold())
    )

    if canonical_scope is not None:
        selected_keys = {
            (row["source_run_id"], row["asset_id"], row["payload_type"])
            for row in canonical_scope["selected_payloads"]
        }
        selected_by_asset: dict[tuple[str, str], set[str]] = {}
        for source_id, asset_id, payload_type in selected_keys:
            selected_by_asset.setdefault((source_id, asset_id), set()).add(payload_type)
        available_by_asset = {
            (row["source_run_id"], row["asset_id"]): {
                payload["payload_type"]
                for payload in row["payload_results"]
                if payload["expected"] is True
            }
            for row in asset_results
        }
        full_assets = {
            key
            for key, selected_types in selected_by_asset.items()
            if available_by_asset.get(key)
            and selected_types == available_by_asset.get(key)
        }
        available_by_source: dict[str, set[tuple[str, str]]] = {}
        selected_by_source: dict[str, set[tuple[str, str]]] = {}
        for (source_id, asset_id), payload_types in available_by_asset.items():
            available_by_source.setdefault(source_id, set()).update(
                (asset_id, payload_type) for payload_type in payload_types
            )
        for source_id, asset_id, payload_type in selected_keys:
            selected_by_source.setdefault(source_id, set()).add((asset_id, payload_type))
        full_sources = {
            source_id
            for source_id, available in available_by_source.items()
            if available and selected_by_source.get(source_id, set()) == available
        }

        scoped_faults: list[dict[str, Any]] = []
        for fault in fault_rows:
            source_id = fault["source_run_id"]
            asset_id = fault["asset_id"]
            payload_type = fault["payload_type"].strip().casefold()
            if asset_id is None:
                if source_id in full_sources:
                    scoped_faults.append(fault)
                continue
            asset_key = (source_id, asset_id)
            if asset_key not in selected_by_asset:
                continue
            if payload_type:
                if payload_type in selected_by_asset[asset_key]:
                    scoped_faults.append(fault)
            elif asset_key in full_assets:
                scoped_faults.append(fault)

        faults_by_asset: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fault in scoped_faults:
            if fault["asset_id"] is not None:
                faults_by_asset.setdefault(
                    (fault["source_run_id"], fault["asset_id"]), []
                ).append(fault)

        scoped_assets: list[dict[str, Any]] = []
        for asset in asset_results:
            asset_key = (asset["source_run_id"], asset["asset_id"])
            selected_types = selected_by_asset.get(asset_key)
            if not selected_types:
                continue
            asset_faults = faults_by_asset.get(asset_key, [])
            selected_payloads: list[dict[str, Any]] = []
            for payload in asset["payload_results"]:
                if payload["payload_type"] not in selected_types:
                    continue
                payload_faults = [
                    fault
                    for fault in asset_faults
                    if fault["payload_type"].strip().casefold() == payload["payload_type"]
                ]
                blocking = sum(
                    fault["severity"].strip().casefold() in _BLOCKING_SEVERITIES
                    for fault in payload_faults
                )
                selected_payloads.append(
                    {
                        **payload,
                        "has_issues": bool(payload_faults),
                        "blocking_issue_count": blocking,
                        "successfully_validated": payload["received"] is True and blocking == 0,
                    }
                )
            expected_payloads = [
                payload for payload in selected_payloads if payload["expected"] is True
            ]
            received_payloads = [
                payload for payload in selected_payloads if payload["received"] is True
            ]
            all_expected_received = bool(expected_payloads) and all(
                payload["received"] is True for payload in expected_payloads
            )
            blocking = sum(
                fault["severity"].strip().casefold() in _BLOCKING_SEVERITIES
                for fault in asset_faults
            )
            observed_times = [
                parsed
                for payload in selected_payloads
                if (parsed := _timestamp(payload["received_at"])) is not None
            ]
            latest_observed = max(observed_times, default=None, key=lambda item: item[0])
            scoped_assets.append(
                {
                    **asset,
                    "observed": bool(received_payloads),
                    "expected_payloads": len(expected_payloads),
                    "received_payloads": sum(
                        payload["received"] is True for payload in expected_payloads
                    ),
                    "all_expected_payloads_received": all_expected_received,
                    "all_received_payloads_successfully_validated": bool(received_payloads)
                    and all(
                        payload["successfully_validated"] is True
                        for payload in received_payloads
                    ),
                    "successfully_validated": all_expected_received and blocking == 0,
                    "issue_count": len(asset_faults),
                    "blocking_issue_count": blocking,
                    "last_observed_at": latest_observed[1] if latest_observed else None,
                    "payload_results": selected_payloads,
                }
            )

        asset_results = scoped_assets
        fault_rows = scoped_faults
        asset_metrics, payload_metrics, fault_metrics, issue_metrics = _computed_metrics(
            asset_results,
            fault_rows,
        )
        selected_unexpected_ids = set(canonical_scope["unexpected_device_ids"])
        chosen_devices: dict[str, dict[str, Any]] = {}
        for device in unexpected_devices:
            if device["id"] in selected_unexpected_ids:
                chosen_devices.setdefault(device["id"], device)
        unexpected_devices = [
            chosen_devices[device_id]
            for device_id in sorted(chosen_devices, key=str.casefold)
        ]
        asset_metrics["unexpected"] = len(selected_unexpected_ids)
        systems_list = _computed_system_metrics(asset_results, fault_rows)
    else:
        asset_metrics.setdefault("unexpected", 0)
        payload_metrics["not_received"] = (
            payload_metrics["expected"] - payload_metrics["received"]
        )
        for system in systems.values():
            system_payload = system["payload_metrics"]
            system_payload["not_received"] = (
                system_payload["expected"] - system_payload["received"]
            )
        systems_list = sorted(systems.values(), key=lambda row: row["system"].casefold())

    retained_assets_by_source: dict[str, set[str]] = {}
    if canonical_scope is not None:
        for asset in asset_results:
            retained_assets_by_source.setdefault(asset["source_run_id"], set()).add(
                asset["asset_id"]
            )
    for source, _contract_value in contracts:
        source_id = _text(getattr(source, "run_id", ""))
        retained_asset_ids = (
            retained_assets_by_source.get(source_id, set())
            if canonical_scope is not None
            else None
        )
        project_sites.update(
            _project_site_values(source, retained_asset_ids=retained_asset_ids)
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
            (row["asset_id"] or "").casefold(),
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
        if fault["asset_id"] is None:
            continue
        matching_keys = [
            key
            for key in matrix
            if key[0] == fault["source_run_id"] and key[1] == fault["asset_id"]
        ]
        for key in matching_keys:
            matrix[key][fault["category"]] = True

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
    if canonical_scope is not None:
        notes.append(
            "This report uses the exact Results payload selection captured when export was "
            "requested. Filter labels are provenance only."
        )
        notes.append(f"Active Results filters: {_filter_summary(canonical_scope['filters'])}.")
    if not unexpected_devices_measured:
        notes.append(
            "Unexpected devices were not measured for every selected source run."
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

    schema_version = (
        "1.1" if any(contract["schema_version"] == "1.1" for _source, contract in contracts) else "1.0"
    )
    project_site = "; ".join(sorted(project_sites, key=str.casefold))
    if len(project_sites) > 1:
        notes.append(
            "Selected source schedules contain more than one Project/site value; all values "
            "are shown in the report header."
        )
    return {
        "schema_version": schema_version,
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
        "system_metrics": systems_list,
        "asset_results": asset_results,
        "fault_matrix": [matrix[key] for key in sorted(matrix, key=lambda item: (item[0], item[2].casefold(), item[1].casefold()))],
        "fault_rows": fault_rows,
        "unexpected_devices": unexpected_devices,
        "unexpected_devices_measured": unexpected_devices_measured,
        "unexpected_devices_measurement_scope": (
            "; ".join(sorted(unexpected_measurement_scopes, key=str.casefold)) or None
        ),
        "project_label": project_site or None,
        "site_label": project_site or None,
        "report_scope": canonical_scope,
        "filter_provenance": canonical_scope["filters"] if canonical_scope else None,
        "filter_summary": _filter_summary(canonical_scope["filters"]) if canonical_scope else None,
        "metric_definitions": list(METRIC_DEFINITIONS),
        "notes": notes,
    }
