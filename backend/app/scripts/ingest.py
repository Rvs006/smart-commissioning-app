"""Offline hub ingest CLI: ingest a carried ``.scbundle`` file into the hub.

Usage (from backend/):

    python -m app.scripts.ingest runs.scbundle

The HUB side of smart_commissioning_core.sync for AIR-GAPPED sites: an operator
carries a ``.scbundle`` (produced on an edge via ``python -m app.scripts.sync
--output``) to the hub on physical media and runs this. It verifies the bundle
against the hub's configured trusted edges and immutably inserts each run,
printing the IngestSummary (what was inserted / skipped / rejected).

Role guard: refuses to run unless deployment_role is 'hub'. Trust comes from
settings (``trusted_edges_path`` / ``trusted_edges_inline``) — never from the
bundle's self-declared identity. Core fails closed: an untrusted edge, a forged
key, a bad signature, or a tampered member rejects the WHOLE bundle (nothing is
written).

Honesty: this runs entirely in-process against the configured database (SQLite
here). A Postgres-backed hub is supported by pointing DATABASE_URL at Postgres,
but that path is not exercised in this environment (live_untested).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from smart_commissioning_core.sync import ingest_sync_bundle

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.ingest")
    parser.add_argument("bundle", type=Path, help="Path to the .scbundle file to ingest.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if settings.deployment_role != "hub":
        print(
            "ERROR: offline ingest is hub-only; set deployment_role=hub on this instance.",
            file=sys.stderr,
        )
        return 2

    if not args.bundle.exists():
        print(f"ERROR: bundle file not found: {args.bundle}", file=sys.stderr)
        return 2

    try:
        trusted_edges = settings.load_trusted_edges()
    except ValueError as error:
        print(f"ERROR: trust configuration error: {error}", file=sys.stderr)
        return 2

    ensure_runtime_directories()
    bundle_bytes = args.bundle.read_bytes()
    summary = ingest_sync_bundle(
        get_engine(),
        bundle_bytes,
        trusted_edges=trusted_edges,
        now=datetime.now(UTC),
    )
    print("Ingest summary:")
    print(json.dumps(summary.as_dict(), indent=2))
    if not summary.accepted:
        # Bundle rejected wholesale (untrusted edge, bad signature, or tampered
        # member): nothing was written.
        return 1
    if summary.rejected_immutable or summary.rejected_bad_hash:
        # Bundle was trusted and verified, but some runs could not be applied
        # (an immutability conflict with an existing hub record, or a member
        # hash mismatch). Surface a distinct non-zero code so operators
        # scripting the CLI do not treat a partial/no-op carry as a clean
        # insert.
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
