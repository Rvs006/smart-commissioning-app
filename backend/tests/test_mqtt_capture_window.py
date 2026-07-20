"""48h ceiling on the MQTT discovery capture window (field ask 2026-07-14).

The API caps an explicit ``capture_seconds`` at MQTT_MAX_CAPTURE_SECONDS (48h)
and the ``discover_mqtt`` worker actor's time limit sits one hour ABOVE that cap
— when the two were equal, a full 48h capture would be killed at the wire by its
own executor. The MQTT cap MUST equal the UDMI cap so the two capture routes can
never drift apart. The cross-component contract is asserted here textually (the
worker's ``app`` package cannot be imported alongside the backend's ``app``).
"""

import re
import unittest
from pathlib import Path

from harness import ApiTestCase

_API_KEY = "test-mqtt-capture-window-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_WORKER_TASKS = Path(__file__).resolve().parents[2] / "worker" / "app" / "tasks.py"


class MqttCaptureWindowCapTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _post_run(self, parameters: dict) -> object:
        return self.client.post(
            "/api/v1/discovery/mqtt/runs",
            json={
                "project_id": "mqtt-capture-window-project",
                "site_id": "mqtt-capture-window-site",
                "job_type": "mqtt_discovery",
                "parameters": parameters,
            },
        )

    def test_capture_window_over_cap_is_400(self) -> None:
        from app.api.routes.discovery import MQTT_MAX_CAPTURE_SECONDS

        # Authorized real request: the cap guard fires BEFORE the run is created
        # or any broker is contacted, so this stays cheap and orphans nothing.
        response = self._post_run(
            {"authorized": True, "capture_seconds": MQTT_MAX_CAPTURE_SECONDS + 1}
        )
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn(str(MQTT_MAX_CAPTURE_SECONDS), detail)
        self.assertIn("48 hours", detail)

    def test_capture_window_at_cap_is_accepted(self) -> None:
        from app.api.routes.discovery import MQTT_MAX_CAPTURE_SECONDS

        # A dry run needs no authorization and does no I/O, so accepting the
        # maximum bound is cheap to prove end to end.
        response = self._post_run(
            {"dry_run": True, "capture_seconds": MQTT_MAX_CAPTURE_SECONDS}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_indefinite_capture_is_accepted(self) -> None:
        # capture_seconds 0 is the indefinite sentinel (run until stopped) and
        # stays legal — it runs until Stop run, the topic cap, or the 48h backstop.
        response = self._post_run({"dry_run": True, "capture_seconds": 0})
        self.assertEqual(response.status_code, 200, response.text)

    def test_worker_actor_time_limit_exceeds_the_capture_cap(self) -> None:
        from app.api.routes.discovery import MQTT_MAX_CAPTURE_SECONDS

        # The MQTT cap and the UDMI cap must be identical so the two capture
        # routes can never silently diverge — both source the one shared core
        # constant (the blank-capture backstop).
        from app.api.routes.validation import MAX_UDMI_CAPTURE_SECONDS
        from smart_commissioning_core.mqtt_settings import INDEFINITE_BACKSTOP_SECONDS

        self.assertEqual(MQTT_MAX_CAPTURE_SECONDS, 172_800)
        self.assertEqual(MQTT_MAX_CAPTURE_SECONDS, MAX_UDMI_CAPTURE_SECONDS)
        self.assertEqual(MQTT_MAX_CAPTURE_SECONDS, INDEFINITE_BACKSTOP_SECONDS)

        source = _WORKER_TASKS.read_text(encoding="utf-8")
        match = re.search(
            r'@dramatiq\.actor\(queue_name="discovery", max_retries=0, '
            r"time_limit=([\d_]+)\)\s*\ndef discover_mqtt",
            source,
        )
        self.assertIsNotNone(match, "discover_mqtt actor declaration not found")
        time_limit_ms = int(match.group(1))
        # Strictly ABOVE the cap so a maximum-length accepted capture is never
        # killed by its own executor (currently cap + 1h margin).
        self.assertGreater(time_limit_ms, MQTT_MAX_CAPTURE_SECONDS * 1000)
        self.assertEqual(time_limit_ms, 176_400_000)


if __name__ == "__main__":
    unittest.main()
