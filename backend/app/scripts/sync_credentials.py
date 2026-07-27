"""Provision a hashed, scoped Sync v2 machine credential on a hub."""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import UTC, datetime
from uuid import uuid4

from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.core.sync_auth import sync_key_sha256


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.sync_credentials")
    parser.add_argument("--edge-id", required=True)
    parser.add_argument("--signing-key-fingerprint", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        nargs=2,
        metavar=("PROJECT_ID", "SITE_ID"),
        required=True,
        help="Approved exact project/site pair. Repeat for more pairs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if get_settings().deployment_role != "hub":
        print("ERROR: synchronization credentials can only be provisioned on a hub.", file=sys.stderr)
        return 2
    fingerprint = args.signing_key_fingerprint.strip().lower()
    if len(fingerprint) != 16 or any(character not in "0123456789abcdef" for character in fingerprint):
        print("ERROR: signing-key fingerprint must be 16 lowercase hexadecimal characters.", file=sys.stderr)
        return 2
    ensure_runtime_directories()
    raw_key = secrets.token_urlsafe(32)
    credential_id = f"sync_{uuid4().hex}"
    SyncV2Repository(get_engine()).create_credential(
        credential_id=credential_id,
        edge_id=args.edge_id,
        api_key_hash=sync_key_sha256(raw_key),
        signing_key_fingerprint=fingerprint,
        scopes=[(project_id, site_id) for project_id, site_id in args.scope],
        now=datetime.now(UTC),
    )
    print(f"Created {credential_id} with {len(set(map(tuple, args.scope)))} exact scope(s).")
    print("Copy this synchronization key now; the hub stores only its SHA-256 hash:")
    print(raw_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
