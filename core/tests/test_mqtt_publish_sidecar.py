"""Unit tests for the mqtt_publish engine (the sealed one-message write).

No HTTP: the transport seam ``mqtt_publish_sidecar._post_json`` is monkeypatched.
Pins the sealed-contract runtime-limits acceptance (the base.py protocol map
edit), the no-I/O dry plan, the digest-drift refusal, and the honesty mappings.
"""

from __future__ import annotations

import hashlib
import unittest
import urllib.error
from unittest.mock import patch

from smart_commissioning_core.engines import mqtt_publish_sidecar
from smart_commissioning_core.engines.base import (
    EngineContext,
    ThrottleConfig,
    _scan_runtime_limits,
)
from smart_commissioning_core.engines.mqtt_publish_sidecar import _run_mqtt_publish


def _ctx(params: dict, *, dry: bool) -> EngineContext:
    return EngineContext(
        run_id="r",
        parameters=dict(params),
        run_store=None,
        execution_mode="inline",
        throttle=ThrottleConfig(),
        dry_run=dry,
        _is_cancelled=lambda: False,
    )


def _params(payload: str = '{"set":1}') -> dict:
    sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "topic": "site/x/config",
        "payload": payload,
        "qos": 1,
        "retain": True,
        "scan_contract_v1": {
            "scan_contract_version": "1.0",
            "job_type": "mqtt_publish",
            "mqtt_publish": {
                "topic": "site/x/config",
                "payload_sha256": sha,
                "qos": 1,
                "retain": True,
                "broker": {"host": "broker.example.local", "port": 8883, "tls": True},
                "policy": {"dispatch_phase_seconds": 30.0, "run_deadline_seconds": 60.0},
            },
        },
        "scan_authorization": {"authorized": True, "authorized_by": "eng", "authorization_id": "auth-1"},
    }


class MqttPublishEngineTest(unittest.TestCase):
    def test_runtime_limits_accept_the_mqtt_publish_contract(self) -> None:
        # Guards the base.py protocol-key map edit: without it the run fails closed.
        limits = _scan_runtime_limits(_params())
        assert limits is not None
        self.assertEqual((limits.dispatch_seconds, limits.run_seconds), (30.0, 60.0))

    def test_dry_plan_echoes_sealed_bytes_without_io(self) -> None:
        result = _run_mqtt_publish(_ctx(_params(), dry=True), base_url=None)
        plan = result.result_summary_extra["dry_run_plan"]
        self.assertEqual(plan["targets"], ["site/x/config"])
        self.assertEqual(plan["qos"], 1)
        self.assertTrue(plan["retain"])
        self.assertEqual(plan["broker_host"], "broker.example.local")

    def test_digest_drift_refuses_to_send(self) -> None:
        drifted = _params()
        drifted["payload"] = '{"set":2}'  # sha no longer matches the sealed digest
        result = _run_mqtt_publish(_ctx(drifted, dry=False), base_url="http://127.0.0.1:1")
        self.assertEqual(result.status_override, "failed")
        self.assertIn("digest", result.error_message or "")

    def test_missing_sidecar_fails_honestly(self) -> None:
        result = _run_mqtt_publish(_ctx(_params(), dry=False), base_url=None)
        self.assertEqual(result.status_override, "failed")

    def test_missing_contract_fails(self) -> None:
        result = _run_mqtt_publish(_ctx({"topic": "t", "payload": "p"}, dry=False), base_url="http://127.0.0.1:1")
        self.assertEqual(result.status_override, "failed")

    def test_successful_publish_records_honest_evidence(self) -> None:
        with patch.object(mqtt_publish_sidecar, "_post_json", lambda *_a, **_k: {"ok": True}):
            result = _run_mqtt_publish(_ctx(_params(), dry=False), base_url="http://127.0.0.1:1")
        publish = result.result_summary_extra["publish"]
        self.assertTrue(publish["accepted_by_sidecar"])
        self.assertFalse(publish["delivery_confirmed"])
        self.assertEqual(publish["authorized_by"], "eng")
        # No broker credentials anywhere in the evidence.
        self.assertNotIn("password", str(publish))
        self.assertNotIn("username", str(publish))

    def test_not_connected_and_unreachable_map_to_failed(self) -> None:
        def raise_409(*_a, **_k):
            raise urllib.error.HTTPError("http://x/api/publish", 409, "Conflict", {}, None)

        with patch.object(mqtt_publish_sidecar, "_post_json", raise_409):
            result = _run_mqtt_publish(_ctx(_params(), dry=False), base_url="http://127.0.0.1:1")
        self.assertEqual(result.status_override, "failed")
        self.assertIn("not connected", (result.error_message or "").lower())

        def raise_url(*_a, **_k):
            raise urllib.error.URLError("boom")

        with patch.object(mqtt_publish_sidecar, "_post_json", raise_url):
            result = _run_mqtt_publish(_ctx(_params(), dry=False), base_url="http://127.0.0.1:1")
        self.assertEqual(result.status_override, "failed")
        self.assertIn("could not be reached", (result.error_message or "").lower())

    def test_client_not_accepting_maps_to_failed(self) -> None:
        with patch.object(mqtt_publish_sidecar, "_post_json", lambda *_a, **_k: {"ok": False}):
            result = _run_mqtt_publish(_ctx(_params(), dry=False), base_url="http://127.0.0.1:1")
        self.assertEqual(result.status_override, "failed")


if __name__ == "__main__":
    unittest.main()
