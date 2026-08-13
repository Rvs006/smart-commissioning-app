import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from alembic import command
from pydantic import ValidationError
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.models import Run
from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from smart_commissioning_core.integrity import SigningKey, sha256_bytes
from smart_commissioning_core.run_context import RunContextV1, canonical_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from smart_commissioning_core.sync_identity import EdgeIdentity
from smart_commissioning_core.sync_v2 import (
    RECEIPT_CLASSES,
    SyncV2Descriptor,
    SyncV2Error,
    SyncV2Receipt,
    build_sync_v2_bundle,
    canonical_json_bytes,
    open_sync_v2_bundle,
    receipt_dict,
    validate_sync_v2_item,
)
from smart_commissioning_core.sync_v2 import (
    _assert_no_forbidden_artifact_material as assert_no_forbidden_artifact_material,
)
from smart_commissioning_core.sync_v2 import (
    _assert_no_secret_material as assert_no_secret_material,
)
from sqlalchemy import inspect

_NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _context(project_id: str, site_id: str) -> RunContextV1:
    return RunContextV1.model_validate(
        {
            "project_id": project_id,
            "site_id": site_id,
            "configuration_snapshot": {"profile": {"version": 3}},
            "configuration_version": 3,
            "registers": [],
            "imports": [],
            "schema_versions": {"context": "1.0"},
            "engine_parameters": {"capture_seconds": 30},
            "network_interface": "192.0.2.10/24",
            "connection_settings": {"private_key": "secret://test-key-v3"},
            "secret_references": {
                "configuration.mqtt.private_key": {
                    "reference": "secret://test-key-v3",
                    "version": "3",
                }
            },
            "requesting_principal": "test-principal",
            "application_version": "0.1.28",
        }
    )


class SyncV2CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine_from_url(default_sqlite_url(Path(temporary.name)))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.lifecycle = RunLifecycleRepository(self.engine)
        self.key = SigningKey.generate()
        self.identity = EdgeIdentity(
            edge_id="edge-public-test",
            public_key_pem=self.key.public_key_pem(),
            public_key_fingerprint=self.key.public_key_fingerprint(),
        )

    def _sealed_run(
        self,
        marker: str,
        *,
        summary: dict[str, object] | None = None,
    ) -> str:
        envelope = self.lifecycle.create_run_with_context(
            job_type="mqtt_discovery",
            context=_context("project-public", f"site-{marker}"),
            execution_mode="inline",
            edge_id=self.identity.edge_id,
            now=_NOW,
        )
        lease = self.lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token=f"owner-{marker}",
            lease_seconds=60,
            now=_NOW,
        )
        self.assertIsNotNone(lease)
        outcome = self.lifecycle.finalize_run(
            envelope.run_id,
            f"owner-{marker}",
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary=summary or {"marker": marker},
            ),
            now=_NOW,
        )
        self.assertTrue(outcome.applied)
        return envelope.run_id

    def _bundle(self, run_ids: list[str]) -> bytes:
        return build_sync_v2_bundle(
            self.engine,
            run_ids=run_ids,
            signing_key=self.key,
            edge_identity=self.identity,
            created_at=_NOW,
            artifact_loader=lambda _manifest: b"unused",
        )

    def _sealed_scan_run(
        self,
        marker: str,
        *,
        import_id: str,
        rows: list[dict[str, object]],
    ) -> str:
        digest = canonical_sha256(rows)
        ImportRepository(self.engine).create(
            import_id=import_id,
            import_type="ip_register",
            project_id="project-public",
            site_id="site-authority",
            original_filename=f"{marker}.csv",
            stored_file_path=f"imports/{marker}.csv",
            summary={
                "accepted_rows": len(rows),
                "accepted_rows_sha256": digest,
                "authority_schema_version": "1.0",
            },
            accepted_rows=rows,
            created_at=_NOW,
        )
        context = RunContextV1(
            project_id="project-public",
            site_id="site-authority",
            configuration_snapshot={},
            configuration_version="fixture-1",
            imports=({"resource_id": import_id, "sha256": digest},),
            engine_parameters={
                "scan_contract_v1": {
                    "ip": {
                        "authority": {
                            "import_id": import_id,
                            "accepted_rows_sha256": digest,
                            "accepted_count": len(rows),
                        }
                    }
                }
            },
            requesting_principal="sync-authority-test",
            application_version="0.1.41",
        )
        envelope = self.lifecycle.create_run_with_context(
            job_type="ip_discovery",
            context=context,
            execution_mode="inline",
            edge_id=self.identity.edge_id,
            now=_NOW,
        )
        lease = self.lifecycle.claim_run(
            envelope.run_id,
            envelope.dispatch_id,
            owner_token=f"owner-{marker}",
            lease_seconds=60,
            now=_NOW,
        )
        self.assertIsNotNone(lease)
        outcome = self.lifecycle.finalize_run(
            envelope.run_id,
            f"owner-{marker}",
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary={"marker": marker},
            ),
            now=_NOW,
        )
        self.assertTrue(outcome.applied)
        return envelope.run_id

    def test_deterministic_bundle_round_trip_binds_result_and_context(self) -> None:
        run_id = self._sealed_run("roundtrip")

        first = self._bundle([run_id])
        second = self._bundle([run_id])
        self.assertEqual(first, second)
        self.assertNotIn(b"owner-roundtrip", first)

        opened = open_sync_v2_bundle(
            first,
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        item = validate_sync_v2_item(opened.members[descriptor.item_member], descriptor)
        self.assertEqual(item["run"]["run_id"], run_id)
        self.assertEqual(item["seal"]["context_sha256"], item["execution_context"]["context_sha256"])

    def test_scan_authority_is_rehashed_and_exported_once_by_content_digest(self) -> None:
        rows = [{"IP Address": "192.168.10.20", "Asset ID": "ahu-1"}]
        first_import_id = "imp-sync-authority-first"
        second_import_id = "imp-sync-authority-second"
        first_run = self._sealed_scan_run(
            "authority-first",
            import_id=first_import_id,
            rows=rows,
        )
        second_run = self._sealed_scan_run(
            "authority-second",
            import_id=second_import_id,
            rows=rows,
        )
        digest = canonical_sha256(rows)

        opened = open_sync_v2_bundle(
            self._bundle([first_run, second_run]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )

        self.assertEqual(len(opened.manifest.authorities), 2)
        self.assertEqual(
            {authority.import_id for authority in opened.manifest.authorities},
            {first_import_id, second_import_id},
        )
        authority = opened.manifest.authorities[0]
        for authority in opened.manifest.authorities:
            self.assertEqual(authority.import_type, "ip_register")
            self.assertEqual(authority.project_id, "project-public")
            self.assertEqual(authority.site_id, "site-authority")
            self.assertEqual(authority.accepted_count, 1)
            self.assertEqual(authority.accepted_rows_sha256, digest)
            self.assertEqual(
                authority.member,
                f"authorities/sha256/{digest}.json",
            )
        self.assertEqual(
            len([name for name in opened.members if name.startswith("authorities/sha256/")]),
            1,
        )
        self.assertEqual(
            json.loads(opened.members[authority.member]),
            {"schema_version": "1.0", "accepted_rows": rows},
        )

    def test_scan_authority_drift_stops_export_before_bundle_bytes_exist(self) -> None:
        rows = [{"IP Address": "192.168.10.20"}]
        import_id = "imp-sync-authority-drift"
        run_id = self._sealed_scan_run(
            "authority-drift",
            import_id=import_id,
            rows=rows,
        )
        factory = session_factory(self.engine)
        from smart_commissioning_core.db.models import ImportRecord

        with factory.begin() as session:
            record = session.get(ImportRecord, import_id)
            self.assertIsNotNone(record)
            record.accepted_rows = [{"IP Address": "192.168.10.99"}]

        with self.assertRaisesRegex(SyncV2Error, "scan authority"):
            self._bundle([run_id])

    def test_bacnet_device_and_point_authorities_are_both_exported(self) -> None:
        devices = [{"Device Instance": "1001", "Asset ID": "ahu-1"}]
        points = [{"Device Instance": "1001", "Object ID": "analogInput:1"}]
        device_digest = canonical_sha256(devices)
        point_digest = canonical_sha256(points)
        imports = ImportRepository(self.engine)
        for import_id, import_type, rows, digest in (
            ("imp-sync-bacnet-devices", "bacnet_register", devices, device_digest),
            ("imp-sync-bacnet-points", "bacnet_points", points, point_digest),
        ):
            imports.create(
                import_id=import_id,
                import_type=import_type,
                project_id="project-public",
                site_id="site-authority",
                original_filename=f"{import_id}.csv",
                stored_file_path=f"imports/{import_id}.csv",
                summary={
                    "accepted_rows": 1,
                    "accepted_rows_sha256": digest,
                    "authority_schema_version": "1.0",
                },
                accepted_rows=rows,
                created_at=_NOW,
            )
        context = RunContextV1(
            project_id="project-public",
            site_id="site-authority",
            configuration_snapshot={},
            configuration_version="fixture-1",
            imports=(
                {"resource_id": "imp-sync-bacnet-devices", "sha256": device_digest},
                {"resource_id": "imp-sync-bacnet-points", "sha256": point_digest},
            ),
            engine_parameters={
                "scan_contract_v1": {
                    "bacnet": {
                        "authorities": {
                            "devices": {
                                "import_id": "imp-sync-bacnet-devices",
                                "accepted_rows_sha256": device_digest,
                                "accepted_count": 1,
                            },
                            "points": {
                                "import_id": "imp-sync-bacnet-points",
                                "accepted_rows_sha256": point_digest,
                                "accepted_count": 1,
                            },
                        }
                    }
                }
            },
            requesting_principal="sync-authority-test",
            application_version="0.1.41",
        )
        envelope = self.lifecycle.create_run_with_context(
            job_type="bacnet_discovery",
            context=context,
            execution_mode="inline",
            edge_id=self.identity.edge_id,
            now=_NOW,
        )
        owner = "owner-bacnet-authorities"
        self.assertIsNotNone(
            self.lifecycle.claim_run(
                envelope.run_id,
                envelope.dispatch_id,
                owner_token=owner,
                lease_seconds=60,
                now=_NOW,
            )
        )
        outcome = self.lifecycle.finalize_run(
            envelope.run_id,
            owner,
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary={"marker": "bacnet-authorities"},
            ),
            now=_NOW,
        )
        self.assertTrue(outcome.applied)

        opened = open_sync_v2_bundle(
            self._bundle([envelope.run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )

        self.assertEqual(
            {authority.import_id: authority.import_type for authority in opened.manifest.authorities},
            {
                "imp-sync-bacnet-devices": "bacnet_register",
                "imp-sync-bacnet-points": "bacnet_points",
            },
        )

    def test_manifest_without_authorities_field_remains_authenticated(self) -> None:
        run_id = self._sealed_run("legacy-no-authorities")
        with ZipFile(BytesIO(self._bundle([run_id]))) as archive:
            members = {name: archive.read(name) for name in archive.namelist() if name != "manifest.json"}
            manifest = json.loads(archive.read("manifest.json"))
        manifest.pop("authorities")
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
        manifest["signature"] = base64.b64encode(self.key.sign(signed_body)).decode("ascii")
        output = BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name, payload in sorted(members.items()):
                archive.writestr(name, payload)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode(),
            )

        opened = open_sync_v2_bundle(
            output.getvalue(),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )

        self.assertIsNone(opened.manifest.authorities)

    def test_secret_sentinel_is_rejected_before_bundle_creation(self) -> None:
        run_id = self._sealed_run("secret", summary={"api_key": "sentinel-never-export"})
        with self.assertRaises(SyncV2Error):
            self._bundle([run_id])

    def test_empty_bundle_cannot_be_built(self) -> None:
        with self.assertRaises(SyncV2Error):
            self._bundle([])

    def test_zip_member_count_is_bounded_before_members_are_read(self) -> None:
        run_id = self._sealed_run("member-cap")
        bundle = self._bundle([run_id])
        source = ZipFile(BytesIO(bundle))
        output = BytesIO()
        with source, ZipFile(output, "w", ZIP_DEFLATED) as writer:
            for name in source.namelist():
                writer.writestr(name, source.read(name))
            writer.writestr("items/00000000000000000000000000000000.json", b"{}")
            writer.writestr("artifacts/sha256/" + ("0" * 64), b"x")
        with self.assertRaises(SyncV2Error):
            open_sync_v2_bundle(
                output.getvalue(),
                expected_edge_id=self.identity.edge_id,
                expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
                max_items=1,
            )

    def test_context_parameter_hash_cannot_disagree_with_validated_context(self) -> None:
        run_id = self._sealed_run("parameter-tamper")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        item = json.loads(opened.members[descriptor.item_member])
        item["run"]["parameters"]["context_sha256"] = "0" * 64
        raw = canonical_json_bytes(item)
        tampered_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(raw)})
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(raw, tampered_descriptor)

    def test_execution_context_wrapper_rejects_ambiguous_extra_fields(self) -> None:
        run_id = self._sealed_run("context-wrapper-tamper")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        item = json.loads(opened.members[descriptor.item_member])
        item["execution_context"]["ignored_extra"] = "ambiguous"
        raw = canonical_json_bytes(item)
        tampered_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(raw)})
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(raw, tampered_descriptor)

    def test_execution_context_tenant_must_match_scoped_run(self) -> None:
        run_id = self._sealed_run("context-tenant-tamper")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        item = json.loads(opened.members[descriptor.item_member])
        item["execution_context"]["context_json"]["project_id"] = "project-other"
        context_hash = RunContextV1.model_validate(item["execution_context"]["context_json"]).sha256()
        item["execution_context"]["context_sha256"] = context_hash
        item["seal"]["context_sha256"] = context_hash
        item["run"]["parameters"]["context_sha256"] = context_hash
        raw = canonical_json_bytes(item)
        tampered_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(raw)})
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(raw, tampered_descriptor)

    def test_wire_scalars_do_not_coerce_strings_or_integers(self) -> None:
        with self.assertRaises(ValidationError):
            SyncV2Descriptor(
                item_id="1" * 32,
                run_id="run-strict",
                project_id="project-public",
                site_id="site-public",
                item_member=f"items/{'1' * 32}.json",
                item_sha256="2" * 64,
                result_sha256="3" * 64,
                artifact_member=f"artifacts/sha256/{'4' * 64}",
                artifact_sha256="4" * 64,
                artifact_size="5",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            SyncV2Receipt.model_validate(
                {
                    "receipt_id": "1" * 64,
                    "item_id": "2" * 32,
                    "run_id": "run-strict",
                    "class": "accepted",
                    "acknowledged": 1,
                    "retryable": False,
                }
            )
        for invalid_receipt_id in ("x" * 64, "1" * 16, "A" * 64):
            with self.subTest(receipt_id=invalid_receipt_id):
                with self.assertRaises(ValidationError):
                    SyncV2Receipt.model_validate(
                        {
                            "receipt_id": invalid_receipt_id,
                            "item_id": "2" * 32,
                            "run_id": "run-strict",
                            "class": "accepted",
                            "acknowledged": True,
                            "retryable": False,
                        }
                    )

    def test_duplicate_and_non_finite_item_json_are_rejected(self) -> None:
        run_id = self._sealed_run("strict-json")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        raw = opened.members[descriptor.item_member]
        duplicate = b'{"schema_version":"2.0",' + raw[1:]
        duplicate_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(duplicate)})
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(duplicate, duplicate_descriptor)

        non_finite = raw.replace(b'"progress_percent":100', b'"progress_percent":NaN')
        self.assertNotEqual(non_finite, raw)
        non_finite_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(non_finite)})
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(non_finite, non_finite_descriptor)

    def test_item_id_must_be_canonical_for_run_and_terminal_digest(self) -> None:
        run_id = self._sealed_run("item-id")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        wrong_item_id = "f" * 32 if descriptor.item_id != "f" * 32 else "e" * 32
        tampered_descriptor = descriptor.model_copy(
            update={
                "item_id": wrong_item_id,
                "item_member": f"items/{wrong_item_id}.json",
            }
        )
        with self.assertRaises(SyncV2Error):
            validate_sync_v2_item(
                opened.members[descriptor.item_member],
                tampered_descriptor,
            )

    def test_run_metadata_must_match_the_sealed_terminal_result(self) -> None:
        run_id = self._sealed_run("terminal-coherence")
        opened = open_sync_v2_bundle(
            self._bundle([run_id]),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        descriptor = opened.manifest.items[0]
        original = json.loads(opened.members[descriptor.item_member])
        mutations = (
            lambda item: item["run"].update({"progress_percent": 7}),
            lambda item: item["run"].update({"error_message": "tampered"}),
            lambda item: item["run"].update({"updated_at": "2026-07-27T08:00:01+00:00"}),
            lambda item: item["result"].update({"created_at": "2026-07-27T08:00:01+00:00"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                item = json.loads(json.dumps(original))
                mutate(item)
                raw = canonical_json_bytes(item)
                tampered_descriptor = descriptor.model_copy(update={"item_sha256": sha256_bytes(raw)})
                with self.assertRaises(SyncV2Error):
                    validate_sync_v2_item(raw, tampered_descriptor)

    def test_secret_key_variants_and_pem_material_are_rejected(self) -> None:
        sensitive_keys = (
            "refresh_token",
            "key_passphrase",
            "client_cert",
            "ca_cert",
            "certificate",
            "apiKey",
            "signing_private_key",
            "password_value",
            "api_key_value",
            "refresh_token_backup",
            "private_key_material",
            "owner_token_copy",
        )
        for index, key in enumerate(sensitive_keys):
            with self.subTest(key=key):
                run_id = self._sealed_run(
                    f"secret-variant-{index}",
                    summary={key: "sentinel-never-export"},
                )
                with self.assertRaises(SyncV2Error):
                    self._bundle([run_id])

        for marker in (
            b"-----BEGIN RSA PRIVATE KEY-----\nsecret",
            b"-----BEGIN EC PRIVATE KEY-----\nsecret",
            b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret",
            b"-----BEGIN CERTIFICATE-----\npublic-but-forbidden",
        ):
            with self.subTest(marker=marker.splitlines()[0]):
                with self.assertRaises(SyncV2Error):
                    assert_no_forbidden_artifact_material(marker)

        assert_no_secret_material(
            {
                "secret_references": {
                    "configuration.mqtt.password": {
                        "reference": "secret://mqtt-password-v4",
                        "version": "4",
                    }
                },
                "api_key_hash": "a" * 64,
                "owner_token_fingerprint": "0123456789abcdef",
            }
        )

    def test_nested_artifact_archives_are_scanned_under_hard_limits(self) -> None:
        nested = BytesIO()
        with ZipFile(nested, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "word/document.xml",
                b"<w:t>-----BEGIN CERTIFICATE-----\nforbidden\n-----END CERTIFICATE-----</w:t>",
            )
        outer = BytesIO()
        with ZipFile(outer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("embedded/report.docx", nested.getvalue())
        self.assertNotIn(b"BEGIN CERTIFICATE", outer.getvalue())
        with self.assertRaises(SyncV2Error):
            assert_no_forbidden_artifact_material(outer.getvalue())

        bounded = BytesIO()
        with ZipFile(bounded, "w", ZIP_DEFLATED) as archive:
            archive.writestr("first.txt", b"safe")
            archive.writestr("second.txt", b"safe")
        with self.assertRaises(SyncV2Error):
            assert_no_forbidden_artifact_material(
                bounded.getvalue(),
                max_archive_members=1,
            )
        with self.assertRaises(SyncV2Error):
            assert_no_forbidden_artifact_material(
                bounded.getvalue(),
                max_archive_uncompressed_bytes=4,
            )

    def test_only_acknowledged_receipt_classes_advance_each_watermark(self) -> None:
        run_ids = [self._sealed_run(f"receipt-{index}") for index, _ in enumerate(RECEIPT_CLASSES)]
        opened = open_sync_v2_bundle(
            self._bundle(run_ids),
            expected_edge_id=self.identity.edge_id,
            expected_signing_key_fingerprint=self.key.public_key_fingerprint(),
        )
        repository = SyncV2Repository(self.engine)
        factory = session_factory(self.engine)
        for receipt_class, descriptor in zip(sorted(RECEIPT_CLASSES), opened.manifest.items, strict=True):
            descriptor_dict = descriptor.model_dump(mode="json")
            applied = repository.apply_delivery_receipts(
                [
                    receipt_dict(
                        receipt_id=(receipt_class.encode().hex() + ("0" * 64))[:64],
                        descriptor=descriptor,
                        receipt_class=receipt_class,
                    )
                ],
                {descriptor.item_id: descriptor_dict},
                now=_NOW,
            )
            with factory() as session:
                synced_at = session.get(Run, descriptor.run_id).synced_at
            if receipt_class in {"accepted", "byte_identical"}:
                self.assertEqual(applied, [descriptor.run_id])
                self.assertIsNotNone(synced_at)
            else:
                self.assertEqual(applied, [])
                self.assertIsNone(synced_at)


class SyncV2MigrationTests(unittest.TestCase):
    def test_additive_upgrade_and_rollback_preserve_lifecycle_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            upgrade_to_head(url)
            engine = create_engine_from_url(url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertTrue(
                    {
                        "sync_credentials",
                        "sync_credential_scopes",
                        "sync_artifacts",
                        "sync_receipts",
                        "sync_delivery_state",
                    }.issubset(tables)
                )
            finally:
                engine.dispose()

            command.downgrade(build_alembic_config(url), "f6a7b8c9d0e1")
            engine = create_engine_from_url(url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertIn("runs", tables)
                self.assertIn("run_seals", tables)
                self.assertNotIn("sync_credentials", tables)
            finally:
                engine.dispose()
