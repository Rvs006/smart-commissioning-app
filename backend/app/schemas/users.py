"""Request/response schemas for the identity + RBAC endpoints (/api/v1/users, /me).

The role is the lowercase Role.value (viewer|reviewer|engineer|admin). User
responses NEVER carry the api_key_hash; the plaintext key is returned exactly
once per issuance — by the create endpoint and by the key re-issue endpoint.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from smart_commissioning_core.rbac import Role


class EffectiveScope(BaseModel):
    """One project/site pair available to a non-global named principal."""

    project_id: str
    site_id: str


class MeResponse(BaseModel):
    """The current principal, returned by GET /api/v1/me (any authenticated caller)."""

    username: str
    role: Role
    source: str  # "user_key" | "shared_key" | "local"
    global_scope: bool = False
    effective_scopes: list[EffectiveScope] = Field(default_factory=list)
    # Whether this deployment enforces the scan/write authorization ceremony.
    # False -> the UI hides the checkbox and the sealed-preview approval and
    # submits runs directly. Defaults True so older clients stay enforced.
    authorization_enforced: bool = True


class UserResponse(BaseModel):
    """A user as returned to admins (no key material, no key hash)."""

    id: str
    username: str
    role: Role
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None = None


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    role: Role


class CreateUserResponse(BaseModel):
    """A user PLUS their plaintext API key, shown exactly once per issuance.

    Returned by POST /users (create) and POST /users/{id}/key (re-issue). The
    key is displayed only in this response; it does not expire by itself.
    """

    user: UserResponse
    api_key: str


class UpdateRoleRequest(BaseModel):
    role: Role


AuditReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]


class CreateScopeGrantRequest(BaseModel):
    """Admin request to grant one existing project/site to a named user."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    reason: AuditReason


class RevokeScopeGrantRequest(BaseModel):
    """Audited reason for revoking one current grant."""

    model_config = ConfigDict(extra="forbid")

    reason: AuditReason


class ScopeGrantResponse(BaseModel):
    """Current or revoked scope grant with server-stamped audit identity."""

    grant_id: str
    user_id: str
    project_id: str
    site_id: str
    active: bool
    granted_by: str
    reason: str
    granted_at: datetime
    revoked_by: str | None = None
    revoke_reason: str | None = None
    revoked_at: datetime | None = None


class UnscopedActiveUser(BaseModel):
    """An active named non-admin blocking scope-enforcement activation."""

    id: str
    username: str
    role: Role


class ScopeActivationPreflightResponse(BaseModel):
    """Fail-closed readiness result for named-user edge/hub scope enforcement."""

    ready: bool
    active_named_admin_count: int = Field(ge=0)
    unscoped_active_non_admin_users: list[UnscopedActiveUser] = Field(
        default_factory=list
    )
