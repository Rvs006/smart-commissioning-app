"""CLI for backup/restore of the edge (SQLite) runtime.

Usage (from backend/):

    # Create a signed bundle of the current runtime (DB + secrets + imports):
    python -m app.scripts.backup create --out backup.zip

    # Inspect/verify a bundle without restoring:
    python -m app.scripts.backup verify --bundle backup.zip

    # Restore into a target runtime root (refuses to overwrite without --force):
    python -m app.scripts.backup restore --bundle backup.zip --target /path/runtime

The bundle is a single zip: a CONSISTENT SQLite snapshot (online backup API),
the secrets dir (encrypted PEMs + store key + signing key), import files, and a
signed manifest. Postgres (hub) is out of scope here — use pg_dump (see the
backup_service docstring / decisions).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from smart_commissioning_core.db.engine import default_sqlite_url

from app.core.config import get_settings
from app.core.runtime import (
    IMPORT_FILES_ROOT,
    SECRETS_ROOT,
    ensure_runtime_directories,
)
from app.services.backup_service import (
    BackupError,
    BackupSources,
    RestoreTarget,
    create_backup_bundle,
    restore_backup_bundle,
    verify_bundle,
)
from app.services.reports_integrity import load_signing_key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.backup")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a signed backup bundle.")
    create_parser.add_argument("--out", required=True, type=Path, help="Output bundle path (.zip).")

    verify_parser = subparsers.add_parser("verify", help="Verify a bundle's signature + hashes.")
    verify_parser.add_argument("--bundle", required=True, type=Path, help="Bundle path to verify.")
    verify_parser.add_argument(
        "--allow-unsigned", action="store_true", help="Accept an unsigned manifest."
    )

    restore_parser = subparsers.add_parser("restore", help="Restore a bundle into a runtime root.")
    restore_parser.add_argument("--bundle", required=True, type=Path, help="Bundle path to restore.")
    restore_parser.add_argument(
        "--target", required=True, type=Path, help="Target runtime root to restore into."
    )
    restore_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing populated database."
    )
    restore_parser.add_argument(
        "--allow-unsigned", action="store_true", help="Accept an unsigned manifest."
    )
    return parser


def _cmd_create(args: argparse.Namespace) -> int:
    ensure_runtime_directories()
    sources = BackupSources(
        database_url=get_settings().database_url,
        secrets_root=SECRETS_ROOT,
        imports_files_root=IMPORT_FILES_ROOT,
    )
    bundle = create_backup_bundle(
        sources,
        created_at=datetime.now(UTC),
        signing_key=load_signing_key(),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bundle)
    print(f"Wrote backup bundle ({len(bundle)} bytes) to {args.out}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    manifest = verify_bundle(args.bundle.read_bytes(), allow_unsigned=args.allow_unsigned)
    print("Bundle verified OK.")
    print(json.dumps({key: manifest[key] for key in ("created_at", "core_version", "bundle_format_version")}, indent=2))
    print(f"Members: {len(manifest.get('members', {}))}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    target_root = args.target
    target = RestoreTarget(
        database_path=Path(default_sqlite_url(target_root).removeprefix("sqlite:///")),
        secrets_root=target_root / "secrets",
        imports_files_root=target_root / "imports" / "files",
    )
    manifest = restore_backup_bundle(
        args.bundle.read_bytes(),
        target,
        force=args.force,
        allow_unsigned=args.allow_unsigned,
    )
    print(f"Restored bundle (created_at={manifest.get('created_at')}) into {target_root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {"create": _cmd_create, "verify": _cmd_verify, "restore": _cmd_restore}
    try:
        return handlers[args.command](args)
    except BackupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
