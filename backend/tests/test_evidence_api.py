"""Tests for evidence integrity, backup, and retention.

Two layers, all in-process with tmp SQLite / tmp dirs (no live infra):

  * EvidenceVerifyApiTests — drives the FastAPI app (api_key auth) against a
    shared temporary SQLite DB, exercising the report verify endpoint end to
    end: generate a report, verify true; tamper the stored hash, verify false.
  * BackupServiceTests / RetentionServiceTests — exercise the backup and
    retention services directly against tmp SQLite + tmp dirs, which is cheaper
    and lets us assert overwrite/force, tamper rejection, and the evidence
    guard without the app harness.
"""

import atexit
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import ApiTestCase

_API_KEY = "test-evidence-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}


class EvidenceVerifyApiTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def before_client(cls) -> None:
        from unittest import mock

        # Point the signing key + backup sources at the temp runtime via patching
        # (SECRETS_ROOT is bound by value at import; patching where it is used is
        # robust and matches test_secret_storage.py). Patches live for the class.
        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-evidence-runtime-")
        atexit.register(shutil.rmtree, cls._temp_runtime, ignore_errors=True)
        secrets_root = Path(cls._temp_runtime) / "secrets"
        report_signing_root = Path(cls._temp_runtime) / "report-signing"
        artifacts_root = Path(cls._temp_runtime) / "artifacts"
        imports_files = Path(cls._temp_runtime) / "imports" / "files"
        secrets_root.mkdir(parents=True, exist_ok=True)
        report_signing_root.mkdir(parents=True, exist_ok=True)
        artifacts_root.mkdir(parents=True, exist_ok=True)
        imports_files.mkdir(parents=True, exist_ok=True)

        import app.api.routes.evidence as evidence_module
        import app.services.report_artifacts as artifacts_module
        import app.services.reports_integrity as integrity_module

        cls._patchers = [
            mock.patch.object(integrity_module, "SECRETS_ROOT", secrets_root),
            mock.patch.object(integrity_module, "REPORT_SIGNING_ROOT", report_signing_root),
            mock.patch.object(artifacts_module, "ARTIFACTS_ROOT", artifacts_root),
            mock.patch.object(evidence_module, "SECRETS_ROOT", secrets_root),
            mock.patch.object(evidence_module, "IMPORT_FILES_ROOT", imports_files),
            mock.patch.object(evidence_module, "ARTIFACTS_ROOT", artifacts_root),
            mock.patch.object(evidence_module, "REPORT_SIGNING_ROOT", report_signing_root),
        ]
        for patcher in cls._patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        for patcher in cls._patchers:
            patcher.stop()

    def _create_report(self, output_format: str = "zip") -> str:
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "evidence_pack",
                "output_format": output_format,
                "source_run_ids": [],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["report_id"]

    def test_created_report_manifest_uses_local_edge_identity(self) -> None:
        from app.core.config import edge_identity
        from app.services.run_service import RunService

        report_id = self._create_report("pdf")
        run = RunService().get_run(report_id)
        manifest = run.result_summary.get("artifact_manifest")
        self.assertIsInstance(manifest, dict)
        self.assertEqual(manifest["origin"], edge_identity().edge_id)

    def test_verify_succeeds_before_first_download(self) -> None:
        report_id = self._create_report()
        response = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["hash_matches"], response.text)

    def test_generate_then_verify_true_for_each_format(self) -> None:
        for output_format in ("zip", "xlsx", "docx", "pdf"):
            with self.subTest(output_format=output_format):
                report_id = self._create_report(output_format)
                download = self.client.get(f"/api/v1/reports/{report_id}/download")
                self.assertEqual(download.status_code, 200, download.text)

                verify = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
                self.assertEqual(verify.status_code, 200, verify.text)
                body = verify.json()
                self.assertTrue(body["hash_matches"], body)
                self.assertTrue(body["signature_valid"], body)
                self.assertIsNotNone(body["signed_at"])
                self.assertIsNotNone(body["public_key_fingerprint"])
                self.assertEqual(body["stored_hash"], body["computed_hash"])

    def test_manifest_verifier_accepts_legacy_and_rejects_bad_evidence_id(self) -> None:
        import base64

        from app.services.report_artifacts import (
            canonical_json_bytes,
            verify_signed_manifest,
        )
        from app.services.reports_integrity import load_signing_key
        from app.services.run_service import RunService
        from smart_commissioning_core.integrity import sha256_bytes

        report_id = self._create_report("zip")
        manifest = dict(RunService().get_run(report_id).result_summary["artifact_manifest"])
        self.assertTrue(verify_signed_manifest(manifest))

        legacy = dict(manifest)
        legacy["schema_version"] = "1.0"
        legacy.pop("evidence_set_id")
        unsigned = {
            key: value
            for key, value in legacy.items()
            if key not in {"signature_algorithm", "signature", "public_key_pem", "signed_manifest_sha256"}
        }
        signing_key = load_signing_key()
        signed_body = canonical_json_bytes(unsigned)
        legacy["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
        legacy["public_key_pem"] = signing_key.public_key_pem()
        legacy["signing_key_id"] = signing_key.public_key_fingerprint()
        legacy["signed_manifest_sha256"] = sha256_bytes(signed_body)
        self.assertTrue(verify_signed_manifest(legacy))

        malformed = dict(manifest)
        malformed["evidence_set_id"] = "evidence_invalid"
        self.assertFalse(verify_signed_manifest(malformed))

    def test_unscoped_evidence_pack_labels_source_runs_honestly(self) -> None:
        # Audit fix: an evidence pack generated with no selected source runs must
        # NOT claim to cover "All completed runs" while carrying zero findings.
        report_id = self._create_report("zip")
        download = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(download.status_code, 200, download.text)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            summary = json.loads(archive.read("summary.json"))
        self.assertEqual(summary["Source runs"], "None selected (no run findings included)")

    def test_download_is_byte_reproducible(self) -> None:
        report_id = self._create_report("xlsx")
        first = self.client.get(f"/api/v1/reports/{report_id}/download")
        second = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(first.content, second.content, "artifact must be reproducible")

    def test_tampered_stored_hash_fails_verification_loudly(self) -> None:
        report_id = self._create_report("zip")
        self.client.get(f"/api/v1/reports/{report_id}/download")

        from app.services.run_service import RunService
        from smart_commissioning_core.db.models import Run
        from sqlalchemy import update

        run_service = RunService()
        run = run_service.get_run(report_id)
        summary = dict(run.result_summary)
        manifest = dict(summary["artifact_manifest"])
        manifest["artifact_sha256"] = "0" * 64
        summary["artifact_manifest"] = manifest
        with run_service.engine.begin() as connection:
            connection.execute(update(Run).where(Run.id == report_id).values(result_summary=summary))

        verify = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify")
        self.assertEqual(verify.status_code, 409, verify.text)

    def test_tampered_signature_makes_signature_invalid(self) -> None:
        import base64

        report_id = self._create_report("zip")
        self.client.get(f"/api/v1/reports/{report_id}/download")

        from app.services.run_service import RunService
        from smart_commissioning_core.db.models import Run
        from sqlalchemy import update

        run_service = RunService()
        run = run_service.get_run(report_id)
        summary = dict(run.result_summary)
        manifest = dict(summary["artifact_manifest"])
        bad = bytearray(base64.b64decode(manifest["signature"]))
        bad[0] ^= 0xFF
        manifest["signature"] = base64.b64encode(bytes(bad)).decode("ascii")
        summary["artifact_manifest"] = manifest
        with run_service.engine.begin() as connection:
            connection.execute(update(Run).where(Run.id == report_id).values(result_summary=summary))

        body = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify").json()
        self.assertTrue(body["hash_matches"], body)
        self.assertFalse(body["signature_valid"], body)

    def test_swapped_key_stored_record_flags_key_mismatch(self) -> None:
        """A swapped-key record is internally consistent yet key_matches_current=False."""
        import base64

        from smart_commissioning_core.integrity import SigningKey

        report_id = self._create_report("zip")
        download = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(download.status_code, 200, download.text)

        # Genuine record verifies AND pins to the current key.
        genuine = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify").json()
        self.assertTrue(genuine["signature_valid"], genuine)
        self.assertTrue(genuine["key_matches_current"], genuine)

        # Forge a DIFFERENT keypair and re-sign the SAME manifest, then swap the
        # whole signature+public_key+fingerprint into the stored record. The
        # self-signature stays internally consistent, but the embedded key is no
        # longer the current signing key.
        from app.services.report_artifacts import canonical_json_bytes
        from app.services.run_service import RunService
        from smart_commissioning_core.db.models import Run
        from smart_commissioning_core.integrity import sha256_bytes
        from sqlalchemy import update

        rogue = SigningKey.generate()
        run_service = RunService()
        run = run_service.get_run(report_id)
        summary = dict(run.result_summary)
        manifest = dict(summary["artifact_manifest"])
        manifest["signing_key_id"] = rogue.public_key_fingerprint()
        unsigned = {
            key: value
            for key, value in manifest.items()
            if key not in {"signature_algorithm", "signature", "public_key_pem", "signed_manifest_sha256"}
        }
        signed_body = canonical_json_bytes(unsigned)
        manifest["signature"] = base64.b64encode(rogue.sign(signed_body)).decode("ascii")
        manifest["public_key_pem"] = rogue.public_key_pem()
        manifest["signed_manifest_sha256"] = sha256_bytes(signed_body)
        summary["artifact_manifest"] = manifest
        with run_service.engine.begin() as connection:
            connection.execute(update(Run).where(Run.id == report_id).values(result_summary=summary))

        body = self.client.get(f"/api/v1/evidence/reports/{report_id}/verify").json()
        # Self-signature is internally consistent (it verifies against the
        # embedded rogue key) but the key is NOT the current one.
        self.assertTrue(body["signature_valid"], body)
        self.assertFalse(body["key_matches_current"], body)

    def test_retention_preview_requires_auth(self) -> None:
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as unauth:  # no API key header
            response = unauth.post("/api/v1/evidence/retention/preview", json={"keep_days": 30})
            self.assertEqual(response.status_code, 401, response.text)

    def test_retention_apply_rejects_missing_confirmation(self) -> None:
        response = self.client.post(
            "/api/v1/evidence/retention/apply",
            json={"keep_days": 0, "confirm": False, "acknowledge": "DELETE"},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_backup_endpoint_returns_a_verifiable_bundle(self) -> None:
        response = self.client.post("/api/v1/evidence/backup")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")

        from app.services.backup_service import verify_bundle

        manifest = verify_bundle(response.content)
        self.assertIn("db/smart_commissioning.db", manifest["members"])
        self.assertIsNotNone(manifest["signature"])


# ---------------------------------------------------------------------------
# Service-level tests (direct, tmp SQLite + tmp dirs)
# ---------------------------------------------------------------------------


def _make_engine(runtime_root: Path):
    from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
    from smart_commissioning_core.db.migrate import upgrade_to_head

    url = default_sqlite_url(runtime_root)
    upgrade_to_head(url)
    return create_engine_from_url(url), url


class BackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.runtime = Path(self._temp.name) / "runtime"
        self.runtime.mkdir(parents=True)
        self.engine, self.url = _make_engine(self.runtime)
        self.addCleanup(self.engine.dispose)

        self.secrets_root = self.runtime / "secrets"
        self.imports_files = self.runtime / "imports" / "files"
        self.report_artifacts = self.runtime / "artifacts"
        self.report_signing = self.runtime / "report-signing"
        self.secrets_root.mkdir(parents=True)
        self.imports_files.mkdir(parents=True)
        self.report_artifacts.mkdir(parents=True)
        self.report_signing.mkdir(parents=True)
        (self.secrets_root / "ca-certificate.pem").write_bytes(b"ENCRYPTED-PEM-BYTES")
        (self.imports_files / "devices.csv").write_text("asset_id,name\nA1,AHU")
        (self.report_artifacts / "sha256" / "ab").mkdir(parents=True)
        (self.report_artifacts / "sha256" / "ab" / "artifact.pdf").write_bytes(
            b"IMMUTABLE-REPORT"
        )
        (self.report_signing / ".evidence_signing_key").write_bytes(b"SIGNING-KEY")

    def _seed_run(self) -> str:
        from smart_commissioning_core.db.db_run_store import DbRunStore

        store = DbRunStore(self.engine)
        record = store.create_run(
            project_id="demo-project",
            site_id="demo-site",
            job_type="udmi_validation",
            parameters={},
        )
        return record["run_id"]

    def _sources(self):
        from app.services.backup_service import BackupSources

        return BackupSources(
            database_url=self.url,
            secrets_root=self.secrets_root,
            imports_files_root=self.imports_files,
            report_artifacts_root=self.report_artifacts,
            report_signing_root=self.report_signing,
        )

    def _signing_key(self):
        from smart_commissioning_core.integrity import SigningKey

        return SigningKey.generate()

    def test_create_then_restore_roundtrip(self) -> None:
        from app.services.backup_service import (
            RestoreTarget,
            create_backup_bundle,
            restore_backup_bundle,
        )
        from smart_commissioning_core.db.db_run_store import DbRunStore

        run_id = self._seed_run()
        bundle = create_backup_bundle(
            self._sources(), created_at=datetime.now(UTC), signing_key=self._signing_key()
        )

        # Restore into a fresh, empty runtime root.
        target_root = Path(self._temp.name) / "restored"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )
        restore_backup_bundle(bundle, target)

        # DB row survives.
        from smart_commissioning_core.db.engine import create_engine_from_url

        restored_engine = create_engine_from_url(f"sqlite:///{target.database_path.as_posix()}")
        self.addCleanup(restored_engine.dispose)
        restored_run = DbRunStore(restored_engine).get_run(run_id)
        self.assertEqual(restored_run["run_id"], run_id)

        # Secret file + import file survive byte-for-byte.
        self.assertEqual(
            (target.secrets_root / "ca-certificate.pem").read_bytes(), b"ENCRYPTED-PEM-BYTES"
        )
        self.assertEqual(
            (target.imports_files_root / "devices.csv").read_text(), "asset_id,name\nA1,AHU"
        )
        self.assertEqual(
            (target_root / "artifacts" / "sha256" / "ab" / "artifact.pdf").read_bytes(),
            b"IMMUTABLE-REPORT",
        )
        self.assertEqual(
            (target_root / "report-signing" / ".evidence_signing_key").read_bytes(),
            b"SIGNING-KEY",
        )

    def test_restore_refuses_overwrite_without_force(self) -> None:
        from app.services.backup_service import (
            BackupError,
            RestoreTarget,
            create_backup_bundle,
            restore_backup_bundle,
        )

        self._seed_run()
        bundle = create_backup_bundle(
            self._sources(), created_at=datetime.now(UTC), signing_key=self._signing_key()
        )

        target_root = Path(self._temp.name) / "occupied"
        target_root.mkdir()
        db_path = target_root / "smart_commissioning.db"
        db_path.write_bytes(b"EXISTING-DB")
        target = RestoreTarget(
            database_path=db_path,
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaises(BackupError):
            restore_backup_bundle(bundle, target)
        # Untouched.
        self.assertEqual(db_path.read_bytes(), b"EXISTING-DB")
        # With force it overwrites.
        restore_backup_bundle(bundle, target, force=True)
        self.assertNotEqual(db_path.read_bytes(), b"EXISTING-DB")

    def test_tampered_bundle_is_rejected(self) -> None:
        import io
        import zipfile

        from app.services.backup_service import (
            BackupError,
            create_backup_bundle,
            verify_bundle,
        )

        self._seed_run()
        bundle = create_backup_bundle(
            self._sources(), created_at=datetime.now(UTC), signing_key=self._signing_key()
        )
        # Verifies clean as produced.
        verify_bundle(bundle)

        # Rewrite a member's bytes without updating the manifest -> hash mismatch.
        source = io.BytesIO(bundle)
        out = io.BytesIO()
        with zipfile.ZipFile(source) as reader, zipfile.ZipFile(out, "w") as writer:
            for name in reader.namelist():
                data = reader.read(name)
                if name == "imports/files/devices.csv":
                    data = b"TAMPERED"
                writer.writestr(name, data)
        with self.assertRaises(BackupError):
            verify_bundle(out.getvalue())

    def test_postgres_url_is_refused(self) -> None:
        from app.services.backup_service import BackupError, BackupSources, create_backup_bundle

        sources = BackupSources(database_url="postgresql+psycopg://u:p@h/db")
        with self.assertRaises(BackupError):
            create_backup_bundle(sources, created_at=datetime.now(UTC))

    def test_restore_rejects_path_traversal_member(self) -> None:
        import io
        import json as _json
        import zipfile

        from app.services.backup_service import (
            BackupError,
            RestoreTarget,
            restore_backup_bundle,
        )

        # Craft a bundle by hand whose secrets member name escapes its root via
        # '../'. Sign nothing — restore with allow_unsigned so the traversal
        # guard is what must reject it (defense-in-depth even when unsigned).
        escaping_member = "secrets/../../../escape.pem"
        from smart_commissioning_core.integrity import sha256_bytes

        manifest = {
            "bundle_format_version": 1,
            "core_version": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "members": {escaping_member: sha256_bytes(b"PWNED")},
            "signature_algorithm": "ed25519",
            "signature": None,
            "public_key_pem": None,
            "public_key_fingerprint": None,
        }
        crafted = io.BytesIO()
        with zipfile.ZipFile(crafted, "w") as writer:
            writer.writestr(escaping_member, b"PWNED")
            writer.writestr("manifest.json", _json.dumps(manifest).encode("utf-8"))

        target_root = Path(self._temp.name) / "traversal-target"
        target = RestoreTarget(
            database_path=target_root / "smart_commissioning.db",
            secrets_root=target_root / "secrets",
            imports_files_root=target_root / "imports" / "files",
        )

        with self.assertRaises(BackupError):
            restore_backup_bundle(crafted.getvalue(), target, allow_unsigned=True)

        # Nothing escaped: no file written outside the secrets root.
        escaped = target_root.parent / "escape.pem"
        self.assertFalse(escaped.exists(), "path-traversal member must not be written")


class RetentionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.runtime = Path(self._temp.name) / "runtime"
        self.runtime.mkdir(parents=True)
        self.engine, self.url = _make_engine(self.runtime)
        self.addCleanup(self.engine.dispose)

    def _create_run(
        self,
        *,
        job_type: str = "udmi_validation",
        age_days: int = 0,
        parameters: dict | None = None,
        with_issue: bool = False,
    ) -> str:
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Project, Run, RunIssue, Site

        factory = session_factory(self.engine)
        created = datetime.now(UTC) - timedelta(days=age_days)
        run_id = f"run_{created.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}"
        with factory.begin() as session:
            if session.get(Project, "demo-project") is None:
                session.add(Project(id="demo-project", name="demo-project"))
                session.add(Site(id="demo-site", project_id="demo-project", name="demo-site"))
                session.flush()
            session.add(
                Run(
                    id=run_id,
                    project_id="demo-project",
                    site_id="demo-site",
                    job_type=job_type,
                    status="succeeded",
                    stage="done",
                    progress_percent=100,
                    parameters=parameters or {},
                    result_summary={},
                    created_at=created,
                    updated_at=created,
                )
            )
            if with_issue:
                session.flush()
                session.add(
                    RunIssue(
                        run_id=run_id,
                        position=0,
                        issue_id="i1",
                        issue_type="unit_mismatch",
                        severity="high",
                        description="bad unit",
                    )
                )
        return run_id

    def _run_exists(self, run_id: str) -> bool:
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Run

        with session_factory(self.engine)() as session:
            return session.get(Run, run_id) is not None

    def _issue_count(self, run_id: str) -> int:
        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import RunIssue
        from sqlalchemy import func, select

        with session_factory(self.engine)() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(RunIssue).where(RunIssue.run_id == run_id)
                )
                or 0
            )

    def test_preview_lists_candidates_and_deletes_nothing(self) -> None:
        from app.services.retention_service import RetentionService, cutoff_from_keep_days

        old_run = self._create_run(age_days=100)
        new_run = self._create_run(age_days=1)

        result = RetentionService(self.engine).preview(before=cutoff_from_keep_days(30))
        candidate_ids = {candidate.run_id for candidate in result.candidates}
        self.assertIn(old_run, candidate_ids)
        self.assertNotIn(new_run, candidate_ids)
        self.assertEqual(result.deleted_run_ids, [])
        self.assertTrue(self._run_exists(old_run))

    def test_apply_deletes_only_past_cutoff_and_cascades_issues(self) -> None:
        from app.services.retention_service import RetentionService, cutoff_from_keep_days

        old_run = self._create_run(age_days=100, with_issue=True)
        new_run = self._create_run(age_days=1, with_issue=True)
        self.assertEqual(self._issue_count(old_run), 1)

        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30), confirm=True
        )
        self.assertIn(old_run, result.deleted_run_ids)
        self.assertNotIn(new_run, result.deleted_run_ids)
        self.assertFalse(self._run_exists(old_run))
        self.assertTrue(self._run_exists(new_run))
        # Cascade: the old run's issue row is gone.
        self.assertEqual(self._issue_count(old_run), 0)

    def test_apply_without_confirm_is_a_dry_run(self) -> None:
        from app.services.retention_service import RetentionService, cutoff_from_keep_days

        old_run = self._create_run(age_days=100)
        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30), confirm=False
        )
        self.assertEqual(result.deleted_run_ids, [])
        self.assertTrue(result.dry_run)
        self.assertTrue(self._run_exists(old_run))

    def test_evidence_linked_runs_are_never_deleted(self) -> None:
        from app.services.retention_service import RetentionService, cutoff_from_keep_days

        # An old source run referenced by an (also old) report run.
        source_run = self._create_run(age_days=100)
        report_run = self._create_run(
            age_days=100,
            job_type="report_generation",
            parameters={"source_run_ids": [source_run]},
        )

        result = RetentionService(self.engine).apply(
            before=cutoff_from_keep_days(30), confirm=True
        )
        self.assertNotIn(source_run, result.deleted_run_ids)
        self.assertNotIn(report_run, result.deleted_run_ids)
        self.assertIn(source_run, result.skipped_evidence_run_ids)
        self.assertIn(report_run, result.skipped_evidence_run_ids)
        self.assertTrue(self._run_exists(source_run))
        self.assertTrue(self._run_exists(report_run))


if __name__ == "__main__":
    unittest.main()
