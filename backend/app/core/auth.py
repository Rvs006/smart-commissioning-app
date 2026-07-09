"""Request authentication + RBAC for the API (see Settings.auth_mode).

Authentication resolves a single :class:`AuthPrincipal` for every request and
stashes it on ``request.state.principal`` so routes and role checks can read who
the caller is. Resolution order (require_auth):

  1. A presented X-API-Key / Bearer key whose SHA-256 hash matches an ACTIVE
     user -> that user's principal (source="user_key"); last_used_at is touched.
     An inactive user's key is rejected (401) — it never falls through to the
     shared key.
  2. Else the legacy shared ``settings.api_key`` (constant-time compare) -> a
     synthetic ADMIN principal (source="shared_key"). This is the bootstrap
     admin key, preserved for backward compatibility.
  3. Else, in ``local`` mode only, a loopback client -> a synthetic ADMIN
     principal (source="local"), matching the portable desktop deployment.

Modes:

- "api_key": every request must resolve via (1) or (2). Otherwise 401, fail
  closed (no key is ever echoed). Loopback does NOT grant access in this mode.
- "local" (default): (1) and (2) still apply; additionally a loopback client is
  trusted even with no key (3).

A user key always wins over the shared key, so promoting an operator to a
named admin user does not silently keep granting via the old shared key for
that same key value (different key values, different hashes).

Health endpoints are exempt (wired in app.api.router) so liveness and
readiness probes stay reachable without credentials.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from smart_commissioning_core.db.repositories import UserRepository
from smart_commissioning_core.rbac import Role

from app.core.config import get_settings
from app.core.db import get_engine

logger = logging.getLogger(__name__)

# Host reported by Starlette's TestClient ASGI transport. It is not a real
# network address — it can never appear as the TCP peer address of a request
# arriving over a real network interface — so treating it as loopback only
# affects in-process test traffic.
_TEST_CLIENT_HOST = "testclient"

PrincipalSource = Literal["user_key", "shared_key", "local"]


@dataclass(frozen=True)
class AuthPrincipal:
    """The authenticated caller for a request.

    Attributes:
        user_id:  the User.id for a real per-user key; None for the synthetic
                  shared_key / local principals (they are not backed by a row).
        username: a display name. The user's username for user_key; a synthetic
                  label ("shared-key" / "local") for the bootstrap principals.
        role:     the caller's RBAC role (smart_commissioning_core.rbac.Role).
                  shared_key and local both resolve to ADMIN so the bootstrap
                  operator can manage users before the first user exists.
        source:   how the caller authenticated — "user_key" | "shared_key" |
                  "local".
    """

    user_id: str | None
    username: str
    role: Role
    source: PrincipalSource


def hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest used to store/look up a per-user API key.

    The plaintext key is never persisted; this digest is what lands in
    ``User.api_key_hash`` and what require_auth hashes a presented key to.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _presented_key(request: Request) -> str | None:
    """Extract the API key from X-API-Key or Authorization: Bearer."""
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            return credential.strip()
    return None


def _key_matches(presented: str | None, configured_key: str) -> bool:
    if presented is None:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), configured_key.encode("utf-8"))


def is_loopback_host(host: str) -> bool:
    """True when the client host is loopback (127.0.0.0/8 or ::1)."""
    if host == _TEST_CLIENT_HOST:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) before the loopback check.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback


def _is_loopback_client(request: Request) -> bool:
    # No transport client information means an in-process ASGI call.
    if request.client is None:
        return True
    return is_loopback_host(request.client.host)


def _resolve_user_principal(presented: str | None, rejection_detail: str) -> AuthPrincipal | None:
    """Resolve a presented key to an active user's principal, or None.

    Returns None only when no key is presented or the key matches NO user row,
    so the caller can fall through to the shared-key / loopback paths. A key
    that DOES match a user row but must not authenticate — the user is
    inactive, or their stored role value is corrupt — raises 401 immediately:
    a real user's key never falls through to the synthetic shared/local admin
    (falling through would quietly grant a deactivated user loopback ADMIN on
    the portable/local profile). The 401 detail is ``rejection_detail`` — the
    caller passes the exact message a key matching NO row would produce for
    the same mode and client location, so the response never discloses that
    the key matched a row (no key-validity oracle). Touches last_used_at on
    success (best-effort; a touch failure never blocks the request).
    """
    if not presented:
        return None
    repository = UserRepository(get_engine())
    user = repository.get_by_api_key_hash(hash_api_key(presented))
    if user is None:
        return None
    if not user["is_active"]:
        raise HTTPException(status_code=401, detail=rejection_detail)
    try:
        role = Role.from_value(str(user["role"]))
    except ValueError as error:
        # A corrupt/unknown role value must not authenticate at some accidental
        # privilege — and, like an inactive user, must not fall through either.
        logger.warning("User %s has an unknown role value; rejecting.", user["id"])
        raise HTTPException(status_code=401, detail=rejection_detail) from error
    try:
        repository.touch_last_used(str(user["id"]))
    except Exception:  # noqa: BLE001 (a last_used touch must never fail a request)
        logger.debug("Failed to touch last_used_at for user %s.", user["id"], exc_info=True)
    return AuthPrincipal(
        user_id=str(user["id"]),
        username=str(user["username"]),
        role=role,
        source="user_key",
    )


def _resolve_principal(request: Request) -> AuthPrincipal:
    """Resolve the request to an AuthPrincipal or raise 401 (fail-closed).

    Order: active user key -> shared bootstrap key -> (local mode) loopback.
    The configured key is never echoed in any error.
    """
    settings = get_settings()
    configured_key = (settings.api_key or "").strip()
    presented = _presented_key(request)

    # ONE generic 401 detail per (mode, client location), shared by every
    # rejection this request can produce — a key matching a deactivated user
    # must be indistinguishable from a key matching no row (no key-validity
    # oracle for remote clients in local mode).
    if settings.auth_mode == "api_key" or _is_loopback_client(request):
        rejection_detail = "Missing or invalid API key."
    else:
        rejection_detail = "Requests from non-local clients require an API key."

    # (1) Per-user key wins.
    user_principal = _resolve_user_principal(presented, rejection_detail)
    if user_principal is not None:
        return user_principal

    if settings.auth_mode == "api_key":
        # Fail closed: with no shared key configured, only users can authenticate.
        if configured_key and _key_matches(presented, configured_key):
            return AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")
        raise HTTPException(status_code=401, detail=rejection_detail)

    # local mode: a configured shared key is accepted from anywhere; otherwise
    # only loopback clients are trusted.
    if configured_key and _key_matches(presented, configured_key):
        return AuthPrincipal(None, "shared-key", Role.ADMIN, "shared_key")
    # On the loopback edge ANY co-resident local process is trusted as ADMIN, so require_role() gates are NOT a
    # security boundary here — they bite only under AUTH_MODE=api_key with per-user keys.
    if _is_loopback_client(request):
        return AuthPrincipal(None, "local", Role.ADMIN, "local")
    raise HTTPException(status_code=401, detail=rejection_detail)


def require_auth(request: Request) -> AuthPrincipal:
    """FastAPI dependency: authenticate the request, returning its principal.

    Resolves the caller per the configured auth mode (see module docstring),
    stashes the principal on ``request.state.principal`` so any downstream code
    (including get_principal) can read it without re-resolving, and returns it.
    Raises 401 with a generic detail message; the configured key is never echoed.
    """
    principal = _resolve_principal(request)
    request.state.principal = principal
    return principal


def get_principal(request: Request) -> AuthPrincipal:
    """FastAPI dependency returning the current AuthPrincipal.

    Reads the principal that require_auth stashed on request.state (every route
    under the protected router runs require_auth first). If it is somehow absent
    (e.g. a route mounted without the protected router), it resolves once here so
    this dependency is always safe to use behind authentication.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        principal = require_auth(request)
    return principal


def require_role(min_role: Role):
    """Dependency factory: require the caller's role be >= ``min_role``.

    Returns a FastAPI dependency that:
      * 401s if the request is unauthenticated (handled by the underlying auth),
      * 403s if the authenticated principal's role is below ``min_role``.

    The 403 body states only the required role — it never leaks the caller's own
    role, other users, or key material.
    """

    def dependency(principal: AuthPrincipal = Depends(get_principal)) -> AuthPrincipal:
        if not principal.role.at_least(min_role):
            raise HTTPException(
                status_code=403,
                detail=f"This action requires the '{min_role.value}' role or higher.",
            )
        return principal

    return dependency
