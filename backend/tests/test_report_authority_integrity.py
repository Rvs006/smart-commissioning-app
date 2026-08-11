"""Reports refuse scan evidence whose selected import changed after preview."""

from __future__ import annotations

import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import mock

from harness import ApiTestCase

_API_KEY = "test-report-authority-integrity-key"
_PROJECT = "report-authority-project"
_SITE = "report-authority-site"


class ReportAuthorityIntegrityTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def _preview_from_import(self) -> tuple[str, str]:
        csv = (
            b"Project/site,System,Asset ID,Asset name,Expected IP address,"
            b"Expected services/ports\n"
            b"Site A,BMS,AHU-1,AHU 1,10.23.0.8,80/tcp\n"
        )
        uploaded = self.client.post(
            "/api/v1/imports",
            data={
                "import_type": "ip_register",
                "project_id": _PROJECT,
                "site_id": _SITE,
            },
            files={"file": ("ip-register.csv", io.BytesIO(csv), "text/csv")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        import_id = uploaded.json()["import_id"]

        preview = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={
                "project_id": _PROJECT,
                "site_id": _SITE,
                "job_type": "ip_discovery",
                "parameters": {"dry_run": True, "ports": [80]},
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["status"], "succeeded")
        preview_run_id = preview.json()["run_id"]

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import RunExecutionContext
        from smart_commissioning_core.execution_context import scan_authority_bindings
        from smart_commissioning_core.run_context import RunContextV1
        from sqlalchemy import select

        with get_engine().connect() as connection:
            stored_context = connection.scalar(
                select(RunExecutionContext.context_json).where(RunExecutionContext.run_id == preview_run_id)
            )
        selected_import_ids = list(scan_authority_bindings(RunContextV1.model_validate(stored_context)))
        self.assertEqual(len(selected_import_ids), 1)
        selected_import_id = selected_import_ids[0]
        self.assertTrue(import_id)
        return selected_import_id, preview_run_id

    def _create_report(self, source_run_id: str):
        return self.client.post(
            "/api/v1/reports",
            json={
                "project_id": _PROJECT,
                "site_id": _SITE,
                "report_type": "ip_discovery",
                "output_format": "zip",
                "source_run_ids": [source_run_id],
            },
        )

    def _large_sealed_ip_source(self, *, device_count: int = 512) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService
        from lifecycle_helpers import finish_run

        documentation_prefixes = ("192.0.2", "198.51.100")
        service = RunService()
        run = service.create_job_run(
            JobCreateRequest(
                project_id=_PROJECT,
                site_id=_SITE,
                job_type="ip_discovery",
                parameters={},
            ),
            expected_job_type="ip_discovery",
        )
        devices = [
            {
                "project_id": _PROJECT,
                "site_id": _SITE,
                "address": (
                    f"{documentation_prefixes[index // 256]}.{index % 256}"
                ),
                "device_type": "ip_host",
                "name": f"controller-{index:04d}.example.test",
                "attributes": {"open_ports": [80, 443]},
            }
            for index in range(device_count)
        ]
        finish_run(
            service,
            run.run_id,
            summary={
                "hosts_scanned": device_count,
                "hosts_responsive": device_count,
            },
            devices=devices,
        )
        return run.run_id

    def test_a_valid_sealed_report_can_be_frozen_as_source_evidence(self) -> None:
        source = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": _PROJECT,
                "site_id": _SITE,
                "report_type": "ip_discovery",
                "output_format": "zip",
                "source_run_ids": [],
            },
        )
        self.assertEqual(source.status_code, 200, source.text)

        report = self._create_report(source.json()["report_id"])

        self.assertEqual(report.status_code, 200, report.text)

    def test_report_snapshot_tamper_is_rejected_before_refreezing(self) -> None:
        source = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": _PROJECT,
                "site_id": _SITE,
                "report_type": "ip_discovery",
                "output_format": "zip",
                "source_run_ids": [],
            },
        )
        self.assertEqual(source.status_code, 200, source.text)
        source_run_id = source.json()["report_id"]

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import Run
        from smart_commissioning_core.run_context import canonical_sha256
        from sqlalchemy import func, select, update

        with get_engine().begin() as connection:
            parameters = connection.scalar(select(Run.parameters).where(Run.id == source_run_id))
            original_parameters = dict(parameters)
            tampered = dict(parameters)
            snapshot = dict(tampered["report_snapshot_v2"])
            snapshot["tampered"] = True
            tampered["report_snapshot_v2"] = snapshot
            tampered["report_snapshot_sha256"] = canonical_sha256(snapshot)
            connection.execute(update(Run).where(Run.id == source_run_id).values(parameters=tampered))
            report_count_before = connection.scalar(
                select(func.count()).select_from(Run).where(Run.job_type == "report_generation")
            )

        try:
            report = self._create_report(source_run_id)

            self.assertEqual(report.status_code, 422, report.text)
            self.assertIn("Source report", report.json()["detail"])
            self.assertIn("run seal", report.json()["detail"])
            with get_engine().connect() as connection:
                report_count_after = connection.scalar(
                    select(func.count()).select_from(Run).where(Run.job_type == "report_generation")
                )
            self.assertEqual(report_count_after, report_count_before)
        finally:
            with get_engine().begin() as connection:
                connection.execute(update(Run).where(Run.id == source_run_id).values(parameters=original_parameters))

    def test_report_rehashes_the_selected_import_before_freezing_evidence(self) -> None:
        import_id, preview_run_id = self._preview_from_import()

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import ImportRecord
        from sqlalchemy import update

        with get_engine().begin() as connection:
            connection.execute(
                update(ImportRecord)
                .where(ImportRecord.import_id == import_id)
                .values(
                    accepted_rows=[
                        {
                            "Project/site": "Site A",
                            "System": "BMS",
                            "Asset ID": "AHU-1",
                            "Asset name": "AHU 1",
                            "Expected IP address": "10.23.0.9",
                            "Expected services/ports": "80/tcp",
                        }
                    ]
                )
            )

        report = self._create_report(preview_run_id)

        self.assertEqual(report.status_code, 422, report.text)
        self.assertIn("failed integrity verification", report.json()["detail"])

    def test_report_recomputes_the_stored_context_digest(self) -> None:
        _, preview_run_id = self._preview_from_import()

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import RunExecutionContext
        from sqlalchemy import select, update

        with get_engine().begin() as connection:
            context_json = connection.execute(
                select(RunExecutionContext.context_json).where(RunExecutionContext.run_id == preview_run_id)
            ).scalar_one()
            tampered = dict(context_json)
            tampered["application_version"] = "tampered-after-seal"
            connection.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == preview_run_id)
                .values(context_json=tampered)
            )

        report = self._create_report(preview_run_id)

        self.assertEqual(report.status_code, 422, report.text)
        self.assertIn("execution context failed integrity verification", report.json()["detail"])

    def test_report_rejects_coherently_tampered_summary_projections_before_writing(self) -> None:
        _, preview_run_id = self._preview_from_import()

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import Run, RunResult
        from sqlalchemy import func, select, update

        engine = get_engine()
        with engine.begin() as connection:
            report_count_before = connection.scalar(
                select(func.count()).select_from(Run).where(Run.job_type == "report_generation")
            )
            tampered_summary = {"devices_found": 999, "tampered": True}
            connection.execute(update(Run).where(Run.id == preview_run_id).values(result_summary=tampered_summary))
            connection.execute(
                update(RunResult).where(RunResult.run_id == preview_run_id).values(summary=tampered_summary)
            )

        report = self._create_report(preview_run_id)

        self.assertEqual(report.status_code, 422, report.text)
        self.assertIn("sealed result", report.json()["detail"])
        with engine.connect() as connection:
            report_count_after = connection.scalar(
                select(func.count()).select_from(Run).where(Run.job_type == "report_generation")
            )
        self.assertEqual(report_count_after, report_count_before)

    def test_report_rejects_terminal_payload_whose_canonical_hash_changed(self) -> None:
        _, preview_run_id = self._preview_from_import()

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import RunResult
        from sqlalchemy import select, update

        with get_engine().begin() as connection:
            payload = connection.scalar(select(RunResult.result_payload).where(RunResult.run_id == preview_run_id))
            tampered_payload = dict(payload)
            tampered_payload["stage"] = "tampered_terminal_stage"
            connection.execute(
                update(RunResult).where(RunResult.run_id == preview_run_id).values(result_payload=tampered_payload)
            )

        report = self._create_report(preview_run_id)

        self.assertEqual(report.status_code, 422, report.text)
        self.assertIn("canonical digest", report.json()["detail"])

    def test_report_rejects_canonical_context_not_bound_to_the_run_seal(self) -> None:
        _, preview_run_id = self._preview_from_import()

        from app.core.db import get_engine
        from smart_commissioning_core.db.models import RunExecutionContext
        from smart_commissioning_core.run_context import RunContextV1
        from sqlalchemy import select, update

        with get_engine().begin() as connection:
            context_json = connection.scalar(
                select(RunExecutionContext.context_json).where(RunExecutionContext.run_id == preview_run_id)
            )
            tampered_context = dict(context_json)
            tampered_context["requesting_principal"] = "tampered-but-canonical"
            tampered_digest = RunContextV1.model_validate(tampered_context).sha256()
            connection.execute(
                update(RunExecutionContext)
                .where(RunExecutionContext.run_id == preview_run_id)
                .values(
                    context_json=tampered_context,
                    context_sha256=tampered_digest,
                )
            )

        report = self._create_report(preview_run_id)

        self.assertEqual(report.status_code, 422, report.text)
        self.assertIn("execution context", report.json()["detail"])
        self.assertIn("run seal", report.json()["detail"])

    def test_large_source_verification_releases_sqlite_writer_before_cpu_work(self) -> None:
        from app.services import run_service as run_service_module

        source_run_id = self._large_sealed_ip_source()
        verification_started = Event()
        release_verification = Event()
        unrelated_writer_started = Event()
        unrelated_writer_committed = Event()
        real_verifier = run_service_module.verify_sealed_run

        def paused_verifier(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if kwargs.get("run_id") == source_run_id:
                if len(kwargs.get("devices", ())) != 512:
                    raise AssertionError("large source projections were not copied for verification")
                verification_started.set()
                if not release_verification.wait(timeout=10):
                    raise AssertionError("test did not release report source verification")
            return real_verifier(*args, **kwargs)

        def create_unrelated_report():  # noqa: ANN202
            unrelated_writer_started.set()
            response = self.client.post(
                "/api/v1/reports",
                json={
                    "project_id": _PROJECT,
                    "site_id": _SITE,
                    "report_type": "ip_discovery",
                    "output_format": "zip",
                    "source_run_ids": [],
                },
            )
            unrelated_writer_committed.set()
            return response

        with (
            mock.patch(
                "app.services.run_service.verify_sealed_run",
                side_effect=paused_verifier,
            ),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            source_report_future = pool.submit(self._create_report, source_run_id)
            self.assertTrue(verification_started.wait(timeout=5))
            unrelated_report_future = pool.submit(create_unrelated_report)
            self.assertTrue(unrelated_writer_started.wait(timeout=2))
            committed_before_verification_resumed = unrelated_writer_committed.wait(timeout=2)
            release_verification.set()
            unrelated_report = unrelated_report_future.result(timeout=10)
            source_report = source_report_future.result(timeout=10)

        self.assertTrue(
            committed_before_verification_resumed,
            "report source verification held SQLite's writer reservation",
        )
        self.assertEqual(unrelated_report.status_code, 200, unrelated_report.text)
        self.assertEqual(source_report.status_code, 200, source_report.text)


if __name__ == "__main__":
    unittest.main()
