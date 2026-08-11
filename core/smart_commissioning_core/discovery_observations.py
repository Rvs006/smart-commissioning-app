"""Versioned, viewer-safe progressive discovery observation contracts."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from smart_commissioning_core.run_context import (
    canonical_json_bytes,
    canonical_sha256,
    json_safe_value,
)

ObservationProtocol = Literal["ip", "bacnet"]
ObservationEntityKind = Literal[
    "lane",
    "host",
    "port",
    "device",
    "object",
    "property",
    "diagnostic",
]
ObservationPhase = Literal[
    "planned",
    "reachability",
    "enrichment",
    "comparison",
    "finalize",
]
ProjectionCollection = Literal["summary", "issues", "devices", "points", "topics"]

PUBLIC_PAYLOAD_V1_MAX_DEPTH = 8
PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS = 4_096
PUBLIC_PAYLOAD_V1_MAX_VALUE_STRING_CHARS = 60_000
PUBLIC_PAYLOAD_V1_MAX_SEQUENCE_ITEMS = 512

OBSERVATION_STREAM_EMPTY_SHA256 = hashlib.sha256().hexdigest()
_OBSERVATION_STREAM_CHAIN_DOMAIN = (
    b"smart-commissioning.discovery-observation-stream-chain.v1\x00"
)

_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])[^\s]*",
    re.IGNORECASE,
)
_POSIX_LOCAL_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:Applications|etc|home|Library|mnt|opt|private|root|srv|tmp|Users|usr|var|Volumes)(?:/|\b)",
    re.IGNORECASE,
)
_RELATIVE_LOCAL_FILE_PATH = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:artifacts?|cache|downloads?|evidence|logs?|output|runtime|temp|tmp|uploads?)[\\/]"
    r"[^\r\n]*?\."
    r"(?:cer|crt|csv|db|json|key|log|pcap|pcapng|pem|pfx|sqlite|sqlite3|txt|xlsx?|xml|ya?ml|zip)\b",
    re.IGNORECASE,
)
_RELATIVE_PATH_TRAVERSAL = re.compile(r"(?<![A-Za-z0-9.])\.{1,2}[\\/]")
_LOCAL_EVIDENCE_MARKER = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:file|secret):(?=\S)|"
    r"-----\s*(?:begin|end)\s+[a-z0-9][a-z0-9 -]{0,63}-----)",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:(?:[A-Za-z0-9]{1,32})[_-]){0,8}"
    r"(?:api[_-]?key|authorization|passphrase|passwd|password|private[_-]?key|pwd|secret|token)"
    r"\s*[:=]\s*(?=\S)",
    re.IGNORECASE,
)
_OPAQUE_OBSERVATION_IDENTITY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    re.ASCII,
)
_OPAQUE_REFERENCE = re.compile(r"^(?:artifact|evidence)_[A-Za-z0-9._:-]{1,240}$")
_XML_MARKUP = re.compile(
    r"<\s*/?\s*[A-Za-z_][A-Za-z0-9_.:-]*(?:\s+[^<>]*)?/?>",
    re.IGNORECASE,
)

_PUBLIC_PAYLOAD_V1_FIELDS = frozenset(
    {
        "accepted",
        "address",
        "attempts",
        "device_instance",
        "diagnostic_text",
        "duplicate",
        "elapsed_ms",
        "lane",
        "late",
        "object_identifier",
        "port",
        "projection_v1",
        "property_name",
        "reachable",
        "reason",
        "responded",
        "response_count",
        "response_ms",
        "state",
        "stderr_evidence_id",
        "transport",
        "units",
        "value",
        "xml_artifact_id",
    }
)

_DEVICE_RECORD_FIELDS = frozenset(
    {
        "project_id",
        "site_id",
        "address",
        "device_type",
        "name",
        "vendor",
        "model",
        "attributes",
    }
)
_POINT_RECORD_FIELDS = frozenset(
    {
        "device_ref",
        "point_id",
        "point_name",
        "observed_value",
        "units",
        "attributes",
    }
)
_TOPIC_RECORD_FIELDS = frozenset(
    {"topic", "last_payload", "message_count", "attributes"}
)
_ISSUE_RECORD_FIELDS = frozenset(
    {
        "issue_id",
        "asset_id",
        "issue_type",
        "severity",
        "description",
        "status",
        "point_name",
        "topic",
        "expected_value",
        "observed_value",
        "match_basis",
        "suggested_action",
        "raw_evidence_uri",
        "status_detail",
        "last_seen_at",
    }
)
_SUMMARY_RECORD_FIELDS = frozenset(
    {
        "backend",
        "bacnet_mode",
        "bbmd_address",
        "device_count",
        "device_instance_high",
        "device_instance_low",
        "discovered_devices",
        "expected_device_count",
        "expected_responding_count",
        "forbidden_ports",
        "hosts_responsive",
        "hosts_scanned",
        "hosts_with_forbidden_open",
        "hosts_with_hostname_mismatch",
        "hosts_with_missing_expected",
        "hosts_with_unexpected_open",
        "point_count",
        "ports_scanned",
        "reachable",
        "unicast_fallback_attempted",
    }
)
_PUBLIC_ATTRIBUTE_FIELDS = frozenset(
    {
        "asset_id",
        "device_instance",
        "duplicate_instance",
        "expected_hostname",
        "expected_network",
        "expected_ports",
        "forbidden_open_ports",
        "forbidden_ports",
        "heard_not_enriched",
        "hostname",
        "lane",
        "mac",
        "mac_address",
        "marker",
        "missing_expected_ports",
        "object_type",
        "objects",
        "object_list_read_failed",
        "observed_ports",
        "online",
        "open",
        "open_ports",
        "position",
        "point_reads_aborted",
        "point_reads_truncated",
        "property_error",
        "qos",
        "reachable",
        "read_error",
        "register_address",
        "register_asset_id",
        "register_asset_name",
        "register_match",
        "register_matched_filter",
        "register_ports_not_probed",
        "responded",
        "response_ms",
        "scanned_port_count",
        "scanned_ports",
        "tampered",
        "unexpected_open_ports",
        "vendor_id",
    }
)


class DiscoveryProjectionV1(BaseModel):
    """One optional terminal-projection contribution carried by an event."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    collection: ProjectionCollection
    position: int = Field(default=0, ge=0, le=50_000)
    present: bool = True
    record: dict[str, Any] | None = None

    @field_validator("record")
    @classmethod
    def _public_projection_record(
        cls,
        value: dict[str, Any] | None,
        info: ValidationInfo,
    ) -> dict[str, Any] | None:
        present = bool(info.data.get("present", True))
        if not present:
            if value not in (None, {}):
                raise ValueError("a projection tombstone cannot carry a record")
            return None
        if value is None:
            raise ValueError("a present projection requires a record")
        collection = info.data.get("collection")
        if collection is None:
            raise ValueError("a projection requires a supported collection")
        return _normalize_projection_record(str(collection), value)


class DiscoveryObservationInputV1(BaseModel):
    """Normalized observation supplied by a provider to its executor-owned sink."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    protocol: ObservationProtocol
    entity_kind: ObservationEntityKind
    entity_key: str = Field(min_length=1, max_length=255)
    entity_version: int = Field(ge=1)
    event_key: str = Field(min_length=1, max_length=255)
    phase: ObservationPhase
    outcome: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    payload_schema_version: Literal["1.0"] = "1.0"
    payload: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None

    @field_validator("entity_key", "event_key")
    @classmethod
    def _bounded_printable_identity(cls, value: str) -> str:
        normalized = value.strip()
        if (
            _OPAQUE_OBSERVATION_IDENTITY.fullmatch(normalized) is None
            or _LOCAL_EVIDENCE_MARKER.search(normalized)
            or _CREDENTIAL_ASSIGNMENT.search(normalized)
        ):
            raise ValueError("observation identities must use opaque normalized syntax")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def _aware_observation_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value

    @field_validator("payload")
    @classmethod
    def _viewer_safe_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_public_payload_v1(value)


class DiscoveryObservationViewV1(DiscoveryObservationInputV1):
    """One durable observation row returned through a scoped cursor page."""

    cursor: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _aware_created_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value


class ObservationAppendOutcomeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cursor: int = Field(ge=1)
    idempotent: bool = False


class ObservationCutoffV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    terminal_cursor: int = Field(ge=0)
    observation_count: int = Field(ge=0)


class ObservationEvidenceV1(BaseModel):
    """Commitment embedded in the terminal result and therefore in its seal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    attempt: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    terminal_cursor: int = Field(ge=0)
    observation_stream_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiscoveryObservationFoldV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    terminal_cursor: int = Field(ge=0)
    observation_stream_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projected_collections: frozenset[ProjectionCollection] = frozenset()
    summary: dict[str, Any] = Field(default_factory=dict)
    issues: tuple[dict[str, Any], ...] = ()
    devices: tuple[dict[str, Any], ...] = ()
    points: tuple[dict[str, Any], ...] = ()
    topics: tuple[dict[str, Any], ...] = ()

    def evidence(self) -> ObservationEvidenceV1:
        return ObservationEvidenceV1(
            attempt=self.attempt,
            observation_count=self.observation_count,
            terminal_cursor=self.terminal_cursor,
            observation_stream_sha256=self.observation_stream_sha256,
        )


def observation_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bytes, str]:
    """Normalize and hash a viewer-safe payload before its append transaction."""

    normalized = json_safe_value(_normalize_public_payload_v1(payload))
    if not isinstance(normalized, dict):
        raise ValueError("observation payload must normalize to an object")
    encoded = canonical_json_bytes(normalized)
    return normalized, encoded, hashlib.sha256(encoded).hexdigest()


def _normalize_public_payload_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized, explicitly allowlisted public payload v1."""

    if not isinstance(value, Mapping):
        raise ValueError("payload must be an object")
    _require_exact_fields(value, _PUBLIC_PAYLOAD_V1_FIELDS, path="payload")
    normalized: dict[str, Any] = {}
    boolean_fields = {
        "accepted",
        "duplicate",
        "late",
        "reachable",
        "responded",
    }
    integer_fields = {"attempts", "device_instance", "port", "response_count"}
    number_fields = {"elapsed_ms", "response_ms"}
    short_string_fields = {
        "address",
        "lane",
        "object_identifier",
        "property_name",
        "reason",
        "state",
        "transport",
        "units",
    }
    for key, item in value.items():
        if key == "projection_v1":
            projection = DiscoveryProjectionV1.model_validate(item)
            normalized[key] = projection.model_dump(
                mode="python",
                exclude_none=True,
                exclude_defaults=True,
            )
        elif key in boolean_fields:
            normalized[key] = _normalize_bool(item, path=f"payload.{key}")
        elif key in integer_fields:
            normalized[key] = _normalize_int(item, path=f"payload.{key}", minimum=0)
        elif key in number_fields:
            normalized[key] = _normalize_number(
                item,
                path=f"payload.{key}",
                minimum=0,
            )
        elif key == "diagnostic_text":
            normalized[key] = _normalize_public_string(
                item,
                path="payload.diagnostic_text",
                max_chars=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
            )
        elif key in {"stderr_evidence_id", "xml_artifact_id"}:
            reference = _normalize_public_string(
                item,
                path=f"payload.{key}",
                max_chars=255,
            )
            if _OPAQUE_REFERENCE.fullmatch(reference) is None:
                raise ValueError(f"payload.{key} must be an opaque evidence reference")
            normalized[key] = reference
        elif key in short_string_fields:
            normalized[key] = _normalize_public_string(
                item,
                path=f"payload.{key}",
                max_chars=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
            )
        elif key == "value":
            normalized[key] = _normalize_public_value(
                item,
                path="payload.value",
                depth=1,
                string_limit=PUBLIC_PAYLOAD_V1_MAX_VALUE_STRING_CHARS,
            )
        else:  # pragma: no cover - exact-field guard is the invariant
            raise ValueError(f"payload.{key} has no public v1 contract")
    return normalized


def _normalize_projection_record(
    collection: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("projection_v1.record must be an object")
    if collection == "devices":
        return _normalize_device_record(value)
    if collection == "points":
        return _normalize_point_record(value)
    if collection == "topics":
        return _normalize_topic_record(value)
    if collection == "issues":
        return _normalize_issue_record(value)
    if collection == "summary":
        return _normalize_summary_record(value)
    raise ValueError("projection_v1.collection has no public v1 contract")


def _normalize_device_record(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "payload.projection_v1.record"
    _require_exact_fields(value, _DEVICE_RECORD_FIELDS, path=path)
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "attributes":
            normalized[key] = _normalize_attributes(item, path=f"{path}.attributes")
        else:
            normalized[key] = _normalize_optional_public_string(
                item,
                path=f"{path}.{key}",
                max_chars=255,
            )
    return normalized


def _normalize_point_record(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "payload.projection_v1.record"
    _require_exact_fields(value, _POINT_RECORD_FIELDS, path=path)
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "attributes":
            normalized[key] = _normalize_attributes(item, path=f"{path}.attributes")
        elif key == "observed_value":
            if not isinstance(item, Mapping):
                raise ValueError(f"{path}.observed_value must be an object")
            _require_exact_fields(
                item,
                frozenset({"in_alarm", "present_value", "value"}),
                path=f"{path}.observed_value",
            )
            normalized[key] = {
                nested_key: _normalize_public_value(
                    nested_value,
                    path=f"{path}.observed_value.{nested_key}",
                    depth=2,
                    string_limit=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
                )
                for nested_key, nested_value in item.items()
            }
        else:
            normalized[key] = _normalize_optional_public_string(
                item,
                path=f"{path}.{key}",
                max_chars=255,
            )
    return normalized


def _normalize_topic_record(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "payload.projection_v1.record"
    _require_exact_fields(value, _TOPIC_RECORD_FIELDS, path=path)
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "attributes":
            normalized[key] = _normalize_attributes(item, path=f"{path}.attributes")
        elif key == "last_payload":
            if not isinstance(item, Mapping):
                raise ValueError(f"{path}.last_payload must be an object")
            _require_exact_fields(
                item,
                frozenset({"_raw_present", "_value"}),
                path=f"{path}.last_payload",
            )
            normalized[key] = {
                nested_key: _normalize_public_value(
                    nested_value,
                    path=f"{path}.last_payload.{nested_key}",
                    depth=2,
                    string_limit=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
                )
                for nested_key, nested_value in item.items()
            }
        elif key == "message_count":
            normalized[key] = _normalize_int(
                item,
                path=f"{path}.message_count",
                minimum=0,
            )
        else:
            normalized[key] = _normalize_public_string(
                item,
                path=f"{path}.{key}",
                max_chars=2_048,
            )
    return normalized


def _normalize_issue_record(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "payload.projection_v1.record"
    _require_exact_fields(value, _ISSUE_RECORD_FIELDS, path=path)
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            normalized[key] = None
            continue
        text = _normalize_public_string(
            item,
            path=f"{path}.{key}",
            max_chars=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
        )
        if key == "raw_evidence_uri" and _OPAQUE_REFERENCE.fullmatch(text) is None:
            raise ValueError(f"{path}.raw_evidence_uri must be an opaque evidence reference")
        normalized[key] = text
    return normalized


def _normalize_summary_record(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "payload.projection_v1.record"
    _require_exact_fields(value, _SUMMARY_RECORD_FIELDS, path=path)
    return {
        key: _normalize_public_value(
            item,
            path=f"{path}.{key}",
            depth=1,
            string_limit=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
        )
        for key, item in value.items()
    }


def _normalize_attributes(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    _require_exact_fields(value, _PUBLIC_ATTRIBUTE_FIELDS, path=path)
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if key in {"heard_not_enriched", "object_list_read_failed"}:
            normalized[key] = _normalize_bool(item, path=item_path)
        elif key == "point_reads_aborted":
            normalized[key] = _normalize_count_record(
                item,
                path=item_path,
                fields=frozenset(
                    {"after_consecutive_failures", "points_not_attempted"}
                ),
            )
        elif key == "point_reads_truncated":
            normalized[key] = _normalize_count_record(
                item,
                path=item_path,
                fields=frozenset({"points_not_attempted"}),
            )
        else:
            normalized[key] = _normalize_public_value(
                item,
                path=item_path,
                depth=2,
                string_limit=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
            )
    return normalized


def _normalize_count_record(
    value: Any,
    *,
    path: str,
    fields: frozenset[str],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    _require_exact_fields(value, fields, path=path)
    if set(value) != fields:
        raise ValueError(f"{path} must contain its complete public contract")
    return {
        key: _normalize_int(item, path=f"{path}.{key}", minimum=0)
        for key, item in value.items()
    }


def _require_exact_fields(
    value: Mapping[Any, Any],
    allowed: frozenset[str],
    *,
    path: str,
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} keys must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{path}.{unknown[0]} has no public v1 contract")


def _normalize_optional_public_string(
    value: Any,
    *,
    path: str,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    return _normalize_public_string(value, path=path, max_chars=max_chars)


def _normalize_public_string(value: Any, *, path: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{path} exceeds its public string limit")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError(f"{path} contains control characters")
    stripped = value.lstrip()
    lowered = stripped.casefold()
    if (
        _WINDOWS_ABSOLUTE_PATH.search(value)
        or _POSIX_LOCAL_PATH.search(value)
        or _RELATIVE_LOCAL_FILE_PATH.search(value)
        or _RELATIVE_PATH_TRAVERSAL.search(value)
        or _LOCAL_EVIDENCE_MARKER.search(value)
        or _CREDENTIAL_ASSIGNMENT.search(value)
        or stripped.startswith(("/", "\\\\"))
        or re.search(r"<\s*\?xml\b", lowered)
        or _XML_MARKUP.search(value)
    ):
        raise ValueError(f"{path} contains raw or locally scoped evidence")
    return value


def _normalize_bool(value: Any, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _normalize_int(value: Any, *, path: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if value < minimum or value > 2**63 - 1:
        raise ValueError(f"{path} is outside its public numeric range")
    return value


def _normalize_number(value: Any, *, path: str, minimum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{path} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{path} is outside its public numeric range")
    return value


def _normalize_public_value(
    value: Any,
    *,
    path: str,
    depth: int,
    string_limit: int,
) -> Any:
    if depth > PUBLIC_PAYLOAD_V1_MAX_DEPTH:
        raise ValueError(f"{path} exceeds the public payload depth limit")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < -(2**63) or value > 2**63 - 1:
            raise ValueError(f"{path} is outside its public numeric range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        return value
    if isinstance(value, str):
        return _normalize_public_string(value, path=path, max_chars=string_limit)
    if isinstance(value, bytes | bytearray | memoryview):
        raise ValueError(f"{path} cannot contain raw bytes")
    if isinstance(value, Mapping):
        raise ValueError(f"{path} has no allowlisted public object contract")
    if isinstance(value, Sequence):
        if len(value) > PUBLIC_PAYLOAD_V1_MAX_SEQUENCE_ITEMS:
            raise ValueError(f"{path} exceeds the public sequence limit")
        return [
            _normalize_public_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                string_limit=string_limit,
            )
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} has no public v1 value contract")


def observation_stream_sha256(
    observations: list[DiscoveryObservationViewV1],
) -> str:
    """Bind every committed event in cursor order with an appendable hash chain."""

    commitment = OBSERVATION_STREAM_EMPTY_SHA256
    for observation in observations:
        commitment = extend_observation_stream_sha256(commitment, observation)
    return commitment


def extend_observation_stream_sha256(
    previous_sha256: str,
    observation: DiscoveryObservationViewV1,
) -> str:
    """Extend a persisted stream commitment with one canonical observation."""

    if re.fullmatch(r"[0-9a-f]{64}", previous_sha256) is None:
        raise ValueError("previous observation stream commitment is invalid")
    item = canonical_json_bytes(observation.model_dump(mode="json"))
    digest = hashlib.sha256()
    digest.update(_OBSERVATION_STREAM_CHAIN_DOMAIN)
    digest.update(bytes.fromhex(previous_sha256))
    digest.update(len(item).to_bytes(8, "big"))
    digest.update(item)
    return digest.hexdigest()


def fold_discovery_observations(
    observations: list[DiscoveryObservationViewV1],
    *,
    terminal_cursor: int,
    expected_count: int | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
) -> DiscoveryObservationFoldV1:
    """Fold one stable cursor prefix without treating cursor as entity version."""

    ordered = sorted(observations, key=lambda item: item.cursor)
    if expected_count is not None and len(ordered) != expected_count:
        raise ValueError("observation fold does not contain the expected prefix count")
    if ordered:
        inferred_run_id = ordered[0].run_id
        inferred_attempt = ordered[0].attempt
        if any(
            item.run_id != inferred_run_id or item.attempt != inferred_attempt
            for item in ordered
        ):
            raise ValueError("observation fold must contain one run and attempt")
        cursors = [item.cursor for item in ordered]
        if len(cursors) != len(set(cursors)):
            raise ValueError("observation fold contains a duplicate cursor")
        if cursors[-1] != terminal_cursor:
            raise ValueError("observation fold does not end at the terminal cursor")
        if run_id is not None and run_id != inferred_run_id:
            raise ValueError("observation fold run does not match its cutoff")
        if attempt is not None and attempt != inferred_attempt:
            raise ValueError("observation fold attempt does not match its cutoff")
        run_id = inferred_run_id
        attempt = inferred_attempt
    else:
        if terminal_cursor != 0:
            raise ValueError("an empty observation fold must use terminal cursor zero")
        if run_id is None or attempt is None:
            raise ValueError("an empty observation fold requires its run and attempt")

    winners: dict[tuple[str, str], DiscoveryObservationViewV1] = {}
    for item in ordered:
        if item.payload_sha256 != canonical_sha256(item.payload):
            raise ValueError("observation payload digest does not match its payload")
        key = (item.entity_kind, item.entity_key)
        previous = winners.get(key)
        if previous is None or item.entity_version > previous.entity_version:
            winners[key] = item
        elif item.entity_version == previous.entity_version and item != previous:
            raise ValueError("observation fold contains conflicting entity versions")

    projected: list[tuple[str, int, str, str, dict[str, Any]]] = []
    projected_collections: set[ProjectionCollection] = set()
    for (entity_kind, entity_key), item in winners.items():
        raw_projection = item.payload.get("projection_v1")
        if raw_projection is None:
            continue
        projection = DiscoveryProjectionV1.model_validate(raw_projection)
        projected_collections.add(projection.collection)
        if not projection.present:
            continue
        if projection.record is None:  # pragma: no cover - model invariant
            raise ValueError("a present projection requires a record")
        projected.append(
            (
                projection.collection,
                projection.position,
                entity_kind,
                entity_key,
                dict(json_safe_value(projection.record)),
            )
        )
    projected.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    summary: dict[str, Any] = {}
    collections: dict[str, list[dict[str, Any]]] = {
        "issues": [],
        "devices": [],
        "points": [],
        "topics": [],
    }
    for collection, _position, _entity_kind, _entity_key, record in projected:
        if collection == "summary":
            summary.update(record)
        else:
            collections[collection].append(record)

    return DiscoveryObservationFoldV1(
        run_id=run_id,
        attempt=attempt,
        observation_count=len(ordered),
        terminal_cursor=terminal_cursor,
        observation_stream_sha256=observation_stream_sha256(ordered),
        projected_collections=frozenset(projected_collections),
        summary=summary,
        issues=tuple(collections["issues"]),
        devices=tuple(collections["devices"]),
        points=tuple(collections["points"]),
        topics=tuple(collections["topics"]),
    )


__all__ = [
    "DiscoveryObservationFoldV1",
    "DiscoveryObservationInputV1",
    "DiscoveryObservationViewV1",
    "DiscoveryProjectionV1",
    "ObservationAppendOutcomeV1",
    "ObservationCutoffV1",
    "ObservationEvidenceV1",
    "OBSERVATION_STREAM_EMPTY_SHA256",
    "extend_observation_stream_sha256",
    "fold_discovery_observations",
    "observation_payload",
    "observation_stream_sha256",
]
