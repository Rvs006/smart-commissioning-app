"""Release-blocking v0.1.26 API reliability contracts.

These tests deliberately describe the target behavior before the legacy report
and lifecycle paths are removed: active sources are rejected for every report
type, reports are materialized once, read endpoints execute no lifecycle DML,
and later source-row mutation cannot change stored report bytes.
"""

from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest import mock

from harness import ApiTestCase

_API_KEY = "test-v0126-reliability-key"
_ARTIFACTS_ROOT = tempfile.mkdtemp(prefix="sct-v0126-artifacts-")
atexit.register(shutil.rmtree, _ARTIFACTS_ROOT, ignore_errors=True)


class V0126ReliabilityContracts(ApiTestCase):
    env = {
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
        "JOB_EXECUTION_MODE": "inline",
        "SMART_COMMISSIONING_ARTIFACTS_ROOT": _ARTIFACTS_ROOT,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def _new_run(
        self,
        job_type: str,
        *,
        terminal: bool,
        parameters: dict[str, object] | None = None,
    ) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        service = RunService()
        run = service.create_job_run(
            JobCreateRequest(
                project_id="reliability-project",
                site_id="reliability-site",
                job_type=job_type,
                parameters={
                    "requested_at": datetime.now(UTC).isoformat(),
                    **(parameters or {}),
                },
            ),
            expected_job_type=job_type,
        )
        if terminal:
            owned = service.claim_owned_run(run.run_id)
            self.assertIsNotNone(owned)
            owned.update_result_summary(
                run.run_id,
                {"discovered_assets": [{"asset_id": "asset-a", "ip_address": "192.0.2.10"}]},
            )
            owned.update_run_status(
                run.run_id,
                status="succeeded",
                stage="complete",
                progress_percent=100,
            )
        return run.run_id

    def _create_report(self, source_run_id: str) -> str:
        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "reliability-project",
                "site_id": "reliability-site",
                "report_type": "ip_discovery",
                "output_format": "zip",
                "source_run_ids": [source_run_id],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return str(response.json()["report_id"])

    def test_every_report_type_rejects_an_active_source(self) -> None:
        source_run_id = self._new_run("ip_discovery", terminal=False)

        response = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "reliability-project",
                "site_id": "reliability-site",
                "report_type": "ip_discovery",
                "output_format": "zip",
                "source_run_ids": [source_run_id],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("not terminal", response.text)

    def test_one_hundred_downloads_are_read_only_and_return_one_hash(self) -> None:
        from app.services.run_service import RunService
        from sqlalchemy import event

        source_run_id = self._new_run("ip_discovery", terminal=True)
        report_id = self._create_report(source_run_id)
        engine = RunService().engine
        dml: list[str] = []

        def record_dml(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in {"DELETE", "INSERT", "UPDATE"}:
                dml.append(statement)

        event.listen(engine, "before_cursor_execute", record_dml)
        try:
            with ThreadPoolExecutor(max_workers=20) as pool:
                responses = list(
                    pool.map(
                        lambda _index: self.client.get(
                            f"/api/v1/reports/{report_id}/download"
                        ),
                        range(100),
                    )
                )
        finally:
            event.remove(engine, "before_cursor_execute", record_dml)

        self.assertTrue(all(response.status_code == 200 for response in responses))
        hashes = {hashlib.sha256(response.content).hexdigest() for response in responses}
        self.assertEqual(len(hashes), 1)
        self.assertEqual(dml, [], "report downloads must execute no database writes")

    def test_report_bytes_survive_source_import_config_and_renderer_mutation(self) -> None:
        from app.services.run_service import RunService
        from smart_commissioning_core.db.models import ImportRecord, Run
        from smart_commissioning_core.db.repositories import (
            ConfigurationRepository,
            ImportRepository,
        )
        from sqlalchemy import update

        service = RunService()
        import_id = "imp_v0126_frozen"
        ImportRepository(service.engine).create(
            import_id=import_id,
            import_type="ip_register",
            original_filename="frozen.csv",
            stored_file_path="imports/files/frozen.csv",
            summary={"accepted_rows": 1},
            accepted_rows=[{"asset_id": "asset-a", "ip_address": "192.0.2.10"}],
            project_id="reliability-project",
            site_id="reliability-site",
        )
        source_run_id = self._new_run(
            "ip_discovery",
            terminal=True,
            parameters={"register_import_id": import_id},
        )
        report_id = self._create_report(source_run_id)
        before = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(before.status_code, 200, before.text)

        report_run = service.get_run(report_id)
        snapshot = report_run.parameters["report_snapshot_v2"]
        self.assertEqual(snapshot["source_run_ids"], [source_run_id])
        self.assertIn(source_run_id, snapshot["source_result_hashes"])
        self.assertIn(source_run_id, snapshot["configuration_provenance"])
        self.assertIn("renderer_version", snapshot)
        self.assertIn("displayed_counts", snapshot)

        # Bypass the public sealed-result contract on purpose. Stored report
        # bytes must remain stable even if a legacy/test tool mutates every
        # mutable input after the artifact has been sealed.
        with service.engine.begin() as connection:
            connection.execute(
                update(Run)
                .where(Run.id == source_run_id)
                .values(result_summary={"discovered_assets": [{"asset_id": "changed"}]})
            )
            connection.execute(
                update(ImportRecord)
                .where(ImportRecord.import_id == import_id)
                .values(accepted_rows=[{"asset_id": "changed-import"}])
            )
        ConfigurationRepository(service.engine).save(
            "reliability-project",
            "reliability-site",
            {"mqtt": {"values": {"Port": "1883"}}},
        )

        with mock.patch(
            "app.api.routes.reports._build_report_artifact",
            side_effect=AssertionError("stored downloads must not invoke the current renderer"),
        ):
            after = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(before.content, after.content)

    def test_canary_credential_never_crosses_a_release_boundary(self) -> None:
        from app.services.job_queue import RunEnqueuer
        from app.services.run_service import RunService
        from smart_commissioning_core.sync import build_sync_bundle
        from smart_commissioning_core.sync_identity import (
            load_edge_signing_key,
            load_or_create_edge_identity,
        )

        canary = "v0126-canary-credential-7f42d9"
        configuration = self.client.get("/api/v1/configuration")
        self.assertEqual(configuration.status_code, 200, configuration.text)
        configuration_payload = configuration.json()
        configuration_payload["mqtt"]["values"]["MQTT Password"] = canary
        saved = self.client.put("/api/v1/configuration", json=configuration_payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        compatibility_export = self.client.get(
            "/api/v1/configuration/export-with-secrets"
        )
        self.assertEqual(
            compatibility_export.status_code, 200, compatibility_export.text
        )
        self.assertNotIn(canary, compatibility_export.text)
        self.assertFalse(compatibility_export.json()["secrets_included"])

        with self.assertLogs(level="INFO") as captured_logs:
            source_run_id = self._new_run(
                "ip_discovery",
                terminal=True,
                parameters={"password": canary},
            )
        service = RunService()
        stored = service.get_run(source_run_id)
        self.assertNotIn(canary, str(stored.parameters))

        api_run = self.client.get(f"/api/v1/discovery/runs/{source_run_id}")
        self.assertEqual(api_run.status_code, 200, api_run.text)
        self.assertNotIn(canary, api_run.text)

        report_id = self._create_report(source_run_id)
        report = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertNotIn(canary.encode("utf-8"), report.content)

        captured_message: dict[str, object] = {}

        class CaptureQueue:
            def _enqueue(self, **message):
                captured_message.update(message)
                return object()

        dispatch = service.get_dispatch_for_run(source_run_id)
        RunEnqueuer(CaptureQueue(), "discover_ip_range", "discovery")(
            stored, dispatch.dispatch_id
        )
        self.assertNotIn(canary, repr(captured_message))
        self.assertEqual(
            captured_message["args"],
            (source_run_id, dispatch.dispatch_id),
        )

        with tempfile.TemporaryDirectory(prefix="sct-canary-sync-") as temp_dir:
            identity = load_or_create_edge_identity(temp_dir, edge_id="canary-edge")
            signing_key = load_edge_signing_key(temp_dir)
            bundle = build_sync_bundle(
                service.engine,
                run_ids=[source_run_id],
                signing_key=signing_key,
                edge_identity=identity,
                created_at=datetime(2026, 7, 26, tzinfo=UTC),
            )
        self.assertNotIn(canary.encode("utf-8"), bundle)
        self.assertNotIn(canary, "\n".join(captured_logs.output))


if __name__ == "__main__":
    import unittest

    unittest.main()
