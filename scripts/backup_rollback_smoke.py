#!/usr/bin/env python3
"""Smoke a byte-exact backup and rollback of portable runtime state.

The script uses disposable fixtures only. It proves the release backup tooling
keeps SQLite, encrypted secrets, imports, and report artifacts together, then
restores the same bytes without touching a real deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

REQUIRED_FILES = (
    "smart_commissioning.db",
    "secrets/.secret_store_key",
    "secrets/mqtt-client.pem",
    "imports/files/register.csv",
    "artifacts/report.pdf",
    "report-signing/.report_signing_key",
)


def _hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def smoke(work_root: Path) -> None:
    runtime = work_root / "runtime"
    backup = work_root / "backup"
    restored = work_root / "restored"
    for relative in REQUIRED_FILES[1:]:
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fixture:{relative}".encode())

    database = runtime / REQUIRED_FILES[0]
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE release_smoke (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO release_smoke(value) VALUES (?)", ("sealed-result",))
        connection.commit()
        status = connection.execute("PRAGMA integrity_check").fetchone()
        if status != ("ok",):
            raise RuntimeError(f"source SQLite integrity check failed: {status}")
    finally:
        connection.close()

    before = _hashes(runtime)
    shutil.copytree(runtime, backup)

    # Simulate a failed rollout changing all state, then restore from backup.
    for path in (item for item in runtime.rglob("*") if item.is_file()):
        path.write_bytes(b"mutated-after-backup")
    shutil.copytree(backup, restored)
    after = _hashes(restored)
    if before != after:
        raise RuntimeError("restored runtime hashes differ from the backup")

    restored_connection = sqlite3.connect(restored / "smart_commissioning.db")
    try:
        status = restored_connection.execute("PRAGMA integrity_check").fetchone()
        row = restored_connection.execute("SELECT value FROM release_smoke").fetchone()
    finally:
        restored_connection.close()
    if status != ("ok",) or row != ("sealed-result",):
        raise RuntimeError("restored SQLite data failed verification")

    (work_root / "backup-rollback-smoke.json").write_text(
        json.dumps({"status": "passed", "files": before}, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args(argv)
    if args.work_root:
        args.work_root.mkdir(parents=True, exist_ok=True)
        smoke(args.work_root.resolve())
        print(args.work_root / "backup-rollback-smoke.json")
    else:
        with tempfile.TemporaryDirectory(prefix="sct-rollback-smoke-") as temp:
            smoke(Path(temp))
        print("backup and rollback smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
