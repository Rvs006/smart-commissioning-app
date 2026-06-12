"""CLI for data retention: purge old runs (and cascading children) safely.

Usage (from backend/):

    # DRY-RUN (default): list what WOULD be deleted, delete nothing.
    python -m app.scripts.retention --keep-days 90

    # Actually delete (requires the explicit --apply flag):
    python -m app.scripts.retention --keep-days 90 --apply

Safety:
  * Dry-run by default; --apply is required to delete.
  * Evidence-linked runs (report/evidence runs and any run referenced by a
    report's source_run_ids) are NEVER deleted.
  * Every deletion is logged.
"""

from __future__ import annotations

import argparse
import json
import logging

from smart_commissioning_core.db.engine import create_engine_from_url

from app.core.config import get_settings
from app.core.runtime import ensure_runtime_directories
from app.services.retention_service import RetentionService, cutoff_from_keep_days


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.retention")
    parser.add_argument(
        "--keep-days",
        type=int,
        required=True,
        help="Retain runs created within this many days; older runs are candidates.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete eligible runs (omit for a safe dry-run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    if args.keep_days < 0:
        print("ERROR: --keep-days must be >= 0", file=__import__("sys").stderr)
        return 2

    ensure_runtime_directories()
    engine = create_engine_from_url(get_settings().database_url)
    try:
        service = RetentionService(engine)
        cutoff = cutoff_from_keep_days(args.keep_days)
        if args.apply:
            result = service.apply(before=cutoff, confirm=True)
        else:
            result = service.preview(before=cutoff)
    finally:
        engine.dispose()

    payload = result.as_dict()
    print(json.dumps(payload, indent=2))
    if result.dry_run:
        eligible = payload["candidate_count"] - payload["skipped_evidence_count"]
        print(f"\nDRY-RUN: {eligible} run(s) would be deleted. Re-run with --apply to delete.")
    else:
        print(f"\nDeleted {payload['deleted_count']} run(s); retained {payload['skipped_evidence_count']} evidence-linked run(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
