import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from smart_commissioning_core.db.engine import default_sqlite_url
from smart_commissioning_core.sync_identity import (
    EdgeIdentity,
    load_edge_signing_key,
    load_or_create_edge_identity,
)

from app.core.runtime import RUNTIME_ROOT


class Settings(BaseSettings):
    environment: str = "development"
    # Portable/edge-friendly default: a SQLite file under the runtime root.
    # Deployments that run Postgres set DATABASE_URL explicitly (see infra/).
    database_url: str = default_sqlite_url(RUNTIME_ROOT)
    # Apply Alembic migrations (smart_commissioning_core.db.migrate) on startup.
    auto_migrate: bool = True
    redis_url: str = "redis://localhost:6379/0"
    # API authentication (enforced by app.core.auth.require_auth):
    # - "local" (default): only loopback clients are accepted, matching the
    #   portable desktop deployment where uvicorn binds 127.0.0.1. If api_key
    #   is also set, a valid key is accepted from any client address.
    # - "api_key": every request must present the configured key via the
    #   X-API-Key header or "Authorization: Bearer <key>".
    auth_mode: Literal["local", "api_key"] = "local"
    api_key: str | None = None
    # Comma-separated allowed CORS origins (env CORS_ORIGINS).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Upload limits for /api/v1/imports: cap on the request body / uploaded
    # file size, and a zip-bomb guard on the declared decompressed size of
    # XLSX archives.
    max_upload_bytes: int = 20 * 1024 * 1024
    max_xlsx_decompressed_bytes: int = 200 * 1024 * 1024
    job_execution_mode: Literal["auto", "queue", "inline"] = "auto"
    allow_inline_worker_fallback: bool = True
    # Run inline (portable-exe) executions on a background thread so the POST
    # returns immediately and the run monitor / Stop-run control render while the
    # run is live (ITEM-4). On by default so the portable build gets it out of the
    # box; the API test suite forces it off (INLINE_RUN_ASYNC=0) because those
    # tests POST a run then assert its terminal status synchronously.
    inline_run_async: bool = True
    # Edge->hub synchronization role (smart_commissioning_core.sync). Determines
    # which sync features are active for this instance:
    #   - "standalone" (default): today's single-instance behavior. No sync; the
    #     hub ingest endpoint is NOT mounted and the sync/ingest CLIs refuse to
    #     run. New runs are still stamped with the local edge_id for provenance.
    #   - "edge": an on-site instance that PUSHES signed run bundles to a hub
    #     (hub_url) or writes them to a file for offline carry. It never accepts
    #     ingests.
    #   - "hub": a central instance that ACCEPTS signed bundles from trusted
    #     edges (POST /api/v1/hub/runs/ingest and the offline ingest CLI),
    #     verifying each against trusted_edges before immutable insert.
    deployment_role: Literal["standalone", "edge", "hub"] = "standalone"
    # Edge-only: base URL of the hub to push bundles to (no trailing /api/v1).
    # The edge sync CLI appends /api/v1/hub/runs/ingest. May be overridden per
    # invocation with --hub-url.
    hub_url: str | None = None
    # Hub-only: path to a JSON file pinning the edges this hub trusts. The file
    # is a list of objects, each with an "edge_id" and EITHER a
    # "public_key_fingerprint" (16 hex chars) OR a full "pem" / "public_key_pem".
    # Loaded via Settings.load_trusted_edges(); a forged self-reported
    # fingerprint cannot fool trust because core derives the authoritative
    # fingerprint from the bundle's embedded PEM (see sync._edge_is_trusted).
    trusted_edges_path: str | None = None
    # Hub-only: inline trusted-edges JSON (same list-of-objects shape), used when
    # a file path is impractical. Merged on top of the file when both are set.
    trusted_edges_inline: str | None = None
    # Conservative active-scan throttle defaults applied to discovery engines
    # (IP/BACnet/MQTT). Gentle by design so a scan pointed at a real building
    # network cannot overwhelm controllers or a broker. Per-run parameters may
    # narrow these further but not exceed the operator's environment policy
    # unless they explicitly override via request parameters.
    scan_max_concurrency: int = 16
    scan_rate_limit_per_sec: float = 10.0
    scan_connect_timeout_s: float = 5.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def load_trusted_edges(self) -> dict[str, str]:
        """Return the hub's trusted-edges map: edge_id -> fingerprint OR PEM.

        Reads ``trusted_edges_path`` (a JSON list of objects) when set, then
        overlays ``trusted_edges_inline`` (same shape) so an inline override wins
        for any colliding edge_id. Each object must carry an ``edge_id`` and one
        of ``public_key_fingerprint`` (16 hex) or ``pem`` / ``public_key_pem``.

        The returned values are passed verbatim to core ``ingest_sync_bundle`` as
        ``trusted_edges``; core derives the authoritative fingerprint from the
        bundle's embedded PEM, so a fingerprint pin and a PEM pin are equally
        safe (a forged manifest fingerprint cannot fool trust).

        Raises ValueError on malformed config so a misconfigured hub fails loudly
        rather than silently trusting nothing (or, worse, everything).
        """
        trusted: dict[str, str] = {}
        if self.trusted_edges_path:
            path = Path(self.trusted_edges_path)
            if not path.exists():
                raise ValueError(f"trusted_edges_path does not exist: {path}")
            trusted.update(_parse_trusted_edges(path.read_text(encoding="utf-8")))
        if self.trusted_edges_inline:
            trusted.update(_parse_trusted_edges(self.trusted_edges_inline))
        return trusted


def _parse_trusted_edges(raw: str) -> dict[str, str]:
    """Parse a trusted-edges JSON document into an edge_id -> trust-value map."""
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"trusted_edges is not valid JSON: {error}") from error
    if not isinstance(entries, list):
        raise ValueError("trusted_edges must be a JSON list of {edge_id, ...} objects.")
    trusted: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each trusted_edges entry must be an object.")
        edge_id = entry.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id:
            raise ValueError("Each trusted_edges entry needs a non-empty 'edge_id'.")
        trust_value = (
            entry.get("public_key_fingerprint")
            or entry.get("pem")
            or entry.get("public_key_pem")
        )
        if not isinstance(trust_value, str) or not trust_value.strip():
            raise ValueError(
                f"trusted_edges entry for {edge_id!r} needs a 'public_key_fingerprint' or 'pem'."
            )
        trusted[edge_id] = trust_value.strip()
    return trusted


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Edge identity (id + signing key) lives under the runtime root so it persists
# across restarts and is shared by run attribution and the sync CLI. These thin
# wrappers pin the directory; the core does the create-once-then-load work.


def edge_identity() -> EdgeIdentity:
    """Resolve (creating once) this instance's stable edge identity.

    Persisted under RUNTIME_ROOT (``edge_id`` + ``edge_signing_key``). The
    ``edge_id`` is always available; the public key PEM/fingerprint are populated
    when ``cryptography`` is installed (it is a backend dependency).
    """
    return load_or_create_edge_identity(RUNTIME_ROOT)


def edge_signing_key():
    """Load (creating once) this instance's Ed25519 signing key under RUNTIME_ROOT.

    Used by the edge sync CLI to sign a bundle manifest. Raises
    IntegrityUnavailableError when ``cryptography`` is unavailable.
    """
    return load_edge_signing_key(RUNTIME_ROOT)
