"""Dedicated machine authentication for Sync v2 hub endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated

from fastapi import Header, HTTPException
from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.db import get_engine


@dataclass(frozen=True)
class SyncPrincipal:
    credential_id: str
    edge_id: str
    signing_key_fingerprint: str


def sync_key_sha256(raw_key: str) -> str:
    return sha256(raw_key.encode("utf-8")).hexdigest()


def require_sync_credential(
    x_sync_key: Annotated[str | None, Header(alias="X-Sync-Key")] = None,
) -> SyncPrincipal:
    """Authenticate only a scoped Sync v2 machine credential."""

    if get_settings().deployment_role != "hub":
        raise HTTPException(status_code=404, detail="Not found.")
    if x_sync_key is None or not 16 <= len(x_sync_key) <= 512:
        raise _unauthorized()
    repository = SyncV2Repository(get_engine())
    record = repository.credential_for_hash(sync_key_sha256(x_sync_key))
    if record is None:
        raise _unauthorized()
    principal = SyncPrincipal(
        credential_id=str(record["credential_id"]),
        edge_id=str(record["edge_id"]),
        signing_key_fingerprint=str(record["signing_key_fingerprint"]),
    )
    try:
        repository.touch_credential(principal.credential_id, now=datetime.now(UTC))
    except SQLAlchemyError:
        # Authentication already succeeded. A transient audit-timestamp write
        # must not turn a retryable evidence upload into a false credential error.
        pass
    return principal


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid synchronization credential.",
        headers={"WWW-Authenticate": "SyncKey"},
    )
