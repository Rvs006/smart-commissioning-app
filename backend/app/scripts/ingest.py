"""Offline hub ingest CLI: ingest a carried ``.scbundle`` file into the hub.

Usage (from backend/):

    python -m app.scripts.ingest runs.scbundle

The HUB side of smart_commissioning_core.sync for AIR-GAPPED sites: an operator
carries a ``.scbundle`` (produced on an edge via ``python -m app.scripts.sync
--output``) to the hub on physical media and runs this. It verifies the bundle
against the hub's configured trusted edges and immutably inserts each run,
printing the IngestSummary (what was inserted / skipped / rejected).

Role guard: refuses to run unless deployment_role is 'hub'. Trust comes from
settings (``trusted_edges_path`` / ``trusted_edges_inline``), never from the
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
import os
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile, ZipFile

from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from smart_commissioning_core.sync import ingest_sync_bundle
from smart_commissioning_core.sync_v2 import SyncV2Error, strict_json_loads

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.core.sync_auth import SyncPrincipal, sync_key_sha256
from app.services.sync_v2_service import ingest_sync_v2_bundle

_MAX_DETECTION_MANIFEST_BYTES = 2 * 1024 * 1024


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

    max_bundle_bytes = int(getattr(settings, "max_sync_bundle_bytes", 20 * 1024 * 1024))
    try:
        bundle_size = args.bundle.stat().st_size
    except OSError as error:
        print(f"ERROR: could not inspect bundle file: {error}", file=sys.stderr)
        return 2
    if bundle_size > max_bundle_bytes:
        print(
            f"ERROR: bundle exceeds the configured {max_bundle_bytes}-byte limit.",
            file=sys.stderr,
        )
        return 2

    ensure_runtime_directories()
    try:
        bundle_bytes = args.bundle.read_bytes()
    except OSError as error:
        print(f"ERROR: could not read bundle file: {error}", file=sys.stderr)
        return 2
    max_items = int(getattr(settings, "max_sync_items", 500))
    max_uncompressed_bytes = int(
        getattr(settings, "max_sync_uncompressed_bytes", 200 * 1024 * 1024)
    )
    try:
        protocol = _detect_sync_protocol(
            bundle_bytes,
            max_items=max_items,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    except SyncV2Error:
        print("ERROR: bundle format is invalid or exceeds configured limits.", file=sys.stderr)
        return 1
    if protocol == "v2":
        raw_key = os.environ.get("SMART_COMMISSIONING_SYNC_KEY")
        if not raw_key:
            print(
                "ERROR: offline Sync v2 ingest requires SMART_COMMISSIONING_SYNC_KEY.",
                file=sys.stderr,
            )
            return 2
        engine = get_engine()
        credential = SyncV2Repository(engine).credential_for_hash(sync_key_sha256(raw_key))
        if credential is None:
            print("ERROR: invalid synchronization credential.", file=sys.stderr)
            return 2
        principal = SyncPrincipal(
            credential_id=str(credential["credential_id"]),
            edge_id=str(credential["edge_id"]),
            signing_key_fingerprint=str(credential["signing_key_fingerprint"]),
        )
        try:
            response = ingest_sync_v2_bundle(
                engine,
                bundle_bytes,
                principal=principal,
                now=datetime.now(UTC),
                max_items=max_items,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
        except SyncV2Error:
            print("ERROR: invalid Sync v2 bundle.", file=sys.stderr)
            return 1
        print("Sync v2 ingest receipts:")
        print(json.dumps(response, indent=2))
        return 0 if response.get("all_acknowledged") else 3

    try:
        trusted_edges = settings.load_trusted_edges()
    except ValueError as error:
        print(f"ERROR: trust configuration error: {error}", file=sys.stderr)
        return 2
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


def _detect_sync_protocol(
    bundle: bytes,
    *,
    max_items: int,
    max_uncompressed_bytes: int,
) -> Literal["v1", "v2"]:
    try:
        with ZipFile(BytesIO(bundle)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise SyncV2Error("Bundle has duplicate or ambiguous members.")
            if len(infos) > 1 + (2 * max_items):
                raise SyncV2Error("Bundle member count exceeds the configured limit.")
            if sum(info.file_size for info in infos) > max_uncompressed_bytes:
                raise SyncV2Error("Bundle expands beyond the configured limit.")
            manifest_info = next(
                info for info in infos if info.filename == "manifest.json"
            )
            if manifest_info.file_size > min(
                _MAX_DETECTION_MANIFEST_BYTES,
                max_uncompressed_bytes,
            ):
                raise SyncV2Error("Bundle manifest exceeds the detection limit.")
            if manifest_info.flag_bits & 0x1:
                raise SyncV2Error("Encrypted bundle manifests are not supported.")
            manifest = strict_json_loads(archive.read(manifest_info).decode("utf-8"))
    except (
        BadZipFile,
        KeyError,
        StopIteration,
        RuntimeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise SyncV2Error("Bundle protocol cannot be detected safely.") from error
    if not isinstance(manifest, dict):
        raise SyncV2Error("Bundle manifest must be a JSON object.")
    if "protocol_version" in manifest or "protocol" in manifest:
        if (
            manifest.get("protocol") == "smart-commissioning-sync"
            and manifest.get("protocol_version") == "2.0"
        ):
            return "v2"
        raise SyncV2Error("Bundle declares an unsupported Sync protocol.")
    if (
        manifest.get("bundle_format_version") == 1
        and manifest.get("schema_version") in {1, 2}
    ):
        return "v1"
    raise SyncV2Error("Bundle does not declare a supported Sync protocol.")


if __name__ == "__main__":
    raise SystemExit(main())
