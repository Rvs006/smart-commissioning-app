"""Public lifecycle-v2 value objects shared by API, inline, and worker paths."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from smart_commissioning_core.run_context import RunContextV1, canonical_sha256


class DispatchEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    dispatch_id: str
    context_sha256: str


class StoredRunContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    context: RunContextV1
    context_sha256: str
    created_at: datetime


class RunDispatchV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dispatch_id: str
    run_id: str
    state: Literal["pending", "published"]
    publish_attempts: int
    created_at: datetime
    published_at: datetime | None = None


class RunLeaseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    dispatch_id: str
    owner_token: str
    attempt: int
    claimed_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
    context_sha256: str


class TerminalResultV1(BaseModel):
    """The complete terminal snapshot sealed by one database transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["succeeded", "failed", "cancelled"]
    stage: str = Field(min_length=1, max_length=128)
    summary: dict[str, Any] = Field(default_factory=dict)
    issues: tuple[dict[str, Any], ...] = ()
    devices: tuple[dict[str, Any], ...] = ()
    points: tuple[dict[str, Any], ...] = ()
    topics: tuple[dict[str, Any], ...] = ()
    error_message: str | None = None

    def sha256(self) -> str:
        return canonical_sha256(self)


class FinalizeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool = False
    idempotent: bool = False
    conflict: bool = False
    reason: str | None = None
    result_sha256: str | None = None


class CancelOutcomeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    state: Literal["cancelled", "cancellation_requested", "terminal"]
    changed: bool


class RunSealViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    terminal_status: Literal["succeeded", "failed", "cancelled"]
    context_sha256: str
    result_sha256: str
    sealed_at: datetime


class ScanAuthorizationV1(BaseModel):
    """Viewer-safe immutable approval state for one sealed preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: str
    preview_run_id: str
    project_id: str
    site_id: str
    packet_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str
    ticket: str
    purpose: str
    not_before: datetime
    not_after: datetime
    max_uses: int = Field(ge=1, le=1)
    use_count: int = Field(ge=0)
    consumed_run_id: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str | None = None
    created_at: datetime
