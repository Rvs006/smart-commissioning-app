"""Register-driven UDMI workbench flow: an imported mqtt_register row becomes
the run's expected asset (topics + points + units + schema version), and a
register-driven run with no register import is refused rather than silently
validating the packaged sample fixture.
"""

import io
import unittest

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
    "Expected points,Expected units,Expected reporting interval,Source protocol\n"
    'Site A,BMS,EM-1,hv/ems/01/em/EM-1/#,1.5.2,"energy_sensor,status_flag,power_sensor","kwh,,kw",60,MQTT\n'
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
    'Site A,BMS,EM-1002002,MNVRHS/EM-1002001/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,EM-1002002,MNVRHS/EM-1002002/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,FCU-1008888,MNVRHS/FCU-1008888/#,1.5.2,supply_air_temperature_sensor,degrees_celsius,60,MQTT\n'
)

# Second row's topic has no recognised payload suffix, so the import rejects it
# (partial import) — the run must then say the asset was dropped.
_PARTIAL_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol\n"
    'Site A,BMS,EM-1,hv/ems/01/em/EM-1/#,1.5.2,energy_sensor,kwh,60,MQTT\n'
    'Site A,BMS,EM-2,hv/ems/01/em/EM-2,1.5.2,energy_sensor,kwh,60,MQTT\n'
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
        self.assertEqual(entry["expected_schedule"]["points"], ["energy_sensor", "status_flag", "power_sensor"])
        self.assertEqual(entry["expected_schedule"]["units"], {"energy_sensor": "kwh", "power_sensor": "kw"})
        self.assertEqual(entry["expected_schedule"]["udmi_version"], "1.5.2")
        self.assertEqual(entry["expected_schedule"]["reporting_interval_seconds"], "60")
        self.assertEqual(entry["state_topic"], "hv/ems/01/em/EM-1/state")
        self.assertEqual(entry["pointset_topic"], "hv/ems/01/em/EM-1/events/pointset")
        self.assertEqual(entry["extra_capture_topics"], ["hv/ems/01/em/EM-1/event/pointset"])
        # Real inline validation, never the packaged sample fixture.
        self.assertEqual(run["result_summary"]["source"], "schedule_payload_inputs")
        descriptions = " ".join(issue["description"] for issue in run["issues"])
        self.assertIn("Expected point energy_sensor was not received", descriptions)

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
        self.assertIn("EM-1002002", error["message"])
        self.assertIn("MNVRHS/EM-1002001", error["message"])
        self.assertIn("MNVRHS/EM-1002002", error["message"])
        self.assertIn("unique Asset ID", error["message"])

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        roots = sorted(entry.get("register_topic_filter", "") for entry in assets)
        self.assertEqual(roots, ["MNVRHS/EM-1002001/#", "MNVRHS/FCU-1008888/#"])
        rejections = [
            issue for issue in run["issues"] if issue["issue_type"] == "register_import"
        ]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["severity"], "high")
        self.assertIn("rejected 1 row(s)", rejections[0]["description"])
        self.assertIn("MNVRHS/EM-1002002", rejections[0]["description"])

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
                register_row("EM-1002002", "MNVRHS/EM-1002001/#", "energy_sensor"),
                register_row("EM-1002002", "MNVRHS/EM-1002002/#", "energy_sensor"),
                register_row("FCU-1008888", "MNVRHS/FCU-1008888/#", "supply_air_temperature_sensor"),
            ],
        )

        response = self._post_run(project, site)
        self.assertEqual(response.status_code, 200, response.text)
        run = self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

        assets = run["parameters"]["assets"]
        # Three entries survive: the two same-ID rows are NOT merged.
        self.assertEqual(len(assets), 3)
        roots = sorted(
            entry.get("register_topic_filter", "") for entry in assets
        )
        self.assertEqual(
            roots,
            ["MNVRHS/EM-1002001/#", "MNVRHS/EM-1002002/#", "MNVRHS/FCU-1008888/#"],
        )
        collisions = [
            issue for issue in run["issues"] if issue["issue_type"] == "register_import"
        ]
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["severity"], "high")
        self.assertIn("multiple rows with Asset ID 'EM-1002002'", collisions[0]["description"])
        self.assertIn("MNVRHS/EM-1002001", collisions[0]["description"])
        self.assertIn("MNVRHS/EM-1002002", collisions[0]["description"])

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
