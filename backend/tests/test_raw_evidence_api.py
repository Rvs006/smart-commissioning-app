"""Project/site-scoped raw evidence download contracts."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from app.api.routes.raw_evidence import get_raw_evidence_store
from app.core.db import get_engine
from app.services.raw_evidence_artifacts import RawEvidenceArtifactStore
from harness import ApiTestCase
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import RawEvidenceDownloadAudit
from sqlalchemy import select


class RawEvidenceApiTests(ApiTestCase):
    env = {
        "AUTH_MODE": "local",
        "API_KEY": None,
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
    }

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = RawEvidenceArtifactStore(
            get_engine(),
            root=Path(self.temporary_directory.name) / "raw-evidence",
        )
        self.app.dependency_overrides[get_raw_evidence_store] = lambda: self.store
        suffix = uuid.uuid4().hex[:10]
        self.project_id = f"raw-project-{suffix}"
        self.site_id = f"raw-site-{suffix}"
        run_store = DbRunStore(get_engine())
        self.run = run_store.create_run(
            project_id=self.project_id,
            site_id=self.site_id,
            job_type="ip_discovery",
            parameters={},
        )
        self.foreign_run = run_store.create_run(
            project_id=f"raw-foreign-project-{suffix}",
            site_id=f"raw-foreign-site-{suffix}",
            job_type="ip_discovery",
            parameters={},
        )
        self.artifact = self.store.import_bytes(
            run_id=self.run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"<nmaprun/>",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        self.foreign_artifact = self.store.import_bytes(
            run_id=self.foreign_run["run_id"],
            artifact_type="nmap_xml",
            media_type="application/xml",
            payload=b"<foreign/>",
            capture_complete=True,
            producer_executor_id="inline:commissioning-host-01",
            max_bytes=64,
        )
        created = self.client.post(
            "/api/v1/users",
            json={"username": f"raw-viewer-{suffix}", "role": "viewer"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.viewer_id = created.json()["user"]["id"]
        self.viewer_headers = {"X-API-Key": created.json()["api_key"]}
        granted = self.client.post(
            f"/api/v1/users/{self.viewer_id}/scope-grants",
            json={
                "project_id": self.project_id,
                "site_id": self.site_id,
                "reason": "Raw evidence review",
            },
        )
        self.assertEqual(granted.status_code, 201, granted.text)

    def tearDown(self) -> None:
        self.app.dependency_overrides.pop(get_raw_evidence_store, None)
        self.temporary_directory.cleanup()

    def test_scoped_download_returns_exact_bytes_without_path_and_is_audited(self) -> None:
        response = self.client.get(
            "/api/v1/discovery/runs/"
            f"{self.run['run_id']}/raw-evidence/{self.artifact.artifact_id}",
            headers=self.viewer_headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"<nmaprun/>")
        self.assertEqual(response.headers["content-type"], "application/xml")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(self.artifact.artifact_id, response.headers["content-disposition"])
        self.assertNotIn(str(self.store.root), response.text)
        with session_factory(get_engine())() as session:
            audit = session.execute(
                select(RawEvidenceDownloadAudit).where(
                    RawEvidenceDownloadAudit.artifact_id == self.artifact.artifact_id
                )
            ).scalar_one()
        self.assertEqual(audit.run_id, self.run["run_id"])
        self.assertEqual(audit.project_id, self.project_id)
        self.assertEqual(audit.site_id, self.site_id)
        self.assertEqual(audit.downloaded_by, self.viewer_id)

    def test_foreign_and_absent_run_ids_are_equally_concealed(self) -> None:
        foreign = self.client.get(
            "/api/v1/discovery/runs/"
            f"{self.foreign_run['run_id']}/raw-evidence/"
            f"{self.foreign_artifact.artifact_id}",
            headers=self.viewer_headers,
        )
        absent = self.client.get(
            "/api/v1/discovery/runs/run_missing/raw-evidence/"
            f"{self.foreign_artifact.artifact_id}",
            headers=self.viewer_headers,
        )

        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(absent.status_code, 404, absent.text)
        self.assertEqual(foreign.json(), absent.json())

    def test_artifact_cannot_be_rebound_to_an_authorized_run(self) -> None:
        response = self.client.get(
            "/api/v1/discovery/runs/"
            f"{self.run['run_id']}/raw-evidence/{self.foreign_artifact.artifact_id}",
            headers=self.viewer_headers,
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("foreign", response.text.lower())

    def test_tamper_returns_sanitized_conflict_without_path_or_bytes(self) -> None:
        next(
            path
            for path in (self.store.root / "objects").rglob("*.bin")
            if self.artifact.artifact_id in path.name
        ).write_bytes(b"tampered")

        response = self.client.get(
            "/api/v1/discovery/runs/"
            f"{self.run['run_id']}/raw-evidence/{self.artifact.artifact_id}",
            headers=self.viewer_headers,
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json(),
            {"detail": "Raw evidence failed integrity verification."},
        )
        self.assertNotIn(str(self.store.root), response.text)
        self.assertNotIn("tampered", response.text)

    def test_local_synthetic_download_uses_unambiguous_audit_identity(self) -> None:
        response = self.client.get(
            "/api/v1/discovery/runs/"
            f"{self.run['run_id']}/raw-evidence/{self.artifact.artifact_id}"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with session_factory(get_engine())() as session:
            audit = session.execute(
                select(RawEvidenceDownloadAudit).where(
                    RawEvidenceDownloadAudit.artifact_id == self.artifact.artifact_id
                )
            ).scalar_one()
        self.assertEqual(audit.downloaded_by, "synthetic:local")
