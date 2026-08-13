"""Project/site isolation matrix for discovery, validation, reports, and evidence."""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from app.core.db import get_engine
from harness import ApiTestCase
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.repositories import ImportRepository

_ROOT_KEY = "project-site-matrix-root-key"


class ProjectSiteScopeMatrixTests(ApiTestCase):
    """Every public object path stays inside the caller's active grant."""

    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _ROOT_KEY,
        "DEPLOYMENT_ROLE": "standalone",
        "JOB_EXECUTION_MODE": "inline",
    }
    client_headers = {"X-API-Key": _ROOT_KEY}

    @classmethod
    def before_client(cls) -> None:
        cls._temp_runtime = tempfile.mkdtemp(prefix="sct-scope-matrix-")
        atexit.register(shutil.rmtree, cls._temp_runtime, ignore_errors=True)
        root = Path(cls._temp_runtime)
        secrets_root = root / "secrets"
        report_signing_root = root / "report-signing"
        artifacts_root = root / "artifacts"
        imports_root = root / "imports" / "files"
        for directory in (secrets_root, report_signing_root, artifacts_root, imports_root):
            directory.mkdir(parents=True, exist_ok=True)

        import app.api.routes.evidence as evidence_module
        import app.services.report_artifacts as artifacts_module
        import app.services.reports_integrity as integrity_module

        cls._patchers = [
            mock.patch.object(integrity_module, "SECRETS_ROOT", secrets_root),
            mock.patch.object(
                integrity_module,
                "REPORT_SIGNING_ROOT",
                report_signing_root,
            ),
            mock.patch.object(artifacts_module, "ARTIFACTS_ROOT", artifacts_root),
            mock.patch.object(evidence_module, "SECRETS_ROOT", secrets_root),
            mock.patch.object(evidence_module, "REPORT_SIGNING_ROOT", report_signing_root),
            mock.patch.object(evidence_module, "ARTIFACTS_ROOT", artifacts_root),
            mock.patch.object(evidence_module, "IMPORT_FILES_ROOT", imports_root),
        ]
        for patcher in cls._patchers:
            patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        for patcher in cls._patchers:
            patcher.stop()
        shutil.rmtree(cls._temp_runtime, ignore_errors=True)

    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.scope_a = (f"matrix-a-project-{suffix}", f"matrix-a-site-{suffix}")
        self.scope_b = (f"matrix-b-project-{suffix}", f"matrix-b-site-{suffix}")
        self.missing_scope = (
            f"matrix-missing-project-{suffix}",
            f"matrix-missing-site-{suffix}",
        )
        self.missing_run_id = f"run_missing_{suffix}"
        self.missing_import_id = f"imp_missing_{suffix}"
        self.missing_report_id = f"report_missing_{suffix}"

        store = DbRunStore(get_engine())
        self.seed_discovery_a = store.create_run(
            project_id=self.scope_a[0],
            site_id=self.scope_a[1],
            job_type="mqtt_discovery",
            parameters={"dry_run": True},
        )
        self.seed_discovery_b = store.create_run(
            project_id=self.scope_b[0],
            site_id=self.scope_b[1],
            job_type="mqtt_discovery",
            parameters={"dry_run": True},
        )
        self.udmi_a = self._terminal_udmi_run(store, self.scope_a)
        self.udmi_b = self._terminal_udmi_run(store, self.scope_b)

        repository = ImportRepository(get_engine())
        self.import_a = self._create_import(repository, self.scope_a, suffix, "a")
        self.import_b = self._create_import(repository, self.scope_b, suffix, "b")

        self.user_a = self._create_user(f"matrix-engineer-a-{suffix}")
        self.user_b = self._create_user(f"matrix-engineer-b-{suffix}")
        self._grant(self.user_a, self.scope_a)
        self._grant(self.user_b, self.scope_b)

    @staticmethod
    def _terminal_udmi_run(
        store: DbRunStore,
        scope: tuple[str, str],
    ) -> dict[str, object]:
        run = store.create_run(
            project_id=scope[0],
            site_id=scope[1],
            job_type="udmi_validation",
            parameters={"capture_seconds": 1},
        )
        store.update_result_summary(
            str(run["run_id"]),
            {
                "raw_evidence": {
                    "records": [],
                    "record_count": 0,
                    "captured_record_count": 0,
                    "truncated": False,
                    "retained_bytes": 0,
                }
            },
        )
        return store.update_run_status(
            str(run["run_id"]),
            status="succeeded",
            stage="validation_complete",
            progress_percent=100,
        )

    @staticmethod
    def _create_import(
        repository: ImportRepository,
        scope: tuple[str, str],
        suffix: str,
        label: str,
    ) -> dict[str, object]:
        import_id = f"imp_matrix_{label}_{suffix}"
        return repository.create(
            import_id=import_id,
            import_type="bacnet_points",
            original_filename=f"points-{label}.csv",
            stored_file_path=f"protected/{import_id}.csv",
            summary={
                "import_id": import_id,
                "accepted_rows": 0,
                "rejected_rows": 0,
            },
            accepted_rows=[],
            project_id=scope[0],
            site_id=scope[1],
        )

    def _create_user(self, username: str) -> dict[str, object]:
        response = self.client.post(
            "/api/v1/users",
            json={"username": username, "role": "engineer"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _grant(self, created_user: dict[str, object], scope: tuple[str, str]) -> None:
        user = created_user["user"]
        assert isinstance(user, dict)
        response = self.client.post(
            f"/api/v1/users/{user['id']}/scope-grants",
            json={
                "project_id": scope[0],
                "site_id": scope[1],
                "reason": "Project/site API isolation matrix",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    @staticmethod
    def _headers(created_user: dict[str, object]) -> dict[str, str]:
        return {"X-API-Key": str(created_user["api_key"])}

    def _post_discovery(
        self,
        created_user: dict[str, object],
        scope: tuple[str, str],
    ):
        return self.client.post(
            "/api/v1/discovery/mqtt/runs",
            headers=self._headers(created_user),
            json={
                "project_id": scope[0],
                "site_id": scope[1],
                "job_type": "mqtt_discovery",
                "parameters": {"dry_run": True, "capture_seconds": 1},
            },
        )

    def _post_validation(
        self,
        created_user: dict[str, object],
        scope: tuple[str, str],
        parameters: dict[str, object] | None = None,
    ):
        return self.client.post(
            "/api/v1/validation/bacnet/runs",
            headers=self._headers(created_user),
            json={
                "project_id": scope[0],
                "site_id": scope[1],
                "job_type": "bacnet_validation",
                "parameters": parameters
                or {
                    "expected_points": [
                        {
                            "Expected point name": "SupplyAirTemp",
                            "Expected value": "20",
                            "Expected value type": "number",
                            "Required/optional flag": "required",
                        }
                    ],
                    "observed_points": [
                        {
                            "point_name": "SupplyAirTemp",
                            "observed_value": {"value": 20.0},
                        }
                    ],
                },
            },
        )

    def _create_report(
        self,
        created_user: dict[str, object],
        scope: tuple[str, str],
        *,
        source_run_ids: list[str] | None = None,
    ):
        return self.client.post(
            "/api/v1/reports",
            headers=self._headers(created_user),
            json={
                "project_id": scope[0],
                "site_id": scope[1],
                "report_type": "evidence_pack",
                "output_format": "zip",
                "source_run_ids": source_run_ids or [],
            },
        )

    def _assert_concealed(
        self,
        foreign_id: str,
        foreign_response,
        missing_id: str,
        missing_response,
    ) -> None:
        """Foreign and absent IDs share status, response shape, and message template."""

        self.assertEqual(foreign_response.status_code, 404, foreign_response.text)
        self.assertEqual(missing_response.status_code, 404, missing_response.text)
        foreign_body = foreign_response.json()
        missing_body = missing_response.json()
        self.assertEqual(set(foreign_body), set(missing_body))

        def normalized(body: object, candidate: str) -> str:
            return json.dumps(body, sort_keys=True).replace(candidate, "<resource-id>")

        self.assertEqual(
            normalized(foreign_body, foreign_id),
            normalized(missing_body, missing_id),
        )

    def test_discovery_create_list_read_results_and_export_are_scope_bound(self) -> None:
        created_a = self._post_discovery(self.user_a, self.scope_a)
        created_b = self._post_discovery(self.user_b, self.scope_b)
        self.assertEqual(created_a.status_code, 200, created_a.text)
        self.assertEqual(created_b.status_code, 200, created_b.text)
        run_a = created_a.json()["run_id"]
        run_b = created_b.json()["run_id"]
        headers_a = self._headers(self.user_a)

        denied_scope = self._post_discovery(self.user_a, self.scope_b)
        missing_scope = self._post_discovery(self.user_a, self.missing_scope)
        self.assertEqual(denied_scope.status_code, 404, denied_scope.text)
        self.assertEqual(missing_scope.status_code, 404, missing_scope.text)
        self.assertEqual(denied_scope.json(), missing_scope.json())

        listing = self.client.get("/api/v1/discovery/runs", headers=headers_a)
        self.assertEqual(listing.status_code, 200, listing.text)
        visible_ids = {item["run_id"] for item in listing.json()["runs"]}
        self.assertIn(run_a, visible_ids)
        self.assertIn(self.seed_discovery_a["run_id"], visible_ids)
        self.assertNotIn(run_b, visible_ids)
        self.assertNotIn(self.seed_discovery_b["run_id"], visible_ids)

        paths = (
            "/api/v1/discovery/runs/{run_id}",
            "/api/v1/discovery/runs/{run_id}/results",
            "/api/v1/discovery/runs/{run_id}/topics.xlsx",
        )
        for path in paths:
            with self.subTest(path=path):
                owned = self.client.get(path.format(run_id=run_a), headers=headers_a)
                self.assertEqual(owned.status_code, 200, owned.text)
                self._assert_concealed(
                    run_b,
                    self.client.get(path.format(run_id=run_b), headers=headers_a),
                    self.missing_run_id,
                    self.client.get(
                        path.format(run_id=self.missing_run_id),
                        headers=headers_a,
                    ),
                )

    def test_validation_create_list_read_export_and_references_are_scope_bound(self) -> None:
        created_a = self._post_validation(self.user_a, self.scope_a)
        created_b = self._post_validation(self.user_b, self.scope_b)
        self.assertEqual(created_a.status_code, 200, created_a.text)
        self.assertEqual(created_b.status_code, 200, created_b.text)
        run_a = created_a.json()["run_id"]
        run_b = created_b.json()["run_id"]
        headers_a = self._headers(self.user_a)

        denied_scope = self._post_validation(self.user_a, self.scope_b)
        missing_scope = self._post_validation(self.user_a, self.missing_scope)
        self.assertEqual(denied_scope.status_code, 404, denied_scope.text)
        self.assertEqual(missing_scope.status_code, 404, missing_scope.text)
        self.assertEqual(denied_scope.json(), missing_scope.json())

        listing = self.client.get("/api/v1/validation/runs", headers=headers_a)
        self.assertEqual(listing.status_code, 200, listing.text)
        visible_ids = {item["run_id"] for item in listing.json()["runs"]}
        self.assertIn(run_a, visible_ids)
        self.assertIn(self.udmi_a["run_id"], visible_ids)
        self.assertNotIn(run_b, visible_ids)
        self.assertNotIn(self.udmi_b["run_id"], visible_ids)

        for path in (
            "/api/v1/validation/runs/{run_id}",
            "/api/v1/validation/runs/{run_id}/issues",
        ):
            with self.subTest(path=path):
                owned = self.client.get(path.format(run_id=run_a), headers=headers_a)
                self.assertEqual(owned.status_code, 200, owned.text)
                self._assert_concealed(
                    run_b,
                    self.client.get(path.format(run_id=run_b), headers=headers_a),
                    self.missing_run_id,
                    self.client.get(
                        path.format(run_id=self.missing_run_id),
                        headers=headers_a,
                    ),
                )

        export_path = "/api/v1/validation/runs/{run_id}/export.json"
        owned_export = self.client.get(
            export_path.format(run_id=self.udmi_a["run_id"]),
            headers=headers_a,
        )
        self.assertEqual(owned_export.status_code, 200, owned_export.text)
        self.assertEqual(owned_export.headers["content-type"], "application/json")
        self._assert_concealed(
            str(self.udmi_b["run_id"]),
            self.client.get(
                export_path.format(run_id=self.udmi_b["run_id"]),
                headers=headers_a,
            ),
            self.missing_run_id,
            self.client.get(
                export_path.format(run_id=self.missing_run_id),
                headers=headers_a,
            ),
        )

        owned_refs = self._post_validation(
            self.user_a,
            self.scope_a,
            {
                "import_id": self.import_a["import_id"],
                "discovery_run_id": self.seed_discovery_a["run_id"],
            },
        )
        self.assertEqual(owned_refs.status_code, 200, owned_refs.text)

        foreign_import = self._post_validation(
            self.user_a,
            self.scope_a,
            {"import_id": self.import_b["import_id"]},
        )
        missing_import = self._post_validation(
            self.user_a,
            self.scope_a,
            {"import_id": self.missing_import_id},
        )
        self._assert_concealed(
            str(self.import_b["import_id"]),
            foreign_import,
            self.missing_import_id,
            missing_import,
        )

        foreign_run = self._post_validation(
            self.user_a,
            self.scope_a,
            {"discovery_run_id": self.seed_discovery_b["run_id"]},
        )
        missing_run = self._post_validation(
            self.user_a,
            self.scope_a,
            {"discovery_run_id": self.missing_run_id},
        )
        self._assert_concealed(
            str(self.seed_discovery_b["run_id"]),
            foreign_run,
            self.missing_run_id,
            missing_run,
        )

    def test_report_create_list_get_download_export_and_delete_are_scope_bound(self) -> None:
        report_a = self._create_report(self.user_a, self.scope_a)
        report_a_delete = self._create_report(self.user_a, self.scope_a)
        report_b = self._create_report(self.user_b, self.scope_b)
        for response in (report_a, report_a_delete, report_b):
            self.assertEqual(response.status_code, 200, response.text)
        report_a_id = report_a.json()["report_id"]
        report_a_delete_id = report_a_delete.json()["report_id"]
        report_b_id = report_b.json()["report_id"]
        headers_a = self._headers(self.user_a)
        headers_b = self._headers(self.user_b)

        denied_scope = self._create_report(self.user_a, self.scope_b)
        missing_scope = self._create_report(self.user_a, self.missing_scope)
        self.assertEqual(denied_scope.status_code, 404, denied_scope.text)
        self.assertEqual(missing_scope.status_code, 404, missing_scope.text)
        self.assertEqual(denied_scope.json(), missing_scope.json())

        foreign_source = self._create_report(
            self.user_a,
            self.scope_a,
            source_run_ids=[str(self.udmi_b["run_id"])],
        )
        missing_source = self._create_report(
            self.user_a,
            self.scope_a,
            source_run_ids=[self.missing_run_id],
        )
        self._assert_concealed(
            str(self.udmi_b["run_id"]),
            foreign_source,
            self.missing_run_id,
            missing_source,
        )

        listing = self.client.get("/api/v1/reports", headers=headers_a)
        self.assertEqual(listing.status_code, 200, listing.text)
        visible_ids = {item["report_id"] for item in listing.json()["reports"]}
        self.assertIn(report_a_id, visible_ids)
        self.assertIn(report_a_delete_id, visible_ids)
        self.assertNotIn(report_b_id, visible_ids)

        for path in (
            "/api/v1/reports/{report_id}",
            "/api/v1/reports/{report_id}/download",
        ):
            with self.subTest(path=path):
                owned = self.client.get(path.format(report_id=report_a_id), headers=headers_a)
                self.assertEqual(owned.status_code, 200, owned.text)
                self._assert_concealed(
                    report_b_id,
                    self.client.get(
                        path.format(report_id=report_b_id),
                        headers=headers_a,
                    ),
                    self.missing_report_id,
                    self.client.get(
                        path.format(report_id=self.missing_report_id),
                        headers=headers_a,
                    ),
                )

        owned_export = self.client.post(
            "/api/v1/reports/export",
            headers=headers_a,
            json={"report_ids": [report_a_id]},
        )
        self.assertEqual(owned_export.status_code, 200, owned_export.text)
        self.assertEqual(owned_export.headers["content-type"], "application/zip")
        self._assert_concealed(
            report_b_id,
            self.client.post(
                "/api/v1/reports/export",
                headers=headers_a,
                json={"report_ids": [report_b_id]},
            ),
            self.missing_report_id,
            self.client.post(
                "/api/v1/reports/export",
                headers=headers_a,
                json={"report_ids": [self.missing_report_id]},
            ),
        )

        self._assert_concealed(
            report_b_id,
            self.client.post(
                "/api/v1/reports/delete",
                headers=headers_a,
                json={"report_ids": [report_b_id]},
            ),
            self.missing_report_id,
            self.client.post(
                "/api/v1/reports/delete",
                headers=headers_a,
                json={"report_ids": [self.missing_report_id]},
            ),
        )
        still_foreign = self.client.get(
            f"/api/v1/reports/{report_b_id}",
            headers=headers_b,
        )
        self.assertEqual(still_foreign.status_code, 200, still_foreign.text)

        deleted = self.client.post(
            "/api/v1/reports/delete",
            headers=headers_a,
            json={"report_ids": [report_a_delete_id]},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_report_ids"], [report_a_delete_id])
        gone = self.client.get(f"/api/v1/reports/{report_a_delete_id}", headers=headers_a)
        self.assertEqual(gone.status_code, 404, gone.text)

    def test_evidence_verify_is_scope_bound(self) -> None:
        report_a = self._create_report(self.user_a, self.scope_a)
        report_b = self._create_report(self.user_b, self.scope_b)
        self.assertEqual(report_a.status_code, 200, report_a.text)
        self.assertEqual(report_b.status_code, 200, report_b.text)
        report_a_id = report_a.json()["report_id"]
        report_b_id = report_b.json()["report_id"]
        headers_a = self._headers(self.user_a)

        verified = self.client.get(
            f"/api/v1/evidence/reports/{report_a_id}/verify",
            headers=headers_a,
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()["hash_matches"], verified.text)
        self.assertTrue(verified.json()["signature_valid"], verified.text)

        self._assert_concealed(
            report_b_id,
            self.client.get(
                f"/api/v1/evidence/reports/{report_b_id}/verify",
                headers=headers_a,
            ),
            self.missing_report_id,
            self.client.get(
                f"/api/v1/evidence/reports/{self.missing_report_id}/verify",
                headers=headers_a,
            ),
        )

    def test_report_serving_rejects_a_foreign_signed_manifest_swap(self) -> None:
        """A scoped report id never becomes an alias for another scope's bytes."""

        report_a = self._create_report(self.user_a, self.scope_a)
        report_b = self._create_report(self.user_b, self.scope_b)
        self.assertEqual(report_a.status_code, 200, report_a.text)
        self.assertEqual(report_b.status_code, 200, report_b.text)
        report_a_id = report_a.json()["report_id"]
        report_b_id = report_b.json()["report_id"]
        headers_a = self._headers(self.user_a)
        headers_b = self._headers(self.user_b)

        report_b_download = self.client.get(
            f"/api/v1/reports/{report_b_id}/download",
            headers=headers_b,
        )
        self.assertEqual(report_b_download.status_code, 200, report_b_download.text)

        from smart_commissioning_core.db.models import Run
        from sqlalchemy import select, update

        with get_engine().begin() as connection:
            original_summary_a = dict(connection.scalar(select(Run.result_summary).where(Run.id == report_a_id)))
            summary_a = dict(original_summary_a)
            summary_b = dict(connection.scalar(select(Run.result_summary).where(Run.id == report_b_id)))
            foreign_manifest = dict(summary_b["artifact_manifest"])
            self.assertEqual(foreign_manifest["report_id"], report_b_id)
            summary_a["artifact_manifest"] = foreign_manifest
            connection.execute(update(Run).where(Run.id == report_a_id).values(result_summary=summary_a))

        try:
            download = self.client.get(
                f"/api/v1/reports/{report_a_id}/download",
                headers=headers_a,
            )
            self.assertEqual(download.status_code, 404, download.text)
            self.assertNotEqual(download.content, report_b_download.content)

            export = self.client.post(
                "/api/v1/reports/export",
                headers=headers_a,
                json={"report_ids": [report_a_id]},
            )
            self.assertEqual(export.status_code, 404, export.text)
            self.assertNotEqual(export.content, report_b_download.content)

            verify = self.client.get(
                f"/api/v1/evidence/reports/{report_a_id}/verify",
                headers=headers_a,
            )
            self.assertEqual(verify.status_code, 404, verify.text)
            self.assertFalse(verify.json().get("signature_valid", False), verify.text)
        finally:
            with get_engine().begin() as connection:
                connection.execute(update(Run).where(Run.id == report_a_id).values(result_summary=original_summary_a))

    def test_report_scope_and_metadata_drift_fail_before_artifact_bytes(self) -> None:
        """Mutable ownership and metadata never authorize a sealed report."""

        from smart_commissioning_core.db.models import Run
        from sqlalchemy import select, update

        headers = self._headers(self.user_a)
        mutations = (
            ("project", {"project_id": self.scope_b[0]}),
            ("site", {"site_id": self.scope_b[1]}),
            ("metadata", {"report_title": "Mutated after sealing"}),
        )
        for label, mutation in mutations:
            with self.subTest(label=label):
                created = self._create_report(self.user_a, self.scope_a)
                self.assertEqual(created.status_code, 200, created.text)
                report_id = created.json()["report_id"]
                original = self.client.get(
                    f"/api/v1/reports/{report_id}/download",
                    headers=headers,
                )
                self.assertEqual(original.status_code, 200, original.text)

                with get_engine().begin() as connection:
                    stored = connection.execute(
                        select(
                            Run.project_id,
                            Run.site_id,
                            Run.parameters,
                        ).where(Run.id == report_id)
                    ).one()
                    original_project_id = stored.project_id
                    original_site_id = stored.site_id
                    original_parameters = dict(stored.parameters)
                    if label == "metadata":
                        parameters = dict(original_parameters)
                        parameters.update(mutation)
                        values = {"parameters": parameters}
                    else:
                        values = mutation
                    connection.execute(update(Run).where(Run.id == report_id).values(**values))

                try:
                    download = self.client.get(
                        f"/api/v1/reports/{report_id}/download",
                        headers=headers,
                    )
                    self.assertEqual(download.status_code, 404, download.text)
                    self.assertNotEqual(download.content, original.content)

                    detail = self.client.get(
                        f"/api/v1/reports/{report_id}",
                        headers=headers,
                    )
                    self.assertEqual(detail.status_code, 404, detail.text)

                    export = self.client.post(
                        "/api/v1/reports/export",
                        headers=headers,
                        json={"report_ids": [report_id]},
                    )
                    self.assertEqual(export.status_code, 404, export.text)

                    verify = self.client.get(
                        f"/api/v1/evidence/reports/{report_id}/verify",
                        headers=headers,
                    )
                    self.assertEqual(verify.status_code, 404, verify.text)

                    for user in (self.user_a, self.user_b):
                        listing = self.client.get(
                            "/api/v1/reports",
                            headers=self._headers(user),
                        )
                        if label == "metadata" and user == self.user_a:
                            self.assertEqual(listing.status_code, 409, listing.text)
                            self.assertEqual(
                                listing.json(),
                                {"detail": ("Stored report evidence failed integrity verification.")},
                            )
                        else:
                            self.assertEqual(listing.status_code, 200, listing.text)
                            self.assertNotIn(
                                report_id,
                                {row["report_id"] for row in listing.json()["reports"]},
                            )
                finally:
                    with get_engine().begin() as connection:
                        connection.execute(
                            update(Run)
                            .where(Run.id == report_id)
                            .values(
                                project_id=original_project_id,
                                site_id=original_site_id,
                                parameters=original_parameters,
                            )
                        )


if __name__ == "__main__":
    import unittest

    unittest.main()
