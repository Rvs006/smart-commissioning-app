"""Semantic backup integrity checks for frozen scan authorities."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import tempfile
import unittest
import warnings
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from app.services.backup_service import (
    BackupError,
    BackupSources,
    RestoreTarget,
    create_backup_bundle,
    restore_backup_bundle,
    verify_bundle,
)
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
)
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.integrity import SigningKey, sha256_bytes
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import RunContextV1, canonical_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1

_IMPORT_ID = "scan-authority-import"
_PROJECT_ID = "backup-project"
_SITE_ID = "backup-site"
_CREATED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _make_engine(runtime_root: Path):
    database_url = default_sqlite_url(runtime_root)
    upgrade_to_head(database_url)
    return create_engine_from_url(database_url), database_url


class BackupScanAuthorityIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.runtime_root = Path(self._temporary.name) / "runtime"
        self.runtime_root.mkdir()
        self.engine, self.database_url = _make_engine(self.runtime_root)
        self.addCleanup(self.engine.dispose)
        self.rows = [{"IP Address": "192.168.10.20", "Asset ID": "ahu-1"}]
        self.digest = canonical_sha256(self.rows)

        ImportRepository(self.engine).create(
            import_id=_IMPORT_ID,
            import_type="ip_register",
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            original_filename="ip-register.csv",
            stored_file_path=str(self.runtime_root / "imports" / "ip-register.csv"),
            summary={
                "accepted_rows": len(self.rows),
                "rejected_rows": 0,
                "file_sha256": "a" * 64,
                "accepted_rows_sha256": self.digest,
            },
            accepted_rows=self.rows,
            created_at=_CREATED_AT,
        )
        context = RunContextV1(
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            configuration_snapshot={},
            configuration_version="fixture-1",
            imports=({"resource_id": _IMPORT_ID, "sha256": self.digest},),
            engine_parameters={
                "dry_run": True,
                "scan_contract_v1": {
                    "resource_keys": [],
                    "ip": {
                        "authority": {
                            "import_id": _IMPORT_ID,
                            "accepted_rows_sha256": self.digest,
                            "accepted_count": len(self.rows),
                        }
                    },
                },
            },
            requesting_principal="backup-test",
            application_version="0.1.41",
        )
        RunLifecycleRepository(self.engine).create_run_with_context(
            job_type="ip_discovery",
            context=context,
            run_id="run_backup_authority",
            dispatch_id="dispatch_backup_authority",
            now=_CREATED_AT,
        )

    def _sources(self) -> BackupSources:
        return BackupSources(database_url=self.database_url)

    def _valid_bundle(self) -> bytes:
        return create_backup_bundle(
            self._sources(),
            created_at=_CREATED_AT,
            signing_key=SigningKey.generate(),
        )

    @staticmethod
    def _resign_bundle(
        bundle: bytes,
        *,
        manifest_updates: dict[str, object] | None = None,
        additional_members: dict[str, bytes] | None = None,
        removed_members: set[str] | None = None,
    ) -> bytes:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = {
                info.filename: archive.read(info) for info in archive.infolist() if info.filename != "manifest.json"
            }
            manifest = json.loads(archive.read("manifest.json"))

        for name in removed_members or set():
            members.pop(name, None)
            manifest["members"].pop(name, None)
        for name, payload in (additional_members or {}).items():
            members[name] = payload
            manifest["members"][name] = sha256_bytes(payload)
        manifest.update(manifest_updates or {})

        signing_key = SigningKey.generate()
        signed_body = json.dumps(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"signature", "public_key_pem", "public_key_fingerprint"}
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
        manifest["public_key_pem"] = signing_key.public_key_pem()
        manifest["public_key_fingerprint"] = signing_key.public_key_fingerprint()

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))
        return output.getvalue()

    @staticmethod
    def _append_member(bundle: bytes, name: str, payload: bytes) -> bytes:
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(bundle)) as reader,
            zipfile.ZipFile(
                output,
                "w",
                zipfile.ZIP_DEFLATED,
            ) as writer,
        ):
            for info in reader.infolist():
                writer.writestr(info, reader.read(info))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                writer.writestr(name, payload)
        return output.getvalue()

    @staticmethod
    def _resign_with_tampered_database(bundle: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}

        with tempfile.TemporaryDirectory() as database_directory:
            database_path = Path(database_directory) / "tampered.db"
            database_path.write_bytes(members["db/smart_commissioning.db"])
            database = sqlite3.connect(str(database_path))
            try:
                database.execute(
                    "UPDATE import_records SET accepted_rows = ? WHERE import_id = ?",
                    (
                        json.dumps(
                            [
                                {
                                    "IP Address": "192.168.10.99",
                                    "Asset ID": "ahu-1",
                                }
                            ]
                        ),
                        _IMPORT_ID,
                    ),
                )
                database.commit()
            finally:
                database.close()
            members["db/smart_commissioning.db"] = database_path.read_bytes()

        manifest = json.loads(members["manifest.json"])
        manifest["members"]["db/smart_commissioning.db"] = sha256_bytes(members["db/smart_commissioning.db"])
        signing_key = SigningKey.generate()
        signed_body = json.dumps(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"signature", "public_key_pem", "public_key_fingerprint"}
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
        manifest["public_key_pem"] = signing_key.public_key_pem()
        manifest["public_key_fingerprint"] = signing_key.public_key_fingerprint()
        members["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return output.getvalue()

    @staticmethod
    def _signed_database_bundle(database_bytes: bytes) -> bytes:
        signing_key = SigningKey.generate()
        manifest = {
            "bundle_format_version": 1,
            "core_version": "0.1.20-pre-lifecycle",
            "created_at": _CREATED_AT.isoformat(),
            "members": {"db/smart_commissioning.db": sha256_bytes(database_bytes)},
            "signature_algorithm": "ed25519",
            "signature": None,
            "public_key_pem": None,
            "public_key_fingerprint": None,
        }
        signed_body = json.dumps(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"signature", "public_key_pem", "public_key_fingerprint"}
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
        manifest["public_key_pem"] = signing_key.public_key_pem()
        manifest["public_key_fingerprint"] = signing_key.public_key_fingerprint()

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("db/smart_commissioning.db", database_bytes)
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode("utf-8"))
        return output.getvalue()

    def test_create_rejects_tampered_bound_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE import_records SET accepted_rows = ? WHERE import_id = ?",
                (
                    json.dumps([{"IP Address": "192.168.10.99", "Asset ID": "ahu-1"}]),
                    _IMPORT_ID,
                ),
            )

        with self.assertRaisesRegex(BackupError, "scan authority"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_context_json_tamper_with_unchanged_hash(self) -> None:
        with self.engine.begin() as connection:
            raw_context, stored_hash = connection.exec_driver_sql(
                "SELECT context_json, context_sha256 FROM run_execution_contexts WHERE run_id = ?",
                ("run_backup_authority",),
            ).one()
            context_json = json.loads(raw_context)
            context_json["requesting_principal"] = "tampered-principal"
            connection.exec_driver_sql(
                "UPDATE run_execution_contexts SET context_json = ? WHERE run_id = ?",
                (json.dumps(context_json), "run_backup_authority"),
            )
            unchanged_hash = connection.exec_driver_sql(
                "SELECT context_sha256 FROM run_execution_contexts WHERE run_id = ?",
                ("run_backup_authority",),
            ).scalar_one()
        self.assertEqual(unchanged_hash, stored_hash)

        with self.assertRaisesRegex(BackupError, "context.*digest"):
            self._valid_bundle()

    def test_valid_bound_authority_survives_backup_and_restore(self) -> None:
        target_root = Path(self._temporary.name) / "valid-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        restore_backup_bundle(self._valid_bundle(), target)

        restored_engine = create_engine_from_url(f"sqlite:///{target.database_path.as_posix()}")
        self.addCleanup(restored_engine.dispose)
        restored_rows = ImportRepository(restored_engine).get_accepted_rows(_IMPORT_ID)
        self.assertEqual(restored_rows, self.rows)

    def test_create_rejects_missing_bound_import(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM import_records WHERE import_id = ?", (_IMPORT_ID,))

        with self.assertRaisesRegex(BackupError, "missing"):
            self._valid_bundle()

    def test_create_rejects_malformed_bound_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE import_records SET accepted_rows = ? WHERE import_id = ?",
                (json.dumps({"not": "a row list"}), _IMPORT_ID),
            )

        with self.assertRaisesRegex(BackupError, "malformed"):
            self._valid_bundle()

    def test_legacy_context_without_scan_contract_remains_restorable(self) -> None:
        legacy_context = RunContextV1(
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            configuration_snapshot={},
            configuration_version="legacy-fixture",
            imports=({"resource_id": _IMPORT_ID, "sha256": self.digest},),
            engine_parameters={"dry_run": True},
            requesting_principal="legacy-backup-test",
            application_version="0.1.40",
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE run_execution_contexts SET context_json = ?, context_sha256 = ? WHERE run_id = ?",
                (
                    json.dumps(legacy_context.model_dump(mode="json")),
                    legacy_context.sha256(),
                    "run_backup_authority",
                ),
            )
            connection.exec_driver_sql("DELETE FROM import_records WHERE import_id = ?", (_IMPORT_ID,))

        target_root = Path(self._temporary.name) / "legacy-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        restore_backup_bundle(self._valid_bundle(), target)

        self.assertTrue(target.database_path.is_file())

    def test_restore_rejects_resigned_inconsistent_database_before_writes(self) -> None:
        bundle = self._resign_with_tampered_database(self._valid_bundle())
        target_root = Path(self._temporary.name) / "rejected-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "scan authority"):
            restore_backup_bundle(bundle, target)

        self.assertFalse(target_root.exists(), "semantic rejection must precede all writes")

    def test_restore_accepts_signed_pre_lifecycle_database_without_context_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as legacy_directory:
            legacy_path = Path(legacy_directory) / "legacy.db"
            legacy_database = sqlite3.connect(str(legacy_path))
            try:
                legacy_database.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
                legacy_database.execute("INSERT INTO legacy_marker (value) VALUES (?)", ("readable",))
                legacy_database.commit()
            finally:
                legacy_database.close()
            bundle = self._signed_database_bundle(legacy_path.read_bytes())

        target_root = Path(self._temporary.name) / "pre-lifecycle-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        restore_backup_bundle(bundle, target)

        restored_database = sqlite3.connect(str(target.database_path))
        try:
            marker = restored_database.execute("SELECT value FROM legacy_marker").fetchone()
        finally:
            restored_database.close()
        self.assertEqual(marker, ("readable",))

    def test_bundle_rejects_a_duplicate_member_name_before_restore_writes(self) -> None:
        valid = self._valid_bundle()
        with zipfile.ZipFile(io.BytesIO(valid)) as archive:
            database_bytes = archive.read("db/smart_commissioning.db")
        duplicate = self._append_member(
            valid,
            "db/smart_commissioning.db",
            database_bytes,
        )
        target_root = Path(self._temporary.name) / "duplicate-member-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "duplicate"):
            verify_bundle(duplicate)
        with self.assertRaisesRegex(BackupError, "duplicate"):
            restore_backup_bundle(duplicate, target)

        self.assertFalse(target_root.exists())

    def test_bundle_rejects_an_undeclared_recognized_member_before_restore_writes(self) -> None:
        undeclared = self._append_member(
            self._valid_bundle(),
            "secrets/undeclared.pem",
            b"must-not-be-restored",
        )
        target_root = Path(self._temporary.name) / "undeclared-member-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "undeclared"):
            verify_bundle(undeclared)
        with self.assertRaisesRegex(BackupError, "undeclared"):
            restore_backup_bundle(undeclared, target)

        self.assertFalse(target_root.exists())

    def test_restore_rejects_a_resigned_future_bundle_version_before_writes(self) -> None:
        future_version_bundle = self._resign_bundle(
            self._valid_bundle(),
            manifest_updates={"bundle_format_version": 2},
        )
        target_root = Path(self._temporary.name) / "future-version-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "version"):
            verify_bundle(future_version_bundle)
        with self.assertRaisesRegex(BackupError, "version"):
            restore_backup_bundle(future_version_bundle, target)

        self.assertFalse(target_root.exists(), "format rejection must precede all restore writes")

    def test_restore_rejects_a_resigned_unknown_declared_member_before_writes(self) -> None:
        unknown_member_bundle = self._resign_bundle(
            self._valid_bundle(),
            additional_members={"future/state.bin": b"future-state"},
        )
        target_root = Path(self._temporary.name) / "unknown-member-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "unsupported"):
            verify_bundle(unknown_member_bundle)
        with self.assertRaisesRegex(BackupError, "unsupported"):
            restore_backup_bundle(unknown_member_bundle, target)

        self.assertFalse(target_root.exists(), "member rejection must precede all restore writes")

    def test_restore_rejects_noncanonical_or_rooted_member_paths_before_writes(self) -> None:
        invalid_names = (
            "secrets/./key.pem",
            "secrets/sub/../key.pem",
            "/secrets/key.pem",
            "C:/secrets/key.pem",
            r"\\server\share\key.pem",
            "secrets/C:/key.pem",
            r"secrets/\\server\share\key.pem",
        )
        for index, name in enumerate(invalid_names):
            with self.subTest(name=name):
                bundle = self._resign_bundle(
                    self._valid_bundle(),
                    additional_members={name: b"must-not-restore"},
                )
                target_root = Path(self._temporary.name) / f"invalid-member-path-{index}"
                target = RestoreTarget(
                    database_path=target_root / "smart_commissioning.db",
                    secrets_root=target_root / "secrets",
                    imports_files_root=target_root / "imports" / "files",
                )

                with self.assertRaisesRegex(BackupError, "member path"):
                    verify_bundle(bundle)
                with self.assertRaisesRegex(BackupError, "member path"):
                    restore_backup_bundle(bundle, target)

                self.assertFalse(target_root.exists(), "path rejection must precede all restore writes")

    def test_restore_rejects_case_aliases_for_one_canonical_destination_before_writes(self) -> None:
        bundle = self._resign_bundle(
            self._valid_bundle(),
            additional_members={
                "secrets/Signing-Key.pem": b"first",
                "secrets/signing-key.pem": b"second",
            },
        )
        target_root = Path(self._temporary.name) / "case-alias-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "canonical restore destination"):
            verify_bundle(bundle)
        with self.assertRaisesRegex(BackupError, "canonical restore destination"):
            restore_backup_bundle(bundle, target)

        self.assertFalse(target_root.exists(), "alias rejection must precede all restore writes")

    def test_restore_rejects_aliases_across_overlapping_target_roots_before_writes(self) -> None:
        bundle = self._resign_bundle(
            self._valid_bundle(),
            additional_members={
                "secrets/Nested/Key.pem": b"first",
                "imports/files/nested/key.pem": b"second",
            },
        )
        target_root = Path(self._temporary.name) / "overlapping-root-alias-restore"
        shared_root = target_root / "shared"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=shared_root,
            imports_files_root=shared_root,
        )

        with self.assertRaisesRegex(BackupError, "canonical restore destination"):
            restore_backup_bundle(bundle, target)

        self.assertFalse(target_root.exists(), "alias rejection must precede all restore writes")

    def test_restore_preserves_valid_nested_supported_members(self) -> None:
        bundle = self._resign_bundle(
            self._valid_bundle(),
            additional_members={
                "secrets/nested/service/key.pem": b"secret",
                "imports/files/nested/register.csv": b"asset,address\nahu-1,192.0.2.10\n",
                "artifacts/nested/report.zip": b"report",
                "report-signing/nested/key.pem": b"signing",
            },
        )
        target_root = Path(self._temporary.name) / "nested-members-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
            report_artifacts_root=target_root / "artifacts",
            report_signing_root=target_root / "report-signing",
        )

        restore_backup_bundle(bundle, target)

        self.assertEqual((target.secrets_root / "nested" / "service" / "key.pem").read_bytes(), b"secret")
        self.assertEqual(
            (target.imports_files_root / "nested" / "register.csv").read_bytes(),
            b"asset,address\nahu-1,192.0.2.10\n",
        )
        self.assertEqual((target.report_artifacts_root / "nested" / "report.zip").read_bytes(), b"report")
        self.assertEqual((target.report_signing_root / "nested" / "key.pem").read_bytes(), b"signing")

    def test_verify_requires_an_exact_integer_bundle_version(self) -> None:
        for invalid_version in (True, "1", None):
            with self.subTest(bundle_format_version=invalid_version):
                bundle = self._resign_bundle(
                    self._valid_bundle(),
                    manifest_updates={"bundle_format_version": invalid_version},
                )

                with self.assertRaisesRegex(BackupError, "version"):
                    verify_bundle(bundle)

    def test_restore_requires_the_declared_database_member_before_writes(self) -> None:
        bundle = self._resign_bundle(
            self._valid_bundle(),
            removed_members={"db/smart_commissioning.db"},
        )
        target_root = Path(self._temporary.name) / "missing-database-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "database"):
            verify_bundle(bundle)
        with self.assertRaisesRegex(BackupError, "database"):
            restore_backup_bundle(bundle, target)

        self.assertFalse(target_root.exists(), "database rejection must precede all restore writes")

    def test_restore_keeps_rejecting_traversal_inside_a_supported_namespace(self) -> None:
        traversal_bundle = self._resign_bundle(
            self._valid_bundle(),
            additional_members={"secrets/../../../escape.pem": b"must-not-escape"},
        )
        target_root = Path(self._temporary.name) / "traversal-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "member path"):
            verify_bundle(traversal_bundle)
        with self.assertRaisesRegex(BackupError, "member path"):
            restore_backup_bundle(traversal_bundle, target)

        self.assertFalse(target_root.exists(), "path rejection must precede all restore writes")
        self.assertFalse((target_root.parent / "escape.pem").exists())


class BackupSealedRunIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.runtime_root = Path(self._temporary.name) / "runtime"
        self.runtime_root.mkdir()
        self.engine, self.database_url = _make_engine(self.runtime_root)
        self.addCleanup(self.engine.dispose)
        self.run_id = "run_backup_sealed_integrity"
        self.dispatch_id = "dispatch_backup_sealed_integrity"
        context = RunContextV1(
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            configuration_snapshot={},
            configuration_version="fixture-1",
            engine_parameters={"dry_run": True},
            requesting_principal="backup-seal-test",
            application_version="0.1.41",
        )
        lifecycle = RunLifecycleRepository(self.engine)
        lifecycle.create_run_with_context(
            job_type="ip_discovery",
            context=context,
            run_id=self.run_id,
            dispatch_id=self.dispatch_id,
            now=_CREATED_AT,
        )
        lease = lifecycle.claim_run(
            self.run_id,
            self.dispatch_id,
            now=_CREATED_AT,
            owner_token="backup-seal-owner",
        )
        assert lease is not None
        finalized = lifecycle.finalize_run(
            self.run_id,
            lease.owner_token,
            TerminalResultV1(
                status="succeeded",
                stage="engine_complete",
                summary={"devices_found": 1},
                issues=(
                    ValidationIssueRecord(
                        issue_id="backup-issue-1",
                        asset_id="ahu-1",
                        issue_type="unexpected-port",
                        severity="high",
                        description="Unexpected service observed.",
                        last_seen_at=_CREATED_AT,
                    ).model_dump(mode="json"),
                ),
                devices=(
                    {
                        "project_id": _PROJECT_ID,
                        "site_id": _SITE_ID,
                        "address": "192.0.2.10",
                        "device_type": "ip_host",
                        "name": "ahu-1",
                        "attributes": {"open_ports": [80]},
                    },
                ),
                points=(
                    {
                        "device_ref": "192.0.2.10",
                        "point_id": "supply-temp",
                        "point_name": "Supply temperature",
                        "observed_value": {"value": 21.5},
                        "units": "C",
                        "attributes": {},
                    },
                ),
                topics=(
                    {
                        "topic": "/devices/ahu-1/state",
                        "last_payload": {"system": {"operational": True}},
                        "message_count": 2,
                        "attributes": {"qos": 1},
                    },
                ),
            ),
            now=_CREATED_AT,
        )
        self.assertTrue(finalized.applied)

    def _sources(self) -> BackupSources:
        return BackupSources(database_url=self.database_url)

    @staticmethod
    def _resign_database_update(
        bundle: bytes,
        statement: str,
        parameters: tuple[object, ...],
    ) -> bytes:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
        with tempfile.TemporaryDirectory() as database_directory:
            database_path = Path(database_directory) / "tampered.db"
            database_path.write_bytes(members["db/smart_commissioning.db"])
            database = sqlite3.connect(str(database_path))
            try:
                database.execute(statement, parameters)
                database.commit()
            finally:
                database.close()
            members["db/smart_commissioning.db"] = database_path.read_bytes()

        manifest = json.loads(members["manifest.json"])
        manifest["members"]["db/smart_commissioning.db"] = sha256_bytes(members["db/smart_commissioning.db"])
        signing_key = SigningKey.generate()
        signed_body = json.dumps(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"signature", "public_key_pem", "public_key_fingerprint"}
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
        manifest["public_key_pem"] = signing_key.public_key_pem()
        manifest["public_key_fingerprint"] = signing_key.public_key_fingerprint()
        members["manifest.json"] = json.dumps(manifest, sort_keys=True).encode("utf-8")

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return output.getvalue()

    def _seed_report_evidence(self) -> str:
        from smart_commissioning_core.db.db_run_store import DbRunStore
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Run, RunResult, RunSeal

        metadata = {
            "output_format": "pdf",
            "report_type": "ip_discovery",
            "report_title_custom": False,
            "report_title": "IP discovery report",
            "report_generated_at": _CREATED_AT.isoformat(),
            "renderer_version": "0.1.40",
            "evidence_set_id": "backup-evidence-set",
            "udmi_report_variant": "technical",
        }
        snapshot = {
            "schema_version": "2.0",
            "project_id": _PROJECT_ID,
            "site_id": _SITE_ID,
            "report_type": "ip_discovery",
            "output_format": "pdf",
            "source_run_ids": [],
            "renderer_version": "0.1.40",
            "evidence_set_id": "backup-evidence-set",
            "report_metadata": metadata,
            "source_run_snapshots": [],
            "source_run_seals": {},
            "source_discovery_snapshots": {},
            "udmi_scope": None,
        }
        snapshot_digest = canonical_sha256(snapshot)
        report_id = DbRunStore(self.engine).create_run(
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            job_type="report_generation",
            parameters={
                **metadata,
                "source_run_ids": [],
                "source_run_snapshots": [],
                "source_run_seals": {},
                "source_discovery_snapshots": {},
                "report_snapshot_v2": snapshot,
                "report_snapshot_sha256": snapshot_digest,
            },
        )["run_id"]
        manifest = {
            "report_id": report_id,
            "snapshot_sha256": snapshot_digest,
            "file_name": "ip-discovery-report.pdf",
            "media_type": "application/pdf",
            "byte_size": 123,
            "renderer_version": "0.1.40",
            "artifact_sha256": "a" * 64,
            "artifact_relpath": f"{report_id}-{'a' * 64}.pdf",
            "evidence_set_id": "backup-evidence-set",
        }
        terminal = TerminalResultV1(
            status="succeeded",
            stage="report_ready",
            summary={"artifact_manifest": manifest},
        )
        result_digest = terminal.sha256()
        with session_factory(self.engine).begin() as session:
            run = session.get(Run, report_id)
            assert run is not None
            run.status = terminal.status
            run.stage = terminal.stage
            run.progress_percent = 100
            run.result_summary = dict(terminal.summary)
            run.error_message = terminal.error_message
            run.result_sha256 = result_digest
            run.terminal_at = _CREATED_AT
            session.add(
                RunResult(
                    run_id=report_id,
                    schema_version=terminal.schema_version,
                    terminal_status=terminal.status,
                    terminal_stage=terminal.stage,
                    summary=dict(terminal.summary),
                    result_payload=terminal.model_dump(mode="json"),
                    result_sha256=result_digest,
                    created_at=_CREATED_AT,
                )
            )
            session.add(
                RunSeal(
                    run_id=report_id,
                    terminal_status=terminal.status,
                    context_sha256=snapshot_digest,
                    result_sha256=result_digest,
                    sealed_at=_CREATED_AT,
                )
            )
        return report_id

    def test_create_rejects_coherently_tampered_sealed_summary_projections(self) -> None:
        from smart_commissioning_core.db.models import Run, RunResult
        from sqlalchemy import update

        tampered_summary = {"devices_found": 999, "tampered": True}
        with self.engine.begin() as connection:
            connection.execute(update(Run).where(Run.id == self.run_id).values(result_summary=tampered_summary))
            connection.execute(
                update(RunResult).where(RunResult.run_id == self.run_id).values(summary=tampered_summary)
            )

        with self.assertRaisesRegex(BackupError, "sealed run"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_terminal_payload_hash_mismatch(self) -> None:
        from smart_commissioning_core.db.models import RunResult
        from sqlalchemy import select, update

        with self.engine.begin() as connection:
            payload = connection.scalar(select(RunResult.result_payload).where(RunResult.run_id == self.run_id))
            tampered = dict(payload)
            tampered["stage"] = "tampered-stage"
            connection.execute(update(RunResult).where(RunResult.run_id == self.run_id).values(result_payload=tampered))

        with self.assertRaisesRegex(BackupError, "canonical digest"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_canonical_context_not_bound_to_its_seal(self) -> None:
        from smart_commissioning_core.db.models import RunExecutionContext
        from sqlalchemy import select, update

        with self.engine.begin() as connection:
            context_json = connection.scalar(
                select(RunExecutionContext.context_json).where(RunExecutionContext.run_id == self.run_id)
            )
            tampered = dict(context_json)
            tampered["requesting_principal"] = "tampered-but-canonical"
            digest = RunContextV1.model_validate(tampered).sha256()
            connection.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == self.run_id)
                .values(context_json=tampered, context_sha256=digest)
            )

        with self.assertRaisesRegex(BackupError, "run seal"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_a_missing_tuple_when_dispatch_proves_modern_lifecycle(self) -> None:
        from smart_commissioning_core.db.models import (
            Run,
            RunExecutionContext,
            RunResult,
            RunSeal,
        )
        from sqlalchemy import delete, update

        with self.engine.begin() as connection:
            for model in (RunExecutionContext, RunResult, RunSeal):
                connection.execute(delete(model).where(model.run_id == self.run_id))
            connection.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(
                    result_sha256=None,
                    terminal_at=None,
                    owner_token=None,
                    claimed_at=None,
                    heartbeat_at=None,
                    lease_expires_at=None,
                    attempt=0,
                    state_version=0,
                )
            )

        with self.assertRaisesRegex(BackupError, "modern lifecycle"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_restore_rejects_resigned_sealed_result_tamper_before_any_target_write(self) -> None:
        from smart_commissioning_core.db.models import RunResult
        from sqlalchemy import select

        with self.engine.connect() as connection:
            payload = connection.scalar(select(RunResult.result_payload).where(RunResult.run_id == self.run_id))
        tampered_payload = dict(payload)
        tampered_payload["stage"] = "tampered-stage"
        bundle = create_backup_bundle(
            self._sources(),
            created_at=_CREATED_AT,
            signing_key=SigningKey.generate(),
        )
        resigned = self._resign_database_update(
            bundle,
            "UPDATE run_results SET result_payload = ? WHERE run_id = ?",
            (json.dumps(tampered_payload), self.run_id),
        )
        target_root = Path(self._temporary.name) / "sealed-reject-restore"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaisesRegex(BackupError, "canonical digest"):
            restore_backup_bundle(resigned, target)

        self.assertFalse(target_root.exists(), "sealed-result rejection must precede all writes")

    def test_create_accepts_the_explicit_report_evidence_seal_contract(self) -> None:
        self._seed_report_evidence()

        bundle = create_backup_bundle(
            self._sources(),
            created_at=_CREATED_AT,
            signing_key=SigningKey.generate(),
        )

        self.assertGreater(len(bundle), 0)

    def test_create_rejects_report_snapshot_tamper_with_updated_superficial_hash(self) -> None:
        from smart_commissioning_core.db.models import Run
        from sqlalchemy import select, update

        report_id = self._seed_report_evidence()
        with self.engine.begin() as connection:
            parameters = connection.scalar(select(Run.parameters).where(Run.id == report_id))
            tampered = dict(parameters)
            snapshot = dict(tampered["report_snapshot_v2"])
            snapshot["tampered"] = True
            tampered["report_snapshot_v2"] = snapshot
            tampered["report_snapshot_sha256"] = canonical_sha256(snapshot)
            connection.execute(update(Run).where(Run.id == report_id).values(parameters=tampered))

        with self.assertRaisesRegex(BackupError, "run seal"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_a_stripped_modern_report_contract(self) -> None:
        from smart_commissioning_core.db.models import Run, RunResult, RunSeal
        from sqlalchemy import delete, select, update

        report_id = self._seed_report_evidence()
        with self.engine.begin() as connection:
            parameters = dict(
                connection.scalar(select(Run.parameters).where(Run.id == report_id))
            )
            parameters.pop("report_snapshot_v2")
            parameters.pop("report_snapshot_sha256")
            connection.execute(delete(RunResult).where(RunResult.run_id == report_id))
            connection.execute(delete(RunSeal).where(RunSeal.run_id == report_id))
            connection.execute(
                update(Run)
                .where(Run.id == report_id)
                .values(
                    parameters=parameters,
                    result_summary={},
                    result_sha256=None,
                    terminal_at=None,
                )
            )

        with self.assertRaisesRegex(BackupError, "report evidence|frozen snapshot|partially present"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_current_schema_with_contract_table_removed(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE report_evidence_contracts")

        with self.assertRaisesRegex(BackupError, "current schema.*missing"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_rejects_modern_report_with_missing_contract_row(self) -> None:
        from smart_commissioning_core.db.models import ReportEvidenceContract
        from sqlalchemy import delete

        report_id = self._seed_report_evidence()
        with self.engine.begin() as connection:
            connection.execute(
                delete(ReportEvidenceContract).where(
                    ReportEvidenceContract.run_id == report_id
                )
            )

        with self.assertRaisesRegex(BackupError, "recognized evidence contract"):
            create_backup_bundle(
                self._sources(),
                created_at=_CREATED_AT,
                signing_key=SigningKey.generate(),
            )

    def test_create_accepts_an_entire_pre_contract_database(self) -> None:
        pre_contract_root = self.runtime_root / "pre-contract"
        pre_contract_root.mkdir()
        database_url = default_sqlite_url(pre_contract_root)
        command.upgrade(build_alembic_config(database_url), "d0e1f2a3b4c5")

        bundle = create_backup_bundle(
            BackupSources(database_url=database_url),
            created_at=_CREATED_AT,
            signing_key=SigningKey.generate(),
        )

        self.assertGreater(len(bundle), 0)


if __name__ == "__main__":
    unittest.main()
