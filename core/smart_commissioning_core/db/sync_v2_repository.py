"""Persistence operations for Sync v2 credentials, receipts, and exact evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from smart_commissioning_core.db.db_run_store import (
    _run_to_dict,
    get_or_create_project_and_site,
)
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    ImportRecord,
    ReportEvidenceContract,
    Run,
    RunExecutionContext,
    RunResult,
    RunSeal,
    SyncArtifact,
    SyncCredential,
    SyncCredentialScope,
    SyncDeliveryState,
    SyncReceipt,
)
from smart_commissioning_core.db.repositories import SyncRepository
from smart_commissioning_core.run_context import canonical_sha256

ACKNOWLEDGED_RECEIPT_CLASSES = frozenset({"accepted", "byte_identical"})


def sync_receipt_id(
    credential_id: str,
    bundle_id: str,
    item_id: str,
    receipt_class: str,
) -> str:
    return sha256("\0".join((credential_id, bundle_id, item_id, receipt_class)).encode("utf-8")).hexdigest()


class SyncV2Repository:
    """Role-neutral storage used by an edge sender and a hub receiver."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)
        self._legacy = SyncRepository(engine)

    def create_credential(
        self,
        *,
        credential_id: str,
        edge_id: str,
        api_key_hash: str,
        signing_key_fingerprint: str,
        scopes: list[tuple[str, str]],
        now: datetime,
    ) -> None:
        """Provision a hashed machine credential and exact project/site scopes."""

        if not scopes:
            raise ValueError("A sync credential must have at least one project/site scope.")
        with self._session_factory.begin() as session:
            session.add(
                SyncCredential(
                    id=credential_id,
                    edge_id=edge_id,
                    api_key_hash=api_key_hash,
                    signing_key_fingerprint=signing_key_fingerprint,
                    is_active=True,
                    created_at=now,
                )
            )
            for project_id, site_id in sorted(set(scopes)):
                session.add(
                    SyncCredentialScope(
                        credential_id=credential_id,
                        project_id=project_id,
                        site_id=site_id,
                    )
                )

    def credential_for_hash(self, api_key_hash: str) -> dict[str, object] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(SyncCredential).where(
                    SyncCredential.api_key_hash == api_key_hash,
                    SyncCredential.is_active.is_(True),
                )
            )
            if row is None:
                return None
            return {
                "credential_id": row.id,
                "edge_id": row.edge_id,
                "signing_key_fingerprint": row.signing_key_fingerprint,
            }

    def touch_credential(self, credential_id: str, *, now: datetime) -> None:
        with self._session_factory.begin() as session:
            row = session.get(SyncCredential, credential_id)
            if row is not None:
                row.last_used_at = now

    def scope_allows(self, credential_id: str, project_id: str, site_id: str) -> bool:
        with self._session_factory() as session:
            return (
                session.get(
                    SyncCredentialScope,
                    {
                        "credential_id": credential_id,
                        "project_id": project_id,
                        "site_id": site_id,
                    },
                )
                is not None
            )

    def list_pending_run_ids(self) -> list[str]:
        """Return sealed terminal runs without a complete v2 acknowledgement."""

        from smart_commissioning_core.db.models import RunSeal

        statement = (
            select(Run.id)
            .join(RunSeal, RunSeal.run_id == Run.id)
            .outerjoin(SyncDeliveryState, SyncDeliveryState.run_id == Run.id)
            .where(Run.status.in_(("succeeded", "failed", "cancelled")))
            .where(SyncDeliveryState.acknowledged_at.is_(None))
            .order_by(Run.created_at.asc(), Run.id.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def get_artifact(self, run_id: str) -> dict[str, object] | None:
        with self._session_factory() as session:
            row = session.get(SyncArtifact, run_id)
            if row is None:
                return None
            return {
                "run_id": row.run_id,
                "artifact_sha256": row.artifact_sha256,
                "byte_size": row.byte_size,
                "storage_relpath": row.storage_relpath,
                "manifest": dict(row.manifest_json),
                "manifest_sha256": row.manifest_sha256,
                "file_name": row.file_name,
                "media_type": row.media_type,
                "renderer_version": row.renderer_version,
                "origin": row.origin,
                "signing_key_id": row.signing_key_id,
            }

    def ingest_verified_item(
        self,
        *,
        item: dict[str, Any],
        edge_id: str,
        credential_id: str,
        signing_key_fingerprint: str,
        bundle_id: str,
        item_id: str,
        item_sha256: str,
        artifact_sha256: str | None,
        authority_snapshots: list[dict[str, object]],
        artifact_factory: Callable[[], dict[str, object]] | None,
        now: datetime,
    ) -> tuple[str, str]:
        """Insert an absent item, or classify an existing immutable item."""

        run = item["run"]
        run_id = str(run["run_id"])
        result = item["result"]
        seal = item["seal"]
        result_sha256 = str(seal["result_sha256"])
        with self._session_factory.begin() as session:
            authorized = self._active_credential_scope(
                session,
                credential_id=credential_id,
                edge_id=edge_id,
                signing_key_fingerprint=signing_key_fingerprint,
                project_id=str(run["project_id"]),
                site_id=str(run["site_id"]),
            )
            existing_run = session.get(Run, run_id) if authorized else None
            if not authorized:
                receipt_class = "unauthorized"
            elif existing_run is not None:
                if self._existing_item_is_identical(
                    session,
                    existing_run=existing_run,
                    item=item,
                    edge_id=edge_id,
                    artifact_sha256=artifact_sha256,
                ):
                    if self._authority_snapshot_conflicts(session, authority_snapshots):
                        receipt_class = "conflict"
                    else:
                        receipt_class = "byte_identical"
                        self._insert_missing_authority_snapshots(
                            session,
                            authority_snapshots,
                            now=now,
                        )
                        self._backfill_identical_evidence(
                            session,
                            run_id=run_id,
                            item=item,
                            artifact_sha256=artifact_sha256,
                            artifact_factory=artifact_factory,
                            now=now,
                        )
                else:
                    receipt_class = "conflict"
            else:
                if self._authority_snapshot_conflicts(session, authority_snapshots):
                    receipt_class = "conflict"
                else:
                    receipt_class = "accepted"
                    artifact = artifact_factory() if artifact_factory is not None else None
                    if artifact is not None and artifact_sha256 is None:
                        raise RuntimeError("Stored artifact has no verified SHA-256.")
                    if artifact is not None and (str(artifact["artifact_sha256"]) != artifact_sha256):
                        raise RuntimeError("Stored artifact digest changed after verification.")
                    get_or_create_project_and_site(session, str(run["project_id"]), str(run["site_id"]))
                    session.add(
                        self._legacy._run_row(  # noqa: SLF001 - shared internal row contract
                            run,
                            edge_id=edge_id,
                            result=result,
                            seal=seal,
                        )
                    )
                    session.flush()
                    if run["job_type"] == "report_generation":
                        session.add(
                            ReportEvidenceContract(
                                run_id=run_id,
                                contract_version="sealed_v1",
                                project_id=str(run["project_id"]),
                                site_id=str(run["site_id"]),
                                classified_at=now,
                            )
                        )
                    self._insert_missing_authority_snapshots(
                        session,
                        authority_snapshots,
                        now=now,
                    )
                    terminal = dict(result["result_payload"])
                    for position, issue in enumerate(terminal.get("issues") or []):
                        session.add(self._legacy._issue_row(run_id, position, issue))  # noqa: SLF001
                    for position, device in enumerate(terminal.get("devices") or []):
                        session.add(
                            self._legacy._discovery._device_row(run_id, position, device)  # noqa: SLF001
                        )
                    for position, point in enumerate(terminal.get("points") or []):
                        session.add(
                            self._legacy._discovery._point_row(run_id, position, point)  # noqa: SLF001
                        )
                    for position, topic in enumerate(terminal.get("topics") or []):
                        session.add(
                            self._legacy._discovery._topic_row(run_id, position, topic)  # noqa: SLF001
                        )
                    session.add(self._legacy._result_row(run_id, result))  # noqa: SLF001
                    session.add(self._legacy._seal_row(run_id, seal))  # noqa: SLF001
                    context = item.get("execution_context")
                    if isinstance(context, dict):
                        session.add(
                            RunExecutionContext(
                                run_id=run_id,
                                schema_version=str(context["schema_version"]),
                                context_json=dict(context["context_json"]),
                                context_sha256=str(context["context_sha256"]),
                                created_at=_parse_datetime(context["created_at"]),
                            )
                        )
                    if artifact is not None:
                        session.add(
                            self._artifact_row(
                                run_id,
                                artifact_sha256=str(artifact_sha256),
                                artifact=artifact,
                                now=now,
                            )
                        )

            receipt_id = sync_receipt_id(credential_id, bundle_id, item_id, receipt_class)
            self._add_receipt(
                session,
                receipt_id=receipt_id,
                credential_id=credential_id,
                bundle_id=bundle_id,
                item_id=item_id,
                run_id=run_id,
                receipt_class=receipt_class,
                item_sha256=item_sha256,
                result_sha256=result_sha256,
                artifact_sha256=artifact_sha256,
                now=now,
            )
        return receipt_class, receipt_id

    @staticmethod
    def _active_credential_scope(
        session: Any,
        *,
        credential_id: str,
        edge_id: str,
        signing_key_fingerprint: str,
        project_id: str,
        site_id: str,
    ) -> bool:
        """Lock and recheck the machine authority in the item transaction."""
        credential = session.scalar(
            select(SyncCredential)
            .where(
                SyncCredential.id == credential_id,
                SyncCredential.edge_id == edge_id,
                SyncCredential.signing_key_fingerprint == signing_key_fingerprint,
                SyncCredential.is_active.is_(True),
            )
            .with_for_update()
        )
        if credential is None:
            return False
        scope = session.scalar(
            select(SyncCredentialScope)
            .where(
                SyncCredentialScope.credential_id == credential_id,
                SyncCredentialScope.project_id == project_id,
                SyncCredentialScope.site_id == site_id,
            )
            .with_for_update()
        )
        return scope is not None

    @staticmethod
    def _authority_snapshot_conflicts(
        session: Any,
        authority_snapshots: list[dict[str, object]],
    ) -> bool:
        for snapshot in authority_snapshots:
            import_id = str(snapshot["import_id"])
            existing = session.get(ImportRecord, import_id)
            if existing is None:
                continue
            rows = list(existing.accepted_rows or [])
            if (
                existing.import_type != snapshot["import_type"]
                or existing.project_id != snapshot["project_id"]
                or existing.site_id != snapshot["site_id"]
                or len(rows) != snapshot["accepted_count"]
                or canonical_sha256(rows) != snapshot["accepted_rows_sha256"]
            ):
                return True
        return False

    @staticmethod
    def _insert_missing_authority_snapshots(
        session: Any,
        authority_snapshots: list[dict[str, object]],
        *,
        now: datetime,
    ) -> None:
        for snapshot in authority_snapshots:
            import_id = str(snapshot["import_id"])
            if session.get(ImportRecord, import_id) is not None:
                continue
            accepted_count = int(snapshot["accepted_count"])
            digest = str(snapshot["accepted_rows_sha256"])
            file_name = f"sync-{import_id}.csv"
            session.add(
                ImportRecord(
                    import_id=import_id,
                    import_type=str(snapshot["import_type"]),
                    project_id=str(snapshot["project_id"]),
                    site_id=str(snapshot["site_id"]),
                    original_filename=file_name,
                    stored_file_path=f"sync-v2://sha256/{digest}",
                    summary={
                        "import_id": import_id,
                        "import_type": str(snapshot["import_type"]),
                        "file_name": file_name,
                        "file_type": "csv",
                        "project_id": str(snapshot["project_id"]),
                        "site_id": str(snapshot["site_id"]),
                        "total_rows": accepted_count,
                        "accepted_rows": accepted_count,
                        "rejected_rows": 0,
                        "status": "accepted",
                        "missing_columns": [],
                        "warnings": [],
                        "stored_file_name": file_name,
                        "created_at": now.isoformat(),
                        "accepted_rows_sha256": digest,
                        "authority_schema_version": "1.0",
                        "sync_authority_member": str(snapshot["member"]),
                    },
                    accepted_rows=list(snapshot["accepted_rows"]),
                    errors=[],
                    created_at=now,
                )
            )

    def _existing_item_is_identical(
        self,
        session: Any,
        *,
        existing_run: Run,
        item: dict[str, Any],
        edge_id: str,
        artifact_sha256: str | None,
    ) -> bool:
        incoming_run = dict(item["run"])
        existing_seal = session.get(RunSeal, existing_run.id)
        existing_result = session.get(RunResult, existing_run.id)
        if existing_seal is None or existing_result is None:
            return False
        if existing_run.edge_id != edge_id:
            return False
        if existing_run.job_type == "report_generation":
            contract = session.get(ReportEvidenceContract, existing_run.id)
            if (
                contract is None
                or contract.contract_version != "sealed_v1"
                or contract.project_id != existing_run.project_id
                or contract.site_id != existing_run.site_id
            ):
                return False

        normalized_run = _run_to_dict(existing_run)
        if existing_run.job_type == "report_generation":
            normalized_run["parameters"] = {
                key: existing_run.parameters[key]
                for key in (
                    "output_format",
                    "report_type",
                    "source_run_ids",
                    "report_title_custom",
                    "report_title",
                    "report_generated_at",
                    "renderer_version",
                    "report_snapshot_v2",
                    "report_snapshot_sha256",
                    "evidence_set_id",
                    "udmi_report_variant",
                    "source_run_snapshots",
                    "source_run_seals",
                    "source_discovery_snapshots",
                    "udmi_scope",
                    "udmi_report_snapshot",
                )
                if key in (existing_run.parameters or {})
            }
        else:
            normalized_run["parameters"] = {"context_sha256": existing_seal.context_sha256}
        if normalized_run != incoming_run:
            return False
        if self._legacy._result_payload(existing_result) != item["result"]:  # noqa: SLF001
            return False
        if self._legacy._seal_payload(existing_seal) != item["seal"]:  # noqa: SLF001
            return False

        incoming_context = item.get("execution_context")
        existing_context = session.get(RunExecutionContext, existing_run.id)
        if incoming_context is None:
            if existing_context is not None:
                return False
        elif existing_context is not None and (
            self._legacy._context_payload(existing_context) != incoming_context  # noqa: SLF001
        ):
            return False

        incoming_manifest = item.get("artifact_manifest")
        existing_artifact = session.get(SyncArtifact, existing_run.id)
        if incoming_manifest is None:
            return existing_artifact is None and artifact_sha256 is None
        if artifact_sha256 is None:
            return False
        if incoming_manifest.get("artifact_sha256") != artifact_sha256 or not isinstance(
            incoming_manifest.get("byte_size"), int
        ):
            return False
        if existing_artifact is None:
            return True
        return (
            existing_artifact.artifact_sha256 == artifact_sha256
            and existing_artifact.byte_size == incoming_manifest["byte_size"]
            and dict(existing_artifact.manifest_json) == incoming_manifest
            and existing_artifact.file_name == incoming_manifest.get("file_name")
            and existing_artifact.media_type == incoming_manifest.get("media_type")
            and existing_artifact.renderer_version == incoming_manifest.get("renderer_version")
            and existing_artifact.origin == incoming_manifest.get("origin")
            and existing_artifact.signing_key_id == incoming_manifest.get("signing_key_id")
        )

    def _backfill_identical_evidence(
        self,
        session: Any,
        *,
        run_id: str,
        item: dict[str, Any],
        artifact_sha256: str | None,
        artifact_factory: Callable[[], dict[str, object]] | None,
        now: datetime,
    ) -> None:
        context = item.get("execution_context")
        if isinstance(context, dict) and session.get(RunExecutionContext, run_id) is None:
            session.add(
                RunExecutionContext(
                    run_id=run_id,
                    schema_version=str(context["schema_version"]),
                    context_json=dict(context["context_json"]),
                    context_sha256=str(context["context_sha256"]),
                    created_at=_parse_datetime(context["created_at"]),
                )
            )
        if item.get("artifact_manifest") is None:
            return
        if session.get(SyncArtifact, run_id) is not None:
            return
        if artifact_sha256 is None or artifact_factory is None:
            raise RuntimeError("Identical report evidence has no verified artifact bytes.")
        artifact = artifact_factory()
        if str(artifact["artifact_sha256"]) != artifact_sha256:
            raise RuntimeError("Stored artifact digest changed after verification.")
        session.add(
            self._artifact_row(
                run_id,
                artifact_sha256=artifact_sha256,
                artifact=artifact,
                now=now,
            )
        )

    @staticmethod
    def _artifact_row(
        run_id: str,
        *,
        artifact_sha256: str,
        artifact: dict[str, object],
        now: datetime,
    ) -> SyncArtifact:
        manifest = artifact["manifest"]
        byte_size = artifact["byte_size"]
        if not isinstance(manifest, dict) or not isinstance(byte_size, int):
            raise RuntimeError("Verified artifact metadata has invalid types.")
        return SyncArtifact(
            run_id=run_id,
            artifact_sha256=artifact_sha256,
            byte_size=byte_size,
            storage_relpath=str(artifact["storage_relpath"]),
            manifest_json=manifest,
            manifest_sha256=str(artifact["manifest_sha256"]),
            file_name=str(artifact["file_name"]),
            media_type=str(artifact["media_type"]),
            renderer_version=str(artifact["renderer_version"]),
            origin=str(artifact["origin"]),
            signing_key_id=str(artifact["signing_key_id"]),
            received_at=now,
        )

    def record_rejection(
        self,
        *,
        receipt_id: str,
        credential_id: str,
        bundle_id: str,
        item_id: str,
        run_id: str,
        receipt_class: str,
        item_sha256: str | None,
        result_sha256: str | None,
        artifact_sha256: str | None,
        now: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            self._add_receipt(
                session,
                receipt_id=receipt_id,
                credential_id=credential_id,
                bundle_id=bundle_id,
                item_id=item_id,
                run_id=run_id,
                receipt_class=receipt_class,
                item_sha256=item_sha256,
                result_sha256=result_sha256,
                artifact_sha256=artifact_sha256,
                now=now,
            )

    @staticmethod
    def _add_receipt(
        session: Any,
        *,
        receipt_id: str,
        credential_id: str,
        bundle_id: str,
        item_id: str,
        run_id: str,
        receipt_class: str,
        item_sha256: str | None,
        result_sha256: str | None,
        artifact_sha256: str | None,
        now: datetime,
    ) -> None:
        existing = session.scalar(
            select(SyncReceipt).where(
                SyncReceipt.credential_id == credential_id,
                SyncReceipt.bundle_id == bundle_id,
                SyncReceipt.item_id == item_id,
                SyncReceipt.receipt_class == receipt_class,
            )
        )
        if existing is not None:
            return
        session.add(
            SyncReceipt(
                id=receipt_id,
                credential_id=credential_id,
                bundle_id=bundle_id,
                item_id=item_id,
                run_id=run_id,
                receipt_class=receipt_class,
                acknowledged=receipt_class in ACKNOWLEDGED_RECEIPT_CLASSES,
                item_sha256=item_sha256,
                result_sha256=result_sha256,
                artifact_sha256=artifact_sha256,
                created_at=now,
            )
        )

    def apply_delivery_receipts(
        self,
        receipts: list[dict[str, object]],
        descriptors: dict[str, dict[str, object]],
        *,
        now: datetime,
    ) -> list[str]:
        """Persist receipt state and return only fully acknowledged run IDs."""

        acknowledged: list[str] = []
        seen_item_ids: set[str] = set()
        with self._session_factory.begin() as session:
            for receipt in receipts:
                item_id = str(receipt.get("item_id") or "")
                if item_id in seen_item_ids:
                    continue
                seen_item_ids.add(item_id)
                descriptor = descriptors.get(item_id)
                if descriptor is None:
                    continue
                run_id = str(descriptor["run_id"])
                receipt_class = str(receipt.get("class") or "malformed")
                if receipt.get("run_id") != run_id:
                    continue
                if receipt.get("acknowledged") is not (receipt_class in ACKNOWLEDGED_RECEIPT_CLASSES):
                    continue
                state = session.get(SyncDeliveryState, run_id)
                if state is None:
                    raw_artifact_sha256 = descriptor.get("artifact_sha256")
                    state = SyncDeliveryState(
                        run_id=run_id,
                        protocol_version="2.0",
                        item_sha256=str(descriptor["item_sha256"]),
                        result_sha256=str(descriptor["result_sha256"]),
                        artifact_sha256=(str(raw_artifact_sha256) if raw_artifact_sha256 is not None else None),
                    )
                    session.add(state)
                elif state.item_sha256 != str(descriptor["item_sha256"]) or state.result_sha256 != str(
                    descriptor["result_sha256"]
                ):
                    raise RuntimeError(f"Local sealed evidence changed for run {run_id}; receipt not applied.")
                state.last_receipt_id = str(receipt.get("receipt_id") or "") or None
                state.last_receipt_class = receipt_class
                state.last_attempt_at = now
                if receipt_class in ACKNOWLEDGED_RECEIPT_CLASSES:
                    state.acknowledged_at = state.acknowledged_at or now
                    run = session.get(Run, run_id)
                    if run is not None:
                        run.synced_at = run.synced_at or now
                    acknowledged.append(run_id)
        return acknowledged


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)
