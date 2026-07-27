"""Edge sync CLI: push un-synced terminal runs to a hub, or write them to a file.

Usage (from backend/):

    # List what WOULD sync (no build, no push, no watermark change):
    python -m app.scripts.sync --dry-run

    # Online push to the configured hub and apply verified per-item receipts:
    python -m app.scripts.sync

    # Push to an explicit hub URL:
    python -m app.scripts.sync --hub-url https://hub.example.org

    # Offline carry: write a .scbundle without changing any watermark:
    python -m app.scripts.sync --output runs.scbundle

    # Bundle specific runs (each must exist and be terminal):
    python -m app.scripts.sync --run-id run_x --run-id run_y --output runs.scbundle

This is the edge sender. Sync v2 selects sealed terminal runs without an accepted
or byte-identical receipt, builds a signed deterministic bundle with the frozen
evidence and exact report bytes, then either:

  * posts it to the dedicated v2 endpoint and advances only run IDs proved by
    accepted or byte-identical per-item receipts; or
  * writes the bytes to ``--output`` for offline transfer without changing the
    watermark.

``--protocol auto`` negotiates v2 and falls back to the unchanged v1 reader only
for an explicit 404 or 406 capability response. Offline v1 and v2 files never
advance a watermark because a file is not a hub receipt.

Role guard: refuses to run unless deployment_role is 'edge' (or 'standalone',
which is allowed for ad-hoc exports). A 'hub' instance does not push.

The test suite covers in-process transfer, receipt validation, conflict handling,
and capability negotiation. Hosted release acceptance exercises the real Docker
network path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

import httpx
from smart_commissioning_core.db.repositories import SyncRepository
from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from smart_commissioning_core.sync import SyncError, build_sync_bundle
from smart_commissioning_core.sync_v2 import (
    BUNDLE_MEDIA_TYPE,
    SyncV2Error,
    SyncV2Receipt,
    build_sync_v2_bundle,
    open_sync_v2_bundle,
    strict_json_loads,
)

from app.core.config import edge_identity, edge_signing_key, get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.services.report_artifacts import load_report_artifact

_INGEST_PATH = "/api/v1/hub/runs/ingest"
_CAPABILITIES_V2_PATH = "/api/v1/hub/sync/capabilities"
_INGEST_V2_PATH = "/api/v1/hub/sync/v2/ingest"


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
    parser.add_argument(
        "--protocol",
        choices=("auto", "v2", "v1"),
        default="auto",
        help="Sync protocol. Online auto negotiates v2 then explicitly falls back to v1.",
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


def _probe_sync_v2(
    hub_url: str,
    *,
    sync_key: str | None,
    legacy_api_key: str | None,
) -> bool:
    """Return False only for an explicit old-reader response (404/406)."""

    headers: dict[str, str] = {}
    if sync_key:
        headers["X-Sync-Key"] = sync_key
    if legacy_api_key:
        headers["X-API-Key"] = legacy_api_key
    response = httpx.get(
        hub_url.rstrip("/") + _CAPABILITIES_V2_PATH,
        headers=headers,
        timeout=15.0,
    )
    if response.status_code in {404, 406}:
        return False
    response.raise_for_status()
    payload = response.json()
    if payload.get("preferred_protocol_version") != "2.0":
        raise RuntimeError("Hub returned an invalid Sync v2 capability document.")
    return True


def _push_v2_bundle(
    hub_url: str,
    bundle: bytes,
    *,
    sync_key: str,
) -> dict[str, object]:
    response = httpx.post(
        hub_url.rstrip("/") + _INGEST_V2_PATH,
        content=bundle,
        headers={"Content-Type": BUNDLE_MEDIA_TYPE, "X-Sync-Key": sync_key},
        timeout=60.0,
    )
    response.raise_for_status()
    payload = strict_json_loads(response.content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Hub returned an invalid Sync v2 response.")
    return payload


def _validated_receipts(
    response: dict[str, object],
    *,
    expected_bundle_id: str,
    expected_edge_id: str,
    expected_descriptors: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if set(response) != {
        "protocol",
        "protocol_version",
        "bundle_id",
        "edge_id",
        "receipts",
        "acknowledged_run_ids",
        "all_acknowledged",
    }:
        raise RuntimeError("Hub response has an invalid Sync v2 response shape.")
    if response.get("protocol") != "smart-commissioning-sync":
        raise RuntimeError("Hub response did not identify the Sync protocol.")
    if response.get("protocol_version") != "2.0":
        raise RuntimeError("Hub response did not identify Sync v2.")
    if response.get("bundle_id") != expected_bundle_id:
        raise RuntimeError("Hub receipt identifies a different bundle.")
    if response.get("edge_id") != expected_edge_id:
        raise RuntimeError("Hub receipt identifies a different edge.")
    raw_receipts = response.get("receipts")
    if not isinstance(raw_receipts, list):
        raise RuntimeError("Hub response has no per-item receipts.")
    receipts = [
        SyncV2Receipt.model_validate(receipt).model_dump(mode="json", by_alias=True)
        for receipt in raw_receipts
    ]
    receipt_item_ids = [str(receipt["item_id"]) for receipt in receipts]
    if (
        len(receipt_item_ids) != len(expected_descriptors)
        or len(receipt_item_ids) != len(set(receipt_item_ids))
        or set(receipt_item_ids) != set(expected_descriptors)
    ):
        raise RuntimeError("Hub response does not contain exactly one receipt per item.")
    by_item_id = {str(receipt["item_id"]): receipt for receipt in receipts}
    ordered = [by_item_id[item_id] for item_id in expected_descriptors]
    for receipt in ordered:
        descriptor = expected_descriptors[str(receipt["item_id"])]
        if receipt["run_id"] != descriptor["run_id"]:
            raise RuntimeError("Hub receipt run ID does not match its item.")
    acknowledged_run_ids = [
        str(receipt["run_id"])
        for receipt in ordered
        if bool(receipt["acknowledged"])
    ]
    if response.get("acknowledged_run_ids") != acknowledged_run_ids:
        raise RuntimeError("Hub response has incoherent acknowledged run IDs.")
    expected_all_acknowledged = len(acknowledged_run_ids) == len(expected_descriptors)
    if response.get("all_acknowledged") is not expected_all_acknowledged:
        raise RuntimeError("Hub response has an incoherent all_acknowledged state.")
    return ordered


def _configured_sync_key(settings: object) -> str | None:
    configured = getattr(settings, "sync_hub_api_key", None)
    if isinstance(configured, str) and configured:
        return configured
    return os.environ.get("SMART_COMMISSIONING_SYNC_KEY") or None


def _proved_v1_acknowledged_run_ids(
    summary: dict[str, object],
    submitted_run_ids: list[str],
) -> list[str]:
    """Return only IDs the v1 hub explicitly proved inserted or identical."""

    raw_inserted = summary.get("inserted_run_ids")
    raw_identical = summary.get("skipped_identical_run_ids", summary.get("skipped_run_ids"))
    proved = {
        run_id
        for values in (raw_inserted, raw_identical)
        if isinstance(values, list)
        for run_id in values
        if isinstance(run_id, str)
    }
    return [run_id for run_id in submitted_run_ids if run_id in proved]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if settings.deployment_role == "hub":
        print("ERROR: a 'hub' instance does not push runs; run this on an edge.", file=sys.stderr)
        return 2

    ensure_runtime_directories()
    engine = get_engine()
    legacy_repository = SyncRepository(engine)
    v2_repository = SyncV2Repository(engine)
    explicit_run_ids = _selected_run_ids(legacy_repository, args) if args.run_ids else None

    if args.dry_run:
        if explicit_run_ids is not None:
            run_ids = explicit_run_ids
        else:
            run_ids = list(
                dict.fromkeys(
                    [
                        *v2_repository.list_pending_run_ids(),
                        *legacy_repository.list_unsynced_terminal_runs(),
                    ]
                )
            )
        print(f"{len(run_ids)} terminal run(s) are pending a supported sync path:")
        for run_id in run_ids:
            print(f"  {run_id}")
        return 0

    hub_url = args.hub_url or settings.hub_url
    if not args.output and not hub_url:
        print("ERROR: no hub URL (set settings.hub_url or pass --hub-url), or use --output.", file=sys.stderr)
        return 2

    protocol = args.protocol
    sync_key = _configured_sync_key(settings)
    if protocol == "auto" and not args.output:
        try:
            protocol = (
                "v2"
                if _probe_sync_v2(
                    str(hub_url),
                    sync_key=sync_key,
                    legacy_api_key=settings.api_key,
                )
                else "v1"
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            print(f"ERROR: Sync capability negotiation failed: {error}", file=sys.stderr)
            return 2
    elif protocol == "auto":
        v2_pending = v2_repository.list_pending_run_ids()
        protocol = "v2" if explicit_run_ids or v2_pending else "v1"

    identity = edge_identity()
    if protocol == "v2":
        run_ids = explicit_run_ids or v2_repository.list_pending_run_ids()
        if not run_ids:
            print("Nothing to sync (no sealed run awaits a v2 receipt).")
            return 0
        try:
            signing_key = edge_signing_key()
            bundle = build_sync_v2_bundle(
                engine,
                run_ids=run_ids,
                signing_key=signing_key,
                edge_identity=identity,
                created_at=datetime.now(UTC),
                artifact_loader=load_report_artifact,
            )
            opened = open_sync_v2_bundle(
                bundle,
                expected_edge_id=identity.edge_id,
                expected_signing_key_fingerprint=signing_key.public_key_fingerprint(),
            )
        except (SyncV2Error, ValueError, RuntimeError, OSError) as error:
            print(f"ERROR: could not build Sync v2 evidence: {error}", file=sys.stderr)
            return 2
        if args.output:
            if args.mark_synced:
                print(
                    "ERROR: Sync v2 offline bundles remain pending until per-item hub receipts are applied.",
                    file=sys.stderr,
                )
                return 2
            with open(args.output, "wb") as handle:
                handle.write(bundle)
            print(
                f"Wrote Sync v2 bundle ({len(bundle)} bytes) for {len(run_ids)} run(s) "
                f"to {args.output}; no watermark changed."
            )
            return 0
        if not sync_key:
            print(
                "ERROR: Sync v2 requires SMART_COMMISSIONING_SYNC_KEY or sync_hub_api_key.",
                file=sys.stderr,
            )
            return 2
        try:
            response = _push_v2_bundle(str(hub_url), bundle, sync_key=sync_key)
            descriptors = {
                item.item_id: item.model_dump(mode="json") for item in opened.manifest.items
            }
            receipts = _validated_receipts(
                response,
                expected_bundle_id=opened.manifest.bundle_id,
                expected_edge_id=opened.manifest.edge_id,
                expected_descriptors=descriptors,
            )
            acknowledged = v2_repository.apply_delivery_receipts(
                receipts,
                descriptors,
                now=datetime.now(UTC),
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as error:
            print(f"ERROR: Sync v2 push failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(response, indent=2))
        print(f"Acknowledged {len(acknowledged)} of {len(run_ids)} run(s).")
        return 0 if len(acknowledged) == len(run_ids) else 1

    run_ids = explicit_run_ids or legacy_repository.list_unsynced_terminal_runs()
    if not run_ids:
        print("Nothing to sync (no un-synced terminal runs).")
        return 0
    try:
        bundle = build_sync_bundle(
            engine,
            run_ids=run_ids,
            signing_key=edge_signing_key(),
            edge_identity=identity,
            created_at=datetime.now(UTC),
        )
    except SyncError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args.output:
        if args.mark_synced:
            print(
                "ERROR: an offline v1 file has no hub receipt, so its watermark cannot advance.",
                file=sys.stderr,
            )
            return 2
        with open(args.output, "wb") as handle:
            handle.write(bundle)
        print(f"Wrote legacy v1 bundle ({len(bundle)} bytes) for {len(run_ids)} run(s) to {args.output}")
        print("Runs NOT marked synced; v1 has no per-item receipt.")
        return 0
    try:
        summary = _push_bundle(str(hub_url), bundle, settings.api_key)
    except httpx.HTTPError as error:
        print(f"ERROR: legacy v1 push failed: {error}", file=sys.stderr)
        return 2
    print("Legacy v1 hub ingest summary:")
    print(json.dumps(summary, indent=2))
    if protocol == "v1" and summary.get("accepted"):
        acknowledged = _proved_v1_acknowledged_run_ids(summary, run_ids)
        if acknowledged:
            updated = legacy_repository.mark_synced(acknowledged, now=datetime.now(UTC))
            print(f"Marked {updated} proved inserted/byte-identical v1 run(s) synced.")
        else:
            print("Hub returned no per-run v1 proof; all watermarks stay pending.")
        return 0 if len(acknowledged) == len(run_ids) else 1
    print("Hub rejected the v1 bundle; runs NOT marked synced.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
