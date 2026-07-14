"""48h ceiling on the UDMI capture window (field ask 2026-07-14).

The API caps an explicit ``capture_seconds`` at MAX_UDMI_CAPTURE_SECONDS (48h)
and the worker actor's time limit sits one hour ABOVE that cap — when the two
were equal, a full 48h capture was guaranteed to be killed at the wire by its
own executor. The cross-component contract is asserted here textually (the
worker's ``app`` package cannot be imported alongside the backend's ``app``).
"""

import re
import unittest
from pathlib import Path

from harness import ApiTestCase

_API_KEY = "test-udmi-capture-window-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
}

_WORKER_TASKS = Path(__file__).resolve().parents[2] / "worker" / "app" / "tasks.py"


class UdmiCaptureWindowCapTests(ApiTestCase):
    env = _ENV_OVERRIDES
    client_headers = {"X-API-Key": _API_KEY}

    def _post_run(self, capture_seconds: object) -> object:
        return self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "udmi-capture-window-project",
                "site_id": "udmi-capture-window-site",
                "job_type": "udmi_validation",
                "parameters": {
                    "expected_schedule": {"asset_id": "EM-1", "udmi_version": "1.5.2"},
                    "state_payload": {
                        "timestamp": "2026-07-14T10:00:00Z",
                        "version": "1.5.2",
                        "system": {},
                    },
                    "capture_seconds": capture_seconds,
                    "use_live_broker": False,
                },
            },
        )

    def test_capture_window_over_cap_is_400(self) -> None:
        from app.api.routes.validation import MAX_UDMI_CAPTURE_SECONDS

        response = self._post_run(MAX_UDMI_CAPTURE_SECONDS + 1)
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn(str(MAX_UDMI_CAPTURE_SECONDS), detail)
        self.assertIn("48 hours", detail)

    def test_capture_window_at_cap_is_accepted(self) -> None:
        from app.api.routes.validation import MAX_UDMI_CAPTURE_SECONDS

        # Pasted payloads with no live broker: the window is not waited out, so
        # accepting the maximum bound is cheap to prove end to end.
        response = self._post_run(MAX_UDMI_CAPTURE_SECONDS)
        self.assertEqual(response.status_code, 200, response.text)

    def test_worker_actor_time_limit_exceeds_the_capture_cap(self) -> None:
        from app.api.routes.validation import MAX_UDMI_CAPTURE_SECONDS

        self.assertEqual(MAX_UDMI_CAPTURE_SECONDS, 172_800)
        source = _WORKER_TASKS.read_text(encoding="utf-8")
        match = re.search(
            r'@dramatiq\.actor\(queue_name="validation", max_retries=0, '
            r"time_limit=([\d_]+)\)\s*\ndef validate_udmi_payloads",
            source,
        )
        self.assertIsNotNone(match, "validate_udmi_payloads actor declaration not found")
        time_limit_ms = int(match.group(1))
        # Strictly ABOVE the cap so a maximum-length accepted capture is never
        # killed by its own executor (currently cap + 1h margin).
        self.assertGreater(time_limit_ms, MAX_UDMI_CAPTURE_SECONDS * 1000)
        self.assertEqual(time_limit_ms, 176_400_000)


if __name__ == "__main__":
    unittest.main()
