import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.models import Project, Site
from smart_commissioning_core.db.repositories import (
    ConfigurationRepository,
    DiscoveryRepository,
    ImportRepository,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run
from sqlalchemy import inspect, select

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
        "topic": "electracom/sct/1532/ahu/l03/events/pointset",
        "expected_value": "degrees-celsius",
        "observed_value": "kelvin",
        "match_basis": "point_name",
        "suggested_action": "Fix the unit mapping.",
        "raw_evidence_uri": None,
        "status_detail": None,
        "last_seen_at": "2026-06-11T10:00:00+00:00",
    }


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

    def test_status_summary_and_issue_updates_roundtrip(self) -> None:
        run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="udmi_validation"
        )["run_id"]

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

        record = self.store.update_run_status(
            run_id, status="failed", progress_percent=100, error_message="boom"
        )
        self.assertEqual(record["error_message"], "boom")

        fetched = self.store.get_run(run_id)
        self.assertEqual(list(fetched), FILE_RECORD_KEYS)
        self.assertEqual(fetched, record, "get_run must match the last update's return value")
        for issue in fetched["issues"]:
            self.assertEqual(list(issue), ISSUE_KEYS)

    def test_replace_issues_preserves_order_on_rewrite(self) -> None:
        run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="mqtt_config_publish"
        )["run_id"]
        issues = [_issue(f"iss-{index}", f"issue {index}") for index in range(5)]

        record = self.store.replace_issues(run_id, issues)
        self.assertEqual([issue["issue_id"] for issue in record["issues"]], [f"iss-{i}" for i in range(5)])

        record = self.store.replace_issues(run_id, list(reversed(issues)))
        self.assertEqual(
            [issue["issue_id"] for issue in record["issues"]],
            [f"iss-{i}" for i in reversed(range(5))],
            "delete+reinsert must preserve the caller's ordering",
        )

        record = self.store.append_issue(run_id, _issue("iss-appended", "appended last"))
        self.assertEqual(record["issues"][-1]["issue_id"], "iss-appended")

    def test_replace_issues_accepts_validation_issue_records(self) -> None:
        run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="udmi_validation"
        )["run_id"]
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
        self.assertEqual(
            {record["run_id"] for record in records}, {first["run_id"], second["run_id"]}
        )
        self.assertEqual(
            [record["run_id"] for record in records],
            [record["run_id"] for record in sorted(records, key=lambda r: (r["created_at"], r["run_id"]), reverse=True)],
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
        run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="udmi_validation"
        )["run_id"]

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
        run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="ip_discovery"
        )["run_id"]

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
        self.run_id = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="ip_discovery"
        )["run_id"]

    def test_replace_list_count_devices_roundtrip(self) -> None:
        devices = [
            {
                "address": "10.10.25.117",
                "device_type": "ahu",
                "name": "AHU-L03-017",
                "vendor": "ExpectedCo",
                "model": "Model-A",
                "project_id": "demo-project",
                "site_id": "demo-site",
                "attributes": {"mac": "aa:bb:cc:dd:ee:ff", "observed_ports": [47808]},
            },
            {"address": "10.10.25.118", "device_type": "vav", "attributes": {}},
        ]

        written = self.repository.replace_devices(self.run_id, devices)
        self.assertEqual(written, 2)
        self.assertEqual(self.repository.count_devices(self.run_id), 2)

        listed = self.repository.list_devices(self.run_id)
        self.assertEqual([row["address"] for row in listed], ["10.10.25.117", "10.10.25.118"])
        self.assertEqual(listed[0]["attributes"], {"mac": "aa:bb:cc:dd:ee:ff", "observed_ports": [47808]})
        self.assertEqual(listed[0]["vendor"], "ExpectedCo")
        self.assertEqual([row["position"] for row in listed], [0, 1])

        # replace is idempotent: re-writing fewer rows replaces, not appends.
        self.repository.replace_devices(self.run_id, [{"address": "10.10.25.200"}])
        self.assertEqual(self.repository.count_devices(self.run_id), 1)
        self.assertEqual([r["address"] for r in self.repository.list_devices(self.run_id)], ["10.10.25.200"])

    def test_replace_list_count_points_roundtrip(self) -> None:
        points = [
            {
                "device_ref": "10.10.25.117",
                "point_id": "ai-1",
                "point_name": "supply_air_temperature_sensor",
                "observed_value": {"present_value": 21.5},
                "units": "degrees-celsius",
                "attributes": {"object_type": "analog-input"},
            },
            {"point_name": "co2_concentration_sensor", "observed_value": {"present_value": 500}},
        ]

        self.assertEqual(self.repository.replace_points(self.run_id, points), 2)
        self.assertEqual(self.repository.count_points(self.run_id), 2)

        listed = self.repository.list_points(self.run_id)
        self.assertEqual(listed[0]["point_name"], "supply_air_temperature_sensor")
        self.assertEqual(listed[0]["observed_value"], {"present_value": 21.5})
        self.assertEqual(listed[0]["units"], "degrees-celsius")
        self.assertEqual(listed[0]["attributes"], {"object_type": "analog-input"})
        self.assertIsNone(listed[1]["device_ref"])

    def test_replace_list_count_topics_roundtrip(self) -> None:
        topics = [
            {
                "topic": "electracom/sct/1532/ahu/l03/events/pointset",
                "last_payload": {"points": {"co2": {"present_value": 500}}},
                "message_count": 7,
                "attributes": {"qos": 1},
            },
            {"topic": "electracom/sct/1532/ahu/l03/state"},
        ]

        self.assertEqual(self.repository.replace_topics(self.run_id, topics), 2)
        self.assertEqual(self.repository.count_topics(self.run_id), 2)

        listed = self.repository.list_topics(self.run_id)
        self.assertEqual(listed[0]["topic"], "electracom/sct/1532/ahu/l03/events/pointset")
        self.assertEqual(listed[0]["message_count"], 7)
        self.assertEqual(listed[0]["last_payload"], {"points": {"co2": {"present_value": 500}}})
        self.assertEqual(listed[1]["message_count"], 0, "message_count defaults to 0")
        self.assertEqual(listed[1]["last_payload"], {})

    def test_rows_are_scoped_per_run(self) -> None:
        other_run = self.store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="bacnet_discovery"
        )["run_id"]
        self.repository.replace_devices(self.run_id, [{"address": "a"}])
        self.repository.replace_devices(other_run, [{"address": "b"}, {"address": "c"}])

        self.assertEqual(self.repository.count_devices(self.run_id), 1)
        self.assertEqual(self.repository.count_devices(other_run), 2)
        self.assertEqual([r["address"] for r in self.repository.list_devices(self.run_id)], ["a"])

    def test_cascade_delete_on_run_delete(self) -> None:
        self.repository.replace_devices(self.run_id, [{"address": "a"}])
        self.repository.replace_points(self.run_id, [{"point_name": "p"}])
        self.repository.replace_topics(self.run_id, [{"topic": "t"}])
        self.assertEqual(self.repository.count_devices(self.run_id), 1)

        from smart_commissioning_core.db.engine import session_factory
        from smart_commissioning_core.db.models import Run

        with session_factory(self.engine).begin() as session:
            session.delete(session.get(Run, self.run_id))

        # FK ondelete=CASCADE (PRAGMA foreign_keys=ON on SQLite) removes the rows.
        self.assertEqual(self.repository.count_devices(self.run_id), 0)
        self.assertEqual(self.repository.count_points(self.run_id), 0)
        self.assertEqual(self.repository.count_topics(self.run_id), 0)


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
        accepted = [{"Asset ID": "AHU-L03-017", "Expected IP address": "10.10.25.117"}]

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
                    {"id", "run_id", "position", "address", "device_type", "attributes", "created_at"}
                    .issubset(device_columns),
                    device_columns,
                )

                # The migrated schema is usable by the store directly.
                store = DbRunStore(engine)
                record = store.create_run(
                    project_id="demo-project", site_id="demo-site", job_type="ip_discovery"
                )
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


if __name__ == "__main__":
    unittest.main()
