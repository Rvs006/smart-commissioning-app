"""Frozen execution-context and protocol identity contracts for lifecycle v2."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_SECRET_REFERENCE_PREFIX = "secret://"
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "mqtt_password",
        "key_password",
        "broker_password",
        "passphrase",
        "key_passphrase",
        "token",
        "access_token",
        "api_token",
        "refresh_token",
        "private_key",
        "client_key",
        "tls_key",
        "secret_key",
        "client_certificate",
        "client_cert",
        "ca_certificate",
        "ca_cert",
        "certificate",
        "credentials",
        "secret",
    }
)


def _unicode_safe_text(value: object) -> str:
    return _LONE_SURROGATE_RE.sub(
        lambda match: f"\\u{ord(match.group(0)):04X}",
        str(value),
    )


def json_safe_value(value: Any) -> Any:
    """Normalize hostile JSON-compatible evidence without losing its meaning."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, float) and not math.isfinite(value):
        return f"{value} (non-standard JSON number)"
    if isinstance(value, Mapping):
        return {
            _unicode_safe_text(key): json_safe_value(child)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_safe_value(child) for child in value]
    if isinstance(value, str):
        return _unicode_safe_text(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable UTF-8 JSON representation used by all v2 digests."""
    return json.dumps(
        json_safe_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class ContextResourceV1(BaseModel):
    """An immutable import/register binding captured by identifier and digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(min_length=1, max_length=255)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("sha256 must be a 64-character lowercase hex digest")
        return normalized


class SecretReferenceV1(BaseModel):
    """A versioned opaque reference. Decrypted material is never persisted here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str = Field(min_length=10, max_length=1024)
    version: str = Field(min_length=1, max_length=128)

    @field_validator("reference")
    @classmethod
    def _opaque_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith(_SECRET_REFERENCE_PREFIX):
            raise ValueError("secret references must use the secret:// scheme")
        name = normalized.removeprefix(_SECRET_REFERENCE_PREFIX)
        if not name or ".." in name or "\\" in name:
            raise ValueError("invalid secret reference")
        return normalized


def _reject_inline_secrets(value: Any, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_").replace(" ", "_")
            if key in _SENSITIVE_KEYS and child not in (None, ""):
                if not isinstance(child, str) or not child.startswith(
                    _SECRET_REFERENCE_PREFIX
                ):
                    location = ".".join((*path, str(raw_key)))
                    raise ValueError(f"{location} must contain a secret:// reference")
            _reject_inline_secrets(child, path=(*path, str(raw_key)))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, path=(*path, str(index)))


class RunContextV1(BaseModel):
    """Exact effective inputs resolved before dispatch.

    The record is intentionally self-contained except for versioned
    ``secret://`` references. Workers must load this stored model and must not
    consult current configuration, demo scope, or message parameters.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    configuration_snapshot: dict[str, Any]
    configuration_version: int | str
    registers: tuple[ContextResourceV1, ...] = ()
    imports: tuple[ContextResourceV1, ...] = ()
    schema_versions: dict[str, str] = Field(default_factory=dict)
    engine_parameters: dict[str, Any] = Field(default_factory=dict)
    network_interface: str | None = None
    connection_settings: dict[str, Any] = Field(default_factory=dict)
    secret_references: dict[str, SecretReferenceV1] = Field(default_factory=dict)
    requesting_principal: str = Field(min_length=1, max_length=255)
    application_version: str = Field(min_length=1, max_length=64)
    protocol_key: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _contains_only_references(self) -> RunContextV1:
        _reject_inline_secrets(self.configuration_snapshot, path=("configuration_snapshot",))
        _reject_inline_secrets(self.engine_parameters, path=("engine_parameters",))
        _reject_inline_secrets(self.connection_settings, path=("connection_settings",))
        if isinstance(self.configuration_version, int) and self.configuration_version < 1:
            raise ValueError("configuration_version must be positive")
        if isinstance(self.configuration_version, str) and not self.configuration_version.strip():
            raise ValueError("configuration_version must not be blank")
        return self

    def sha256(self) -> str:
        return canonical_context_sha256(self)


def canonical_context_sha256(context: RunContextV1 | Mapping[str, Any]) -> str:
    normalized = (
        context
        if isinstance(context, RunContextV1)
        else RunContextV1.model_validate(context)
    )
    return canonical_sha256(normalized)


def _protocol_digest(protocol: str, values: Mapping[str, Any]) -> str:
    return f"{protocol}:{canonical_sha256({'protocol': protocol, **dict(values)})}"


def canonical_mqtt_protocol_key(
    *,
    host: str,
    port: int,
    tls: bool,
    source: str,
    client_certificate_reference: str | None = None,
) -> str:
    """Hash the connection profile without exposing broker or credential data."""
    return _protocol_digest(
        "mqtt",
        {
            "host": host.strip().casefold(),
            "port": int(port),
            "tls": bool(tls),
            "source": source.strip().casefold(),
            "client_certificate_reference": (
                client_certificate_reference.strip()
                if client_certificate_reference is not None
                else None
            ),
        },
    )


def canonical_bacnet_protocol_key(*, bind_address: str, port: int) -> str:
    raw = bind_address.strip()
    try:
        normalized = str(ipaddress.ip_interface(raw))
    except ValueError:
        normalized = raw.casefold()
    return _protocol_digest("bacnet", {"bind_address": normalized, "port": int(port)})


def mqtt_client_id(*, deployment: str, run_id: str, attempt: int, channel: str) -> str:
    """Return a collision-resistant MQTT 3.1-compatible identifier (23 bytes)."""
    material = "\x00".join((deployment, run_id, str(attempt), channel)).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    suffix = base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:20]
    return f"sc-{suffix}"
