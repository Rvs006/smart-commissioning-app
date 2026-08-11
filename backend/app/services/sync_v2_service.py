"""Hub-side verification and immutable persistence for Sync v2 bundles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from smart_commissioning_core.db.sync_v2_repository import (
    SyncV2Repository,
    sync_receipt_id,
)
from smart_commissioning_core.integrity import sha256_bytes
from smart_commissioning_core.sync_v2 import (
    OpenedSyncV2Bundle,
    SyncV2Descriptor,
    SyncV2Error,
    _assert_no_forbidden_artifact_material,
    open_sync_v2_bundle,
    receipt_dict,
    validate_sync_v2_authority_snapshots,
    validate_sync_v2_item,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.core.sync_auth import SyncPrincipal
from app.services.report_artifacts import (
    canonical_json_bytes,
    store_content_addressed_artifact,
    verify_signed_manifest,
)


def ingest_sync_v2_bundle(
    engine: Engine,
    bundle: bytes,
    *,
    principal: SyncPrincipal,
    now: datetime,
    max_items: int = 500,
    max_uncompressed_bytes: int = 200 * 1024 * 1024,
) -> dict[str, object]:
    """Verify a bundle and return durable, independent per-item receipts."""

    opened = open_sync_v2_bundle(
        bundle,
        expected_edge_id=principal.edge_id,
        expected_signing_key_fingerprint=principal.signing_key_fingerprint,
        max_items=max_items,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    repository = SyncV2Repository(engine)
    receipts: list[dict[str, object]] = []
    acknowledged_run_ids: list[str] = []
    for descriptor in opened.manifest.items:
        if not repository.scope_allows(
            principal.credential_id,
            descriptor.project_id,
            descriptor.site_id,
        ):
            receipt = _reject(
                repository,
                opened,
                principal,
                descriptor,
                "unauthorized",
                now=now,
            )
            receipts.append(receipt)
            continue

        item_bytes = opened.members.get(descriptor.item_member)
        if item_bytes is None:
            receipts.append(
                _reject(
                    repository,
                    opened,
                    principal,
                    descriptor,
                    "partial_bundle",
                    now=now,
                )
            )
            continue
        try:
            item = validate_sync_v2_item(item_bytes, descriptor)
        except SyncV2Error:
            receipts.append(
                _reject(
                    repository,
                    opened,
                    principal,
                    descriptor,
                    "malformed",
                    now=now,
                )
            )
            continue

        try:
            authority_snapshots = validate_sync_v2_authority_snapshots(
                opened,
                item,
            )
        except SyncV2Error as error:
            receipt_class = "partial_bundle" if str(error) == "partial_bundle" else "malformed"
            receipts.append(
                _reject(
                    repository,
                    opened,
                    principal,
                    descriptor,
                    receipt_class,
                    now=now,
                )
            )
            continue

        receipt_class, artifact = _verify_artifact(
            opened,
            descriptor,
            item,
        )
        if receipt_class is not None:
            receipts.append(
                _reject(
                    repository,
                    opened,
                    principal,
                    descriptor,
                    receipt_class,
                    now=now,
                )
            )
            continue

        try:
            receipt_class, receipt_id = repository.ingest_verified_item(
                item=item,
                edge_id=principal.edge_id,
                credential_id=principal.credential_id,
                signing_key_fingerprint=principal.signing_key_fingerprint,
                bundle_id=opened.manifest.bundle_id,
                item_id=descriptor.item_id,
                item_sha256=descriptor.item_sha256,
                artifact_sha256=descriptor.artifact_sha256,
                authority_snapshots=authority_snapshots,
                artifact_factory=_verified_artifact_factory(artifact),
                now=now,
            )
        except (IntegrityError, OSError, RuntimeError, ValueError):
            receipts.append(
                _reject(
                    repository,
                    opened,
                    principal,
                    descriptor,
                    "partial_bundle",
                    now=now,
                )
            )
            continue
        receipt = receipt_dict(
            receipt_id=receipt_id,
            descriptor=descriptor,
            receipt_class=receipt_class,
        )
        receipts.append(receipt)
        if bool(receipt["acknowledged"]):
            acknowledged_run_ids.append(descriptor.run_id)

    return {
        "protocol": "smart-commissioning-sync",
        "protocol_version": "2.0",
        "bundle_id": opened.manifest.bundle_id,
        "edge_id": opened.manifest.edge_id,
        "receipts": receipts,
        "acknowledged_run_ids": acknowledged_run_ids,
        "all_acknowledged": len(acknowledged_run_ids) == len(opened.manifest.items),
    }


def _verify_artifact(
    opened: OpenedSyncV2Bundle,
    descriptor: SyncV2Descriptor,
    item: dict[str, Any],
) -> tuple[str | None, dict[str, object] | None]:
    is_report = item["run"].get("job_type") == "report_generation"
    manifest = item.get("artifact_manifest")
    if not is_report:
        if descriptor.artifact_member is not None or manifest is not None:
            return "malformed", None
        return None, None
    if not isinstance(manifest, dict) or descriptor.artifact_member is None:
        return "missing_artifact", None
    if not verify_signed_manifest(manifest):
        return "manifest_signature_failed", None
    if manifest.get("origin") != opened.manifest.edge_id:
        return "malformed", None
    if descriptor.artifact_size != manifest.get("byte_size"):
        return "artifact_size_failed", None
    if descriptor.artifact_sha256 != manifest.get("artifact_sha256"):
        return "artifact_hash_failed", None
    artifact = opened.members.get(descriptor.artifact_member)
    if artifact is None:
        return "partial_bundle", None
    if len(artifact) != descriptor.artifact_size:
        return "artifact_size_failed", None
    if sha256_bytes(artifact) != descriptor.artifact_sha256:
        return "artifact_hash_failed", None
    try:
        _assert_no_forbidden_artifact_material(artifact)
    except SyncV2Error:
        return "malformed", None
    fields = {
        "file_name": manifest.get("file_name"),
        "media_type": manifest.get("media_type"),
        "renderer_version": manifest.get("renderer_version"),
        "origin": manifest.get("origin"),
        "signing_key_id": manifest.get("signing_key_id"),
    }
    if any(not isinstance(value, str) or not value for value in fields.values()):
        return "malformed", None
    if Path(str(fields["file_name"])).name != fields["file_name"]:
        return "malformed", None
    return None, {
        "artifact_bytes": artifact,
        "artifact_sha256": descriptor.artifact_sha256,
        "byte_size": descriptor.artifact_size,
        "manifest": manifest,
        "manifest_sha256": sha256_bytes(canonical_json_bytes(manifest)),
        **fields,
    }


def _store_verified_artifact(artifact: dict[str, object]) -> dict[str, object]:
    artifact_bytes = artifact.get("artifact_bytes")
    artifact_sha256 = artifact.get("artifact_sha256")
    if not isinstance(artifact_bytes, bytes) or not isinstance(artifact_sha256, str):
        raise ValueError("Verified artifact staging data is incomplete.")
    storage_relpath = store_content_addressed_artifact(
        artifact_bytes,
        artifact_sha256,
    )
    return {key: value for key, value in artifact.items() if key != "artifact_bytes"} | {
        "storage_relpath": storage_relpath
    }


def _verified_artifact_factory(
    artifact: dict[str, object] | None,
) -> Callable[[], dict[str, object]] | None:
    if artifact is None:
        return None

    def store() -> dict[str, object]:
        return _store_verified_artifact(artifact)

    return store


def _reject(
    repository: SyncV2Repository,
    opened: OpenedSyncV2Bundle,
    principal: SyncPrincipal,
    descriptor: SyncV2Descriptor,
    receipt_class: str,
    *,
    now: datetime,
) -> dict[str, object]:
    receipt_id = sync_receipt_id(
        principal.credential_id,
        opened.manifest.bundle_id,
        descriptor.item_id,
        receipt_class,
    )
    repository.record_rejection(
        receipt_id=receipt_id,
        credential_id=principal.credential_id,
        bundle_id=opened.manifest.bundle_id,
        item_id=descriptor.item_id,
        run_id=descriptor.run_id,
        receipt_class=receipt_class,
        item_sha256=descriptor.item_sha256,
        result_sha256=descriptor.result_sha256,
        artifact_sha256=descriptor.artifact_sha256,
        now=now,
    )
    return receipt_dict(
        receipt_id=receipt_id,
        descriptor=descriptor,
        receipt_class=receipt_class,
    )
