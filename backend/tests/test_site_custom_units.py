"""Site Custom Units: a unit the pinned DBO list lacks (milligrams_per_liter)
is rejected at register import until the site declares it under Configuration
> Validation Rules; after that the import accepts it and the UDMI run freezes
the saved list into its parameters, so the unit verdict matches the frozen
configuration snapshot.
"""

import io
import unittest

from harness import ApiTestCase

_API_KEY = "test-site-custom-units-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

# Distinct project/site so this class never shares configuration or register
# imports with other test classes on the per-process database.
_PROJECT = "site-custom-units-project"
_SITE = "site-custom-units-site"
_SCOPE = {"project_id": _PROJECT, "site_id": _SITE}

_REGISTER_CSV = (
    "Project/site,System,Asset ID,Expected topic,Expected schema version,"
    "Expected points,Expected units,Expected reporting interval,Source protocol,Payload applicability\n"
    "Site A,BMS,DO-1,site-a/bms/DO-1/#,1.5.2,oxygen_concentration_sensor,milligrams_per_liter,60,MQTT,metadata\n"
)


class SiteCustomUnitsTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _upload_register(self) -> dict:
        response = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", **_SCOPE},
            files={"file": ("register.csv", io.BytesIO(_REGISTER_CSV.encode()), "text/csv")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _save_custom_units(self, value: str) -> object:
        configuration = self.client.get("/api/v1/configuration", params=_SCOPE).json()
        configuration["validation"]["values"]["Custom Units"] = value
        return self.client.put("/api/v1/configuration", params=_SCOPE, json=configuration)

    def test_custom_unit_flows_from_configuration_to_import_and_run(self) -> None:
        defaults = self.client.get("/api/v1/configuration", params=_SCOPE).json()
        self.assertEqual(
            defaults["validation"]["values"],
            {"Custom Units": "", "Ignore Payloads Outside Applicability": "Disabled"},
        )
        self.assertEqual(defaults["validation"]["status"], "Optional")

        rejected = self._upload_register()
        self.assertEqual(rejected["status"], "rejected", rejected)
        errors = self.client.get(f"/api/v1/imports/{rejected['import_id']}/errors").json()["errors"]
        self.assertEqual([error["code"] for error in errors], ["invalid_unit"])
        self.assertIn("milligrams_per_liter", errors[0]["message"])
        self.assertIn("Configuration > Validation Rules > Custom Units", errors[0]["message"])

        saved = self._save_custom_units("milligrams_per_liter\nntu")
        self.assertEqual(saved.status_code, 200, saved.text)

        accepted = self._upload_register()
        self.assertEqual(accepted["status"], "accepted", accepted)

        run = self._run(client_custom_units=["anything_goes"])
        # The client copy is discarded: the saved configuration is the only source.
        self.assertEqual(run["parameters"]["custom_units"], ["milligrams_per_liter", "ntu"])
        self.assertNotIn("not a recognized DBO unit", self._descriptions(run))

        # Each run reads the configuration saved at its own start, so removing
        # the custom unit brings the finding back for the same accepted import.
        cleared = self._save_custom_units("")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        run = self._run()
        self.assertNotIn("custom_units", run["parameters"])
        self.assertIn(
            "Metadata unit 'milligrams_per_liter' for oxygen_concentration_sensor is not a "
            "recognized DBO unit",
            self._descriptions(run),
        )

    def _run(
        self,
        client_custom_units: list[str] | None = None,
        **extra: object,
    ) -> dict:
        parameters: dict[str, object] = {
            "use_register": True,
            "capture_seconds": 1,
            "use_live_broker": False,
            **extra,
        }
        if client_custom_units is not None:
            parameters["custom_units"] = client_custom_units
        response = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={**_SCOPE, "job_type": "udmi_validation", "parameters": parameters},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return self.client.get(f"/api/v1/validation/runs/{response.json()['run_id']}").json()

    @staticmethod
    def _descriptions(run: dict) -> str:
        return " ".join(issue["description"] for issue in run["issues"])

    def test_configuration_file_without_validation_section_still_saves(self) -> None:
        configuration = self.client.get("/api/v1/configuration", params=_SCOPE).json()
        configuration.pop("validation")
        response = self.client.put("/api/v1/configuration", params=_SCOPE, json=configuration)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["validation"]["values"],
            {"Custom Units": "", "Ignore Payloads Outside Applicability": "Disabled"},
        )
        self.assertEqual(response.json()["validation"]["status"], "Optional")

    def _save_ignore_toggle(self, value: str) -> object:
        configuration = self.client.get("/api/v1/configuration", params=_SCOPE).json()
        configuration["validation"]["values"]["Ignore Payloads Outside Applicability"] = value
        return self.client.put("/api/v1/configuration", params=_SCOPE, json=configuration)

    def test_ignore_toggle_is_frozen_from_saved_configuration(self) -> None:
        self.assertEqual(self._save_custom_units("milligrams_per_liter").status_code, 200)
        self.assertEqual(self._upload_register()["status"], "accepted")

        saved = self._save_ignore_toggle("Enabled")
        self.assertEqual(saved.status_code, 200, saved.text)
        # The client copy is discarded: the saved configuration is the only source.
        run = self._run(ignore_unapproved_payloads=False)
        self.assertIs(run["parameters"]["ignore_unapproved_payloads"], True)

        self.assertEqual(self._save_ignore_toggle("Disabled").status_code, 200)
        run = self._run(ignore_unapproved_payloads=True)
        self.assertNotIn("ignore_unapproved_payloads", run["parameters"])

        invalid = self._save_ignore_toggle("maybe")
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertIn("Ignore Payloads Outside Applicability must be Enabled or Disabled", invalid.text)

    def test_custom_units_entries_are_bounded(self) -> None:
        too_many = self._save_custom_units(",".join(f"unit_{index}" for index in range(101)))
        self.assertEqual(too_many.status_code, 400, too_many.text)
        self.assertIn("at most 100 entries", too_many.text)

        too_long = self._save_custom_units("x" * 65)
        self.assertEqual(too_long.status_code, 400, too_long.text)
        self.assertIn("at most 64 characters", too_long.text)


if __name__ == "__main__":
    unittest.main()
