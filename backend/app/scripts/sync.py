"""Edge sync CLI: push un-synced terminal runs to a hub, or write them to a file.

Usage (from backend/):

    # List what WOULD sync (no build, no push, no watermark change):
    python -m app.scripts.sync --dry-run

    # Online push to the configured hub (settings.hub_url) and mark synced:
    python -m app.scripts.sync

    # Push to an explicit hub URL:
    python -m app.scripts.sync --hub-url https://hub.example.org

    # Offline carry: write a .scbundle for an air-gapped site WITHOUT marking
    # synced (so an operator can confirm the hub ingested before advancing the
    # watermark). Pass --mark-synced once confirmed.
    python -m app.scripts.sync --output runs.scbundle
    python -m app.scripts.sync --output runs.scbundle --mark-synced

    # Bundle specific runs (each must exist and be terminal):
    python -m app.scripts.sync --run-id run_x --run-id run_y --output runs.scbundle

This is the EDGE side of smart_commissioning_core.sync. It selects terminal runs
(succeeded/failed/cancelled) that have not yet been synced from here
(``synced_at IS NULL``), builds a signed, reproducible ``.scbundle`` with the
edge's signing key + identity, and either:

  * POSTs it to ``<hub-url>/api/v1/hub/runs/ingest`` (httpx) with the edge's API
    key, then marks the runs synced ONLY if the hub accepted them; or
  * writes the bytes to ``--output`` for offline transfer (marks synced only with
    ``--mark-synced``).

Role guard: refuses to run unless deployment_role is 'edge' (or 'standalone',
which is allowed for ad-hoc exports). A 'hub' instance does not push.

Honesty: the in-process round-trip (build here, ingest via the hub route) is
covered by tests using a FastAPI TestClient. The REAL network push to a remote
hub over TLS is live_untested (no remote hub in this environment).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

import httpx
from smart_commissioning_core.db.repositories import SyncRepository
from smart_commissioning_core.sync import SyncError, build_sync_bundle

from app.core.config import edge_identity, edge_signing_key, get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories

_INGEST_PATH = "/api/v1/hub/runs/ingest"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.sync")
    parser.add_argument(
        "--hub-url",
        default=None,
        help="Hub base URL to push to (overrides settings.hub_url). No trailing /api/v1.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the bundle to this .scbundle file instead of pushing (offline carry).",
    )
    parser.add_argument(
        "--since",
        action="store_true",
        help="Use the watermark set (every un-synced terminal run). Default when no --run-id given.",
    )
    parser.add_argument(
        "--run-id",
        action="append",
        default=None,
        dest="run_ids",
        help="Bundle a specific run id (repeatable). Each must exist and be terminal.",
    )
    parser.add_argument(
        "--mark-synced",
        action="store_true",
        help="Advance the watermark (mark runs synced) after writing an --output file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the run ids that WOULD sync and exit (no build, no push, no watermark change).",
    )
    return parser


def _selected_run_ids(repository: SyncRepository, args: argparse.Namespace) -> list[str]:
    """Resolve the ordered run ids to sync from the CLI flags.

    Explicit --run-id wins (validated by core at build time). Otherwise the
    un-synced terminal watermark set (oldest-first) is used; --since just makes
    that intent explicit.
    """
    if args.run_ids:
        if args.since:
            raise SystemExit("ERROR: pass either --run-id or --since, not both.")
        # De-dup preserving order; core re-validates existence + terminal status.
        seen: set[str] = set()
        ordered: list[str] = []
        for run_id in args.run_ids:
            if run_id not in seen:
                seen.add(run_id)
                ordered.append(run_id)
        return ordered
    return repository.list_unsynced_terminal_runs()


def _push_bundle(hub_url: str, bundle: bytes, api_key: str | None) -> dict[str, object]:
    """POST the bundle to the hub ingest endpoint; return the IngestSummary dict.

    Raw application/octet-stream body (the hub route accepts raw bytes or a
    multipart file). The edge API key, when configured, rides X-API-Key so the
    hub's require_auth accepts the request in api_key mode.

    This is the path that touches a real network. In this environment there is no
    remote hub, so this function is exercised only against an in-process
    TestClient transport in tests; the real-TLS push is live_untested.
    """
    url = hub_url.rstrip("/") + _INGEST_PATH
    headers = {"Content-Type": "application/octet-stream"}
    if api_key:
        headers["X-API-Key"] = api_key
    response = httpx.post(url, content=bundle, headers=headers, timeout=60.0)
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if settings.deployment_role == "hub":
        print("ERROR: a 'hub' instance does not push runs; run this on an edge.", file=sys.stderr)
        return 2

    ensure_runtime_directories()
    engine = get_engine()
    repository = SyncRepository(engine)

    run_ids = _selected_run_ids(repository, args)

    if args.dry_run:
        print(f"{len(run_ids)} run(s) would sync:")
        for run_id in run_ids:
            print(f"  {run_id}")
        return 0

    if not run_ids:
        print("Nothing to sync (no un-synced terminal runs).")
        return 0

    identity = edge_identity()
    try:
        bundle = build_sync_bundle(
            engine,
            run_ids=list(args.run_ids) if args.run_ids else None,
            signing_key=edge_signing_key(),
            edge_identity=identity,
            created_at=datetime.now(UTC),
        )
    except SyncError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if args.output:
        with open(args.output, "wb") as handle:
            handle.write(bundle)
        print(f"Wrote bundle ({len(bundle)} bytes) for {len(run_ids)} run(s) to {args.output}")
        if args.mark_synced:
            updated = repository.mark_synced(run_ids, now=datetime.now(UTC))
            print(f"Marked {updated} run(s) synced.")
        else:
            print("Runs NOT marked synced (pass --mark-synced once the hub confirms ingest).")
        return 0

    hub_url = args.hub_url or settings.hub_url
    if not hub_url:
        print("ERROR: no hub URL (set settings.hub_url or pass --hub-url), or use --output.", file=sys.stderr)
        return 2

    try:
        summary = _push_bundle(hub_url, bundle, settings.api_key)
    except httpx.HTTPError as error:
        print(f"ERROR: push to hub failed: {error}", file=sys.stderr)
        return 2

    print("Hub ingest summary:")
    print(json.dumps(summary, indent=2))
    if summary.get("accepted"):
        updated = repository.mark_synced(run_ids, now=datetime.now(UTC))
        print(f"Marked {updated} run(s) synced.")
    else:
        print("Hub rejected the bundle; runs NOT marked synced.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
