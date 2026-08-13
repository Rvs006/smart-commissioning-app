"""Versioned, viewer-safe progressive discovery observation contracts."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from smart_commissioning_core.run_context import (
    canonical_json_bytes,
    canonical_sha256,
    json_safe_value,
)
from smart_commissioning_core.scan_contract import MAX_PROTOCOL_PORTS

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
IPCoverageStateV1 = Literal["pending", "attempted", "not_attempted", "cancelled"]
IPReachabilityStateV1 = Literal[
    "pending",
    "reachable",
    "unconfirmed",
    "not_applicable",
]
IPProbeOutcomeV1 = Literal[
    "connected",
    "connection_refused",
    "timed_out",
    "network_unreachable",
    "host_unreachable",
    "permission_denied",
    "cancelled",
    "provider_error",
]
IPRegisterMatchV1 = Literal[
    "not_configured",
    "expected_match",
    "wrong_ip_review",
    "ambiguous_review",
    "unregistered",
]
IPPolicyVerdictV1 = Literal[
    "pending",
    "pass",
    "forbidden_open",
    "unexpected_open_review",
    "expected_closed",
    "unconfirmed",
    "not_attempted",
    "not_applicable",
]
IPTransportV1 = Literal["tcp", "udp"]
IPControlReasonV1 = Literal[
    "stop_requested",
    "authorization_expired",
    "authorization_revoked",
    "grant_revoked",
    "ownership_lost",
    "control_store_error",
    "dispatch_deadline_elapsed",
]
IPCapabilityActionV1 = Literal["use_bacnet_discovery"]

PUBLIC_PAYLOAD_V1_MAX_DEPTH = 8
PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS = 4_096
PUBLIC_PAYLOAD_V1_MAX_VALUE_STRING_CHARS = 60_000
PUBLIC_PAYLOAD_V1_MAX_SEQUENCE_ITEMS = 512
OBSERVATION_PAYLOAD_MAX_CANONICAL_BYTES = 65_536

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
        "ip_v1",
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


class IPObservationProvenanceV1(BaseModel):
    """Immutable public references that explain how one IP fact was produced."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    profile: Literal["gentle", "planned_extended", "operator_xml_import"]
    source_ip: str | None = None
    source_interface: str | None = None
    packet_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    register_import_id: str | None = Field(default=None, max_length=255)
    register_rows_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("source_ip")
    @classmethod
    def _numeric_ipv4_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_ipv4(value, path="payload.ip_v1.provenance.source_ip")

    @field_validator("source_interface")
    @classmethod
    def _viewer_safe_interface(cls, value: str | None) -> str | None:
        return _normalize_optional_public_string(
            value,
            path="payload.ip_v1.provenance.source_interface",
            max_chars=255,
        )

    @field_validator("register_import_id")
    @classmethod
    def _opaque_register_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_public_string(
            value,
            path="payload.ip_v1.provenance.register_import_id",
            max_chars=255,
        ).strip()
        if _OPAQUE_OBSERVATION_IDENTITY.fullmatch(normalized) is None:
            raise ValueError("register_import_id must use opaque normalized syntax")
        return normalized

    @model_validator(mode="after")
    def _complete_register_reference(self) -> IPObservationProvenanceV1:
        has_import = self.register_import_id is not None
        has_digest = self.register_rows_sha256 is not None
        if has_import != has_digest:
            raise ValueError(
                "register_import_id and register_rows_sha256 must be supplied together"
            )
        return self


class IPObservationPayloadV1(BaseModel):
    """Strict, viewer-safe evidence for one staged IP discovery observation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    schema_version: Literal["1.0"] = "1.0"
    coverage_state: IPCoverageStateV1
    reachability_state: IPReachabilityStateV1
    probe_outcome: IPProbeOutcomeV1 | None
    register_match: IPRegisterMatchV1
    policy_verdict: IPPolicyVerdictV1
    target: str
    port: int | None = Field(default=None, ge=1, le=65_535)
    transport: IPTransportV1 | None = None
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    provider_version: str = Field(min_length=1, max_length=64)
    provider_contract_version: Literal["1.0"] = "1.0"
    provenance: IPObservationProvenanceV1
    reason: str | None = Field(default=None, max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS)
    attempts: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    port_hint: str | None = Field(default=None, max_length=255)
    detected_service: str | None = Field(default=None, max_length=255)
    detected_version: str | None = Field(default=None, max_length=255)
    capability_action: IPCapabilityActionV1 | None = None
    control_reason: IPControlReasonV1 | None = None
    last_packet_dispatched_at: datetime | None = None

    @field_validator("target")
    @classmethod
    def _numeric_ipv4_target(cls, value: str) -> str:
        return _normalize_ipv4(value, path="payload.ip_v1.target")

    @field_validator("port", mode="before")
    @classmethod
    def _strict_port(cls, value: Any) -> int | None:
        if value is None:
            return None
        return _normalize_int(value, path="payload.ip_v1.port", minimum=1)

    @field_validator("attempts", mode="before")
    @classmethod
    def _strict_attempts(cls, value: Any) -> int:
        return _normalize_int(value, path="payload.ip_v1.attempts", minimum=0)

    @field_validator("elapsed_ms", mode="before")
    @classmethod
    def _strict_elapsed_ms(cls, value: Any) -> int | float:
        return _normalize_number(value, path="payload.ip_v1.elapsed_ms", minimum=0)

    @field_validator(
        "provider_version",
        "reason",
        "port_hint",
        "detected_service",
        "detected_version",
    )
    @classmethod
    def _viewer_safe_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _normalize_optional_public_string(
            value,
            path=f"payload.ip_v1.{info.field_name}",
            max_chars=(
                PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS
                if info.field_name == "reason"
                else 255
            ),
        )

    @field_validator("last_packet_dispatched_at")
    @classmethod
    def _aware_last_packet_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("last_packet_dispatched_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _evidence_strength_matches_reachability(self) -> IPObservationPayloadV1:
        has_register = self.provenance.register_import_id is not None
        if (self.register_match == "not_configured" and has_register) or (
            self.register_match != "not_configured" and not has_register
        ):
            raise ValueError(
                "register_match must agree with frozen register provenance"
            )
        if self.provider == "builtin_tcp_connect" and (
            self.detected_service is not None or self.detected_version is not None
        ):
            raise ValueError(
                "the built-in TCP provider cannot claim detected service evidence"
            )
        if self.detected_version is not None and self.detected_service is None:
            raise ValueError("detected_version requires detected_service")
        if self.capability_action is not None and not (
            self.capability_action == "use_bacnet_discovery"
            and self.coverage_state == "not_attempted"
            and self.probe_outcome is None
            and self.attempts == 0
            and self.port == 47808
            and self.transport == "udp"
        ):
            raise ValueError(
                "BACnet capability action requires an unattempted UDP/47808 port"
            )
        if self.provider == "builtin_tcp_connect" and self.transport == "udp":
            if (
                self.coverage_state != "not_attempted"
                or self.probe_outcome is not None
                or self.attempts != 0
            ):
                raise ValueError(
                    "the built-in TCP provider cannot emit attempted UDP evidence"
                )
            if self.port == 47808 and self.capability_action != "use_bacnet_discovery":
                raise ValueError(
                    "built-in UDP/47808 omissions require use_bacnet_discovery"
                )
        if self.probe_outcome == "connected":
            if self.reachability_state != "reachable":
                raise ValueError("positive TCP evidence requires reachable state")
        elif self.probe_outcome is not None and self.reachability_state != "unconfirmed":
            raise ValueError("probe outcome requires unconfirmed reachability")
        if self.port is not None:
            if self.coverage_state == "not_attempted" and (
                self.probe_outcome is not None
                or self.attempts != 0
                or self.policy_verdict != "not_attempted"
                or self.detected_service is not None
                or self.detected_version is not None
            ):
                raise ValueError(
                    "not_attempted coverage cannot carry probe or policy evidence"
                )
            if (
                self.policy_verdict
                in {"forbidden_open", "unexpected_open_review"}
                and self.probe_outcome != "connected"
            ):
                raise ValueError("an open-port verdict requires connected evidence")
            if (
                self.policy_verdict == "expected_closed"
                and self.probe_outcome != "connection_refused"
            ):
                raise ValueError("expected_closed requires refusal evidence")
        return self

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
_IP_PORT_SEQUENCE_ATTRIBUTE_FIELDS = frozenset(
    {
        "open_ports",
        "scanned_ports",
        "expected_ports",
        "forbidden_ports",
        "forbidden_open_ports",
        "unexpected_open_ports",
        "missing_expected_ports",
        "register_ports_not_probed",
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

    @model_validator(mode="after")
    def _stable_ip_projection(self) -> DiscoveryObservationInputV1:
        raw_ip = self.payload.get("ip_v1")
        if raw_ip is None:
            return self
        if self.protocol != "ip":
            raise ValueError("payload.ip_v1 requires protocol ip")

        ip_payload = IPObservationPayloadV1.model_validate(raw_ip)
        projection = self.payload.get("projection_v1")
        if self.entity_kind == "host":
            if self.entity_key != f"host:{ip_payload.target}":
                raise ValueError("IP host entity_key must be stable for its target")
            if projection is None:
                return self
            normalized_projection = DiscoveryProjectionV1.model_validate(projection)
            if normalized_projection.collection != "devices":
                raise ValueError("an IP host can project only to devices")
            if normalized_projection.present:
                record = normalized_projection.record
                if record is None or record.get("address") != ip_payload.target:
                    raise ValueError(
                        "an IP device projection address must match its target"
                    )
            return self

        if self.entity_kind == "port":
            if ip_payload.port is None or ip_payload.transport is None:
                raise ValueError("an IP port entity requires port and transport")
            expected_key = (
                f"port:{ip_payload.target}:{ip_payload.port}:{ip_payload.transport}"
            )
            if self.entity_key != expected_key:
                raise ValueError(
                    "IP port entity_key must preserve target, port, and transport"
                )

        if projection is not None:
            raise ValueError(
                "IP port and diagnostic entities cannot carry a terminal projection"
            )
        return self


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
    if len(encoded) > OBSERVATION_PAYLOAD_MAX_CANONICAL_BYTES:
        raise ValueError(
            "discovery observation payload exceeds the 65,536-byte canonical "
            "UTF-8 byte limit"
        )
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
        if key == "ip_v1":
            normalized[key] = IPObservationPayloadV1.model_validate(item).model_dump(
                mode="json"
            )
        elif key == "projection_v1":
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
        elif key in _IP_PORT_SEQUENCE_ATTRIBUTE_FIELDS:
            normalized[key] = _normalize_ip_port_sequence(item, path=item_path)
        else:
            normalized[key] = _normalize_public_value(
                item,
                path=item_path,
                depth=2,
                string_limit=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
            )
    return normalized


def _normalize_ip_port_sequence(value: Any, *, path: str) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, str | bytes | bytearray | memoryview) or not isinstance(
        value, Sequence
    ):
        raise ValueError(f"{path} must be a sequence of numeric ports")
    if len(value) > MAX_PROTOCOL_PORTS:
        raise ValueError(
            f"{path} exceeds the IP protocol-port sequence limit {MAX_PROTOCOL_PORTS}"
        )
    normalized = [
        _normalize_int(item, path=f"{path}[{index}]", minimum=1)
        for index, item in enumerate(value)
    ]
    if any(port > 65_535 for port in normalized):
        raise ValueError(f"{path} contains a port outside 1..65535")
    if normalized != sorted(set(normalized)):
        raise ValueError(f"{path} ports must be unique and numerically sorted")
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


def normalize_public_string_v1(value: Any, *, path: str, max_chars: int) -> str:
    """Validate one viewer-visible string with the public observation rules."""

    return _normalize_public_string(value, path=path, max_chars=max_chars)


def _normalize_ipv4(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a numeric IPv4 address")
    try:
        parsed = ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise ValueError(f"{path} must be a numeric IPv4 address") from error
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError(f"{path} must be a numeric IPv4 address")
    return str(parsed)


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
    "IPControlReasonV1",
    "IPCoverageStateV1",
    "IPObservationPayloadV1",
    "IPObservationProvenanceV1",
    "IPPolicyVerdictV1",
    "IPProbeOutcomeV1",
    "IPReachabilityStateV1",
    "IPRegisterMatchV1",
    "IPTransportV1",
    "ObservationAppendOutcomeV1",
    "ObservationCutoffV1",
    "ObservationEvidenceV1",
    "OBSERVATION_STREAM_EMPTY_SHA256",
    "extend_observation_stream_sha256",
    "fold_discovery_observations",
    "normalize_public_string_v1",
    "observation_payload",
    "observation_stream_sha256",
]
