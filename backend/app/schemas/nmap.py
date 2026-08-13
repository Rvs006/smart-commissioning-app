"""Strict public contracts for deployment-scoped Nmap authority.

Local installation paths and Windows trust internals deliberately have no API
field. They exist only in the protected append-only confirmation record.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from smart_commissioning_core.engines.ip.nmap_profiles import NmapProfileName
from smart_commissioning_core.engines.ip.nmap_trust import NmapReviewedScriptV1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NmapDeploymentLane(StrEnum):
    INTERNAL_SAME_ORGANIZATION = "internal_same_organization"
    EXTERNAL_CUSTOMER = "external_customer"


class NmapProviderMode(StrEnum):
    DISABLED = "disabled"
    OPERATOR_XML_IMPORT = "operator_xml_import"
    INTERNAL_OPERATOR_MANAGED = "internal_operator_managed"


class NmapCapabilityState(StrEnum):
    DISABLED = "disabled"
    XML_IMPORT_ONLY = "xml_import_only"
    CONFIRMATION_REQUIRED = "confirmation_required"
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


class NmapNpcapState(StrEnum):
    NOT_CHECKED = "not_checked"
    MISSING = "missing"
    ADMIN_ONLY = "admin_only"
    CONNECT_ONLY = "connect_only"
    RAW_CAPABLE = "raw_capable"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _canonical_text_tuple(
    value: tuple[str, ...],
    *,
    label: str,
    max_items: int,
    max_length: int,
    casefold: bool = False,
) -> tuple[str, ...]:
    if len(value) > max_items:
        raise ValueError(f"{label} contains too many values")
    if any(not item or len(item) > max_length or _has_control(item) for item in value):
        raise ValueError(f"{label} contains an invalid value")
    key = (lambda item: item.casefold()) if casefold else (lambda item: item)
    if len({key(item) for item in value}) != len(value):
        raise ValueError(f"{label} must not contain duplicate values")
    if tuple(sorted(value, key=key)) != value:
        raise ValueError(f"{label} must be canonically sorted")
    return value


class NmapProjectSiteScope(_StrictModel):
    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)

    @field_validator("project_id", "site_id")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if _has_control(value):
            raise ValueError("project and site identifiers cannot contain control characters")
        return value


class NmapProfilePolicyV1(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    permitted_profiles: tuple[NmapProfileName, ...] = Field(default=(), max_length=8)

    @field_validator("permitted_profiles")
    @classmethod
    def _canonical_profiles(cls, value: tuple[NmapProfileName, ...]) -> tuple[NmapProfileName, ...]:
        if len(set(value)) != len(value) or tuple(sorted(value, key=lambda item: item.value)) != value:
            raise ValueError("permitted_profiles must be unique and canonically sorted")
        return value


class NmapDeploymentPolicyCreateRequest(_StrictModel):
    deployment_lane: NmapDeploymentLane
    provider_mode: NmapProviderMode
    deployment_owner: str = Field(min_length=1, max_length=255)
    operator_install_responsibility: str = Field(min_length=1, max_length=4096)
    permitted_project_sites: tuple[NmapProjectSiteScope, ...] = Field(min_length=1, max_length=256)
    update_owner: str = Field(min_length=1, max_length=255)
    reviewed_version_policy: str = Field(min_length=1, max_length=4096)
    permitted_publishers: tuple[str, ...] = Field(default=(), max_length=8)
    permitted_versions: tuple[str, ...] = Field(default=(), max_length=32)
    permitted_signer_sha256: tuple[str, ...] = Field(default=(), max_length=32)
    permitted_executable_sha256: tuple[str, ...] = Field(default=(), max_length=64)
    permitted_data_manifest_sha256: tuple[str, ...] = Field(default=(), max_length=64)
    permitted_licence_sha256: tuple[str, ...] = Field(default=(), max_length=64)
    permitted_npsl_versions: tuple[str, ...] = Field(default=(), max_length=16)
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = Field(default=(), max_length=64)
    max_data_files: int = Field(default=8192, ge=1, le=65_536)
    max_file_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1024,
        le=512 * 1024 * 1024,
    )
    max_manifest_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024 * 1024,
    )
    profile_policy: NmapProfilePolicyV1 = Field(default_factory=NmapProfilePolicyV1)
    acknowledged_no_redistribution: bool = False
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator(
        "deployment_owner", "update_owner", "operator_install_responsibility", "reviewed_version_policy", "reason"
    )
    @classmethod
    def _safe_text(cls, value: str) -> str:
        if _has_control(value):
            raise ValueError("Nmap policy text cannot contain control characters")
        return value

    @field_validator("permitted_project_sites")
    @classmethod
    def _canonical_scopes(cls, value: tuple[NmapProjectSiteScope, ...]) -> tuple[NmapProjectSiteScope, ...]:
        pairs = tuple((item.project_id, item.site_id) for item in value)
        if len(set(pairs)) != len(pairs) or tuple(sorted(pairs)) != pairs:
            raise ValueError("permitted_project_sites must be unique and canonically sorted")
        return value

    @field_validator("permitted_publishers")
    @classmethod
    def _publishers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_text_tuple(
            value,
            label="permitted_publishers",
            max_items=8,
            max_length=512,
            casefold=True,
        )

    @field_validator("permitted_versions")
    @classmethod
    def _versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_text_tuple(
            value,
            label="permitted_versions",
            max_items=32,
            max_length=128,
        )

    @field_validator("permitted_npsl_versions")
    @classmethod
    def _npsl_versions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = _canonical_text_tuple(
            value,
            label="permitted_npsl_versions",
            max_items=16,
            max_length=32,
        )
        if any(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", item) is None for item in canonical):
            raise ValueError("permitted_npsl_versions contains an invalid version")
        return canonical

    @field_validator(
        "permitted_signer_sha256",
        "permitted_executable_sha256",
        "permitted_data_manifest_sha256",
        "permitted_licence_sha256",
    )
    @classmethod
    def _digests(cls, value: tuple[str, ...], info) -> tuple[str, ...]:  # noqa: ANN001
        if len(set(value)) != len(value) or tuple(sorted(value)) != value:
            raise ValueError(f"{info.field_name} must be unique and canonically sorted")
        if any(_SHA256.fullmatch(item) is None for item in value):
            raise ValueError(f"{info.field_name} must contain lowercase SHA-256 values")
        return value

    @field_validator("reviewed_scripts")
    @classmethod
    def _reviewed_scripts(cls, value: tuple[NmapReviewedScriptV1, ...]) -> tuple[NmapReviewedScriptV1, ...]:
        if (
            len({item.name for item in value}) != len(value)
            or tuple(sorted(value, key=lambda item: item.name)) != value
        ):
            raise ValueError("reviewed_scripts must be unique and canonically sorted")
        return value

    @model_validator(mode="after")
    def _lane_and_policy_consistency(self) -> Self:
        if self.max_manifest_bytes < self.max_file_bytes:
            raise ValueError("max_manifest_bytes cannot be less than max_file_bytes")
        if self.deployment_lane is NmapDeploymentLane.EXTERNAL_CUSTOMER:
            if self.provider_mode is not NmapProviderMode.DISABLED:
                raise ValueError("external deployments must keep Nmap disabled")
        if self.provider_mode is not NmapProviderMode.DISABLED and not self.acknowledged_no_redistribution:
            raise ValueError("enabled Nmap modes require the no-redistribution acknowledgement")
        if self.provider_mode is NmapProviderMode.INTERNAL_OPERATOR_MANAGED:
            required = (
                self.permitted_publishers,
                self.permitted_versions,
                self.permitted_signer_sha256,
                self.permitted_executable_sha256,
                self.permitted_data_manifest_sha256,
                self.permitted_licence_sha256,
                self.permitted_npsl_versions,
                self.profile_policy.permitted_profiles,
            )
            if any(not values for values in required):
                raise ValueError("process execution requires exact trust and profile allowlists")
        return self


class NmapDeploymentPolicyResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str
    deployment_id: str
    network_executor_id: str
    revision: int
    deployment_lane: NmapDeploymentLane
    provider_mode: NmapProviderMode
    organization_internal: bool
    deployment_owner: str
    operator_install_responsibility: str
    permitted_project_sites: tuple[NmapProjectSiteScope, ...]
    update_owner: str
    reviewed_version_policy: str
    permitted_publishers: tuple[str, ...]
    permitted_versions: tuple[str, ...]
    permitted_signer_sha256: tuple[str, ...]
    permitted_executable_sha256: tuple[str, ...]
    permitted_data_manifest_sha256: tuple[str, ...]
    permitted_licence_sha256: tuple[str, ...]
    permitted_npsl_versions: tuple[str, ...]
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...]
    max_data_files: int
    max_file_bytes: int
    max_manifest_bytes: int
    profile_policy: NmapProfilePolicyV1
    reviewed_at: datetime
    acknowledged_no_redistribution: bool
    created_by: str
    reason: str
    created_at: datetime
    supersedes_policy_id: str | None = None


class NmapInstallationConfirmationRequest(_StrictModel):
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("reason")
    @classmethod
    def _safe_reason(cls, value: str) -> str:
        if _has_control(value):
            raise ValueError("confirmation reason cannot contain control characters")
        return value


class NmapInstallationConfirmationResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    confirmation_id: str
    policy_id: str
    deployment_id: str
    network_executor_id: str
    policy_revision: int
    provider: Literal["nmap"] = "nmap"
    state: NmapCapabilityState
    reason: str
    publisher: str
    version: str
    fingerprint_sha256: str
    signer_sha256: str
    executable_sha256: str
    data_manifest_sha256: str
    data_file_count: int
    data_total_bytes: int
    licence_sha256: str
    npsl_version: str
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...]
    npcap_version: str | None = None
    npcap_state: NmapNpcapState
    raw_capable: bool
    confirmed_by: str
    confirmed_at: datetime


class NmapDetectedInstallationResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    provider: Literal["nmap"] = "nmap"
    state: NmapCapabilityState
    reason: str
    publisher: str | None = None
    version: str | None = None
    registry_view: Literal["32", "64"] | None = None
    display_name: str | None = None
    fingerprint_sha256: str | None = None
    signer_sha256: str | None = None
    executable_sha256: str | None = None
    data_manifest_sha256: str | None = None
    data_file_count: int | None = None
    data_total_bytes: int | None = None
    licence_sha256: str | None = None
    npsl_version: str | None = None
    reviewed_scripts: tuple[NmapReviewedScriptV1, ...] = ()
    npcap_version: str | None = None
    npcap_state: NmapNpcapState = NmapNpcapState.NOT_CHECKED
    raw_capable: bool = False


class NmapCapabilityResponse(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    provider: Literal["nmap"] = "nmap"
    state: NmapCapabilityState
    reason: str
    provider_mode: NmapProviderMode = NmapProviderMode.DISABLED
    policy_id: str | None = None
    policy_revision: int | None = None
    publisher: str | None = None
    version: str | None = None
    fingerprint_sha256: str | None = None
    npcap_version: str | None = None
    npcap_state: NmapNpcapState = NmapNpcapState.NOT_CHECKED
    raw_capable: bool = False
    process_selection_allowed: bool = False
    xml_import_allowed: bool = False
    permitted_profiles: tuple[NmapProfileName, ...] = ()
