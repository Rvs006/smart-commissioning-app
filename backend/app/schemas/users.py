"""Request/response schemas for the identity + RBAC endpoints (/api/v1/users, /me).

The role is the lowercase Role.value (viewer|reviewer|engineer|admin). User
responses NEVER carry the api_key_hash; the one-time plaintext key is returned
only by the create endpoint, exactly once.
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
    """The created user PLUS the one-time plaintext API key (shown exactly once)."""

    user: UserResponse
    api_key: str


class UpdateRoleRequest(BaseModel):
    role: Role
