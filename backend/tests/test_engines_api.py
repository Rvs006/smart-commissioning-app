"""API tests for the wired discovery/validation engines.

Boots the FastAPI app against a temporary SQLite database in api_key auth mode
(same shared SCT_TEST_DATABASE_URL / JOB_EXECUTION_MODE=inline pattern as
test_runs_api.py), and drives the engine routes end to end through the
TestClient.

HONESTY: there is NO real building network, BACnet device, or MQTT broker here.
The only real I/O exercised is an IP TCP-connect sweep against an ephemeral
loopback listener this test opens itself (127.0.0.1) — the honest, environment-
safe slice of the IP engine's real path. BACnet discovery runs against the
OFFLINE SimulatedBacnetBackend (the engine default), and the validation/
comparison/mqtt-config tests use inline parameters / fakes. No assertion in this
file depends on real hardware.
"""

import atexit
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

_API_KEY = "test-engines-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_AUTH = {"authorized": True}


def _shared_test_database_url() -> str:
    existing = os.environ.get("SCT_TEST_DATABASE_URL")
    if existing:
        return existing
    temp_dir = tempfile.mkdtemp(prefix="sct-test-db-")
    atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
    url = f"sqlite:///{(Path(temp_dir) / 'smart_commissioning.db').as_posix()}"
    os.environ["SCT_TEST_DATABASE_URL"] = url
    return url


class _EngineApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_env = {}
        for key, value in {"DATABASE_URL": _shared_test_database_url(), **_ENV_OVERRIDES}.items():
            cls._previous_env[key] = os.environ.get(key)
            os.environ[key] = value

        from app.core import config as config_module
        from app.core import db as db_module

        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        from app.main import app
        from fastapi.testclient import TestClient

        cls._client_context = TestClient(app, headers={"X-API-Key": _API_KEY})
        cls.client = cls._client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        from app.core import config as config_module
        from app.core import db as db_module

        cls._client_context.__exit__(None, None, None)
        db_module.get_engine().dispose()
        for key, value in cls._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

    def _post(self, path: str, parameters: dict, job_type: str) -> dict:
        response = self.client.post(
            path,
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": job_type,
                "parameters": parameters,
            },
        )
        return response


class IpDiscoveryApiTests(_EngineApiTestCase):
    def test_inline_loopback_scan_discovers_persists_and_lists(self) -> None:
        # Open a real ephemeral loopback listener so the production default
        # asyncio connect probe finds an open port on 127.0.0.1.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        open_port = listener.getsockname()[1]
        try:
            response = self._post(
                "/api/v1/discovery/ip/runs",
                {
                    **_AUTH,
                    "cidr": "127.0.0.1/32",
                    "ports": [open_port],
                    "scan_max_concurrency": 4,
                    "scan_rate_limit_per_sec": 0,  # disable rate limiting for speed
                    "scan_connect_timeout_s": 2,
                },
                "ip_discovery",
            )
        finally:
            listener.close()

        self.assertEqual(response.status_code, 200, response.text)
        accepted = response.json()
        self.assertEqual(accepted["status"], "succeeded", "inline mode runs synchronously")
        run_id = accepted["run_id"]

        results = self.client.get(f"/api/v1/discovery/runs/{run_id}/results")
        self.assertEqual(results.status_code, 200, results.text)
        body = results.json()
        assets = body["discovered_assets"]
        self.assertEqual(len(assets), 1, "127.0.0.1 should be responsive on the open port")
        self.assertEqual(assets[0]["ip_address"], "127.0.0.1")
        self.assertIn(open_port, [p["port"] for p in assets[0]["observed_ports"]])
        # Structured rows were persisted via DiscoveryRepository.
        self.assertEqual(len(body["devices"]), 1)
        self.assertEqual(body["devices"][0]["device_type"], "ip_host")
        self.assertIn(open_port, body["devices"][0]["attributes"]["open_ports"])

    def test_dry_run_returns_plan_without_scanning(self) -> None:
        response = self._post(
            "/api/v1/discovery/ip/runs",
            {"cidr": "10.99.0.0/30", "ports": [80, 443], "dry_run": True},
            "ip_discovery",
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]

        results = self.client.get(f"/api/v1/discovery/runs/{run_id}/results").json()
        summary = results["result_summary"]
        self.assertTrue(summary["dry_run"])
        plan = summary["dry_run_plan"]
        self.assertEqual(plan["engine"], "ip_discovery")
        self.assertEqual(plan["target_count"], 4)  # 2 hosts x 2 ports
        self.assertEqual(summary["hosts_responsive"], 0)
        self.assertEqual(results["devices"], [], "dry run persists no devices")

    def test_unauthorized_real_scan_rejected_with_403(self) -> None:
        # No authorization and not a dry run => boundary 403 before any scan.
        response = self._post(
            "/api/v1/discovery/ip/runs",
            {"cidr": "10.0.0.0/30", "ports": [80]},
            "ip_discovery",
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("authoriz", response.json()["detail"].lower())

    def test_no_cidr_falls_back_to_imported_register_addresses(self) -> None:
        # Upload an IP register with NO hostname column + a duplicate address,
        # then run discovery with no cidr/range: the route must scan the register's
        # deduped Expected IP addresses (not fail) — the core bug this fixes.
        csv = (
            b"Project/site,System,Asset ID,Asset name,Expected IP address,Expected services/ports\n"
            b"M,ACS,A1,Cam,10.10.100.230,80/tcp\n"
            b"M,ACS,A2,Door,10.10.100.230,80/tcp\n"  # dup address -> deduped
            b"M,ACS,A3,NVR,10.10.100.74,80/tcp\n"
        )
        up = self.client.post(
            "/api/v1/imports",
            data={"import_type": "ip_register", "project_id": "demo-project", "site_id": "demo-site"},
            files={"file": ("reg.csv", csv, "text/csv")},
        )
        self.assertEqual((up.status_code, up.json()["accepted_rows"], up.json()["rejected_rows"]), (200, 3, 0), up.text)

        run = self._post(
            "/api/v1/discovery/ip/runs",
            {"authorized": True, "ports": [9], "scan_connect_timeout_s": 1, "scan_rate_limit_per_sec": 0},
            "ip_discovery",
        )
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["status"], "succeeded")
        record = self.client.get(f"/api/v1/discovery/runs/{run.json()['run_id']}").json()
        self.assertEqual(sorted(record["parameters"]["addresses"]), ["10.10.100.230", "10.10.100.74"])

    def test_no_targets_and_no_register_rejected_with_400(self) -> None:
        response = self.client.post(
            "/api/v1/discovery/ip/runs",
            json={"project_id": "empty-p", "site_id": "empty-s", "job_type": "ip_discovery",
                  "parameters": {"authorized": True}},
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("scan target", response.json()["detail"].lower())


class BacnetDiscoveryApiTests(_EngineApiTestCase):
    def test_simulated_backend_end_to_end_persists_devices_and_points(self) -> None:
        response = self._post(
            "/api/v1/discovery/bacnet/runs",
            {**_AUTH, "bacnet_backend": "simulated"},
            "bacnet_discovery",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded")
        run_id = response.json()["run_id"]

        results = self.client.get(f"/api/v1/discovery/runs/{run_id}/results").json()
        self.assertEqual(results["result_summary"]["backend"], "simulated")
        self.assertGreaterEqual(len(results["devices"]), 1)
        self.assertGreaterEqual(len(results["points"]), 1)
        self.assertTrue(all(d["device_type"] == "bacnet_device" for d in results["devices"]))

        # The points view endpoint returns the same persisted points.
        points = self.client.get(f"/api/v1/discovery/runs/{run_id}/points").json()
        self.assertEqual(len(points["points"]), len(results["points"]))

    def test_bacnet_dry_run_emits_no_broadcast(self) -> None:
        response = self._post(
            "/api/v1/discovery/bacnet/runs",
            {**_AUTH, "dry_run": True, "device_instance_low": 1000, "device_instance_high": 2000},
            "bacnet_discovery",
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        results = self.client.get(f"/api/v1/discovery/runs/{run_id}/results").json()
        self.assertIn("dry_run_plan", results["result_summary"])
        self.assertEqual(results["devices"], [])


class MqttDiscoveryApiTests(_EngineApiTestCase):
    def test_mqtt_dry_run_describes_plan_without_connecting(self) -> None:
        response = self._post(
            "/api/v1/discovery/mqtt/runs",
            {"topic_prefix": "udmi", "dry_run": True, "broker_host": "mqtt.example.local"},
            "mqtt_discovery",
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        results = self.client.get(f"/api/v1/discovery/runs/{run_id}/results").json()
        plan = results["result_summary"]["dry_run_plan"]
        self.assertEqual(plan["engine"], "mqtt_discovery")
        self.assertIn("udmi/#", plan["targets"])
        self.assertEqual(plan["broker_host"], "mqtt.example.local")
        # Credentials must never appear in the plan.
        self.assertNotIn("password", str(plan).lower())

    def test_mqtt_unauthorized_real_capture_rejected(self) -> None:
        response = self._post(
            "/api/v1/discovery/mqtt/runs",
            {"topic_prefix": "udmi", "broker_host": "mqtt.example.local"},
            "mqtt_discovery",
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_topics_view_endpoint_available(self) -> None:
        # A dry run produces no topics, but the endpoint must respond cleanly.
        run_id = self._post(
            "/api/v1/discovery/mqtt/runs",
            {"dry_run": True, "broker_host": "mqtt.example.local"},
            "mqtt_discovery",
        ).json()["run_id"]
        topics = self.client.get(f"/api/v1/discovery/runs/{run_id}/topics")
        self.assertEqual(topics.status_code, 200, topics.text)
        self.assertEqual(topics.json()["topics"], [])

    def _mqtt_dry_run_id(self) -> str:
        return self._post(
            "/api/v1/discovery/mqtt/runs",
            {"dry_run": True, "broker_host": "mqtt.example.local"},
            "mqtt_discovery",
        ).json()["run_id"]

    def test_topics_xlsx_export_empty_for_dry_run(self) -> None:
        # mq9nhbzu Excel export: a dry run has no topics, so the workbook must be
        # header-only (no fabricated rows) and carry the xlsx download headers.
        from io import BytesIO

        from openpyxl import load_workbook

        run_id = self._mqtt_dry_run_id()
        resp = self.client.get(f"/api/v1/discovery/runs/{run_id}/topics.xlsx")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn(".xlsx", resp.headers["content-disposition"])
        sheet = load_workbook(BytesIO(resp.content)).active
        rows = list(sheet.iter_rows(values_only=True))
        self.assertEqual(rows[0], ("Topic", "Asset", "Last Seen", "Message Count", "Latest Payload"))
        self.assertEqual(len(rows), 1, "empty capture stays empty — no fabricated data rows")

    def test_topics_xlsx_export_includes_persisted_rows(self) -> None:
        from io import BytesIO

        from app.api.routes import discovery as discovery_routes
        from openpyxl import load_workbook

        run_id = self._mqtt_dry_run_id()
        discovery_routes._discovery_repository().replace_topics(
            run_id,
            [
                {
                    "topic": "334os/b1/ahu-1/state",
                    "last_payload": {"present_value": 22},
                    "message_count": 3,
                    "attributes": {"device_ref": "AHU-1"},
                }
            ],
        )
        resp = self.client.get(f"/api/v1/discovery/runs/{run_id}/topics.xlsx")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = list(load_workbook(BytesIO(resp.content)).active.iter_rows(values_only=True))
        self.assertEqual(len(rows), 2)
        data = rows[1]
        self.assertEqual(data[0], "334os/b1/ahu-1/state")
        self.assertEqual(data[1], "AHU-1")
        self.assertEqual(data[3], "3")
        self.assertIn("present_value", data[4])

    def test_topics_xlsx_topic_filter_narrows_rows(self) -> None:
        from io import BytesIO

        from app.api.routes import discovery as discovery_routes
        from openpyxl import load_workbook

        run_id = self._mqtt_dry_run_id()
        discovery_routes._discovery_repository().replace_topics(
            run_id,
            [
                {"topic": "334os/b1/ahu-1/state", "last_payload": {"a": 1}, "message_count": 1, "attributes": {}},
                {
                    "topic": "334os/b1/ahu-1/events/pointset",
                    "last_payload": {"b": 2},
                    "message_count": 1,
                    "attributes": {},
                },
            ],
        )
        resp = self.client.get(
            f"/api/v1/discovery/runs/{run_id}/topics.xlsx",
            params={"topic_filter": "334os/+/+/state"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        topics = [row[0] for row in load_workbook(BytesIO(resp.content)).active.iter_rows(min_row=2, values_only=True)]
        self.assertEqual(topics, ["334os/b1/ahu-1/state"])

    def test_topics_xlsx_404_for_unknown_run(self) -> None:
        resp = self.client.get("/api/v1/discovery/runs/run_does_not_exist/topics.xlsx")
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_run_parameter_secrets_redacted_to_client_but_real_internally(self) -> None:
        # A broker password / inline private key passed as run parameters must be
        # redacted in the API response (any viewer) but kept server-side so
        # execution + rollback still read the real values.
        run_id = self._post(
            "/api/v1/discovery/mqtt/runs",
            {
                "dry_run": True,
                "broker_host": "mqtt.example.local",
                "password": "hunter2",
                "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            },
            "mqtt_discovery",
        ).json()["run_id"]
        got = self.client.get(f"/api/v1/discovery/runs/{run_id}")
        self.assertEqual(got.status_code, 200, got.text)
        params = got.json()["parameters"]
        self.assertEqual(params["password"], "********")
        self.assertEqual(params["private_key"], "********")
        self.assertEqual(params["broker_host"], "mqtt.example.local")  # non-secret untouched
        self.assertNotIn("hunter2", got.text)
        # Server-side attribute access (rollback/execution) keeps the real value.
        from app.api.routes import discovery as discovery_routes

        self.assertEqual(discovery_routes.service.get_run(run_id).parameters["password"], "hunter2")


class PointValidationApiTests(_EngineApiTestCase):
    def test_inline_point_validation_flags_mismatch(self) -> None:
        response = self._post(
            "/api/v1/validation/bacnet/runs",
            {
                "expected_points": [
                    {
                        "Expected point name": "SupplyAirTemp",
                        "Expected value": "20",
                        "Expected value type": "number",
                        "Required/optional flag": "required",
                    },
                    {
                        "Expected point name": "MissingPoint",
                        "Required/optional flag": "required",
                    },
                ],
                "observed_points": [
                    {"point_name": "SupplyAirTemp", "observed_value": {"value": 25.0}},
                ],
            },
            "bacnet_validation",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded")
        run_id = response.json()["run_id"]

        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()
        summary = run["result_summary"]
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["out_of_tolerance"], 1)

        issues = self.client.get(f"/api/v1/validation/runs/{run_id}/issues").json()["issues"]
        issue_types = {issue["issue_type"] for issue in issues}
        self.assertIn("out_of_tolerance", issue_types)
        self.assertIn("missing_point", issue_types)


class ReportFindingsApiTests(_EngineApiTestCase):
    def test_report_zip_carries_source_run_findings(self) -> None:
        import json as _json
        import zipfile
        from io import BytesIO

        # A validation run that produces a real finding (a required point missing).
        run_id = self._post(
            "/api/v1/validation/bacnet/runs",
            {
                "expected_points": [
                    {"Expected point name": "MissingPoint", "Required/optional flag": "required"}
                ],
                "observed_points": [],
            },
            "bacnet_validation",
        ).json()["run_id"]
        # A report scoped to that run must carry its findings, not just metadata.
        report = self.client.post(
            "/api/v1/reports",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "report_type": "issue_report",
                "output_format": "zip",
                "source_run_ids": [run_id],
            },
        )
        self.assertEqual(report.status_code, 200, report.text)
        report_id = report.json()["report_id"]
        download = self.client.get(f"/api/v1/reports/{report_id}/download")
        self.assertEqual(download.status_code, 200, download.text)
        archive = zipfile.ZipFile(BytesIO(download.content))
        self.assertIn("findings.json", archive.namelist())
        findings = _json.loads(archive.read("findings.json"))
        self.assertTrue(findings, "a scoped report must carry the source run's findings")
        self.assertEqual(findings[0]["Source Run"], run_id)
        self.assertIn("missing_point", {finding["Type"] for finding in findings})


class MappingComparisonApiTests(_EngineApiTestCase):
    def test_inline_mapping_comparison_detects_out_of_tolerance(self) -> None:
        response = self._post(
            "/api/v1/validation/mapping/runs",
            {
                "mapping_rows": [
                    {
                        "Asset ID": "AHU-1",
                        "BACnet object name": "SupplyAirTemp",
                        "MQTT field/path": "supply_air_temperature",
                        "MQTT topic": "udmi/AHU-1/pointset",
                        "Tolerance": "0.5",
                        "Mapping required flag": "required",
                    },
                ],
                "bacnet_observed": [
                    {"point_name": "SupplyAirTemp", "observed_value": {"value": 18.6}},
                ],
                "mqtt_observed": [
                    {"field": "supply_air_temperature", "observed_value": {"value": 21.0}},
                ],
            },
            "mapping_validation",
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        issues = self.client.get(f"/api/v1/validation/runs/{run_id}/issues").json()["issues"]
        self.assertTrue(any(issue["issue_type"] == "out_of_tolerance" for issue in issues))


class CancelEndpointApiTests(_EngineApiTestCase):
    def test_cancel_sets_flag_and_404_for_missing(self) -> None:
        # Create a run, then request cancellation. We assert the cancel flag is
        # honoured by the store (a fresh inline run picks it up via the engine's
        # is_cancel_requested checker). We verify the plumbing deterministically:
        # the cancel endpoint returns the run, and the store reports cancellation.
        run_id = self._post(
            "/api/v1/discovery/ip/runs",
            {"cidr": "10.0.0.0/30", "dry_run": True},
            "ip_discovery",
        ).json()["run_id"]

        cancel = self.client.post(f"/api/v1/runs/{run_id}/cancel")
        self.assertEqual(cancel.status_code, 200, cancel.text)
        self.assertEqual(cancel.json()["run_id"], run_id)

        # The store now reports cancellation requested for this run.
        from app.services.run_service import RunService

        self.assertTrue(RunService().is_cancel_requested(run_id))

        missing = self.client.post("/api/v1/runs/run_00000000000000_deadbeef/cancel")
        self.assertEqual(missing.status_code, 404)

    def test_cancelled_flag_makes_subsequent_run_report_cancelled(self) -> None:
        # Deterministic cancellation: the point-validation engine checks
        # cancellation at 200-row chunk boundaries. We create a run with a LARGE
        # register, pre-set its cancel flag, then drive the inline processor so
        # the engine observes the flag and flips status to cancelled. This is
        # the same plumbing the worker/inline-fallback path uses.
        from app.services.engine_dispatch import make_cancel_checker
        from app.services.run_service import RunService
        from smart_commissioning_core.engines.point_validation import (
            process_bacnet_validation_run,
        )

        service = RunService()
        new_run = service.create_job_run(
            _bacnet_request_with_points(250),
            expected_job_type="bacnet_validation",
        )
        service.request_cancel(new_run.run_id)

        # The inline route path builds the checker from the store exactly like
        # this; with the flag pre-set the engine observes it at a chunk boundary.
        processed = process_bacnet_validation_run(
            new_run.run_id,
            dict(new_run.parameters),
            run_store=service,
            execution_mode="inline_local_fallback",
            is_cancelled=make_cancel_checker(service, new_run.run_id),
        )
        self.assertEqual(processed.status, "cancelled")
        self.assertTrue(processed.result_summary["cancelled"])


class MqttConfigRollbackApiTests(_EngineApiTestCase):
    def test_publish_captures_previous_then_rollback_republishes(self) -> None:
        # Forward publish (validate-only, no live broker) that records a prior
        # config value supplied in the request for rollback.
        publish = self._post(
            "/api/v1/validation/mqtt-config/runs",
            {
                "topic": "334os/b1/ahu-1/config",
                "payload": '{"pointset":{"points":{"sat":{"set_value":22}}}}',
                "confirmed": True,
                "previous_config_payload": {"pointset": {"points": {"sat": {"set_value": 18}}}},
            },
            "mqtt_config_publish",
        )
        self.assertEqual(publish.status_code, 200, publish.text)
        run_id = publish.json()["run_id"]

        run = self.client.get(f"/api/v1/validation/runs/{run_id}").json()
        previous = run["result_summary"]["previous_config"]
        self.assertTrue(previous["captured"])
        self.assertIn("18", previous["payload"])

        # Rollback republishes the captured previous value.
        rollback = self.client.post(f"/api/v1/validation/mqtt-config/runs/{run_id}/rollback")
        self.assertEqual(rollback.status_code, 200, rollback.text)

        rolled = self.client.get(f"/api/v1/validation/runs/{run_id}").json()
        self.assertTrue(rolled["result_summary"]["rollback"])

    def test_rollback_without_captured_value_returns_400(self) -> None:
        publish = self._post(
            "/api/v1/validation/mqtt-config/runs",
            {
                "topic": "334os/b1/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                # No previous_config_payload -> nothing captured to roll back to.
            },
            "mqtt_config_publish",
        )
        run_id = publish.json()["run_id"]
        rollback = self.client.post(f"/api/v1/validation/mqtt-config/runs/{run_id}/rollback")
        self.assertEqual(rollback.status_code, 400, rollback.text)

    def test_live_publish_without_authorization_rejected(self) -> None:
        response = self._post(
            "/api/v1/validation/mqtt-config/runs",
            {
                "topic": "334os/b1/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "use_live_broker": True,
                "broker_host": "mqtt.example.local",
            },
            "mqtt_config_publish",
        )
        self.assertEqual(response.status_code, 403, response.text)


def _bacnet_request_with_points(count: int):
    from app.schemas.jobs import JobCreateRequest

    return JobCreateRequest.model_validate(
        {
            "project_id": "demo-project",
            "site_id": "demo-site",
            "job_type": "bacnet_validation",
            "parameters": {
                "expected_points": [
                    {"Expected point name": f"P{i}", "Required/optional flag": "optional"}
                    for i in range(count)
                ],
                "observed_points": [],
            },
        }
    )


if __name__ == "__main__":
    unittest.main()
