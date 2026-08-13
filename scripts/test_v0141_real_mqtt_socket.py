#!/usr/bin/env python3
"""Fail-closed real-socket v0.1.41 legacy-register capture gate.

Start an isolated anonymous Mosquitto listener first, for example:

``docker run --rm -d --name sct-v0141-mqtt -p 18883:1883 eclipse-mosquitto:2``

The test imports a 780-row blank-applicability register through the backend API,
then uses the production MQTT socket client for capture and publishing. It never
accepts a fake client or a list-only capture result.
"""

from __future__ import annotations

import argparse
import io
import threading
import time
import unittest

from harness import ApiTestCase
from smart_commissioning_core.mqtt_transport import (
    MqttClient,
    MqttConnectionSettings,
    subscribe_and_capture_with_outcome,
)
from smart_commissioning_core.udmi_validation import validate_udmi_full_report


class RealMqttSocketGate(ApiTestCase):
    """Production transport proof against a local broker, not a mock."""

    host = "127.0.0.1"
    port = 18883
    env = {"JOB_EXECUTION_MODE": "inline", "AUTH_MODE": "local"}

    def _mapped_legacy_parameters(self) -> dict[str, object]:
        project_id = "v0141-socket-project"
        site_id = "v0141-socket-site"
        header = (
            "Project/site,System,Asset ID,Expected topic,Expected schema version,"
            "Expected reporting interval,Source protocol\n"
        )
        rows = "".join(
            f"Socket Site,BMS,DDC-{index:03d},socket/DDC-{index:03d}/#,1.5.2,60,MQTT\n"
            for index in range(780)
        )
        upload = self.client.post(
            "/api/v1/imports",
            data={"import_type": "mqtt_register", "project_id": project_id, "site_id": site_id},
            files={"file": ("legacy-blank.csv", io.BytesIO((header + rows).encode()), "text/csv")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        created = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": project_id,
                "site_id": site_id,
                "job_type": "udmi_validation",
                "parameters": {"use_register": True, "use_live_broker": False},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        mapped = self.client.get(f"/api/v1/validation/runs/{created.json()['run_id']}").json()
        assets = mapped["parameters"]["assets"]
        self.assertEqual(len(assets), 780)
        self.assertEqual(sum(len(asset["expected_schedule"]["payload_types"]) for asset in assets), 2340)
        return dict(mapped["parameters"])

    def _publish_traffic(self) -> None:
        # Invalid expected payload JSON still constitutes observed expected-topic
        # traffic; diagnostic topics exceed their independently bounded lane.
        settings = MqttConnectionSettings(
            host=self.host, port=self.port, client_id="v0141-socket-publisher", timeout_seconds=5
        )
        # The validation client subscribes to 2,340 concrete topics. A fixed
        # one-shot delay can publish entirely before its final SUBACK arrives.
        # Repeat a bounded batch for eight seconds at 50ms intervals. This covers
        # the configured five-second CONNECT/SUBACK setup ceiling, the two-second
        # measurement window, and modest scheduling slack without an unbounded flood.
        # Repeated diagnostics retain the same 650 concrete topics, so this is
        # bounded traffic while still proving high-cardinality lane truncation.
        until = time.monotonic() + 8.0
        diagnostic_cursor = 0
        with MqttClient(settings) as client:
            while time.monotonic() < until:
                for index in range(25):
                    client.publish(f"socket/DDC-{index:03d}/state", b"{invalid-json")
                # Interleave one 50-topic diagnostic slice with the complete
                # expected set. Thirteen slices cover all 650 distinct
                # diagnostics without allowing one large QoS-0 burst to starve
                # expected evidence on the same local socket.
                for index in range(diagnostic_cursor, min(diagnostic_cursor + 50, 650)):
                    client.publish(f"socket/diagnostic/{index}", b"{}")
                diagnostic_cursor = (diagnostic_cursor + 50) % 650
                time.sleep(0.05)

    def test_real_socket_legacy_register_deadline_and_stop_isolation(self) -> None:
        parameters = self._mapped_legacy_parameters()
        parameters.update(
            {
                "use_live_broker": True,
                "broker_host": self.host,
                "broker_port": self.port,
                "broker_use_tls": False,
                "client_id": "v0141-socket-validation",
                "capture_seconds": 2,
                "use_tls": False,
                "topic_discovery_enabled": True,
                "topic_discovery_scope": "all",
                "topic_discovery_all_scope_confirmed": True,
                # Offer 650 distinct diagnostics but use a 100-topic secondary
                # test budget so local QoS-0 scheduling cannot make the
                # truncation assertion machine-speed dependent.
                "unexpected_max_messages": 100,
            }
        )
        capture_entered = threading.Event()

        def live_capture_wrapper(
            settings: MqttConnectionSettings, **capture_options: object
        ) -> object:
            capture_entered.set()
            return subscribe_and_capture_with_outcome(settings, **capture_options)

        def publish_after_capture_entry() -> None:
            self.assertTrue(capture_entered.wait(timeout=20), "live capture did not start")
            self._publish_traffic()

        publisher = threading.Thread(target=publish_after_capture_entry)
        publisher.start()
        result = validate_udmi_full_report(
            parameters, cancel_check=lambda: False, live_capture=live_capture_wrapper
        )
        publisher.join(timeout=10)
        self.assertTrue(capture_entered.is_set(), "live capture wrapper was not entered")
        self.assertFalse(publisher.is_alive(), "publisher did not complete")
        summary = result.result_summary
        validation = summary["validation_summary_v1"]
        self.assertTrue(summary["window_completed"])
        self.assertEqual(summary["termination_reason"], "window_elapsed")
        self.assertTrue(summary["capture_retention"]["secondary_count_truncated"])
        self.assertEqual(summary["capture_retention"]["secondary_retained_count"], 100)
        self.assertEqual(validation["payload_metrics"]["expected"], 2340)
        self.assertGreaterEqual(validation["asset_metrics"]["observed"], 25)
        self.assertGreaterEqual(validation["payload_metrics"]["received"], 25)

        settings = MqttConnectionSettings(
            host=self.host, port=self.port, client_id="v0141-socket-stop", timeout_seconds=5
        )
        stopped = subscribe_and_capture_with_outcome(
            settings, topics=["socket/#"], timeout_seconds=1, max_messages=1, cancel_check=lambda: True
        )
        self.assertEqual(stopped.termination, "cancelled")
        later = subscribe_and_capture_with_outcome(
            settings, topics=["socket/#"], timeout_seconds=0.3, max_messages=1, cancel_check=lambda: False
        )
        self.assertEqual(later.termination, "deadline_elapsed")
        self.assertTrue(later.deadline_elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18883)
    args, remainder = parser.parse_known_args()
    RealMqttSocketGate.host = args.host
    RealMqttSocketGate.port = args.port
    unittest.main(argv=[__file__, *remainder])


if __name__ == "__main__":
    main()
