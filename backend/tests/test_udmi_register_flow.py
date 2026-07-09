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
    'Site A,BMS,EM-1,hv/ems/01/em/EM-1/#,1.5.2,"energy_sensor,power_sensor","kwh,kw",60,MQTT\n'
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
        self.assertEqual(entry["expected_schedule"]["udmi_version"], "1.5.2")
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


if __name__ == "__main__":
    unittest.main()
