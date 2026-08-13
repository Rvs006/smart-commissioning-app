import json
import runpy
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
    session_factory,
)
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.models import (
    Project,
    ReportEvidenceContract,
    RunResult,
    RunSeal,
    Site,
)
from smart_commissioning_core.db.repositories import (
    ConfigurationRepository,
    DiscoveryRepository,
    ImportRepository,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run
from sqlalchemy import func, inspect, select, text

# The JSON file run-record shape produced today by backend RunService
# (RunRecord.model_dump_json) and worker FileRunStore. DbRunStore must return
# dicts with exactly these keys so API responses do not change.
FILE_RECORD_KEYS = [
    "run_id",
    "job_type",
    "status",
    "stage",
    "progress_percent",
    "created_at",
    "updated_at",
    "project_id",
    "site_id",
    "parameters",
    "result_summary",
    "issues",
    "error_message",
]

ISSUE_KEYS = list(ValidationIssueRecord.model_fields)


def _issue(issue_id: str, description: str) -> dict[str, object]:
    return {
        "issue_id": issue_id,
        "asset_id": "AHU-L03-017",
        "issue_type": "unit_mismatch",
        "severity": "high",
        "description": description,
        "status": "open",
        "point_name": "supply_air_temperature_sensor",
        "topic": "demo-site/b1/ahu/l03/events/pointset",
        "expected_value": "degrees-celsius",
        "observed_value": "kelvin",
        "match_basis": "point_name",
        "suggested_action": "Fix the unit mapping.",
        "raw_evidence_uri": None,
        "status_detail": None,
        "last_seen_at": "2026-06-11T10:00:00+00:00",
    }


class DatabaseEngineConfigurationTests(unittest.TestCase):
    def test_postgres_connections_bound_connect_lock_and_statement_waits(self) -> None:
        sentinel = object()
        with patch(
            "smart_commissioning_core.db.engine.create_engine",
            return_value=sentinel,
        ) as create_engine:
            engine = create_engine_from_url(
                "postgresql+psycopg://user:password@db.example.test/app"
                "?options=-c%20search_path%3Dcommissioning"
            )

        self.assertIs(engine, sentinel)
        connect_args = create_engine.call_args.kwargs["connect_args"]
        self.assertEqual(connect_args["connect_timeout"], 10)
        self.assertEqual(connect_args["tcp_user_timeout"], 30_000)
        options = connect_args["options"]
        self.assertTrue(options.startswith("-c search_path=commissioning "))
        self.assertIn("lock_timeout=35000", options)
        self.assertIn("statement_timeout=30000", options)
        self.assertIn("idle_in_transaction_session_timeout=30000", options)


class SqliteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.runtime_root = Path(self._temp_dir.name)
        self.engine = create_engine_from_url(default_sqlite_url(self.runtime_root))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)


class RunLifecycleTests(SqliteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = DbRunStore(self.engine)

    def test_create_run_matches_file_record_shape_and_defaults(self) -> None:
        record = self.store.create_run(
            project_id="demo-project",
            site_id="demo-site",
            job_type="udmi_validation",
            parameters={"topic": "a/b"},
        )

        self.assertEqual(list(record), FILE_RECORD_KEYS)
        self.assertTrue(record["run_id"].startswith("run_"))
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["stage"], "awaiting_worker")
        self.assertEqual(record["progress_percent"], 0)
        self.assertEqual(record["parameters"], {"topic": "a/b"})
        self.assertEqual(record["result_summary"], {"queued": True, "worker_required": True})
        self.assertEqual(record["issues"], [])
        self.assertIsNone(record["error_message"])
        for key in ("created_at", "updated_at"):
            parsed = datetime.fromisoformat(record[key])
            self.assertIsNotNone(parsed.tzinfo, f"{key} must be timezone-aware")

    def test_report_creation_stamps_the_sealed_evidence_contract_atomically(self) -> None:
        report = self.store.create_run(
            project_id="demo-project",
            site_id="demo-site",
            job_type="report_generation",
        )

        with session_factory(self.engine)() as session:
            contract = session.get(ReportEvidenceContract, report["run_id"])

        self.assertIsNotNone(contract)
        self.assertEqual(contract.contract_version, "sealed_v1")
        self.assertEqual(contract.project_id, "demo-project")
        self.assertEqual(contract.site_id, "demo-site")

    def test_status_summary_and_issue_updates_roundtrip(self) -> None:
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="udmi_validation")[
            "run_id"
        ]

        record = self.store.update_run_status(
            run_id, status="running", stage="loading_udmi_fixture", progress_percent=150
        )
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["stage"], "loading_udmi_fixture")
        self.assertEqual(record["progress_percent"], 100, "progress is clamped to 0..100")

        record = self.store.update_result_summary(run_id, {"issue_count": 2, "source": "fixture"}, merge=False)
        self.assertEqual(record["result_summary"], {"issue_count": 2, "source": "fixture"})

        record = self.store.update_result_summary(run_id, {"issue_count": 3, "execution_mode": "queue"})
        self.assertEqual(
            record["result_summary"],
            {"issue_count": 3, "source": "fixture", "execution_mode": "queue"},
            "merge=True must read-modify-write the existing summary",
        )

        record = self.store.replace_issues(run_id, [_issue("iss-1", "first"), _issue("iss-2", "second")])
        self.assertEqual([issue["issue_id"] for issue in record["issues"]], ["iss-1", "iss-2"])

        record = self.store.update_run_status(run_id, status="failed", progress_percent=100, error_message="boom")
        self.assertEqual(record["error_message"], "boom")

        fetched = self.store.get_run(run_id)
        self.assertEqual(list(fetched), FILE_RECORD_KEYS)
        self.assertEqual(fetched, record, "get_run must match the last update's return value")
        for issue in fetched["issues"]:
            self.assertEqual(list(issue), ISSUE_KEYS)

    def test_terminal_run_is_not_resurrected_by_a_late_progress_write(self) -> None:
        # REGRESSION: a still-in-flight engine unit's best-effort progress write
        # (status='running') could land AFTER the terminal write and flip a failed
        # run back to running, erasing the vetted error_message and fossilizing the
        # run. Once terminal, a non-terminal status write must be dropped.
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="bacnet_discovery")[
            "run_id"
        ]
        self.store.update_run_status(run_id, status="failed", progress_percent=100, error_message="transport dead")

        # A late 'running' progress write is a no-op: status, message, progress hold.
        record = self.store.update_run_status(run_id, status="running", stage="engine_running", progress_percent=60)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_message"], "transport dead")
        self.assertEqual(record["progress_percent"], 100)
        self.assertEqual(self.store.get_run(run_id)["status"], "failed")

        # A conflicting terminal writer is also a no-op. The first terminal
        # snapshot is immutable; lifecycle-v2 accounts for the conflict separately.
        record = self.store.update_run_status(run_id, status="cancelled", error_message=None)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error_message"], "transport dead")

    def test_replace_issues_preserves_order_on_rewrite(self) -> None:
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="mqtt_config_publish")[
            "run_id"
        ]
        issues = [_issue(f"iss-{index}", f"issue {index}") for index in range(5)]

        record = self.store.replace_issues(run_id, issues)
        self.assertEqual([issue["issue_id"] for issue in record["issues"]], [f"iss-{i}" for i in range(5)])

        record = self.store.replace_issues(run_id, list(reversed(issues)))
        self.assertEqual(
            [issue["issue_id"] for issue in record["issues"]],
            [f"iss-{i}" for i in reversed(range(5))],
            "delete+reinsert must preserve the caller's ordering",
        )

    def test_replace_issues_accepts_validation_issue_records(self) -> None:
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="udmi_validation")[
            "run_id"
        ]
        record_issue = ValidationIssueRecord.model_validate(_issue("iss-model", "from model"))

        record = self.store.replace_issues(run_id, [record_issue])

        self.assertEqual(record["issues"][0]["issue_id"], "iss-model")
        self.assertEqual(record["issues"][0]["last_seen_at"], record_issue.model_dump(mode="json")["last_seen_at"])

    def test_missing_run_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.get_run("run_00000000000000_deadbeef")
        with self.assertRaises(FileNotFoundError):
            self.store.update_run_status("run_00000000000000_deadbeef", status="running")

    def test_list_runs_filters_orders_and_paginates(self) -> None:
        first = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="ip_discovery")
        second = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="udmi_validation")
        other_site = self.store.create_run(project_id="demo-project", site_id="other-site", job_type="ip_discovery")

        records = self.store.list_runs("demo-project", "demo-site")
        self.assertEqual({record["run_id"] for record in records}, {first["run_id"], second["run_id"]})
        self.assertEqual(
            [record["run_id"] for record in records],
            [
                record["run_id"]
                for record in sorted(records, key=lambda r: (r["created_at"], r["run_id"]), reverse=True)
            ],
            "list_runs must return newest-first",
        )
        for record in records:
            self.assertEqual(list(record), FILE_RECORD_KEYS)

        self.assertEqual(
            [record["run_id"] for record in self.store.list_runs("demo-project", "demo-site", "ip_discovery")],
            [first["run_id"]],
        )
        self.assertEqual(
            {record["run_id"] for record in self.store.list_runs("demo-project", job_type={"ip_discovery"})},
            {first["run_id"], other_site["run_id"]},
        )
        self.assertEqual(len(self.store.list_runs("demo-project", "demo-site", limit=1)), 1)
        self.assertEqual(len(self.store.list_runs("demo-project", "demo-site", limit=5, offset=1)), 1)
        self.assertEqual(self.store.list_runs("missing-project"), [])

    def test_project_and_site_rows_are_created_once(self) -> None:
        self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="ip_discovery")
        self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="udmi_validation")

        from smart_commissioning_core.db.engine import session_factory

        with session_factory(self.engine)() as session:
            projects = session.scalars(select(Project)).all()
            sites = session.scalars(select(Site)).all()

        self.assertEqual([project.id for project in projects], ["demo-project"])
        self.assertEqual([site.id for site in sites], ["demo-site"])
        self.assertEqual(sites[0].project_id, "demo-project")

    def test_shared_udmi_processor_runs_against_db_store(self) -> None:
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="udmi_validation")[
            "run_id"
        ]

        record = process_udmi_validation_run(
            run_id,
            {
                "expected_schedule": {
                    "asset_id": "AHU-1000001",
                    "manufacturer": "ExpectedCo",
                    "model": "Model-A",
                    "guid": "ifc://expected",
                    "units": {"co2_concentration_sensor": "parts_per_million"},
                },
                "state_payload": {
                    "timestamp": "2026-04-01T10:47:38.697+01:00",
                    "system": {"hardware": {"make": "ObservedCo", "model": "Model-B"}},
                },
                "metadata_payload": {
                    "timestamp": "2026-04-01T10:48:00.000+01:00",
                    "system": {"physical_tag": {"asset": {"guid": "ifc://observed"}}},
                    "pointset": {"points": {"co2_concentration_sensor": {"units": "parts_per_million"}}},
                },
                "pointset_payload": {
                    "timestamp": "2026-04-01T10:48:56.312+01:00",
                    "points": {"co2_concentration_sensor": {"present_value": 500}},
                },
            },
            run_store=self.store,
            execution_mode="inline_local_fallback",
        )

        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["progress_percent"], 100)
        self.assertEqual(record["result_summary"]["execution_mode"], "inline_local_fallback")
        self.assertGreater(len(record["issues"]), 0)
        self.assertEqual(self.store.get_run(run_id), record)

    def test_request_cancel_and_is_cancel_requested_roundtrip(self) -> None:
        run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="ip_discovery")[
            "run_id"
        ]

        self.assertFalse(self.store.is_cancel_requested(run_id), "new run is not cancel-requested")

        self.store.request_cancel(run_id)
        self.assertTrue(self.store.is_cancel_requested(run_id))

        # The public run dict shape is unchanged (no cancel_requested key leaks
        # into the API contract); cancellation is observed via is_cancel_requested.
        self.assertEqual(list(self.store.get_run(run_id)), FILE_RECORD_KEYS)

    def test_is_cancel_requested_false_for_missing_run(self) -> None:
        self.assertFalse(self.store.is_cancel_requested("run_00000000000000_deadbeef"))

    def test_request_cancel_missing_run_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.request_cancel("run_00000000000000_deadbeef")


class DiscoveryRepositoryTests(SqliteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = DbRunStore(self.engine)
        self.repository = DiscoveryRepository(self.engine)
        self.run_id = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="ip_discovery")[
            "run_id"
        ]

    def test_replace_list_devices_roundtrip(self) -> None:
        devices = [
            {
                "address": "192.0.2.117",
                "device_type": "ahu",
                "name": "AHU-L03-017",
                "vendor": "ExpectedCo",
                "model": "Model-A",
                "project_id": "demo-project",
                "site_id": "demo-site",
                "attributes": {"mac": "aa:bb:cc:dd:ee:ff", "observed_ports": [47808]},
            },
            {"address": "192.0.2.118", "device_type": "vav", "attributes": {}},
        ]

        written = self.repository.replace_devices(self.run_id, devices)
        self.assertEqual(written, 2)
        self.assertEqual(len(self.repository.list_devices(self.run_id)), 2)

        listed = self.repository.list_devices(self.run_id)
        self.assertEqual([row["address"] for row in listed], ["192.0.2.117", "192.0.2.118"])
        self.assertEqual(listed[0]["attributes"], {"mac": "aa:bb:cc:dd:ee:ff", "observed_ports": [47808]})
        self.assertEqual(listed[0]["vendor"], "ExpectedCo")
        self.assertEqual([row["position"] for row in listed], [0, 1])

        # replace is idempotent: re-writing fewer rows replaces, not appends.
        self.repository.replace_devices(self.run_id, [{"address": "192.0.2.200"}])
        self.assertEqual([r["address"] for r in self.repository.list_devices(self.run_id)], ["192.0.2.200"])

    def test_replace_list_points_roundtrip(self) -> None:
        points = [
            {
                "device_ref": "192.0.2.117",
                "point_id": "ai-1",
                "point_name": "supply_air_temperature_sensor",
                "observed_value": {"present_value": 21.5},
                "units": "degrees-celsius",
                "attributes": {"object_type": "analog-input"},
            },
            {"point_name": "co2_concentration_sensor", "observed_value": {"present_value": 500}},
        ]

        self.assertEqual(self.repository.replace_points(self.run_id, points), 2)
        self.assertEqual(len(self.repository.list_points(self.run_id)), 2)

        listed = self.repository.list_points(self.run_id)
        self.assertEqual(listed[0]["point_name"], "supply_air_temperature_sensor")
        self.assertEqual(listed[0]["observed_value"], {"present_value": 21.5})
        self.assertEqual(listed[0]["units"], "degrees-celsius")
        self.assertEqual(listed[0]["attributes"], {"object_type": "analog-input"})
        self.assertIsNone(listed[1]["device_ref"])

    def test_replace_list_topics_roundtrip(self) -> None:
        topics = [
            {
                "topic": "demo-site/b1/ahu/l03/events/pointset",
                "last_payload": {"points": {"co2": {"present_value": 500}}},
                "message_count": 7,
                "attributes": {"qos": 1},
            },
            {"topic": "demo-site/b1/ahu/l03/state"},
        ]

        self.assertEqual(self.repository.replace_topics(self.run_id, topics), 2)
        self.assertEqual(len(self.repository.list_topics(self.run_id)), 2)

        listed = self.repository.list_topics(self.run_id)
        self.assertEqual(listed[0]["topic"], "demo-site/b1/ahu/l03/events/pointset")
        self.assertEqual(listed[0]["message_count"], 7)
        self.assertEqual(listed[0]["last_payload"], {"points": {"co2": {"present_value": 500}}})
        self.assertEqual(listed[1]["message_count"], 0, "message_count defaults to 0")
        self.assertEqual(listed[1]["last_payload"], {})

    def test_rows_are_scoped_per_run(self) -> None:
        other_run = self.store.create_run(project_id="demo-project", site_id="demo-site", job_type="bacnet_discovery")[
            "run_id"
        ]
        self.repository.replace_devices(self.run_id, [{"address": "a"}])
        self.repository.replace_devices(other_run, [{"address": "b"}, {"address": "c"}])

        self.assertEqual(len(self.repository.list_devices(self.run_id)), 1)
        self.assertEqual(len(self.repository.list_devices(other_run)), 2)
        self.assertEqual([r["address"] for r in self.repository.list_devices(self.run_id)], ["a"])

    def test_cascade_delete_on_run_delete(self) -> None:
        self.repository.replace_devices(self.run_id, [{"address": "a"}])
        self.repository.replace_points(self.run_id, [{"point_name": "p"}])
        self.repository.replace_topics(self.run_id, [{"topic": "t"}])
        self.assertEqual(len(self.repository.list_devices(self.run_id)), 1)

        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Run

        with session_factory(self.engine).begin() as session:
            session.delete(session.get(Run, self.run_id))

        # FK ondelete=CASCADE (PRAGMA foreign_keys=ON on SQLite) removes the rows.
        self.assertEqual(self.repository.list_devices(self.run_id), [])
        self.assertEqual(self.repository.list_points(self.run_id), [])
        self.assertEqual(self.repository.list_topics(self.run_id), [])


class ConfigurationRepositoryTests(SqliteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = ConfigurationRepository(self.engine)

    def test_versions_are_monotonic_and_current_is_highest(self) -> None:
        self.assertIsNone(self.repository.get_current("demo-project", "demo-site"))

        first_version = self.repository.save("demo-project", "demo-site", {"mqtt": {"Port": "8883"}})
        second_version = self.repository.save("demo-project", "demo-site", {"mqtt": {"Port": "1883"}})

        self.assertEqual(first_version, 1)
        self.assertEqual(second_version, 2)
        self.assertEqual(
            self.repository.get_current("demo-project", "demo-site"),
            {"mqtt": {"Port": "1883"}},
            "current configuration must be the highest version",
        )

    def test_versions_are_scoped_per_project_and_site(self) -> None:
        self.repository.save("demo-project", "demo-site", {"value": "a"})
        other_version = self.repository.save("demo-project", "other-site", {"value": "b"})

        self.assertEqual(other_version, 1)
        self.assertEqual(self.repository.get_current("demo-project", "other-site"), {"value": "b"})
        self.assertIsNone(self.repository.get_current("other-project", "demo-site"))


class ImportRepositoryTests(SqliteTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = ImportRepository(self.engine)

    def test_import_record_roundtrip(self) -> None:
        summary = {
            "import_id": "imp_20260611120000_ab12cd34",
            "import_type": "ip_register",
            "file_name": "register.csv",
            "file_type": "csv",
            "project_id": "demo-project",
            "site_id": "demo-site",
            "total_rows": 2,
            "accepted_rows": 1,
            "rejected_rows": 1,
            "status": "partial",
            "missing_columns": [],
            "stored_file_name": "imp_20260611120000_ab12cd34_register.csv",
            "created_at": "2026-06-11T12:00:00+00:00",
        }
        errors = [{"row_number": 3, "field": "Expected IP address", "code": "invalid_ip", "message": "bad ip"}]
        accepted = [{"Asset ID": "AHU-L03-017", "Expected IP address": "192.0.2.117"}]

        created = self.repository.create(
            import_id="imp_20260611120000_ab12cd34",
            import_type="ip_register",
            project_id="demo-project",
            site_id="demo-site",
            original_filename="register.csv",
            stored_file_path="runtime/imports/files/imp_20260611120000_ab12cd34_register.csv",
            summary=summary,
            accepted_rows=accepted,
            errors=errors,
        )

        fetched = self.repository.get("imp_20260611120000_ab12cd34")
        self.assertEqual(fetched, created)
        self.assertEqual(self.repository.get_summary("imp_20260611120000_ab12cd34"), summary)
        self.assertEqual(
            self.repository.get_errors("imp_20260611120000_ab12cd34"),
            {"import_id": "imp_20260611120000_ab12cd34", "errors": errors},
        )
        self.assertEqual(self.repository.get_accepted_rows("imp_20260611120000_ab12cd34"), accepted)

        listed = self.repository.list(project_id="demo-project", site_id="demo-site", import_type="ip_register")
        self.assertEqual([record["import_id"] for record in listed], ["imp_20260611120000_ab12cd34"])
        self.assertEqual(self.repository.list(project_id="other-project"), [])

        with self.assertRaises(FileNotFoundError):
            self.repository.get("imp_missing")

    def test_monotonic_scope_created_at_makes_second_equal_timestamp_import_newest(self) -> None:
        created_at = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

        def create(import_id: str) -> dict[str, object]:
            return self.repository.create(
                import_id=import_id,
                import_type="mqtt_register",
                project_id="demo-project",
                site_id="demo-site",
                original_filename=f"{import_id}.csv",
                stored_file_path=f"runtime/imports/files/{import_id}.csv",
                summary={"import_id": import_id, "created_at": created_at.isoformat()},
                created_at=created_at,
                monotonic_scope_created_at=True,
            )

        first = create("imp_z_first")
        second = create("imp_a_second")

        listed = self.repository.list(
            project_id="demo-project",
            site_id="demo-site",
            import_type="mqtt_register",
        )
        self.assertEqual([record["import_id"] for record in listed], ["imp_a_second", "imp_z_first"])
        self.assertEqual(first["created_at"], created_at.isoformat())
        self.assertEqual(second["created_at"], (created_at + timedelta(microseconds=1)).isoformat())
        self.assertEqual(second["summary"]["created_at"], second["created_at"])


class MigrationTests(unittest.TestCase):
    EXPECTED_TABLES = {
        "projects",
        "sites",
        "runs",
        "run_issues",
        "configuration_snapshots",
        "import_records",
        # Added by the engine-framework migration c4a7ced176a9.
        "discovered_devices",
        "discovered_points",
        "discovered_topics",
    }

    def test_upgrade_to_head_creates_schema_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))

            upgrade_to_head(url)
            upgrade_to_head(url)  # second run must be a no-op

            engine = create_engine_from_url(url)
            try:
                inspector = inspect(engine)
                tables = set(inspector.get_table_names())
                self.assertTrue(self.EXPECTED_TABLES.issubset(tables), tables)
                self.assertIn("alembic_version", tables)

                run_columns = {column["name"] for column in inspector.get_columns("runs")}
                self.assertTrue(
                    {
                        "id",
                        "project_id",
                        "site_id",
                        "job_type",
                        "status",
                        "stage",
                        "progress_percent",
                        "parameters",
                        "result_summary",
                        "execution_mode",
                        "error_message",
                        "created_at",
                        "updated_at",
                        # Added by the engine-framework migration.
                        "cancel_requested",
                    }.issubset(run_columns),
                    run_columns,
                )

                issue_columns = {column["name"] for column in inspector.get_columns("run_issues")}
                self.assertTrue(set(ISSUE_KEYS).issubset(issue_columns), issue_columns)

                device_columns = {c["name"] for c in inspector.get_columns("discovered_devices")}
                self.assertTrue(
                    {"id", "run_id", "position", "address", "device_type", "attributes", "created_at"}.issubset(
                        device_columns
                    ),
                    device_columns,
                )

                # The migrated schema is usable by the store directly.
                store = DbRunStore(engine)
                record = store.create_run(project_id="demo-project", site_id="demo-site", job_type="ip_discovery")
                self.assertEqual(list(record), FILE_RECORD_KEYS)
                # cancel flag defaults to False on a freshly migrated DB.
                self.assertFalse(store.is_cancel_requested(record["run_id"]))
            finally:
                engine.dispose()

    def test_upgrade_to_head_has_zero_metadata_drift(self) -> None:
        # After upgrading to head, alembic's compare_metadata against the ORM
        # models must report no differences (the migration matches the models).
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            upgrade_to_head(url)

            engine = create_engine_from_url(url)
            try:
                with engine.connect() as connection:
                    context = MigrationContext.configure(connection)
                    diffs = compare_metadata(context, Base.metadata)
                self.assertEqual(diffs, [], f"schema drift detected after upgrade: {diffs}")
            finally:
                engine.dispose()

    def test_multi_resource_downgrade_requires_drained_active_slots(self) -> None:
        # Alembic's exception path can retain a pooled SQLite handle briefly on
        # Windows even though the application engine is disposed below.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            command.upgrade(build_alembic_config(url), "b8c9d0e1f2a3")
            engine = create_engine_from_url(url)
            try:
                run_id = DbRunStore(engine).create_run(
                    project_id="project-downgrade",
                    site_id="site-downgrade",
                    job_type="bacnet_discovery",
                )["run_id"]
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO active_protocol_slots "
                            "(protocol_key, run_id, owner_token, acquired_at) VALUES "
                            "('interface:192.0.2.10', :run_id, NULL, :now), "
                            "('bacnet:192.0.2.10:47808', :run_id, NULL, :now)"
                        ),
                        {"run_id": run_id, "now": datetime(2026, 8, 10, 20, 0)},
                    )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "drain active protocol slots",
                ):
                    command.downgrade(build_alembic_config(url), "a7b8c9d0e1f2")

                indexes = {
                    index["name"]: index
                    for index in inspect(engine).get_indexes("active_protocol_slots")
                }
                self.assertFalse(indexes["ix_active_protocol_slots_run_id"]["unique"])
                with engine.connect() as connection:
                    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                self.assertEqual(revision, "b8c9d0e1f2a3")
            finally:
                engine.dispose()

    def test_legacy_terminal_backfill_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            command.upgrade(build_alembic_config(url), "e5f6a7b8c9d0")
            engine = create_engine_from_url(url)
            now = "2026-07-25 10:00:00+00:00"
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO projects (id, name, created_at) "
                            "VALUES ('project-legacy', 'project-legacy', :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO sites (id, project_id, name, created_at) "
                            "VALUES ('site-legacy', 'project-legacy', 'site-legacy', :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO runs "
                            "(id, project_id, site_id, job_type, status, stage, "
                            "progress_percent, parameters, result_summary, execution_mode, "
                            "error_message, cancel_requested, edge_id, synced_at, created_at, updated_at) "
                            "VALUES ('run_legacy', 'project-legacy', 'site-legacy', "
                            "'report_generation', 'succeeded', 'engine_complete', 100, "
                            "'{}', '{\"device_count\": 1}', 'inline', NULL, 0, NULL, NULL, :now, :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO discovered_devices "
                            "(run_id, position, project_id, site_id, address, device_type, "
                            "name, vendor, model, attributes, created_at) VALUES "
                            "('run_legacy', 0, 'project-legacy', 'site-legacy', '192.0.2.20', "
                            "NULL, NULL, NULL, NULL, '{}', :now)"
                        ),
                        {"now": now},
                    )
            finally:
                engine.dispose()

            upgrade_to_head(url)
            engine = create_engine_from_url(url)
            try:
                revision = runpy.run_path(
                    str(
                        Path(__file__).resolve().parents[1]
                        / "alembic"
                        / "versions"
                        / "f6a7b8c9d0e1_reliability_lifecycle_v2.py"
                    )
                )
                hostile = {"observed\ud800key": float("nan"), "value": "raw\udffftext"}
                self.assertEqual(
                    revision["_json_safe"](hostile),
                    {
                        "observed\\uD800key": "nan (non-standard JSON number)",
                        "value": "raw\\uDFFFtext",
                    },
                )
                self.assertEqual(len(revision["_canonical_sha256"](hostile)), 64)

                with engine.begin() as connection:
                    revision["_backfill_terminal_results"](connection)
                    revision["_backfill_terminal_results"](connection)
                self.assertEqual(
                    revision["_legacy_report_integrity"]({})["classification"],
                    "missing",
                )
                self.assertEqual(
                    revision["_legacy_report_integrity"]({"integrity": {"hash": "not-a-digest"}})["classification"],
                    "conflicting",
                )
                with session_factory(engine)() as session:
                    result = session.get(RunResult, "run_legacy")
                    seal = session.get(RunSeal, "run_legacy")
                    contract = session.get(ReportEvidenceContract, "run_legacy")
                    self.assertIsNotNone(result)
                    self.assertIsNotNone(seal)
                    self.assertIsNotNone(contract)
                    self.assertEqual(contract.contract_version, "legacy_pre_lifecycle")
                    self.assertEqual(result.result_sha256, seal.result_sha256)
                    self.assertEqual(len(result.result_payload["devices"]), 1)
                    self.assertEqual(
                        result.summary["legacy_report_integrity"],
                        {
                            "classification": "missing",
                            "migration": "v0.1.26",
                            "silently_resigned": False,
                        },
                    )
                    self.assertEqual(session.scalar(select(func.count()).select_from(RunResult)), 1)
            finally:
                engine.dispose()

    def test_report_contract_backfill_classifies_only_unambiguous_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            command.upgrade(build_alembic_config(url), "d0e1f2a3b4c5")
            engine = create_engine_from_url(url)
            now = "2026-08-11 09:00:00+00:00"
            rows = (
                (
                    "run_contract_legacy",
                    {},
                    {
                        "legacy_report_integrity": {
                            "classification": "missing",
                            "migration": "v0.1.26",
                            "silently_resigned": False,
                        }
                    },
                ),
                (
                    "run_contract_snapshot",
                    {"report_snapshot_v2": {}, "report_snapshot_sha256": "a" * 64},
                    {},
                ),
                (
                    "run_contract_manifest",
                    {},
                    {"artifact_manifest": {"report_id": "run_contract_manifest"}},
                ),
                ("run_contract_ambiguous", {}, {}),
            )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO projects (id, name, created_at) "
                            "VALUES ('project-contract', 'project-contract', :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO sites (id, project_id, name, created_at) "
                            "VALUES ('site-contract', 'project-contract', 'site-contract', :now)"
                        ),
                        {"now": now},
                    )
                    for run_id, parameters, summary in rows:
                        connection.execute(
                            text(
                                "INSERT INTO runs "
                                "(id, project_id, site_id, job_type, status, stage, progress_percent, "
                                "parameters, result_summary, execution_mode, error_message, "
                                "cancel_requested, created_at, updated_at) VALUES "
                                "(:run_id, 'project-contract', 'site-contract', 'report_generation', "
                                "'succeeded', 'report_ready', 100, :parameters, :summary, "
                                "'inline', NULL, 0, :now, :now)"
                            ),
                            {
                                "run_id": run_id,
                                "parameters": json.dumps(parameters),
                                "summary": json.dumps(summary),
                                "now": now,
                            },
                        )
            finally:
                engine.dispose()

            upgrade_to_head(url)
            engine = create_engine_from_url(url)
            try:
                with session_factory(engine)() as session:
                    contracts = {
                        row.run_id: (
                            row.contract_version,
                            row.project_id,
                            row.site_id,
                        )
                        for row in session.scalars(select(ReportEvidenceContract)).all()
                    }
                self.assertEqual(
                    contracts["run_contract_legacy"],
                    ("legacy_pre_lifecycle", "project-contract", "site-contract"),
                )
                self.assertEqual(
                    contracts["run_contract_snapshot"],
                    ("sealed_v1", "project-contract", "site-contract"),
                )
                self.assertEqual(
                    contracts["run_contract_manifest"],
                    ("sealed_v1", "project-contract", "site-contract"),
                )
                self.assertNotIn("run_contract_ambiguous", contracts)
                scope_index = next(
                    index
                    for index in inspect(engine).get_indexes(
                        "report_evidence_contracts"
                    )
                    if index["name"] == "ix_report_evidence_contracts_scope"
                )
                self.assertEqual(
                    scope_index["column_names"],
                    ["project_id", "site_id", "run_id"],
                )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
