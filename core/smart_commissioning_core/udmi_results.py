"""Versioned, evidence-based summary contract for UDMI validation results.

The validator persists this projection inside ``result_summary`` so every
consumer (UI and report/export renderers) can use the same counts.  The helpers
operate only on the run inputs and structured issue records; they perform no
I/O and never infer an online/offline state from broker silence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

VALIDATION_SUMMARY_SCHEMA_VERSION = "1.1"
UNSPECIFIED_SYSTEM = "Unspecified"

_PAYLOAD_TYPES = ("state", "metadata", "pointset")
_BLOCKING_SEVERITIES = frozenset({"critical", "high", "medium"})
_FAULT_CATEGORIES = (
    "payload_formatting_issues",
    "missing_points",
    "point_naming_issues",
    "additional_points",
    "stale_or_cadence",
    "other_issues",
)


def _issue_value(issue: object, name: str) -> object:
    if isinstance(issue, dict):
        return issue.get(name)
    return getattr(issue, name, None)


def _dict_value(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _system_name(value: object) -> str:
    text = str(value or "").strip()
    return text or UNSPECIFIED_SYSTEM


def _unexpected_devices(parameters: dict[str, object]) -> list[dict[str, object]]:
    """Return a stable, report-safe projection of measured unregistered publishers."""

    raw_devices = parameters.get("unexpected_devices")
    if not isinstance(raw_devices, list):
        return []
    devices: dict[str, dict[str, object]] = {}
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        device_id = str(raw.get("id") or "").strip()
        if not device_id:
            continue
        topics = raw.get("topics")
        device: dict[str, object] = {
            "id": device_id,
            "topic_root": str(raw.get("topic_root") or "").strip(),
            "topics": sorted(
                {str(topic).strip() for topic in topics if str(topic).strip()},
                key=str.casefold,
            )
            if isinstance(topics, list)
            else [],
            "last_seen": str(raw.get("last_seen") or "").strip() or None,
        }
        devices.setdefault(device_id, device)
    return [devices[key] for key in sorted(devices, key=str.casefold)]


def _payload_type_from_topic(value: object) -> str | None:
    topic = str(value or "").casefold().rstrip("/")
    if topic.endswith("/state"):
        return "state"
    if topic.endswith("/metadata"):
        return "metadata"
    if topic.endswith("/pointset"):
        return "pointset"
    return None


def payload_type_for_issue(issue: object) -> str | None:
    """Return the payload facet an issue belongs to, when structured evidence allows it."""
    issue_type = str(_issue_value(issue, "issue_type") or "").casefold()
    direct = {
        "state_validation": "state",
        "metadata_validation": "metadata",
        "pointset_validation": "pointset",
        "pointset_timestamp": "pointset",
    }.get(issue_type)
    if direct:
        return direct

    topic_type = _payload_type_from_topic(_issue_value(issue, "topic"))
    if topic_type:
        return topic_type

    # Capture/parse issues sometimes have no topic field.  Use a facet name only
    # when exactly one is named, so a generic broker failure is never assigned to
    # an arbitrary payload.
    evidence = " ".join(
        str(_issue_value(issue, field) or "")
        for field in ("description", "raw_evidence_uri")
    ).casefold()
    mentioned = [payload_type for payload_type in _PAYLOAD_TYPES if payload_type in evidence]
    return mentioned[0] if len(mentioned) == 1 else None


def fault_category_for_issue(issue: object) -> str:
    """Map one structured issue to the stakeholder's stable fault categories."""
    issue_type = str(_issue_value(issue, "issue_type") or "").casefold()
    description = str(_issue_value(issue, "description") or "").casefold()
    expected = str(_issue_value(issue, "expected_value") or "").casefold()
    observed = str(_issue_value(issue, "observed_value") or "").casefold()
    point_name = str(_issue_value(issue, "point_name") or "").strip()

    if issue_type == "pointset_timestamp":
        return "stale_or_cadence"
    if "similarly named" in description or "misnamed point" in description:
        return "point_naming_issues"
    if point_name and (
        expected == "not in register"
        or "not found in the expected schedule" in description
        or "not in the expected schedule" in description
    ):
        return "additional_points"
    if point_name and observed == "missing" and (
        ("expected point" in description and "not received" in description)
        or "not defined in the metadata pointset" in description
    ):
        return "missing_points"
    if issue_type in {
        "payload_error",
        "state_validation",
        "metadata_validation",
        "pointset_validation",
    }:
        # Structural, schema, type, version, unit, and value-shape faults share
        # this stakeholder-facing category after the point-specific cases above.
        return "payload_formatting_issues"
    return "other_issues"


def _is_blocking(issue: object) -> bool:
    return (
        str(_issue_value(issue, "issue_type") or "").casefold() != "not_publishing"
        and str(_issue_value(issue, "severity") or "").casefold() in _BLOCKING_SEVERITIES
    )


def _expected_payload_types(source: dict[str, object], *, synthetic: bool) -> set[str]:
    expected = {
        payload_type
        for payload_type in _PAYLOAD_TYPES
        if source.get(f"{payload_type}_topic")
    }
    if expected or synthetic:
        return expected
    # Direct-input/back-compat path: a schedule without register topic slots has
    # historically produced all three expected UDMI facets.
    return set(_PAYLOAD_TYPES) if _dict_value(source.get("expected_schedule")) else set()


def _payload_observations(source: dict[str, object]) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {
        payload_type: {
            "received": False,
            "topic": str(source.get(f"{payload_type}_topic") or "") or None,
            "received_at": None,
        }
        for payload_type in _PAYLOAD_TYPES
    }
    for payload_type in _PAYLOAD_TYPES:
        key = f"{payload_type}_payload"
        if key in source and isinstance(source.get(key), (dict, str)):
            value = source.get(key)
            parsed = _dict_value(value)
            # Presence is evidence even when the body is an empty object or an
            # invalid JSON string. Validation issues explain why it failed.
            observations[payload_type]["received"] = True
            received_at = source.get(f"{key}_received_at")
            timestamp = parsed.get("timestamp")
            observations[payload_type]["received_at"] = (
                str(received_at or timestamp) if (received_at or timestamp) else None
            )

    messages = source.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        message_payload_type = _payload_type_from_topic(message.get("topic"))
        if message_payload_type is None:
            continue
        observations[message_payload_type]["received"] = True
        observations[message_payload_type]["topic"] = (
            str(message.get("topic") or "") or None
        )
        if message.get("received_at"):
            observations[message_payload_type]["received_at"] = str(
                message["received_at"]
            )
    return observations


def _asset_sources(
    parameters: dict[str, object],
    fallback_expected_asset_ids: Iterable[object],
    fallback_observed_asset_ids: Iterable[object],
) -> list[tuple[dict[str, object], bool, bool]]:
    assets = parameters.get("assets")
    if isinstance(assets, list) and assets:
        return [(entry, False, False) for entry in assets if isinstance(entry, dict)]
    if _dict_value(parameters.get("expected_schedule")):
        return [(parameters, False, False)]

    observed_ids = {str(item) for item in fallback_observed_asset_ids}
    return [
        (
            {"expected_schedule": {"asset_id": str(asset_id), "system": UNSPECIFIED_SYSTEM}},
            True,
            str(asset_id) in observed_ids,
        )
        for asset_id in fallback_expected_asset_ids
    ]


def _empty_asset_metrics() -> dict[str, int]:
    return {
        "expected": 0,
        "observed": 0,
        "not_observed": 0,
        "with_issues": 0,
        "successfully_validated": 0,
        "unexpected": 0,
    }


def _empty_payload_metrics() -> dict[str, int]:
    return {
        "expected": 0,
        "received": 0,
        "not_received": 0,
        "with_issues": 0,
        "successfully_validated": 0,
    }


def _empty_fault_metrics() -> dict[str, int]:
    return {category: 0 for category in _FAULT_CATEGORIES}


def _latest_observed_at(payloads: Iterable[dict[str, object]]) -> str | None:
    raw_values = [
        str(payload["received_at"])
        for payload in payloads
        if payload.get("received_at")
    ]
    parsed_values: list[tuple[datetime, str]] = []
    for raw in raw_values:
        candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        parsed_values.append((parsed.astimezone(UTC), raw))
    if parsed_values:
        return max(parsed_values, key=lambda item: item[0])[1]
    # Older retained evidence may contain a non-RFC3339 token. Preserve a
    # deterministic value instead of discarding the only observation marker.
    return max(raw_values, default=None)


def _fault_row(issue: object, system_by_asset: dict[str, str]) -> dict[str, object]:
    asset_id = str(_issue_value(issue, "asset_id") or "") or None
    return {
        "issue_id": str(_issue_value(issue, "issue_id") or ""),
        "asset_id": asset_id,
        "system": system_by_asset.get(str(asset_id), UNSPECIFIED_SYSTEM),
        "payload_type": payload_type_for_issue(issue),
        "category": fault_category_for_issue(issue),
        "severity": str(_issue_value(issue, "severity") or ""),
        "description": str(_issue_value(issue, "description") or ""),
        "point_name": _issue_value(issue, "point_name"),
        "expected_value": _issue_value(issue, "expected_value"),
        "observed_value": _issue_value(issue, "observed_value"),
        "suggested_action": _issue_value(issue, "suggested_action"),
        "raw_evidence_uri": _issue_value(issue, "raw_evidence_uri"),
    }


def build_validation_summary_v1(
    parameters: dict[str, object],
    issues: Iterable[object],
    *,
    fallback_expected_asset_ids: Iterable[object] = (),
    fallback_observed_asset_ids: Iterable[object] = (),
) -> dict[str, object]:
    """Build the persisted version-1 UDMI validation result projection."""
    # Older fixture imports represented unregistered publishers as validation
    # issues. They are now a separate measured inventory and must not influence
    # validation, fault, system, or compliance metrics even when an old caller
    # still supplies one of those records.
    issue_list = [
        issue
        for issue in issues
        if str(_issue_value(issue, "issue_type") or "").casefold()
        != "unexpected_device"
    ]
    sources = _asset_sources(
        parameters,
        fallback_expected_asset_ids,
        fallback_observed_asset_ids,
    )

    system_by_asset: dict[str, str] = {}
    for source, _synthetic, _synthetic_observed in sources:
        expected = _dict_value(source.get("expected_schedule"))
        asset_id = str(expected.get("asset_id") or "UDMI asset")
        system_by_asset.setdefault(asset_id, _system_name(expected.get("system")))

    fault_rows = [_fault_row(issue, system_by_asset) for issue in issue_list]
    asset_results: list[dict[str, object]] = []
    for source, synthetic, synthetic_observed in sources:
        expected = _dict_value(source.get("expected_schedule"))
        asset_id = str(expected.get("asset_id") or "UDMI asset")
        system = _system_name(expected.get("system"))
        expected_types = _expected_payload_types(source, synthetic=synthetic)
        observations = _payload_observations(source)
        asset_issues = [issue for issue in issue_list if str(_issue_value(issue, "asset_id") or "") == asset_id]
        asset_blocking = sum(1 for issue in asset_issues if _is_blocking(issue))

        payload_results: list[dict[str, object]] = []
        for payload_type in _PAYLOAD_TYPES:
            received = bool(observations[payload_type]["received"])
            is_expected = payload_type in expected_types
            if not is_expected and not received:
                continue
            payload_issues = [
                issue for issue in asset_issues if payload_type_for_issue(issue) == payload_type
            ]
            payload_blocking = sum(1 for issue in payload_issues if _is_blocking(issue))
            payload_results.append(
                {
                    "payload_type": payload_type,
                    "expected": is_expected,
                    "received": received,
                    "has_issues": bool(payload_issues),
                    "blocking_issue_count": payload_blocking,
                    "successfully_validated": received and payload_blocking == 0,
                    "topic": observations[payload_type]["topic"],
                    "received_at": observations[payload_type]["received_at"],
                }
            )

        expected_payloads = sum(1 for payload in payload_results if payload["expected"])
        expected_received_results = [
            payload
            for payload in payload_results
            if payload["expected"] and payload["received"]
        ]
        received_payloads = len(expected_received_results)
        observed = synthetic_observed if synthetic else received_payloads > 0
        all_expected_received = expected_payloads > 0 and all(
            bool(payload["received"])
            for payload in payload_results
            if payload["expected"]
        )
        all_received_validated = bool(expected_received_results) and all(
            bool(payload["successfully_validated"])
            for payload in expected_received_results
        )
        asset_results.append(
            {
                "asset_id": asset_id,
                "system": system,
                "observed": observed,
                "expected_payloads": expected_payloads,
                "received_payloads": received_payloads,
                "all_expected_payloads_received": all_expected_received,
                "all_received_payloads_successfully_validated": all_received_validated,
                "successfully_validated": all_expected_received and asset_blocking == 0,
                "issue_count": len(asset_issues),
                "blocking_issue_count": asset_blocking,
                "last_observed_at": _latest_observed_at(expected_received_results),
                "payload_results": payload_results,
            }
        )

    def aggregate_assets(rows: list[dict[str, object]]) -> dict[str, int]:
        metrics = _empty_asset_metrics()
        metrics["expected"] = len(rows)
        metrics["observed"] = sum(bool(row["observed"]) for row in rows)
        metrics["not_observed"] = metrics["expected"] - metrics["observed"]
        metrics["with_issues"] = sum(bool(row["issue_count"]) for row in rows)
        metrics["successfully_validated"] = sum(bool(row["successfully_validated"]) for row in rows)
        return metrics

    def aggregate_payloads(rows: list[dict[str, object]]) -> dict[str, int]:
        payloads: list[dict[str, object]] = []
        for row in rows:
            raw_payloads = row.get("payload_results")
            if not isinstance(raw_payloads, list):
                continue
            payloads.extend(
                payload
                for payload in raw_payloads
                if isinstance(payload, dict) and payload.get("expected") is True
            )
        metrics = _empty_payload_metrics()
        metrics["expected"] = len(payloads)
        metrics["received"] = sum(bool(payload["received"]) for payload in payloads)
        metrics["not_received"] = metrics["expected"] - metrics["received"]
        metrics["with_issues"] = sum(
            bool(payload["received"]) and bool(payload["has_issues"])
            for payload in payloads
        )
        metrics["successfully_validated"] = sum(
            bool(payload["successfully_validated"]) for payload in payloads
        )
        return metrics

    def aggregate_faults(rows: list[dict[str, object]]) -> dict[str, int]:
        metrics = _empty_fault_metrics()
        for row in rows:
            category = str(row["category"])
            metrics[category] += 1
        return metrics

    def aggregate_issues(rows: list[object]) -> dict[str, int]:
        blocking = sum(1 for row in rows if _is_blocking(row))
        return {"blocking": blocking, "warning": len(rows) - blocking}

    system_metrics: list[dict[str, object]] = []
    for system in sorted({str(row["system"]) for row in asset_results}, key=str.casefold):
        system_assets = [row for row in asset_results if row["system"] == system]
        system_faults = [row for row in fault_rows if row["system"] == system]
        system_issues = [
            issue
            for issue in issue_list
            if system_by_asset.get(str(_issue_value(issue, "asset_id") or "")) == system
        ]
        system_metrics.append(
            {
                "system": system,
                "asset_metrics": aggregate_assets(system_assets),
                "payload_metrics": aggregate_payloads(system_assets),
                "fault_metrics": aggregate_faults(system_faults),
                "issue_metrics": aggregate_issues(system_issues),
            }
        )

    unexpected_devices = _unexpected_devices(parameters)
    asset_metrics = aggregate_assets(asset_results)
    asset_metrics["unexpected"] = len(unexpected_devices)
    return {
        "schema_version": VALIDATION_SUMMARY_SCHEMA_VERSION,
        "asset_metrics": asset_metrics,
        "payload_metrics": aggregate_payloads(asset_results),
        "fault_metrics": aggregate_faults(fault_rows),
        "issue_metrics": aggregate_issues(issue_list),
        "system_metrics": system_metrics,
        "asset_results": asset_results,
        "fault_rows": fault_rows,
        "unexpected_devices": unexpected_devices,
        "unexpected_devices_measured": parameters.get("unexpected_devices_measured") is True,
        "unexpected_devices_measurement_scope": (
            str(parameters.get("unexpected_devices_measurement_scope") or "").strip() or None
        ),
    }
