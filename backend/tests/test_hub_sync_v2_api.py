import base64
import json
import shutil
import tempfile
import unittest
import warnings
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from harness import ApiTestCase
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.models import RunSeal
from smart_commissioning_core.db.repositories import (
    ImportRepository,
    SyncRepository,
    UserRepository,
)
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from smart_commissioning_core.integrity import SigningKey, sha256_bytes
from smart_commissioning_core.run_context import RunContextV1, canonical_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from smart_commissioning_core.sync_identity import EdgeIdentity
from smart_commissioning_core.sync_v2 import (
    SyncV2Error,
    build_sync_v2_bundle,
    canonical_json_bytes,
)
from sqlalchemy.exc import IntegrityError

_API_KEY = "public-test-api-key-v2"
_NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class SyncV2CliContractTests(unittest.TestCase):
    def test_v2_sender_negotiates_to_explicit_v1_reader_404(self) -> None:
        import httpx
        from app.scripts.sync import _probe_sync_v2

        response = httpx.Response(404, request=httpx.Request("GET", "https://hub.invalid"))
        with mock.patch("app.scripts.sync.httpx.get", return_value=response) as get:
            supported = _probe_sync_v2(
                "https://hub.invalid",
                sync_key="sync-key-public-test",
                legacy_api_key="legacy-key-public-test",
            )
        self.assertFalse(supported)
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {
                "X-Sync-Key": "sync-key-public-test",
                "X-API-Key": "legacy-key-public-test",
            },
        )

    def test_v1_conflicts_and_unproven_ids_never_advance(self) -> None:
        from app.scripts.sync import _proved_v1_acknowledged_run_ids

        submitted = ["run-inserted", "run-identical", "run-conflict"]
        self.assertEqual(
            _proved_v1_acknowledged_run_ids(
                {
                    "accepted": True,
                    "inserted_run_ids": ["run-inserted"],
                    "skipped_run_ids": ["run-identical"],
                    "rejected_immutable_run_ids": ["run-conflict"],
                },
                submitted,
            ),
            ["run-inserted", "run-identical"],
        )
        self.assertEqual(
            _proved_v1_acknowledged_run_ids({"accepted": True}, submitted),
            [],
        )

    def test_receipt_validation_rejects_duplicate_a_and_missing_b(self) -> None:
        from app.scripts.sync import _validated_receipts

        item_a = "a" * 32
        item_b = "b" * 32
        duplicate_a = {
            "receipt_id": "1" * 64,
            "item_id": item_a,
            "run_id": "run-a",
            "class": "accepted",
            "acknowledged": True,
            "retryable": False,
        }
        response = {
            "protocol": "smart-commissioning-sync",
            "protocol_version": "2.0",
            "bundle_id": "c" * 64,
            "edge_id": "edge-public",
            "receipts": [duplicate_a, duplicate_a],
            "acknowledged_run_ids": ["run-a", "run-b"],
            "all_acknowledged": True,
        }
        with self.assertRaises(RuntimeError):
            _validated_receipts(
                response,
                expected_bundle_id="c" * 64,
                expected_edge_id="edge-public",
                expected_descriptors={
                    item_a: {"run_id": "run-a"},
                    item_b: {"run_id": "run-b"},
                },
            )

        malformed_id = dict(duplicate_a)
        malformed_id["receipt_id"] = "x" * 64
        with self.assertRaises(ValueError):
            _validated_receipts(
                {
                    **response,
                    "receipts": [malformed_id],
                    "acknowledged_run_ids": ["run-a"],
                },
                expected_bundle_id="c" * 64,
                expected_edge_id="edge-public",
                expected_descriptors={item_a: {"run_id": "run-a"}},
            )

    def test_auto_v1_fallback_marks_only_proved_run_ids(self) -> None:
        from app.scripts import sync

        legacy_repository = mock.Mock()
        legacy_repository.list_unsynced_terminal_runs.return_value = [
            "run-inserted",
            "run-conflict",
        ]
        settings = SimpleNamespace(
            deployment_role="edge",
            hub_url="https://hub.invalid",
            api_key="legacy-public-key",
            sync_hub_api_key="sync-public-key",
        )
        with (
            mock.patch.object(sync, "get_settings", return_value=settings),
            mock.patch.object(sync, "ensure_runtime_directories"),
            mock.patch.object(sync, "get_engine", return_value=object()),
            mock.patch.object(sync, "SyncRepository", return_value=legacy_repository),
            mock.patch.object(sync, "SyncV2Repository"),
            mock.patch.object(sync, "_probe_sync_v2", return_value=False),
            mock.patch.object(sync, "edge_identity", return_value=object()),
            mock.patch.object(sync, "edge_signing_key", return_value=object()),
            mock.patch.object(sync, "build_sync_bundle", return_value=b"v1-bundle"),
            mock.patch.object(
                sync,
                "_push_bundle",
                return_value={
                    "accepted": True,
                    "inserted_run_ids": ["run-inserted"],
                    "rejected_immutable_run_ids": ["run-conflict"],
                },
            ),
        ):
            exit_code = sync.main([])
        self.assertEqual(exit_code, 1)
        legacy_repository.mark_synced.assert_called_once()
        self.assertEqual(
            legacy_repository.mark_synced.call_args.args[0],
            ["run-inserted"],
        )

    def test_offline_ingest_rejects_oversize_before_reading(self) -> None:
        from app.scripts import ingest

        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "oversize.scbundle"
            bundle.write_bytes(b"12345")
            settings = SimpleNamespace(
                deployment_role="hub",
                max_sync_bundle_bytes=4,
            )
            with (
                mock.patch.object(ingest, "get_settings", return_value=settings),
                mock.patch.object(Path, "read_bytes") as read_bytes,
            ):
                exit_code = ingest.main([str(bundle)])
        self.assertEqual(exit_code, 2)
        read_bytes.assert_not_called()

    def test_offline_protocol_detector_rejects_compressed_manifest_bomb(self) -> None:
        from app.scripts.ingest import _detect_sync_protocol

        bundle = _zip_bytes(
            {
                "manifest.json": json.dumps(
                    {
                        "protocol": "smart-commissioning-sync",
                        "protocol_version": "2.0",
                        "padding": "x" * 4096,
                    }
                ).encode()
            }
        )
        with self.assertRaises(SyncV2Error):
            _detect_sync_protocol(
                bundle,
                max_items=10,
                max_uncompressed_bytes=1024,
            )

    def test_offline_protocol_detector_rejects_duplicate_manifest(self) -> None:
        from app.scripts.ingest import _detect_sync_protocol

        output = BytesIO()
        payload = b'{"bundle_format_version":1,"schema_version":2}'
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", payload)
                archive.writestr("manifest.json", payload)
        with self.assertRaises(SyncV2Error):
            _detect_sync_protocol(
                output.getvalue(),
                max_items=10,
                max_uncompressed_bytes=1024,
            )

    def test_offline_protocol_detector_allows_bounded_authority_members(self) -> None:
        from app.scripts.ingest import _detect_sync_protocol

        manifest = json.dumps(
            {
                "protocol": "smart-commissioning-sync",
                "protocol_version": "2.0",
            }
        ).encode()
        bounded = _zip_bytes(
            {
                "manifest.json": manifest,
                "items/item.json": b"{}",
                "artifacts/sha256/artifact": b"artifact",
                "authorities/sha256/first.json": b"{}",
                "authorities/sha256/second.json": b"{}",
            }
        )
        self.assertEqual(
            _detect_sync_protocol(
                bounded,
                max_items=1,
                max_uncompressed_bytes=1024,
            ),
            "v2",
        )

        over_limit = _zip_bytes(
            {
                "manifest.json": manifest,
                "items/item.json": b"{}",
                "artifacts/sha256/artifact": b"artifact",
                "authorities/sha256/first.json": b"{}",
                "authorities/sha256/second.json": b"{}",
                "authorities/sha256/third.json": b"{}",
            }
        )
        with self.assertRaises(SyncV2Error):
            _detect_sync_protocol(
                over_limit,
                max_items=1,
                max_uncompressed_bytes=1024,
            )


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            info = ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _read_bundle(bundle: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
    with ZipFile(BytesIO(bundle)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    return manifest, members


def _resign_bundle(
    manifest: dict[str, object],
    members: dict[str, bytes],
    signing_key: SigningKey,
) -> bytes:
    manifest["bundle_id"] = sha256_bytes(
        canonical_json_bytes(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"bundle_id", "signature", "signed_manifest_sha256"}
            }
        )
    )
    signed_body = canonical_json_bytes(
        {key: value for key, value in manifest.items() if key not in {"signature", "signed_manifest_sha256"}}
    )
    manifest["signed_manifest_sha256"] = sha256_bytes(signed_body)
    manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
    return _zip_bytes(
        {
            **members,
            "manifest.json": json.dumps(manifest, indent=2, sort_keys=True).encode(),
        }
    )


def _alter_terminal_summary(
    bundle: bytes,
    signing_key: SigningKey,
    mutate_summary,
) -> bytes:
    manifest, members = _read_bundle(bundle)
    descriptor = manifest["items"][0]
    item_member = descriptor["item_member"]
    item = json.loads(members[item_member])
    terminal_payload = dict(item["result"]["result_payload"])
    summary = dict(terminal_payload["summary"])
    mutate_summary(summary, item)
    terminal_payload["summary"] = summary
    terminal = TerminalResultV1.model_validate(terminal_payload)
    result_sha256 = terminal.sha256()
    item["result"]["summary"] = summary
    item["result"]["result_payload"] = terminal.model_dump(mode="json")
    item["result"]["result_sha256"] = result_sha256
    item["seal"]["result_sha256"] = result_sha256
    item["run"]["result_summary"] = summary
    descriptor["result_sha256"] = result_sha256
    item_bytes = canonical_json_bytes(item)
    item_id = sha256_bytes(f"{descriptor['run_id']}\0{result_sha256}".encode())[:32]
    descriptor["item_id"] = item_id
    descriptor["item_member"] = f"items/{item_id}.json"
    descriptor["item_sha256"] = sha256_bytes(item_bytes)
    members.pop(item_member)
    members[descriptor["item_member"]] = item_bytes
    return _resign_bundle(manifest, members, signing_key)


def _alter_item(bundle: bytes, signing_key: SigningKey, mutate_item) -> bytes:
    manifest, members = _read_bundle(bundle)
    descriptor = manifest["items"][0]
    item_member = descriptor["item_member"]
    item = json.loads(members[item_member])
    mutate_item(item)
    item_bytes = canonical_json_bytes(item)
    descriptor["item_sha256"] = sha256_bytes(item_bytes)
    members[item_member] = item_bytes
    return _resign_bundle(manifest, members, signing_key)


class HubSyncV2ApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
        "DEPLOYMENT_ROLE": "hub",
    }
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.mkdtemp(prefix="sync-v2-api-")
        root = Path(cls._temporary)
        cls.edge_key = SigningKey.generate()
        cls.artifact_key = SigningKey.generate()
        cls.identity = EdgeIdentity(
            edge_id="edge-public-hosted-test",
            public_key_pem=cls.edge_key.public_key_pem(),
            public_key_fingerprint=cls.edge_key.public_key_fingerprint(),
        )
        edge_root = root / "edge-db"
        edge_root.mkdir()
        edge_url = default_sqlite_url(edge_root)
        upgrade_to_head(edge_url)
        cls.edge_engine = create_engine_from_url(edge_url)
        cls.edge_artifacts: dict[str, bytes] = {}

        from app.services import report_artifacts

        cls._artifact_patcher = mock.patch.object(
            report_artifacts,
            "ARTIFACTS_ROOT",
            root / "hub-artifacts",
        )
        cls._artifact_patcher.start()
        super().setUpClass()

        # Hub user-facing routes require a real named principal. The shared
        # bootstrap key deliberately has no global scope outside standalone
        # deployments, while Sync v2 continues to use its separate X-Sync-Key
        # credential below.
        from app.core.auth import hash_api_key
        from app.core.db import get_engine

        cls.hub_admin_key = "hub-user-public-key-0000000000001"
        UserRepository(get_engine()).create_user(
            user_id="user-sync-v2-hub-admin",
            username="sync-v2-hub-admin",
            role="admin",
            api_key_hash=hash_api_key(cls.hub_admin_key),
            created_at=_NOW,
        )
        cls.client.headers["X-API-Key"] = cls.hub_admin_key

        cls.allowed_key = "sync-public-allowed-key-00000001"
        cls.privileged_key = "sync-public-privileged-key-0001"
        cls._provision(
            "credential-allowed",
            cls.allowed_key,
            [("project-alpha", "site-alpha")],
        )
        cls._provision(
            "credential-privileged",
            cls.privileged_key,
            [
                ("project-denied", "site-alpha"),
                ("project-alpha", "site-denied"),
            ],
        )

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        cls.edge_engine.dispose()
        cls._artifact_patcher.stop()
        shutil.rmtree(cls._temporary, ignore_errors=True)

    @classmethod
    def _provision(
        cls,
        credential_id: str,
        raw_key: str,
        scopes: list[tuple[str, str]],
    ) -> None:
        from app.core.db import get_engine
        from app.core.sync_auth import sync_key_sha256

        SyncV2Repository(get_engine()).create_credential(
            credential_id=credential_id,
            edge_id=cls.identity.edge_id,
            api_key_hash=sync_key_sha256(raw_key),
            signing_key_fingerprint=cls.edge_key.public_key_fingerprint(),
            scopes=scopes,
            now=_NOW,
        )

    @classmethod
    def _report(
        cls,
        project_id: str,
        site_id: str,
        marker: str,
        *,
        artifact: bytes | None = None,
        output_format: str = "pdf",
    ) -> str:
        from app.schemas.jobs import ReportRequest
        from app.services.report_artifacts import REPORT_RENDERER_VERSION
        from app.services.report_artifacts import canonical_json_bytes as artifact_json
        from app.services.run_service import RunService

        service = RunService(cls.edge_engine)
        with mock.patch("app.services.run_service.edge_identity", return_value=cls.identity):
            run, _report = service.create_report_run(
                ReportRequest(
                    project_id=project_id,
                    site_id=site_id,
                    report_type="evidence_pack",
                    output_format=output_format,
                    source_run_ids=[],
                    report_title=f"Public sync test {marker}",
                )
            )
        artifact = artifact or f"%PDF-1.4\npublic sync artifact {marker}\n%%EOF\n".encode()
        media_types = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "zip": "application/zip",
        }
        unsigned = {
            "schema_version": "1.1",
            "report_id": run.run_id,
            "snapshot_sha256": run.parameters["report_snapshot_sha256"],
            "file_name": f"public-sync-{marker}.{output_format}",
            "media_type": media_types[output_format],
            "byte_size": len(artifact),
            "renderer_version": REPORT_RENDERER_VERSION,
            "artifact_sha256": sha256_bytes(artifact),
            "artifact_relpath": f"edge-{marker}.{output_format}",
            # Match POST /reports exactly: the signed artifact takes its origin
            # from the RunRecord returned by normal report creation. This keeps
            # the end-to-end accepted receipt sensitive to stale attribution.
            "origin": str(run.edge_id or "api"),
            "signing_key_id": cls.artifact_key.public_key_fingerprint(),
            "signed_at": run.parameters["report_generated_at"],
            "evidence_set_id": run.parameters["evidence_set_id"],
        }
        signed_body = artifact_json(unsigned)
        manifest = {
            **unsigned,
            "signature_algorithm": "ed25519",
            "signature": base64.b64encode(cls.artifact_key.sign(signed_body)).decode("ascii"),
            "public_key_pem": cls.artifact_key.public_key_pem(),
            "signed_manifest_sha256": sha256_bytes(signed_body),
        }
        service.complete_report_run(run.run_id, manifest)
        cls.edge_artifacts[run.run_id] = artifact
        return run.run_id

    @classmethod
    def _bundle(cls, run_ids: list[str]) -> bytes:
        return build_sync_v2_bundle(
            cls.edge_engine,
            run_ids=run_ids,
            signing_key=cls.edge_key,
            edge_identity=cls.identity,
            created_at=_NOW,
            artifact_loader=lambda manifest: cls.edge_artifacts[str(manifest["report_id"])],
        )

    @classmethod
    def _scan_run(
        cls,
        marker: str,
        *,
        import_id: str | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> tuple[str, str, list[dict[str, object]]]:
        authority_rows = rows or [{"IP Address": "192.168.10.20", "Asset ID": f"ahu-{marker}"}]
        authority_id = import_id or f"imp-sync-{marker}"
        digest = canonical_sha256(authority_rows)
        ImportRepository(cls.edge_engine).create(
            import_id=authority_id,
            import_type="ip_register",
            project_id="project-alpha",
            site_id="site-alpha",
            original_filename=f"{marker}.csv",
            stored_file_path=f"imports/{marker}.csv",
            summary={
                "accepted_rows": len(authority_rows),
                "accepted_rows_sha256": digest,
                "authority_schema_version": "1.0",
            },
            accepted_rows=authority_rows,
            created_at=_NOW,
        )
        context = RunContextV1(
            project_id="project-alpha",
            site_id="site-alpha",
            configuration_snapshot={},
            configuration_version="fixture-1",
            imports=({"resource_id": authority_id, "sha256": digest},),
            engine_parameters={
                "scan_contract_v1": {
                    "ip": {
                        "authority": {
                            "import_id": authority_id,
                            "accepted_rows_sha256": digest,
                            "accepted_count": len(authority_rows),
                        }
                    }
                }
            },
            requesting_principal="sync-authority-test",
            application_version="0.1.41",
        )
        lifecycle = RunLifecycleRepository(cls.edge_engine)
        envelope = lifecycle.create_run_with_context(
            job_type="ip_discovery",
            context=context,
            execution_mode="inline",
            edge_id=cls.identity.edge_id,
            now=_NOW,
        )
        owner = f"owner-{marker}"
        lease = lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token=owner,
            lease_seconds=60,
            now=_NOW,
        )
        if lease is None:
            raise AssertionError("scan fixture lease was not acquired")
        outcome = lifecycle.finalize_run(
            envelope.run_id,
            owner,
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary={"marker": marker},
            ),
            now=_NOW,
        )
        if not outcome.applied:
            raise AssertionError("scan fixture did not seal")
        return envelope.run_id, authority_id, authority_rows

    def _post(self, bundle: bytes, raw_key: str = ""):
        return self.client.post(
            "/api/v1/hub/sync/v2/ingest",
            content=bundle,
            headers={
                "Content-Type": "application/vnd.smart-commissioning.sync-v2+zip",
                "X-Sync-Key": raw_key or self.allowed_key,
            },
        )

    def test_capability_path_and_dedicated_auth_boundary(self) -> None:
        capability = self.client.get(
            "/api/v1/hub/sync/capabilities",
            headers={"X-Sync-Key": self.allowed_key},
        )
        self.assertEqual(capability.status_code, 200, capability.text)
        self.assertEqual(capability.json()["preferred_protocol_version"], "2.0")

        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as isolated:
            no_sync_key = isolated.get(
                "/api/v1/hub/sync/capabilities",
                headers={"X-API-Key": _API_KEY},
            )
            sync_key_on_user_route = isolated.get(
                "/api/v1/runs",
                headers={"X-Sync-Key": self.allowed_key},
            )
        self.assertEqual(no_sync_key.status_code, 401)
        self.assertEqual(sync_key_on_user_route.status_code, 401)

    def test_success_exact_download_and_lost_response_retry(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "success")
        bundle = self._bundle([run_id])
        self.assertNotIn(self.allowed_key.encode(), bundle)

        first = self._post(bundle)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["receipts"][0]["class"], "accepted")

        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import ReportEvidenceContract

        with session_factory(get_engine())() as session:
            contract = session.get(ReportEvidenceContract, run_id)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.contract_version, "sealed_v1")
        self.assertEqual(contract.project_id, "project-alpha")
        self.assertEqual(contract.site_id, "site-alpha")

        # Simulate a lost response by ignoring the first body and sending the
        # exact same bytes again. The hub must return byte_identical.
        retry = self._post(bundle)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["receipts"][0]["class"], "byte_identical")
        with session_factory(get_engine())() as session:
            retry_contract = session.get(ReportEvidenceContract, run_id)
        self.assertEqual(retry_contract.contract_version, "sealed_v1")
        self.assertEqual(retry_contract.project_id, "project-alpha")
        self.assertEqual(retry_contract.site_id, "site-alpha")
        self.assertEqual(retry_contract.classified_at, contract.classified_at)

        shared_headers = {"X-API-Key": _API_KEY}
        shared_me = self.client.get("/api/v1/me", headers=shared_headers)
        self.assertEqual(shared_me.status_code, 200, shared_me.text)
        self.assertFalse(shared_me.json()["global_scope"])
        shared_download = self.client.get(
            f"/api/v1/reports/{run_id}/download",
            headers=shared_headers,
        )
        self.assertEqual(shared_download.status_code, 404, shared_download.text)

        named_headers = {"X-API-Key": self.hub_admin_key}
        named_me = self.client.get("/api/v1/me", headers=named_headers)
        self.assertEqual(named_me.status_code, 200, named_me.text)
        self.assertTrue(named_me.json()["global_scope"])
        first_download = self.client.get(
            f"/api/v1/reports/{run_id}/download",
            headers=named_headers,
        )
        second_download = self.client.get(
            f"/api/v1/reports/{run_id}/download",
            headers=named_headers,
        )
        self.assertEqual(first_download.status_code, 200, first_download.text)
        self.assertEqual(first_download.content, self.edge_artifacts[run_id])
        self.assertEqual(first_download.content, second_download.content)

        from app.schemas.jobs import ReportRequest
        from app.services.run_service import RunService

        derived, _summary = RunService(get_engine()).create_report_run(
            ReportRequest(
                project_id="project-alpha",
                site_id="site-alpha",
                report_type="evidence_pack",
                output_format="zip",
                source_run_ids=[run_id],
                report_title="Synchronized report source proof",
            )
        )
        self.assertEqual(derived.parameters["source_run_ids"], [run_id])

    def test_synchronized_manifest_must_own_the_requested_report(self) -> None:
        run_a = self._report("project-alpha", "site-alpha", "sync-owner-a")
        run_b = self._report("project-alpha", "site-alpha", "sync-owner-b")
        accepted = self._post(self._bundle([run_a, run_b]))
        self.assertEqual(accepted.status_code, 200, accepted.text)

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import SyncArtifact
        from sqlalchemy import select, update

        with get_engine().begin() as connection:
            original_manifest_a = dict(
                connection.scalar(select(SyncArtifact.manifest_json).where(SyncArtifact.run_id == run_a))
            )
            manifest_b = dict(connection.scalar(select(SyncArtifact.manifest_json).where(SyncArtifact.run_id == run_b)))
            self.assertEqual(manifest_b["report_id"], run_b)
            connection.execute(
                update(SyncArtifact).where(SyncArtifact.run_id == run_a).values(manifest_json=manifest_b)
            )

        try:
            rejected = self.client.get(f"/api/v1/reports/{run_a}/download")
            # The synchronized ownership conflict is detected before a trustworthy
            # scope can be authorized, so the report remains concealed.
            self.assertEqual(rejected.status_code, 404, rejected.text)
            self.assertNotEqual(rejected.content, self.edge_artifacts[run_b])

            owned = self.client.get(f"/api/v1/reports/{run_b}/download")
            self.assertEqual(owned.status_code, 200, owned.text)
            self.assertEqual(owned.content, self.edge_artifacts[run_b])
        finally:
            with get_engine().begin() as connection:
                connection.execute(
                    update(SyncArtifact).where(SyncArtifact.run_id == run_a).values(manifest_json=original_manifest_a)
                )

    def test_scan_authority_snapshot_is_verified_and_persisted_with_the_run(self) -> None:
        run_id, import_id, rows = self._scan_run("authority-accepted")
        bundle = self._bundle([run_id])

        first = self._post(bundle)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["receipts"][0]["class"], "accepted")

        from app.core.db import get_engine

        imported = ImportRepository(get_engine()).get(import_id)
        self.assertEqual(imported["accepted_rows"], rows)
        self.assertEqual(imported["project_id"], "project-alpha")
        self.assertEqual(imported["site_id"], "site-alpha")
        self.assertIsNotNone(SyncRepository(get_engine()).get_run_for_export(run_id))

        retry = self._post(bundle)
        self.assertEqual(retry.json()["receipts"][0]["class"], "byte_identical")

    def test_missing_or_changed_scan_authority_never_writes_the_run(self) -> None:
        missing_run, missing_import, _rows = self._scan_run("authority-missing")
        manifest, members = _read_bundle(self._bundle([missing_run]))
        authority_member = manifest["authorities"][0]["member"]
        members.pop(authority_member)

        missing = self._post(_zip_bytes({**members, "manifest.json": json.dumps(manifest).encode()}))
        self.assertEqual(missing.json()["receipts"][0]["class"], "partial_bundle")

        changed_run, changed_import, _rows = self._scan_run("authority-changed")
        manifest, members = _read_bundle(self._bundle([changed_run]))
        authority_member = manifest["authorities"][0]["member"]
        snapshot = json.loads(members[authority_member])
        snapshot["accepted_rows"][0]["IP Address"] = "192.168.10.99"
        members[authority_member] = canonical_json_bytes(snapshot)
        changed = self._post(_zip_bytes({**members, "manifest.json": json.dumps(manifest).encode()}))
        self.assertEqual(changed.json()["receipts"][0]["class"], "malformed")

        from app.core.db import get_engine

        hub = get_engine()
        self.assertIsNone(SyncRepository(hub).get_run_for_export(missing_run))
        self.assertIsNone(SyncRepository(hub).get_run_for_export(changed_run))
        with self.assertRaises(FileNotFoundError):
            ImportRepository(hub).get(missing_import)
        with self.assertRaises(FileNotFoundError):
            ImportRepository(hub).get(changed_import)

    def test_conflicting_hub_import_id_fails_closed_before_run_insert(self) -> None:
        run_id, import_id, _rows = self._scan_run("authority-conflict")

        from app.core.db import get_engine

        conflicting_rows = [{"IP Address": "192.168.10.99", "Asset ID": "other"}]
        ImportRepository(get_engine()).create(
            import_id=import_id,
            import_type="ip_register",
            project_id="project-alpha",
            site_id="site-alpha",
            original_filename="conflict.csv",
            stored_file_path="imports/conflict.csv",
            summary={"accepted_rows": 1},
            accepted_rows=conflicting_rows,
            created_at=_NOW,
        )

        response = self._post(self._bundle([run_id]))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "conflict")
        self.assertIsNone(SyncRepository(get_engine()).get_run_for_export(run_id))

    def test_receipt_free_report_without_contract_fails_closed(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "v1-identical")
        exported = SyncRepository(self.edge_engine).get_run_for_export(run_id)
        self.assertIsNotNone(exported)
        from app.core.db import get_engine

        SyncRepository(get_engine()).insert_run_record(
            run=exported["run"],
            issues=exported["issues"],
            devices=exported["devices"],
            points=exported["points"],
            topics=exported["topics"],
            edge_id=self.identity.edge_id,
            result=exported["result"],
            seal=exported["seal"],
        )

        response = self._post(self._bundle([run_id]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "conflict")
        self.assertEqual(response.json()["acknowledged_run_ids"], [])
        artifact = SyncV2Repository(get_engine()).get_artifact(run_id)
        self.assertIsNone(artifact)
        download = self.client.get(f"/api/v1/reports/{run_id}/download")
        self.assertEqual(download.status_code, 404, download.text)

    def test_scope_denials_are_generic_before_and_after_run_exists(self) -> None:
        denied_run = self._report("project-denied", "site-alpha", "denied-project")
        denied_bundle = self._bundle([denied_run])
        before = self._post(denied_bundle)
        self.assertEqual(before.json()["receipts"][0]["class"], "unauthorized")

        inserted = self._post(denied_bundle, self.privileged_key)
        self.assertEqual(inserted.json()["receipts"][0]["class"], "accepted")
        after = self._post(denied_bundle)
        self.assertEqual(before.json(), after.json())

        denied_site = self._report("project-alpha", "site-denied", "denied-site")
        site_response = self._post(self._bundle([denied_site]))
        self.assertEqual(site_response.json()["receipts"][0]["class"], "unauthorized")
        self.assertEqual(
            set(site_response.json()["receipts"][0]),
            set(before.json()["receipts"][0]),
        )

    def test_credential_revocation_is_rechecked_inside_item_ingest(self) -> None:
        """A credential revoked after auth/preflight cannot commit the item."""
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import SyncCredential

        run_id = self._report("project-alpha", "site-alpha", "revoked-during-ingest")
        original_scope_allows = SyncV2Repository.scope_allows

        def revoke_after_preflight(
            repository: SyncV2Repository,
            credential_id: str,
            project_id: str,
            site_id: str,
        ) -> bool:
            allowed = original_scope_allows(
                repository,
                credential_id,
                project_id,
                site_id,
            )
            if allowed and credential_id == "credential-allowed":
                with session_factory(get_engine()).begin() as session:
                    credential = session.get(SyncCredential, credential_id)
                    self.assertIsNotNone(credential)
                    credential.is_active = False
            return allowed

        try:
            with mock.patch.object(
                SyncV2Repository,
                "scope_allows",
                revoke_after_preflight,
            ):
                response = self._post(self._bundle([run_id]))
        finally:
            with session_factory(get_engine()).begin() as session:
                credential = session.get(SyncCredential, "credential-allowed")
                self.assertIsNotNone(credential)
                credential.is_active = True

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "unauthorized")
        self.assertIsNone(SyncRepository(get_engine()).get_run_for_export(run_id))

    def test_scope_revocation_is_rechecked_inside_item_ingest(self) -> None:
        """An exact scope removed after preflight cannot commit the item."""
        from app.core.db import get_engine
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import SyncCredentialScope

        run_id = self._report("project-alpha", "site-alpha", "scope-revoked-during-ingest")
        original_scope_allows = SyncV2Repository.scope_allows
        scope_key = {
            "credential_id": "credential-allowed",
            "project_id": "project-alpha",
            "site_id": "site-alpha",
        }

        def revoke_after_preflight(
            repository: SyncV2Repository,
            credential_id: str,
            project_id: str,
            site_id: str,
        ) -> bool:
            allowed = original_scope_allows(
                repository,
                credential_id,
                project_id,
                site_id,
            )
            if allowed and credential_id == "credential-allowed":
                with session_factory(get_engine()).begin() as session:
                    scope = session.get(SyncCredentialScope, scope_key)
                    self.assertIsNotNone(scope)
                    session.delete(scope)
            return allowed

        try:
            with mock.patch.object(
                SyncV2Repository,
                "scope_allows",
                revoke_after_preflight,
            ):
                response = self._post(self._bundle([run_id]))
        finally:
            with session_factory(get_engine()).begin() as session:
                if session.get(SyncCredentialScope, scope_key) is None:
                    session.add(SyncCredentialScope(**scope_key))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "unauthorized")
        self.assertIsNone(SyncRepository(get_engine()).get_run_for_export(run_id))

    def test_mixed_receipts_advance_independently(self) -> None:
        allowed = self._report("project-alpha", "site-alpha", "mixed-allowed")
        denied = self._report("project-denied", "site-alpha", "mixed-denied")
        response = self._post(self._bundle([allowed, denied]))
        self.assertEqual(response.status_code, 200, response.text)
        classes = {receipt["run_id"]: receipt["class"] for receipt in response.json()["receipts"]}
        self.assertEqual(classes[allowed], "accepted")
        self.assertEqual(classes[denied], "unauthorized")
        self.assertEqual(response.json()["acknowledged_run_ids"], [allowed])

    def test_artifact_failure_receipts_and_interrupted_retry(self) -> None:
        hash_run = self._report("project-alpha", "site-alpha", "bad-hash")
        hash_bundle = self._bundle([hash_run])
        manifest, members = _read_bundle(hash_bundle)
        artifact_member = manifest["items"][0]["artifact_member"]
        changed = bytearray(members[artifact_member])
        changed[0] ^= 0x01
        members[artifact_member] = bytes(changed)
        bad_hash = self._post(_zip_bytes({**members, "manifest.json": json.dumps(manifest).encode()}))
        self.assertEqual(bad_hash.json()["receipts"][0]["class"], "artifact_hash_failed")

        size_run = self._report("project-alpha", "site-alpha", "bad-size")
        size_bundle = self._bundle([size_run])
        manifest, members = _read_bundle(size_bundle)
        artifact_member = manifest["items"][0]["artifact_member"]
        members[artifact_member] += b"x"
        bad_size = self._post(_zip_bytes({**members, "manifest.json": json.dumps(manifest).encode()}))
        self.assertEqual(bad_size.json()["receipts"][0]["class"], "artifact_size_failed")

        partial_run = self._report("project-alpha", "site-alpha", "partial")
        original = self._bundle([partial_run])
        manifest, members = _read_bundle(original)
        members.pop(manifest["items"][0]["artifact_member"])
        partial = self._post(_zip_bytes({**members, "manifest.json": json.dumps(manifest).encode()}))
        self.assertEqual(partial.json()["receipts"][0]["class"], "partial_bundle")
        retry = self._post(original)
        self.assertEqual(retry.json()["receipts"][0]["class"], "accepted")

    def test_duplicate_manifest_json_and_scalar_coercion_fail_closed(self) -> None:
        duplicate_run = self._report("project-alpha", "site-alpha", "duplicate-json")
        manifest, members = _read_bundle(self._bundle([duplicate_run]))
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        duplicate_manifest = b'{"protocol":"smart-commissioning-sync",' + manifest_bytes[1:]
        duplicate = self._post(_zip_bytes({**members, "manifest.json": duplicate_manifest}))
        self.assertEqual(duplicate.status_code, 400, duplicate.text)

        coercion_run = self._report("project-alpha", "site-alpha", "scalar-coercion")
        manifest, members = _read_bundle(self._bundle([coercion_run]))
        manifest["items"][0]["artifact_size"] = str(manifest["items"][0]["artifact_size"])
        coercion = self._post(_resign_bundle(manifest, members, self.edge_key))
        self.assertEqual(coercion.status_code, 400, coercion.text)

    def test_missing_artifact_and_manifest_signature_receipts(self) -> None:
        missing_run = self._report("project-alpha", "site-alpha", "missing")
        missing_bundle = self._bundle([missing_run])
        manifest, members = _read_bundle(missing_bundle)
        descriptor = manifest["items"][0]
        members.pop(descriptor["artifact_member"])
        descriptor["artifact_member"] = None
        descriptor["artifact_sha256"] = None
        descriptor["artifact_size"] = None
        missing = self._post(_resign_bundle(manifest, members, self.edge_key))
        self.assertEqual(missing.json()["receipts"][0]["class"], "missing_artifact")

        signature_run = self._report("project-alpha", "site-alpha", "bad-signature")
        signature_bundle = self._bundle([signature_run])

        def corrupt_signature(summary, item) -> None:
            artifact_manifest = dict(summary["artifact_manifest"])
            artifact_manifest["signature"] = base64.b64encode(b"invalid").decode()
            summary["artifact_manifest"] = artifact_manifest
            item["artifact_manifest"] = artifact_manifest

        invalid = _alter_terminal_summary(signature_bundle, self.edge_key, corrupt_signature)
        signature = self._post(invalid)
        self.assertEqual(
            signature.json()["receipts"][0]["class"],
            "manifest_signature_failed",
        )

    def test_report_parameter_snapshot_hash_tamper_is_malformed(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "snapshot-tamper")
        bundle = self._bundle([run_id])

        def change_snapshot_hash(item) -> None:
            item["run"]["parameters"]["report_snapshot_sha256"] = "0" * 64

        response = self._post(_alter_item(bundle, self.edge_key, change_snapshot_hash))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "malformed")

    def test_valid_artifact_signature_cannot_claim_another_origin(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "origin-tamper")
        bundle = self._bundle([run_id])
        from app.services.report_artifacts import canonical_json_bytes as artifact_json

        def change_origin(summary, item) -> None:
            artifact_manifest = dict(summary["artifact_manifest"])
            artifact_manifest["origin"] = "edge-other-tenant"
            unsigned = {
                key: value
                for key, value in artifact_manifest.items()
                if key
                not in {
                    "signature_algorithm",
                    "signature",
                    "public_key_pem",
                    "signed_manifest_sha256",
                }
            }
            signed_body = artifact_json(unsigned)
            artifact_manifest["signature"] = base64.b64encode(self.artifact_key.sign(signed_body)).decode()
            artifact_manifest["signed_manifest_sha256"] = sha256_bytes(signed_body)
            summary["artifact_manifest"] = artifact_manifest
            item["artifact_manifest"] = artifact_manifest

        response = self._post(_alter_terminal_summary(bundle, self.edge_key, change_origin))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "malformed")

    def test_secret_variants_and_certificate_artifact_never_cross(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "secret-variant")
        bundle = self._bundle([run_id])

        def add_secret(summary, _item) -> None:
            summary["refreshToken"] = "sentinel-never-export"

        response = self._post(_alter_terminal_summary(bundle, self.edge_key, add_secret))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "malformed")
        self.assertNotIn("sentinel-never-export", response.text)

        artifact_run = self._report(
            "project-alpha",
            "site-alpha",
            "certificate-artifact",
        )
        self.edge_artifacts[artifact_run] = b"-----BEGIN CERTIFICATE-----\nforbidden\n-----END CERTIFICATE-----"
        with self.assertRaises(SyncV2Error):
            self._bundle([artifact_run])

    def test_hub_rejects_nested_certificate_artifact_from_a_signed_edge(self) -> None:
        nested = BytesIO()
        with ZipFile(nested, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "word/document.xml",
                b"<w:t>-----BEGIN CERTIFICATE-----\nforbidden\n-----END CERTIFICATE-----</w:t>",
            )
        run_id = self._report(
            "project-alpha",
            "site-alpha",
            "nested-certificate",
            artifact=nested.getvalue(),
            output_format="docx",
        )
        # Model a compromised edge that can sign a valid bundle but bypasses its
        # own preflight. The hub remains an independent secret boundary.
        with mock.patch("smart_commissioning_core.sync_v2._assert_no_forbidden_artifact_material"):
            crafted = self._bundle([run_id])
        response = self._post(crafted)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["receipts"][0]["class"], "malformed")
        self.assertFalse(response.json()["receipts"][0]["acknowledged"])

        from app.core.db import get_engine

        self.assertIsNone(SyncV2Repository(get_engine()).get_artifact(run_id))

    def test_immutable_conflict_does_not_write_artifact(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "conflict")
        original = self._bundle([run_id])
        self.assertEqual(self._post(original).json()["receipts"][0]["class"], "accepted")

        def add_conflict(summary, _item) -> None:
            summary["conflict_marker"] = "different-terminal-digest"

        conflict_bundle = _alter_terminal_summary(original, self.edge_key, add_conflict)
        with mock.patch("app.services.sync_v2_service.store_content_addressed_artifact") as store:
            conflict = self._post(conflict_bundle)
        self.assertEqual(conflict.json()["receipts"][0]["class"], "conflict")
        store.assert_not_called()

    def test_concurrent_insert_integrity_error_returns_retryable_receipt(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "insert-race")
        bundle = self._bundle([run_id])
        with mock.patch.object(
            SyncV2Repository,
            "ingest_verified_item",
            side_effect=IntegrityError("insert", {}, RuntimeError("race")),
        ):
            response = self._post(bundle)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()["receipts"][0]
        self.assertEqual(receipt["class"], "partial_bundle")
        self.assertTrue(receipt["retryable"])
        self.assertFalse(receipt["acknowledged"])
        retry = self._post(bundle)
        self.assertEqual(retry.json()["receipts"][0]["class"], "accepted")

    def test_new_report_run_is_sealed_to_its_complete_snapshot(self) -> None:
        run_id = self._report("project-alpha", "site-alpha", "seal")
        from smart_commissioning_core.db.engine import session_factory

        with session_factory(self.edge_engine)() as session:
            seal = session.get(RunSeal, run_id)
        self.assertIsNotNone(seal)
        from app.services.run_service import RunService

        run = RunService(self.edge_engine).get_run(run_id)
        self.assertEqual(seal.context_sha256, run.parameters["report_snapshot_sha256"])
