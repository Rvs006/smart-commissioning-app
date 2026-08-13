"""Protected raw-evidence storage and Nmap adapter contracts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.services.raw_evidence_artifacts import (
    RawEvidenceArtifactStore,
    RawEvidenceBackupRecord,
    RawEvidenceIncompleteError,
    RawEvidenceIntegrityError,
    RawEvidenceLimitError,
    RawEvidenceNotFoundError,
    RawEvidenceSecurityError,
)
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import create_engine_from_url, session_factory
from smart_commissioning_core.db.models import Run
from sqlalchemy import update


class _RecordingSecurity:
    def __init__(self) -> None:
        self.directories: list[Path] = []
        self.files: list[Path] = []
        self.reparse_points: set[Path] = set()

    def secure_directory(self, path: Path) -> None:
        self.directories.append(path)

    def secure_file(self, path: Path) -> None:
        self.files.append(path)

    def is_reparse_point(self, path: Path) -> bool:
        return path in self.reparse_points


class RawEvidenceArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.engine = create_engine_from_url(
            f"sqlite:///{(root / 'artifacts.db').as_posix()}"
        )
        Base.metadata.create_all(self.engine)
        self.run = DbRunStore(self.engine).create_run(
            project_id="project-raw",
            site_id="site-raw",
            job_type="ip_discovery",
            parameters={},
        )
        self.security = _RecordingSecurity()
        self.artifact_root = root / "raw-evidence"
        self.store = RawEvidenceArtifactStore(
            self.engine,
            root=self.artifact_root,
            security=self.security,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def test_import_preserves_exact_run_owned_bytes_and_verified_metadata(self) -> None:
        payload = b"<nmaprun><runstats><finished exit='success'/></runstats></nmaprun>"

        descriptor = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=payload,
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=4096,
        )

        loaded, exact_bytes = self.store.read_verified(
            run_id=self.run["run_id"],
            artifact_id=descriptor.artifact_id,
        )
        self.assertEqual(exact_bytes, payload)
        self.assertEqual(loaded, descriptor)
        self.assertEqual(descriptor.project_id, "project-raw")
        self.assertEqual(descriptor.site_id, "site-raw")
        self.assertEqual(descriptor.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(descriptor.size_bytes, len(payload))
        self.assertTrue(descriptor.capture_complete)
        self.assertLessEqual(descriptor.created_at, descriptor.sealed_at)
        self.assertTrue(descriptor.artifact_id.startswith("artifact_"))
        self.assertTrue(self.security.directories)
        self.assertTrue(self.security.files)
        self.assertNotIn(str(self.artifact_root), repr(descriptor))

    def test_incomplete_capture_is_retained_but_cannot_authorize_normalization(self) -> None:
        descriptor = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"<nmaprun>",
            capture_complete=False,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )

        loaded, payload = self.store.read_verified(
            run_id=self.run["run_id"], artifact_id=descriptor.artifact_id
        )
        self.assertEqual(payload, b"<nmaprun>")
        self.assertFalse(loaded.capture_complete)
        with self.assertRaisesRegex(
            RawEvidenceIncompleteError, "cannot authorize normalized claims"
        ):
            self.store.read_for_normalization(
                run_id=self.run["run_id"], artifact_id=descriptor.artifact_id
            )

    def test_byte_limit_fails_without_publishing_partial_metadata_or_file(self) -> None:
        spool = self.store.create_spool(
            run_id=self.run["run_id"],
            artifact_type="nmap_stderr",
            media_type="text/plain",
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=4,
        )

        with self.assertRaisesRegex(RawEvidenceLimitError, "byte limit"):
            spool.write(b"12345")
        spool.abort()

        self.assertEqual(tuple(self.store.iter_backup_records()), ())
        reconciliation = self.store.reconcile_orphans()
        self.assertEqual(reconciliation.orphan_storage_keys, ())
        self.assertEqual(reconciliation.staged_temp_keys, ())

    def test_spool_rejects_non_boolean_completeness_without_publishing(self) -> None:
        spool = self.store.create_spool(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        spool.write(b"<nmaprun/>")

        with self.assertRaisesRegex(TypeError, "boolean"):
            spool.finalize(capture_complete=1)  # type: ignore[arg-type]
        spool.abort()
        self.assertEqual(tuple(self.store.iter_backup_records()), ())

    def test_tampered_bytes_fail_read_backup_and_reconciliation(self) -> None:
        descriptor = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"trusted",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        target = next((self.artifact_root / "objects").rglob("*.bin"))
        target.write_bytes(b"changed")

        with self.assertRaisesRegex(RawEvidenceIntegrityError, "digest|size"):
            self.store.read_verified(
                run_id=self.run["run_id"], artifact_id=descriptor.artifact_id
            )
        with self.assertRaises(RawEvidenceIntegrityError):
            tuple(self.store.iter_backup_records())
        self.assertEqual(
            self.store.reconcile_orphans().tampered_artifact_ids,
            (descriptor.artifact_id,),
        )

    def test_reparse_storage_root_fails_before_artifact_creation(self) -> None:
        self.artifact_root.mkdir()
        self.security.reparse_points.add(self.artifact_root)

        with self.assertRaisesRegex(RawEvidenceSecurityError, "reparse"):
            self.store.create_spool(
                run_id=self.run["run_id"],
                artifact_type="nmap_xml",
                media_type="application/xml",
                producer_executor_id="inline:commissioning-host-01",
                max_bytes=64,
            )

    def test_read_rejects_artifact_if_owning_run_scope_changes(self) -> None:
        descriptor = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"scope-bound",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        other = DbRunStore(self.engine).create_run(
            project_id="other-project",
            site_id="other-site",
            job_type="ip_discovery",
            parameters={},
        )
        with session_factory(self.engine).begin() as session:
            session.execute(
                update(Run)
                .where(Run.id == self.run["run_id"])
                .values(project_id=other["project_id"], site_id=other["site_id"])
            )

        with self.assertRaises(RawEvidenceNotFoundError):
            self.store.read_verified(
                run_id=self.run["run_id"], artifact_id=descriptor.artifact_id
            )

    def test_nmap_adapter_uses_direct_protocol_and_rejects_nonliteral_targets(self) -> None:
        adapter = self.store.for_nmap_run(
            run_id=self.run["run_id"],
            producer_executor_id="inline:commissioning-host-01",
        )
        target_path = Path(adapter.create_owner_only_target_list(b"192.0.2.10\n"))
        self.assertEqual(target_path.read_bytes(), b"192.0.2.10\n")
        private_directory = Path(adapter.private_temp_directory())
        self.assertEqual(private_directory, target_path.parent)
        adapter.remove_target_list(str(target_path))
        self.assertFalse(target_path.exists())
        self.assertFalse(private_directory.exists())
        with self.assertRaisesRegex(ValueError, "literal"):
            adapter.create_owner_only_target_list(b"example.com\n")

        spool = adapter.create_owner_only_xml_spool(max_bytes=64)
        spool.write(b"<nmaprun/>")
        artifact = spool.finalize(capture_complete=True)
        self.assertTrue(artifact.artifact_id.startswith("artifact_"))
        self.assertEqual(artifact.size_bytes, len(b"<nmaprun/>"))

    def test_backup_delete_restore_round_trip_reapplies_security_and_exact_identity(self) -> None:
        original = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_pcap",
            media_type="application/vnd.tcpdump.pcap",
            payload=b"pcap-bytes",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        record = next(self.store.iter_backup_records())
        secured_file_count = len(self.security.files)

        self.assertTrue(
            self.store.delete_verified(
                run_id=self.run["run_id"], artifact_id=original.artifact_id
            )
        )
        with self.assertRaises(RawEvidenceNotFoundError):
            self.store.read_verified(
                run_id=self.run["run_id"], artifact_id=original.artifact_id
            )
        restored = self.store.restore_backup_record(record)

        self.assertEqual(restored, original)
        self.assertEqual(self.store.verify_restored_artifact(record), original)
        self.assertGreater(len(self.security.files), secured_file_count)

    def test_backup_iteration_streams_and_invalid_completeness_cannot_publish(self) -> None:
        original = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"backup-me",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        records = self.store.iter_backup_records()
        self.assertNotIsInstance(records, tuple)
        record = next(records)
        self.assertEqual(record.descriptor, original)
        self.assertEqual(record.payload, b"backup-me")
        self.assertEqual(tuple(records), ())

        self.assertTrue(
            self.store.delete_verified(
                run_id=self.run["run_id"], artifact_id=original.artifact_id
            )
        )
        invalid = RawEvidenceBackupRecord(
            descriptor=replace(record.descriptor, capture_complete=1),
            payload=record.payload,
        )
        with self.assertRaisesRegex(RawEvidenceIntegrityError, "completeness"):
            self.store.restore_backup_record(invalid)
        with self.assertRaises(RawEvidenceNotFoundError):
            self.store.get_descriptor(
                run_id=self.run["run_id"], artifact_id=original.artifact_id
            )

        restored = self.store.restore_backup_record(record)
        with self.assertRaisesRegex(RawEvidenceIntegrityError, "completeness"):
            self.store.verify_restored_artifact(invalid)
        self.assertEqual(restored, record.descriptor)
        secured_file_count = len(self.security.files)
        self.assertEqual(self.store.verify_restored_store(), (record.descriptor,))
        self.assertGreater(len(self.security.files), secured_file_count)

    def test_reconcile_lists_and_optionally_removes_orphan_files(self) -> None:
        self.artifact_root.mkdir()
        objects = self.artifact_root / "objects" / "ff"
        objects.mkdir(parents=True)
        orphan = objects / "orphan.bin"
        orphan.write_bytes(b"orphan")

        preview = self.store.reconcile_orphans()
        self.assertEqual(
            preview.orphan_storage_keys,
            ("objects/ff/orphan.bin",),
        )
        self.assertTrue(orphan.exists())
        applied = self.store.reconcile_orphans(delete_orphans=True)
        self.assertEqual(applied.deleted_orphan_count, 1)
        self.assertFalse(orphan.exists())

    def test_reconcile_recovers_interrupted_delete_when_database_row_survives(self) -> None:
        descriptor = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"recover-me",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        target = next((self.artifact_root / "objects").rglob("*.bin"))
        staging = self.artifact_root / "staging"
        quarantine = staging / f".delete-{descriptor.artifact_id}-{'2' * 32}.tmp"
        target.replace(quarantine)

        result = self.store.reconcile_orphans()

        self.assertEqual(
            result.recovered_delete_artifact_ids,
            (descriptor.artifact_id,),
        )
        loaded, payload = self.store.read_verified(
            run_id=self.run["run_id"], artifact_id=descriptor.artifact_id
        )
        self.assertEqual(loaded, descriptor)
        self.assertEqual(payload, b"recover-me")
        self.assertFalse(quarantine.exists())


if __name__ == "__main__":
    unittest.main()
