"""Request/response schemas for the identity + RBAC endpoints (/api/v1/users, /me).

The role is the lowercase Role.value (viewer|reviewer|engineer|admin). User
responses NEVER carry the api_key_hash; the plaintext key is returned exactly
once per issuance — by the create endpoint and by the key re-issue endpoint.
"""

from datetime import datetime

from pydantic import BaseModel, Field
from smart_commissioning_core.rbac import Role


class MeResponse(BaseModel):
    """The current principal, returned by GET /api/v1/me (any authenticated caller)."""

    username: str
    role: Role
    source: str  # "user_key" | "shared_key" | "local"


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
