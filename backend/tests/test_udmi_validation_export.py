"""Focused API coverage for the versioned UDMI validation JSON export."""

import copy
import json
import uuid
from pathlib import Path

from harness import ApiTestCase
from jsonschema import Draft202012Validator
from smart_commissioning_core.udmi_results import build_validation_summary_v1

_SHARED_KEY = "test-udmi-validation-export-key"
_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _SHARED_KEY,
}
_REDACTED = "********"


def _export_schema() -> dict:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "schemas"
        / "udmi-validation-export-v1.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


class UdmiValidationExportApiTests(ApiTestCase):
    env = _ENV_OVERRIDES

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        response = cls.client.post(
            "/api/v1/users",
            headers=cls._admin_headers(),
            json={
                "username": f"udmi-export-viewer-{uuid.uuid4().hex[:8]}",
                "role": "viewer",
            },
        )
        assert response.status_code == 201, response.text
        cls._viewer_key = response.json()["api_key"]

    @classmethod
    def _admin_headers(cls) -> dict[str, str]:
        return {"X-API-Key": _SHARED_KEY}

    @classmethod
    def _viewer_headers(cls) -> dict[str, str]:
        return {"X-API-Key": cls._viewer_key}

    def _seed_run(
        self,
        *,
        job_type: str = "udmi_validation",
        status: str = "succeeded",
        site_id: str = "demo-site",
        result_summary: dict | None = None,
        issues: list[dict] | None = None,
        error_message: str | None = None,
    ) -> str:
        from app.schemas.jobs import JobCreateRequest
        from app.services.run_service import RunService

        run_service = RunService()
        run = run_service.create_job_run(
            JobCreateRequest(
                project_id="demo-project",
                site_id=site_id,
                job_type=job_type,
                parameters={},
            ),
            expected_job_type=job_type,
        )
        run_service.update_result_summary(run.run_id, result_summary or {}, merge=False)
        if issues is not None:
            run_service.replace_issues(run.run_id, issues)
        run_service.update_run_status(
            run.run_id,
            status=status,
            stage="done" if status == "succeeded" else f"{status}_with_partial_results",
            progress_percent=100,
            error_message=(
                error_message
                if error_message is not None
                else "Validation stopped after partial evidence."
                if status == "failed"
                else None
            ),
        )
        return run.run_id

    def _download(self, run_id: str, *, viewer: bool = False):
        headers = self._viewer_headers() if viewer else self._admin_headers()
        return self.client.get(
            f"/api/v1/validation/runs/{run_id}/export.json",
            headers=headers,
        )

    def test_requires_authentication_and_allows_viewer(self) -> None:
        run_id = self._seed_run()

        unauthenticated = self.client.get(
            f"/api/v1/validation/runs/{run_id}/export.json"
        )
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)

        viewer_download = self._download(run_id, viewer=True)
        self.assertEqual(viewer_download.status_code, 200, viewer_download.text)

    def test_missing_and_non_udmi_runs_are_not_exported(self) -> None:
        missing = self._download("missing-run")
        self.assertEqual(missing.status_code, 404, missing.text)

        bacnet_run_id = self._seed_run(job_type="bacnet_validation")
        wrong_type = self._download(bacnet_run_id)
        self.assertEqual(wrong_type.status_code, 404, wrong_type.text)

    def test_export_is_stable_schema_valid_and_recursively_redacted(self) -> None:
        validation_summary = build_validation_summary_v1(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "AHU-1", "system": "BMS"},
                        "state_topic": "site/ahu-1/state",
                        "state_payload": {"timestamp": "2026-07-20T10:00:00Z"},
                    }
                ]
            },
            [],
        )
        result_summary = {
            "validation_summary_v1": validation_summary,
            "raw_result": {
                "password": "password-value",
                "privateKey": "camel-private-key-value",
                "connectionString": "camel-connection-value",
                "sessionKey": "camel-session-value",
                "pwd": "short-password-value",
                "passwd": "unix-password-value",
                "nested": [
                    {"api-token": "token-value"},
                    {
                        "private_material": (
                            "-----BEGIN RSA PRIVATE KEY-----\nprivate-value\n"
                            "-----END RSA PRIVATE KEY-----"
                        )
                    },
                ],
                "ordinary_value": "kept",
                "legacy_non_finite": float("nan"),
                "invalid_unicode": "bad\ud800value",
                "bad\udfffkey": "key-value",
            },
        }
        issues = [
            {
                "issue_id": "UDMI-1",
                "asset_id": "AHU-1",
                "issue_type": "payload_error",
                "severity": "high",
                "description": "Payload could not be parsed.",
                "observed_value": (
                    "-----BEGIN PRIVATE KEY-----\nissue-private-value\n"
                    "-----END PRIVATE KEY-----"
                ),
            }
        ]
        run_id = self._seed_run(
            site_id="../ Bad/Site",
            result_summary=result_summary,
            issues=issues,
        )

        first = self._download(run_id)
        second = self._download(run_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.headers["content-type"], "application/json")
        disposition = first.headers["content-disposition"]
        self.assertTrue(disposition.startswith('attachment; filename="udmi-validation-Bad-Site-'))
        self.assertNotIn("..", disposition)
        self.assertNotIn("/", disposition)

        body = json.loads(first.content)
        self.assertEqual(body["schema_version"], "1.0")
        self.assertEqual(
            body["result_summary"]["validation_summary_v1"]["schema_version"],
            "1.1",
        )
        self.assertEqual(body["exported_at"], body["run"]["updated_at"])
        raw_result = body["result_summary"]["raw_result"]
        self.assertEqual(raw_result["password"], _REDACTED)
        self.assertEqual(raw_result["privateKey"], _REDACTED)
        self.assertEqual(raw_result["connectionString"], _REDACTED)
        self.assertEqual(raw_result["sessionKey"], _REDACTED)
        self.assertEqual(raw_result["pwd"], _REDACTED)
        self.assertEqual(raw_result["passwd"], _REDACTED)
        self.assertEqual(raw_result["nested"][0]["api-token"], _REDACTED)
        self.assertEqual(raw_result["nested"][1]["private_material"], _REDACTED)
        self.assertEqual(raw_result["ordinary_value"], "kept")
        self.assertEqual(raw_result["legacy_non_finite"], "nan (non-standard JSON number)")
        self.assertEqual(raw_result["invalid_unicode"], "bad\\uD800value")
        self.assertEqual(raw_result["bad\\uDFFFkey"], "key-value")
        self.assertEqual(body["issues"][0]["observed_value"], _REDACTED)
        for secret in (
            "password-value",
            "token-value",
            "private-value",
            "camel-private-key-value",
            "camel-connection-value",
            "camel-session-value",
            "short-password-value",
            "unix-password-value",
        ):
            self.assertNotIn(secret, first.text)

        schema = _export_schema()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(body)

    def test_schema_requires_v11_additions_and_accepts_legacy_v10(self) -> None:
        schema = _export_schema()
        summary_schema = {
            "$schema": schema["$schema"],
            "$ref": "#/$defs/validationSummary",
            "$defs": schema["$defs"],
        }
        validator = Draft202012Validator(summary_schema)
        summary = build_validation_summary_v1(
            {
                "assets": [
                    {
                        "expected_schedule": {"asset_id": "AHU-1", "system": "BMS"},
                        "state_topic": "demo-site/AHU-1/state",
                        "state_payload": {"timestamp": "2026-07-20T10:00:00Z"},
                    }
                ]
            },
            [],
        )
        validator.validate(summary)

        legacy = copy.deepcopy(summary)
        legacy["schema_version"] = "1.0"
        legacy["asset_metrics"].pop("unexpected")
        legacy["payload_metrics"].pop("not_received")
        legacy.pop("unexpected_devices")
        legacy.pop("unexpected_devices_measured")
        legacy.pop("unexpected_devices_measurement_scope")
        for system in legacy["system_metrics"]:
            system["asset_metrics"].pop("unexpected")
            system["payload_metrics"].pop("not_received")
        validator.validate(legacy)

        missing_cases = (
            ("top-level unexpected count", ("asset_metrics", "unexpected")),
            ("top-level not-received count", ("payload_metrics", "not_received")),
            ("unexpected device rows", ("unexpected_devices",)),
            ("unexpected measurement flag", ("unexpected_devices_measured",)),
            ("unexpected measurement scope", ("unexpected_devices_measurement_scope",)),
            ("system unexpected count", ("system_metrics", 0, "asset_metrics", "unexpected")),
            ("system not-received count", ("system_metrics", 0, "payload_metrics", "not_received")),
        )
        for label, path in missing_cases:
            with self.subTest(label=label):
                invalid = copy.deepcopy(summary)
                parent = invalid
                for segment in path[:-1]:
                    parent = parent[segment]
                parent.pop(path[-1])
                self.assertTrue(list(validator.iter_errors(invalid)), label)

    def test_partial_terminal_and_legacy_runs_remain_exportable(self) -> None:
        legacy_summary = {"expected_devices": 2, "publishing_seen": 1, "issue_count": 0}
        for status in ("cancelled", "failed"):
            with self.subTest(status=status):
                error_message = (
                    "-----BEGIN PRIVATE KEY-----\nerror-private-value\n"
                    "-----END PRIVATE KEY-----"
                    if status == "failed"
                    else None
                )
                run_id = self._seed_run(
                    status=status,
                    result_summary=legacy_summary,
                    error_message=error_message,
                )
                response = self._download(run_id)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["run"]["status"], status)
                self.assertTrue(body["run"]["partial"])
                self.assertEqual(body["result_summary"], legacy_summary)
                self.assertNotIn("validation_summary_v1", body["result_summary"])
                if status == "failed":
                    self.assertEqual(body["run"]["error_message"], _REDACTED)
                    self.assertNotIn("error-private-value", response.text)

    def test_non_terminal_runs_are_not_exportable(self) -> None:
        for status in ("queued", "running"):
            with self.subTest(status=status):
                run_id = self._seed_run(status=status, result_summary={"expected_devices": 2})
                response = self._download(run_id)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Raw validation JSON is available after the run reaches a terminal status.",
                )


if __name__ == "__main__":
    import unittest

    unittest.main()
