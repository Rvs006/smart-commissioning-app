"""Migration and database constraints for protected raw evidence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.models import (
    RawEvidenceArtifact,
    RawEvidenceDownloadAudit,
)
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_SHA = "a" * 64


class RawEvidenceSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary_directory.cleanup)
        database_path = Path(self.temporary_directory.name) / "raw-evidence.db"
        self.url = f"sqlite:///{database_path.as_posix()}"
        upgrade_to_head(self.url)
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

    def _create_run(self) -> str:
        return DbRunStore(self.engine).create_run(
            project_id="project-raw",
            site_id="site-raw",
            job_type="ip_discovery",
            parameters={},
        )["run_id"]

    @staticmethod
    def _artifact_values(run_id: str, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "artifact_id": "artifact_" + "1" * 32,
            "run_id": run_id,
            "project_id": "project-raw",
            "site_id": "site-raw",
            "artifact_type": "nmap_xml",
            "media_type": "application/xml",
            "storage_relpath": "objects/aa/artifact.bin",
            "sha256": _SHA,
            "size_bytes": 12,
            "capture_complete": True,
            "producer_executor_id": "inline:commissioning-host-01",
            "created_at": _NOW,
            "sealed_at": _NOW,
        }
        values.update(overrides)
        if "artifact_id" in overrides and "storage_relpath" not in overrides:
            values["storage_relpath"] = f"objects/aa/{values['artifact_id']}.bin"
        return values

    def test_fresh_migration_matches_models_and_creates_raw_evidence_tables(self) -> None:
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("raw_evidence_artifacts", tables)
        self.assertIn("raw_evidence_download_audits", tables)
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            self.assertEqual(compare_metadata(context, Base.metadata), [])
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE 'trg_raw_evidence%'"
                    )
                )
            }
        self.assertEqual(
            triggers,
            {
                "trg_raw_evidence_artifacts_no_update",
                "trg_raw_evidence_artifacts_scope_owner",
                "trg_raw_evidence_download_audits_no_update",
                "trg_raw_evidence_download_audits_no_delete",
            },
        )

    def test_artifact_metadata_is_immutable_and_database_constraints_fail_closed(self) -> None:
        run_id = self._create_run()
        with Session(self.engine) as session, session.begin():
            session.add(RawEvidenceArtifact(**self._artifact_values(run_id)))

        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(
                    update(RawEvidenceArtifact)
                    .where(RawEvidenceArtifact.artifact_id == "artifact_" + "1" * 32)
                    .values(capture_complete=False)
                )

        invalid = (
            {"artifact_id": "artifact_NOT_OPAQUE"},
            {"artifact_id": "artifact_" + "2" * 32, "sha256": "A" * 64},
            {
                "artifact_id": "artifact_" + "3" * 32,
                "storage_relpath": "../escaped.bin",
            },
            {
                "artifact_id": "artifact_" + "4" * 32,
                "sealed_at": datetime(2026, 8, 12, 11, 59, tzinfo=UTC),
            },
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError):
                with Session(self.engine) as session, session.begin():
                    session.add(
                        RawEvidenceArtifact(
                            **self._artifact_values(run_id, **overrides)
                        )
                    )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.execute(
                    text(
                        "INSERT INTO raw_evidence_artifacts "
                        "(artifact_id, run_id, project_id, site_id, artifact_type, "
                        "media_type, storage_relpath, sha256, size_bytes, "
                        "capture_complete, producer_executor_id, created_at, sealed_at) "
                        "VALUES (:artifact_id, :run_id, :project_id, :site_id, "
                        ":artifact_type, :media_type, :storage_relpath, :sha256, "
                        ":size_bytes, :capture_complete, :producer_executor_id, "
                        ":created_at, :sealed_at)"
                    ),
                    self._artifact_values(
                        run_id,
                        artifact_id="artifact_" + "5" * 32,
                        capture_complete=2,
                    ),
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.execute(
                    text("DELETE FROM runs WHERE id = :run_id"),
                    {"run_id": run_id},
                )

        with self.assertRaises(IntegrityError):
            with Session(self.engine) as session, session.begin():
                session.add(
                    RawEvidenceArtifact(
                        **self._artifact_values(
                            run_id,
                            artifact_id="artifact_" + "6" * 32,
                            project_id="wrong-project",
                        )
                    )
                )

    def test_download_audit_survives_retention_delete_and_rejects_mutation(self) -> None:
        run_id = self._create_run()
        artifact_id = "artifact_" + "1" * 32
        with Session(self.engine) as session, session.begin():
            session.add(RawEvidenceArtifact(**self._artifact_values(run_id)))
            session.add(
                RawEvidenceDownloadAudit(
                    audit_id="rawdl_" + "2" * 32,
                    artifact_id=artifact_id,
                    run_id=run_id,
                    project_id="project-raw",
                    site_id="site-raw",
                    artifact_sha256=_SHA,
                    size_bytes=12,
                    downloaded_by="viewer-01",
                    downloaded_at=_NOW,
                )
            )

        with Session(self.engine) as session, session.begin():
            artifact = session.get(RawEvidenceArtifact, artifact_id)
            assert artifact is not None
            session.delete(artifact)
        with Session(self.engine) as session:
            audit = session.scalar(
                select(RawEvidenceDownloadAudit).where(
                    RawEvidenceDownloadAudit.artifact_id == artifact_id
                )
            )
            self.assertIsNotNone(audit)
        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE raw_evidence_download_audits "
                        "SET downloaded_by = 'rewritten'"
                    )
                )
        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(text("DELETE FROM raw_evidence_download_audits"))

    def test_downgrade_refuses_nonempty_evidence_and_empty_downgrade_is_clean(self) -> None:
        run_id = self._create_run()
        with Session(self.engine) as session, session.begin():
            session.add(RawEvidenceArtifact(**self._artifact_values(run_id)))

        with self.assertRaisesRegex(RuntimeError, "must be empty"):
            command.downgrade(build_alembic_config(self.url), "f4c5d6e7f8a9")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT version_num FROM alembic_version")),
                "f5d6e7f8a9b0",
            )

        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM raw_evidence_artifacts"))
        command.downgrade(build_alembic_config(self.url), "f4c5d6e7f8a9")
        self.assertNotIn("raw_evidence_artifacts", inspect(self.engine).get_table_names())

    def test_postgres_downgrade_locks_history_before_testing_emptiness(self) -> None:
        migration_path = (
            Path(__file__).parents[1]
            / "alembic"
            / "versions"
            / "f5d6e7f8a9b0_protected_raw_evidence_artifacts.py"
        )
        spec = spec_from_file_location("raw_evidence_f5_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)

        calls: list[tuple[str, str]] = []

        class _StopAfterFirstCount(RuntimeError):
            pass

        class _PostgresBind:
            dialect = SimpleNamespace(name="postgresql")

            def execute(self, statement: object) -> None:
                calls.append(("execute", str(statement)))

            def scalar(self, statement: object) -> int:
                calls.append(("scalar", str(statement)))
                raise _StopAfterFirstCount

        with patch.object(migration.op, "get_bind", return_value=_PostgresBind()):
            with self.assertRaises(_StopAfterFirstCount):
                migration.downgrade()

        self.assertEqual(calls[0][0], "execute")
        self.assertIn("LOCK TABLE", calls[0][1])
        self.assertIn("raw_evidence_artifacts", calls[0][1])
        self.assertIn("raw_evidence_download_audits", calls[0][1])
        self.assertEqual(calls[1][0], "scalar")


if __name__ == "__main__":
    unittest.main()
