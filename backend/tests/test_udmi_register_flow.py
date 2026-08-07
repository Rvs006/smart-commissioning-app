"""Register-driven UDMI workbench flow: an imported mqtt_register row becomes
the run's expected asset (topics + points + units + schema version), and a
register-driven run with no register import is refused rather than silently
validating the packaged sample fixture.
"""

import hashlib
import io
import json
import unicodedata
import unittest
from zipfile import ZipFile

from harness import ApiTestCase

_API_KEY = "test-udmi-register-flow-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

# Distinct project/site so the shared per-process database never leaks this
# register into other test classes' runs (or theirs into ours).
_PROJECT = "udmi-register-flow-project"
_SITE = "udmi-register-flow-site"

_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol,Payload applicability\n"
    'Site A,BMS,EM-1,hv/ems/01/em/EM-1/#,1.5.2,"energy_sensor,status_flag,power_sensor","kwh,,kw",60,MQTT,"state,metadata,pointset"\n'
)

# One asset spread over one row per payload type (a real site register shape):
# these must merge into ONE assets entry, not three entries with the same id.
_PER_TYPE_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol,Payload type\n"
    'Site A,BMS,EM-9,mn/em/EM-9/state,1.5.2,energy_sensor,kwh,60,MQTT,state\n'
    'Site A,BMS,EM-9,mn/em/EM-9/metadata,1.5.2,energy_sensor,kwh,60,MQTT,metadata\n'
    'Site A,BMS,EM-9,mn/em/EM-9/events/pointset,1.5.2,"energy_sensor,power_sensor","kwh,kw",60,MQTT,pointset\n'
)

# On-site 2026-07-13 screenshot scenario: one row reuses another asset's ID for
# a different device's topics (copy-paste error). The import now rejects the
# later conflicting row (first row wins) naming both topic roots, so the
# operator learns about the collision at upload time instead of a device
# silently vanishing from the validation results.
_DUPLICATE_ID_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol\n"
    'Site A,BMS,DEMO-1000002,demo-site/DEMO-1000001/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,DEMO-1000002,demo-site/DEMO-1000002/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,DEMO-1000003,demo-site/DEMO-1000003/#,1.5.2,supply_air_temperature_sensor,degrees_celsius,60,MQTT\n'
)

# Second row's topic has no recognised payload suffix, so the import rejects it
# (partial import) — the run must then say the asset was dropped.
_PARTIAL_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol\n"
    'Site A,BMS,EM-1,hv/ems/01/em/EM-1/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,EM-2,hv/ems/01/em/EM-2,1.5.2,energy_sensor,kwh,60,MQTT\n'
)

# A newer upload with no usable rows must not make a register-driven run fall
# back to the previous accepted import. That fallback makes newly added assets
# appear to be missing when the edited register was rejected in full.
_FULLY_REJECTED_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol\n"
    'Site A,BMS,EM-NEW,hv/ems/01/em/EM-NEW,1.5.2,energy_sensor,kwh,60,MQTT\n'
)


class UdmiRegisterFlowTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _post_run(self, project_id: str, site_id: str) -> object:
        return self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": project_id,
                "site_id": site_id,
                "job_type": "udmi_validation",
                "parameters": {"use_register": True, "capture_seconds": 1, "use_live_broker": False},
            },
        )

    def test_single_row_register_drives_run_with_capture_topics(self) -> None:
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": _PROJECT, "site_id": _SITE},
            files={"file": ("register.csv", io.BytesIO(_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["status"], "accepted", upload.text)

        response = self._post_run(_PROJECT, _SITE)
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]

        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()
        self.assertEqual(run["status"], "succeeded")
        # The single register row is a one-entry assets list that KEEPS its
        # register-derived capture topics (incl. the legacy event/pointset
        # alias) so a live capture can actually subscribe.
        assets = run["parameters"]["assets"]
        self.assertEqual(len(assets), 1)
        entry = assets[0]
        self.assertEqual(entry["expected_schedule"]["asset_id"], "EM-1")
        self.assertEqual(entry["expected_schedule"]["project_site"], "Site A")
        self.assertEqual(entry["expected_schedule"]["system"], "BMS")
        self.assertEqual(entry["expected_schedule"]["points"], ["energy_sensor", "status_flag", "power_sensor"])
        self.assertEqual(entry["expected_schedule"]["units"], {"energy_sensor": "kwh", "power_sensor": "kw"})
        self.assertEqual(entry["expected_schedule"]["udmi_version"], "1.5.2")
        self.assertEqual(entry["expected_schedule"]["reporting_interval_seconds"], "60")
        self.assertEqual(entry["state_topic"], "hv/ems/01/em/EM-1/state")
        self.assertEqual(entry["pointset_topic"], "hv/ems/01/em/EM-1/events/pointset")
        self.assertEqual(entry["extra_capture_topics"], ["hv/ems/01/em/EM-1/event/pointset"])
        # Real inline validation, never the packaged sample fixture.
        self.assertEqual(run["result_summary"]["source"], "schedule_payload_inputs")
        validation_summary = run["result_summary"]["validation_summary_v1"]
        self.assertEqual(validation_summary["schema_version"], "1.1")
        self.assertEqual(validation_summary["asset_metrics"]["expected"], 1)
        self.assertEqual(validation_summary["system_metrics"][0]["system"], "BMS")
        self.assertEqual(validation_summary["asset_results"][0]["system"], "BMS")
        self.assertEqual(run["result_summary"]["payload_views"][0]["system"], "BMS")
        # The exact input register is frozen with this run so a later import can
        # never change which rows a report annotates.
        self.assertEqual(run["parameters"]["register_import_id"], upload.json()["import_id"])
        self.assertEqual(run["parameters"]["register_import_filename"], "register.csv")
        self.assertEqual(
            run["parameters"]["register_sha256"],
            hashlib.sha256(_REGISTER_CSV.encode()).hexdigest(),
        )
        self.assertEqual(
            run["parameters"]["register_columns"],
            [
                "Project/site",
                "System",
                "Asset ID",
                "Expected topic",
                "Expected schema version",
                "Expected points",
                "Expected units",
                "Expected reporting interval",
                "Source protocol",
                "Payload applicability",
            ],
        )
        self.assertEqual(run["parameters"]["register_rows"], [
            {
                "Project/site": "Site A",
                "System": "BMS",
                "Asset ID": "EM-1",
                "Expected topic": "hv/ems/01/em/EM-1/#",
                "Expected schema version": "1.5.2",
                "Expected points": "energy_sensor,status_flag,power_sensor",
                "Expected units": "kwh,,kw",
                "Expected reporting interval": "60",
                "Source protocol": "MQTT",
                "Payload applicability": "state,metadata,pointset",
            }
        ])
        # No pointset payload was supplied. Its absence is one neutral state,
        # not one invented high-severity issue per expected point.
        descriptions = " ".join(issue["description"] for issue in run["issues"])
        self.assertNotIn("Expected point energy_sensor was not received", descriptions)

    def test_floor_column_is_optional_metadata_and_does_not_change_asset_selection(self) -> None:
        variants = {
            "with-floor": _REGISTER_CSV.replace(
                "Payload applicability\n",
                "Payload applicability,Floor\n",
            ).replace(
                '"state,metadata,pointset"\n',
                '"state,metadata,pointset",L02\n',
            ),
            "without-floor": _REGISTER_CSV,
        }
        observed: dict[str, tuple[str, str, str, str]] = {}

        for suffix, register in variants.items():
            project = f"{_PROJECT}-floor-{suffix}"
            site = f"{_SITE}-floor-{suffix}"
            upload = self.client.post(
                "/api/v1/imports",
                data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
                files={"file": ("register.csv", io.BytesIO(register.encode()), "text/csv")},
            )
            self.assertEqual(upload.status_code, 200, upload.text)
            self.assertEqual(upload.json()["accepted_rows"], 1)

            response = self._post_run(project, site)
            self.assertEqual(response.status_code, 200, response.text)
            run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()
            entry = run["parameters"]["assets"][0]
            schedule = entry["expected_schedule"]
            observed[suffix] = (
                schedule["asset_id"],
                entry["state_topic"],
                entry["metadata_topic"],
                entry["pointset_topic"],
            )
            if suffix == "with-floor":
                self.assertEqual(schedule["floor"], "L02")
                self.assertIn("Floor", run["parameters"]["register_columns"])
                self.assertEqual(run["parameters"]["register_rows"][0]["Floor"], "L02")
            else:
                self.assertNotIn("floor", schedule)

        self.assertEqual(observed["with-floor"], observed["without-floor"])

    def test_register_row_secrets_are_redacted_in_run_api_response(self) -> None:
        register = _REGISTER_CSV.replace(
            "Payload applicability\n",
            "Payload applicability,Broker password\n",
        ).replace(
            '"state,metadata,pointset"\n',
            '"state,metadata,pointset",broker-secret\n',
        )
        project = f"{_PROJECT}-redaction"
        site = f"{_SITE}-redaction"
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("register.csv", io.BytesIO(register.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()
        self.assertEqual(run["parameters"]["register_rows"][0]["Broker password"], "********")

    def test_register_rejection_text_escapes_control_characters(self) -> None:
        project = f"{_PROJECT}-rejection-text"
        site = f"{_SITE}-rejection-text"
        rejected = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={
                "file": (
                    "edited\r\nregister.csv",
                    io.BytesIO(_FULLY_REJECTED_REGISTER_CSV.encode()),
                    "text/csv",
                )
            },
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 400, response.text)
        flattened = response.json()["detail"]
        self.assertFalse(
            any(unicodedata.category(character) in {"Cc", "Cf"} for character in flattened)
        )

    def test_fully_rejected_new_register_does_not_fall_back_to_previous_import(self) -> None:
        project = f"{_PROJECT}-stale-register"
        site = f"{_SITE}-stale-register"
        accepted = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("previous-register.csv", io.BytesIO(_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["accepted_rows"], 1)

        rejected = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={
                "file": (
                    "edited-register.csv",
                    io.BytesIO(_FULLY_REJECTED_REGISTER_CSV.encode()),
                    "text/csv",
                )
            },
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "rejected")
        self.assertEqual(rejected.json()["accepted_rows"], 0)

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("edited-register.csv", detail)
        self.assertIn("no accepted rows", detail)
        self.assertIn("row 2", detail)

    def test_source_header_variants_remain_exact_in_annotated_register(self) -> None:
        project, site = f"{_PROJECT}-source-headings", f"{_SITE}-source-headings"
        source_columns = [
            "project/site",
            "system",
            "asset id",
            "expected  topic",
            "expected schema version",
            "expected points",
            "expected units",
            "expected reporting interval",
            "source protocol",
        ]
        source_csv = (
            ",".join(source_columns)
            + "\n"
            + "Site A,BMS,ASSET-021,site/a/ASSET-021/#,1.5.2,energy_sensor,kwh,60,MQTT\n"
        )
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("source-headings.csv", io.BytesIO(source_csv.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["status"], "accepted", upload.text)

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()
        self.assertEqual(run["parameters"]["register_columns"], source_columns)
        frozen_row = run["parameters"]["register_rows"][0]
        self.assertEqual(list(frozen_row), source_columns)
        self.assertNotIn("Asset ID", frozen_row)
        self.assertEqual(frozen_row["asset id"], "ASSET-021")

        report = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": project,
                "site_id": site,
                "report_type": "udmi_validation",
                "output_format": "zip",
                "source_run_ids": [run_id],
                "udmi_report_variant": "technical",
            },
        )
        self.assertEqual(report.status_code, 200, report.text)
        download = self.client.get(
            f"/api/v1/reports/{report.json()['report_id']}/download"
        )
        self.assertEqual(download.status_code, 200, download.text)
        with ZipFile(io.BytesIO(download.content)) as archive:
            annotated = json.loads(archive.read("annotated_input_register.json"))
        self.assertEqual(annotated["columns"][: len(source_columns)], source_columns)
        exported_row = annotated["rows"][0]
        self.assertEqual(
            {column: exported_row[column] for column in source_columns},
            frozen_row,
        )
        self.assertNotIn("Asset ID", exported_row)

    def test_register_mode_without_register_import_is_refused(self) -> None:
        response = self._post_run("project-with-no-register", "site-with-no-register")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("No accepted MQTT register import", response.json()["detail"])

    def test_per_payload_type_rows_merge_into_one_asset_entry(self) -> None:
        # On-site 2026-07-13: one asset per payload type row produced N entries
        # with the same asset_id and every issue appeared N times.
        project, site = f"{_PROJECT}-merge", f"{_SITE}-merge"
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("register.csv", io.BytesIO(_PER_TYPE_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        # Same Asset ID + same topic root is ONE device split per payload type:
        # the import-time conflicting-Asset-ID gate must accept every row.
        self.assertEqual(upload.json()["status"], "accepted", upload.text)
        self.assertEqual(upload.json()["accepted_rows"], 3)

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        self.assertEqual(len(assets), 1)
        entry = assets[0]
        self.assertEqual(entry["expected_schedule"]["asset_id"], "EM-9")
        self.assertEqual(entry["state_topic"], "mn/em/EM-9/state")
        self.assertEqual(entry["metadata_topic"], "mn/em/EM-9/metadata")
        self.assertEqual(entry["pointset_topic"], "mn/em/EM-9/events/pointset")
        self.assertEqual(entry["expected_schedule"]["points"], ["energy_sensor", "power_sensor"])
        self.assertEqual(
            entry["expected_schedule"]["units"],
            {"energy_sensor": "kwh", "power_sensor": "kw"},
        )
        # Exactly one payload view / issue set for the asset — not one per row.
        views = run["result_summary"]["payload_views"]
        self.assertEqual([view["asset_id"] for view in views], ["EM-9"])

    def test_duplicate_asset_id_rows_are_rejected_at_import_and_reported(self) -> None:
        # Two different device topic roots under one Asset ID is a register
        # copy-paste error: the import rejects the later conflicting row (first
        # row wins — here the WRONG row, so the error must carry both roots),
        # and the run reports the rejection instead of silently narrowing.
        project, site = f"{_PROJECT}-dupid", f"{_SITE}-dupid"
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("register.csv", io.BytesIO(_DUPLICATE_ID_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        summary = upload.json()
        self.assertEqual(summary["status"], "partial", upload.text)
        self.assertEqual(summary["accepted_rows"], 2)
        self.assertEqual(summary["rejected_rows"], 1)

        errors = self.client.get(f"/api/v1/imports/{summary['import_id']}/errors").json()["errors"]
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["row_number"], 3)
        self.assertEqual(error["field"], "Expected topic")
        self.assertEqual(error["code"], "conflicting_asset_topic")
        self.assertIn("DEMO-1000002", error["message"])
        self.assertIn("demo-site/DEMO-1000001", error["message"])
        self.assertIn("demo-site/DEMO-1000002", error["message"])
        self.assertIn("unique Asset ID", error["message"])

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        roots = sorted(entry.get("register_topic_filter", "") for entry in assets)
        self.assertEqual(roots, ["demo-site/DEMO-1000001/#", "demo-site/DEMO-1000003/#"])
        rejections = [
            issue for issue in run["issues"] if issue["issue_type"] == "register_import"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["severity"], "high")
        self.assertIn("rejected 1 row(s)", rejections[0]["description"])
        self.assertIn("demo-site/DEMO-1000002", rejections[0]["description"])

    def test_preexisting_conflicting_rows_stay_separate_and_are_reported(self) -> None:
        # Imports accepted BEFORE the import-time conflicting-Asset-ID gate can
        # still hold same-ID/different-root rows in the database. The run-time
        # merge guard is the defence in depth for them: entries stay separate
        # and the run names the collision with both topic roots.
        from app.core.db import get_engine
        from smart_commissioning_core.db.repositories import ImportRepository

        project, site = f"{_PROJECT}-legacy-dupid", f"{_SITE}-legacy-dupid"

        def register_row(asset_id: str, topic: str, point: str) -> dict[str, str]:
            return {
                "Project/site": "Site A",
                "System": "BMS",
                "Asset ID": asset_id,
                "Expected topic": topic,
                "Expected schema version": "1.5.2",
                "Expected points": point,
                "Expected units": "kwh",
                "Expected reporting interval": "60",
                "Source protocol": "MQTT",
            }

        # Seed the repository directly (no upload) to model a pre-gate import.
        ImportRepository(get_engine()).create(
            import_id="imp_legacy_conflicting_rows",
            import_type="mqtt_register",
            project_id=project,
            site_id=site,
            original_filename="legacy-register.csv",
            stored_file_path="legacy-register.csv",
            summary={"status": "accepted"},
            accepted_rows=[
                register_row("DEMO-1000002", "demo-site/DEMO-1000001/#", "energy_sensor"),
                # The SECOND device under the duplicated ID arrives as legacy
                # per-payload-type rows: they must coalesce into ONE entry even
                # though their shared root is not the identity's first-seen root.
                register_row("DEMO-1000002", "demo-site/DEMO-1000002/state", "energy_sensor"),
                register_row("DEMO-1000002", "demo-site/DEMO-1000002/metadata", "energy_sensor"),
                register_row("DEMO-1000002", "demo-site/DEMO-1000002/events/pointset", "energy_sensor"),
                register_row("DEMO-1000003", "demo-site/DEMO-1000003/#", "supply_air_temperature_sensor"),
            ],
        )

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        # Three entries survive: one per DEVICE — the conflicting-root rows are
        # not merged with each other, but each device's own rows coalesce.
        self.assertEqual(len(assets), 3)
        state_topics = sorted(entry.get("state_topic", "") for entry in assets)
        self.assertEqual(
            state_topics,
            [
                "demo-site/DEMO-1000001/state",
                "demo-site/DEMO-1000002/state",
                "demo-site/DEMO-1000003/state",
            ],
        )
        second_device = next(
            entry for entry in assets if entry.get("state_topic") == "demo-site/DEMO-1000002/state"
        )
        self.assertEqual(second_device["metadata_topic"], "demo-site/DEMO-1000002/metadata")
        self.assertEqual(second_device["pointset_topic"], "demo-site/DEMO-1000002/events/pointset")
        collisions = [
            issue for issue in run["issues"] if issue["issue_type"] == "register_import"
        ]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["severity"], "high")
        self.assertIn("multiple rows with Asset ID 'DEMO-1000002'", collisions[0]["description"])
        self.assertIn("demo-site/DEMO-1000001", collisions[0]["description"])
        self.assertIn("demo-site/DEMO-1000002", collisions[0]["description"])

    def test_rejected_register_rows_are_reported_by_the_run(self) -> None:
        # On-site 2026-07-13: a publishing device was missing from the results
        # because its register row was rejected at import; the run said nothing.
        project, site = f"{_PROJECT}-partial", f"{_SITE}-partial"
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project, "site_id": site},
            files={"file": ("register.csv", io.BytesIO(_PARTIAL_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["status"], "partial", upload.text)

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        self.assertEqual([a["expected_schedule"]["asset_id"] for a in assets], ["EM-1"])
        rejection_issues = [
            issue for issue in run["issues"] if issue["issue_type"] == "register_import"
        ]
        self.assertEqual(len(rejection_issues), 1)
        self.assertEqual(rejection_issues[0]["severity"], "high")
        self.assertIn("rejected 1 row(s)", rejection_issues[0]["description"])
        self.assertIn("Expected topic", rejection_issues[0]["description"])


if __name__ == "__main__":
    unittest.main()
