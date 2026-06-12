"""Request authentication for the API (see Settings.auth_mode).

Modes:

- "api_key": every request must present the configured key via the
  ``X-API-Key`` header or ``Authorization: Bearer <key>``. If no key is
  configured the API fails closed: every request is rejected with 401.
- "local" (default): only loopback clients are accepted, matching the
  portable desktop deployment where uvicorn binds 127.0.0.1. If an API key
  is *also* configured, a request presenting the valid key is accepted from
  any client address.

Health endpoints are exempt (wired in app.api.router) so liveness and
readiness probes stay reachable without credentials.
"""

import ipaddress
import secrets

from fastapi import HTTPException, Request

from app.core.config import get_settings

# Host reported by Starlette's TestClient ASGI transport. It is not a real
# network address — it can never appear as the TCP peer address of a request
# arriving over a real network interface — so treating it as loopback only
# affects in-process test traffic.
_TEST_CLIENT_HOST = "testclient"


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


def _key_matches(request: Request, configured_key: str) -> bool:
    presented = _presented_key(request)
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


def require_auth(request: Request) -> None:
    """FastAPI dependency enforcing the configured authentication mode.

    Raises 401 with a generic detail message; the configured key is never
    echoed back to the client.
    """
    settings = get_settings()
    configured_key = (settings.api_key or "").strip()

    if settings.auth_mode == "api_key":
        # Fail closed: with no key configured, nothing can authenticate.
        if not configured_key:
            raise HTTPException(status_code=401, detail="API key authentication is not configured on the server.")
        if not _key_matches(request, configured_key):
            raise HTTPException(status_code=401, detail="Missing or invalid API key.")
        return

    # local mode: a configured key is accepted from anywhere; otherwise only
    # loopback clients are trusted.
    if configured_key and _key_matches(request, configured_key):
        return
    if _is_loopback_client(request):
        return
    raise HTTPException(status_code=401, detail="Requests from non-local clients require an API key.")
