"""Versioned contracts for application-owned IP discovery providers."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smart_commissioning_core.discovery_observations import (
    PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    normalize_public_string_v1,
)

_MAC_ADDRESS = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")


class ProviderDiagnosticV1(BaseModel):
    """Bounded diagnostic text that never includes raw exception messages."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["1.0"] = "1.0"
    code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=256)
    errno: int | None = Field(default=None, ge=0, le=65535)


class TCPProviderCapabilityV1(BaseModel):
    """Typed answer for a protocol requested from the built-in provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    provider_contract_version: Literal["1.0"] = "1.0"
    provider: Literal["builtin_tcp_connect"] = "builtin_tcp_connect"
    requested_protocol: Literal["tcp", "udp"]
    supported: bool
    reason_code: Literal["supported_protocol", "unsupported_protocol"]
    reason: str = Field(min_length=1, max_length=256)
    recommended_provider: Literal["bacnet_discovery"] | None = None


class ProviderIdentityEvidenceV1(BaseModel):
    """Optional identity evidence supplied only by an approved provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["1.0"] = "1.0"
    evidence_kind: Literal[
        "protocol_identity",
        "passive_neighbor_cache",
        "operator_import",
        "approved_provider",
    ]
    confidence: Literal["low", "medium", "high"]
    asset_id: str | None = Field(
        default=None,
        max_length=PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS,
    )
    hostname: str | None = Field(default=None, max_length=253)
    mac_address: str | None = None
    corroborating_fields: tuple[
        Literal["asset_id", "hostname", "mac_address"], ...
    ] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_identity(self) -> ProviderIdentityEvidenceV1:
        for field_name in ("asset_id", "hostname"):
            value = getattr(self, field_name)
            if value is not None:
                safe = normalize_public_string_v1(
                    value,
                    path=f"provider_identity.{field_name}",
                    max_chars=(
                        PUBLIC_PAYLOAD_V1_MAX_STRING_CHARS
                        if field_name == "asset_id"
                        else 253
                    ),
                )
                object.__setattr__(self, field_name, safe.strip() or None)
        if self.mac_address is not None:
            normalized_mac = self.mac_address.strip().replace("-", ":").upper()
            if not _MAC_ADDRESS.fullmatch(normalized_mac):
                raise ValueError("mac_address must contain six hexadecimal octets")
            object.__setattr__(self, "mac_address", normalized_mac)

        present = {
            field_name
            for field_name in ("asset_id", "hostname", "mac_address")
            if getattr(self, field_name) is not None
        }
        if not present:
            raise ValueError("identity evidence requires at least one identity value")
        if not set(self.corroborating_fields).issubset(present):
            raise ValueError("corroborating_fields must name populated identity values")
        return self


class TCPProbeRequestV1(BaseModel):
    """One literal-IP transport probe bounded by one application deadline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_contract_version: Literal["1.0"] = "1.0"
    target_ip: str
    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    source_ip: str | None = None
    application_deadline_s: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("target_ip", "source_ip")
    @classmethod
    def require_numeric_ipv4(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = ipaddress.ip_address(value.strip())
        except (AttributeError, ValueError) as error:
            raise ValueError("TCP probes require a numeric IPv4 address") from error
        if not isinstance(parsed, ipaddress.IPv4Address):
            raise ValueError("TCP probes require a numeric IPv4 address")
        return str(parsed)


class TCPPortObservationV1(BaseModel):
    """Normalized evidence returned for one transport-aware port probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    provider_contract_version: Literal["1.0"] = "1.0"
    provider: Literal["builtin_tcp_connect"] = "builtin_tcp_connect"
    target_ip: str
    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    outcome: Literal[
        "connected",
        "connection_refused",
        "timed_out",
        "network_unreachable",
        "host_unreachable",
        "permission_denied",
        "cancelled",
        "unsupported",
        "provider_error",
    ]
    diagnostic: ProviderDiagnosticV1
    capability: TCPProviderCapabilityV1
    cleanup_diagnostic: ProviderDiagnosticV1 | None = None
    identity_evidence: None = None
    port_hint: str | None = None
    detected_service: None = None
    detected_version: None = None
