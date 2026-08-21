"""In-process Advanced-panel sessions.

The panel opens a session (POST .../raw/session) that binds a project/site and
owner, and the browser gets an opaque HttpOnly cookie. The proxy resolves that
cookie on the iframe's subresource requests so evidence rows are attributed to
the right project/site/owner - which those cookieless requests cannot carry
otherwise. Single-process, in-memory (sidecars are single-process too); a panel
just re-opens its session after a restart.

ponytail: the cookie carries project/site attribution only, not authentication -
auth stays the route's require_engineer (loopback ADMIN in local/portable). Make
it an auth credential when a keyed hosted deployment needs the Advanced tab.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

_TTL_SECONDS = 8 * 3600
COOKIE_NAME = "sct_panel"


@dataclass(frozen=True)
class PanelSession:
    session_id: str
    owner: str
    project_id: str
    site_id: str
    proto: str
    expires_mono: float


_SESSIONS: dict[str, PanelSession] = {}


def create(*, owner: str, project_id: str, site_id: str, proto: str) -> PanelSession:
    session = PanelSession(
        session_id=secrets.token_urlsafe(24),
        owner=owner,
        project_id=project_id,
        site_id=site_id,
        proto=proto,
        expires_mono=time.monotonic() + _TTL_SECONDS,
    )
    _SESSIONS[session.session_id] = session
    return session


def resolve(session_id: str | None) -> PanelSession | None:
    if not session_id:
        return None
    session = _SESSIONS.get(session_id)
    if session is None:
        return None
    if time.monotonic() >= session.expires_mono:
        _SESSIONS.pop(session_id, None)
        return None
    return session


def clear() -> None:
    """Test hook: drop all sessions and write tokens."""
    _SESSIONS.clear()
    _WRITE_TOKENS.clear()


# --------------------------------------------------------------------------
# Write-confirm tokens (M4). A device write from the panel is allowed only with a
# single-use token bound to sha256(method | path | body) for the confirming
# session. Hash binding means the confirm dialog cannot show one payload while
# different bytes reach the wire.
# --------------------------------------------------------------------------

_WRITE_TOKEN_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class WriteToken:
    session_id: str
    digest: str
    expires_mono: float


_WRITE_TOKENS: dict[str, WriteToken] = {}


def mint_write_token(*, session_id: str, digest: str) -> str:
    token = secrets.token_urlsafe(24)
    _WRITE_TOKENS[token] = WriteToken(
        session_id=session_id, digest=digest, expires_mono=time.monotonic() + _WRITE_TOKEN_TTL_SECONDS
    )
    return token


def consume_write_token(token: str | None, *, session_id: str, digest: str) -> bool:
    """Verify and single-use-consume a write token. True only if it exists, is for
    this session, matches this request's digest, and has not expired."""
    if not token:
        return False
    entry = _WRITE_TOKENS.pop(token, None)
    if entry is None:
        return False
    if time.monotonic() >= entry.expires_mono:
        return False
    return entry.session_id == session_id and entry.digest == digest
