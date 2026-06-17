"""Identity + RBAC endpoints (mounted under the protected /api/v1 router).

Routes:
  * GET  /api/v1/me                       — the current principal (any caller).
  * POST /api/v1/users                    — create a user (admin only); returns
                                            the user + the ONE-TIME plaintext key.
  * GET  /api/v1/users                    — list users (admin only); no key hashes.
  * POST /api/v1/users/{id}/deactivate    — soft-disable a user (admin only).
  * POST /api/v1/users/{id}/role          — change a user's role (admin only).

All routes already sit behind require_auth (the parent protected router). The
admin-only routes additionally depend on require_role(Role.ADMIN), which 403s a
non-admin principal. Bootstrap: until the first user exists, the shared/local
principal is ADMIN, so an operator can create the first named user.

The raw per-user key is generated here, returned exactly ONCE by the create
endpoint, and never stored — only its SHA-256 hash lands in the users table.
"""

from __future__ import annotations

import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from smart_commissioning_core.db.repositories import LastAdminError, UserRepository
from smart_commissioning_core.rbac import Role
from sqlalchemy.exc import IntegrityError

from app.core.auth import AuthPrincipal, get_principal, hash_api_key, require_role
from app.core.db import get_engine
from app.schemas.users import (
    CreateUserRequest,
    CreateUserResponse,
    MeResponse,
    UpdateRoleRequest,
    UserResponse,
)

router = APIRouter()

# Bytes of entropy for a generated per-user key (token_urlsafe -> ~43 chars).
_KEY_ENTROPY_BYTES = 32


def _repository() -> UserRepository:
    return UserRepository(get_engine())


@router.get("/me", response_model=MeResponse)
def get_me(principal: AuthPrincipal = Depends(get_principal)) -> MeResponse:
    """Return the current principal's identity. Available to any authenticated caller."""
    return MeResponse(
        username=principal.username,
        role=principal.role,
        source=principal.source,
    )


@router.post(
    "/users",
    response_model=CreateUserResponse,
    status_code=201,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
def create_user(payload: CreateUserRequest) -> CreateUserResponse:
    """Create a user and return it WITH a one-time plaintext API key (admin only).

    The key is generated server-side, hashed (SHA-256) for storage, and returned
    in this response exactly once — it cannot be retrieved again. A duplicate
    username is a 409.
    """
    raw_key = secrets.token_urlsafe(_KEY_ENTROPY_BYTES)
    try:
        created = _repository().create_user(
            user_id=str(uuid4()),
            username=payload.username,
            role=payload.role.value,
            api_key_hash=hash_api_key(raw_key),
        )
    except IntegrityError as error:
        raise HTTPException(
            status_code=409, detail=f"A user named {payload.username!r} already exists."
        ) from error
    return CreateUserResponse(user=UserResponse(**created), api_key=raw_key)


@router.get(
    "/users",
    response_model=list[UserResponse],
    dependencies=[Depends(require_role(Role.ADMIN))],
)
def list_users() -> list[UserResponse]:
    """List all users (admin only). Never includes key hashes or key material."""
    return [UserResponse(**user) for user in _repository().list_users()]


@router.post(
    "/users/{user_id}/deactivate",
    response_model=UserResponse,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
def deactivate_user(user_id: str) -> UserResponse:
    """Deactivate a user (admin only). Their key then fails authentication (401).

    Refuses to deactivate the last active admin user (409): the system must
    always retain at least one active admin USER ROW. (The synthetic shared-key
    bootstrap admin is a separate recovery path and is not counted here.)
    """
    try:
        updated = _repository().set_active(user_id, is_active=False)
    except LastAdminError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return UserResponse(**updated)


@router.post(
    "/users/{user_id}/role",
    response_model=UserResponse,
    dependencies=[Depends(require_role(Role.ADMIN))],
)
def update_user_role(user_id: str, payload: UpdateRoleRequest) -> UserResponse:
    """Change a user's role (admin only).

    Refuses to demote the last active admin user away from ``admin`` (409): the
    system must always retain at least one active admin USER ROW. (The synthetic
    shared-key bootstrap admin is a separate recovery path and is not counted.)
    """
    try:
        updated = _repository().update_role(user_id, role=payload.role.value)
    except LastAdminError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return UserResponse(**updated)
