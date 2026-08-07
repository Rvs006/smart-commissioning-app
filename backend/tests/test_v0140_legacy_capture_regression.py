"""v0.1.40 regression: blank register -> transport outcome -> validation."""

import io
import unittest
from unittest import mock

from harness import ApiTestCase
from smart_commissioning_core import mqtt_transport
from smart_commissioning_core.mqtt_transport import MqttMessage
from smart_commissioning_core.udmi_validation import validate_udmi_full_report

_API_KEY = "test-v0140-legacy-capture-key"


class _CaptureClock:
    def __init__(self) -> None:
        self.exhausted = False

    def __call__(self) -> float:
        return 2.0 if self.exhausted else 0.0


class _FakeMqttClient:
    def __init__(self, messages: list[MqttMessage], clock: _CaptureClock) -> None:
        self.messages = messages
        self.clock = clock

    def __enter__(self) -> "_FakeMqttClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def subscribe_many(self, _topics: list[str], _qos: int = 0) -> None:
        return None

    def ping(self) -> None:
        return None

    def read_publish_any(self, **_kwargs: object) -> MqttMessage | None:
        if self.messages:
            return self.messages.pop(0)
        self.clock.exhausted = True
        return None


class V0140LegacyCaptureRegressionTests(ApiTestCase):
    env = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "api_key", "API_KEY": _API_KEY}
    client_headers = {"X-API-Key": _API_KEY}

    def test_blank_register_reaches_deadline_after_diagnostic_saturation(self) -> None:
        project_id = "v0140-legacy-project"
        site_id = "v0140-legacy-site"
        header = (
            "Project/site,System,Asset ID,Expected topic,Expected schema version,"
            "Expected reporting interval,Source protocol\n"
        )
        rows = "".join(
            f"Site A,BMS,DDC-{index:03d},site/DDC-{index:03d}/#,1.5.2,60,MQTT\n"
            for index in range(780)
        )
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project_id, "site_id": site_id},
            files={"file": ("legacy.csv", io.BytesIO((header + rows).encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        run = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": project_id,
                "site_id": site_id,
                "job_type": "udmi_validation",
                "parameters": {"use_register": True, "use_live_broker": False},
            },
        )
        self.assertEqual(run.status_code, 200, run.text)
        mapped = self.client.get(f"/api/v1/validation/runs/{run.json()['run_id']}").json()
        assets = mapped["parameters"]["assets"]
        self.assertEqual(len(assets), 780)
        expected_payload_count = sum(
            len(asset["expected_schedule"]["payload_types"]) for asset in assets
        )
        self.assertEqual(expected_payload_count, 2340)

        expected_messages = [
            MqttMessage(f"site/DDC-{index:03d}/state", b"{}") for index in range(25)
        ]
        diagnostics = [MqttMessage(f"site/noise/{index}", b"{}") for index in range(501)]
        clock = _CaptureClock()
        fake = _FakeMqttClient([*expected_messages, *diagnostics], clock)
        parameters = {
            **mapped["parameters"],
            "use_live_broker": True,
            "broker_host": "192.0.2.10",
            "capture_seconds": 1,
            "topic_discovery_enabled": True,
            "topic_discovery_scope": "bounded",
            "unexpected_max_messages": 500,
        }
        with (
            mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: fake),
            mock.patch.object(mqtt_transport.time, "monotonic", clock),
        ):
            result = validate_udmi_full_report(parameters, cancel_check=lambda: False)

        summary = result.result_summary
        validation = summary["validation_summary_v1"]
        self.assertTrue(summary["window_completed"])
        self.assertEqual(summary["termination_reason"], "window_elapsed")
        self.assertTrue(summary["capture_retention"]["secondary_count_truncated"])
        self.assertGreater(validation["payload_metrics"]["expected"], 0)
        self.assertGreater(validation["payload_metrics"]["received"], 0)
        self.assertEqual(validation["payload_metrics"]["received"], 25)
        self.assertEqual(validation["asset_metrics"]["observed"], 25)
        discovery = summary["asset_topic_discovery"]
        self.assertFalse(discovery["capture_complete"])
        self.assertEqual(discovery["capture_status"], "secondary_topic_limit_reached")


if __name__ == "__main__":
    unittest.main()
