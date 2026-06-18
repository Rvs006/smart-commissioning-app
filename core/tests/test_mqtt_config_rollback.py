"""Unit tests for MQTT config-publish previous-value capture + rollback.

HONESTY: there is NO real MQTT broker here. These tests drive the rollback
plumbing (previous-value capture, the rollback republish, the run processor)
with an INJECTED fake publisher or no broker at all. The live raw-socket
republish against a real broker is the SAME untested path as the forward
publish and is listed in the task's live_untested output.
"""

import json
import unittest
from typing import Any

from smart_commissioning_core.engines.safety import ScanNotAuthorized
from smart_commissioning_core.mqtt_config_publish import (
    _capture_previous_config,
    rollback_config,
    validate_and_publish_config,
)
from smart_commissioning_core.mqtt_config_publish_processor import (
    process_mqtt_config_publish_run,
    process_mqtt_config_rollback_run,
)
from smart_commissioning_core.mqtt_transport import MqttMessage, MqttTransportError


class FakeRunStore:
    def __init__(self) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}
        self.issues: list[Any] = []
        self.last_status: str | None = None

    def update_run_status(self, run_id: str, *, status: str, stage: str | None = None,
                          progress_percent: int | None = None, error_message: str | None = None) -> dict[str, Any]:
        self.last_status = status
        self.status_calls.append({"status": status, "stage": stage, "error_message": error_message})
        return {"run_id": run_id, "status": status, "stage": stage,
                "progress_percent": progress_percent, "error_message": error_message,
                "result_summary": dict(self.summary)}

    def update_result_summary(self, run_id: str, result_summary: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
        if merge:
            self.summary.update(result_summary)
        else:
            self.summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        self.issues = list(issues)
        return {"run_id": run_id}


class CapturePreviousConfigTests(unittest.TestCase):
    def test_request_supplied_dict_is_captured_as_json(self) -> None:
        captured = _capture_previous_config(
            {"previous_config_payload": {"pointset": {"points": {"sat": {"set_value": 18}}}}},
            "site/ahu-1/config",
        )
        self.assertTrue(captured["captured"])
        self.assertEqual(captured["source"], "request_supplied")
        self.assertIn("18", captured["payload"])
        self.assertEqual(json.loads(captured["payload"])["pointset"]["points"]["sat"]["set_value"], 18)

    def test_request_supplied_string_is_captured_verbatim(self) -> None:
        captured = _capture_previous_config({"previous_config_payload": '{"a":1}'}, "t/config")
        self.assertTrue(captured["captured"])
        self.assertEqual(captured["payload"], '{"a":1}')

    def test_no_prior_value_records_not_captured(self) -> None:
        captured = _capture_previous_config({}, "t/config")
        self.assertFalse(captured["captured"])
        self.assertIsNone(captured["payload"])
        self.assertEqual(captured["source"], "not_captured_no_broker_read")


class ForwardPublishCapturesPreviousTests(unittest.TestCase):
    def test_publish_result_carries_previous_config(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": '{"pointset":{"points":{"sat":{"set_value":22}}}}',
                "confirmed": True,
                "previous_config_payload": {"pointset": {"points": {"sat": {"set_value": 18}}}},
            }
        )
        previous = result.result_summary["previous_config"]
        self.assertTrue(previous["captured"])
        self.assertEqual(previous["topic"], "site/ahu-1/config")
        self.assertIn("18", previous["payload"])

    def test_live_retained_read_captures_prior_value_before_publish(self) -> None:
        # A live broker_reader snapshots the device's PRIOR retained config before
        # the forward publish, so rollback restores the real prior value (18), not
        # the value being published (22). Injected reader => no real socket.
        reads: dict[str, Any] = {}

        def fake_reader(_settings: Any, *, config_topic: str, timeout_seconds: float) -> str | None:
            reads["topic"] = config_topic
            return '{"pointset":{"points":{"sat":{"set_value":18}}}}'

        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": '{"pointset":{"points":{"sat":{"set_value":22}}}}',
                "confirmed": True,
                "use_live_broker": True,
                "broker_host": "mqtt.example.local",
                "authorized": True,
            },
            broker_publisher=lambda *a, **k: MqttMessage(topic="x", payload=b'{"pointset":{"points":{}}}'),
            broker_reader=fake_reader,
        )
        previous = result.result_summary["previous_config"]
        self.assertEqual(previous["source"], "live_retained_read")
        self.assertTrue(previous["captured"])
        self.assertIn("18", previous["payload"])  # PRIOR value, not the published 22
        self.assertEqual(reads["topic"], "site/ahu-1/config")


class RollbackConfigTests(unittest.TestCase):
    def test_rollback_republishes_previous_payload_via_publisher(self) -> None:
        calls: dict[str, Any] = {}

        def fake_publisher(settings: Any, *, config_topic: str, config_payload: str,
                           pointset_topic: str, timeout_seconds: float) -> MqttMessage | None:
            calls["config_topic"] = config_topic
            calls["config_payload"] = config_payload
            # Return a pointset so the publish path completes without timeout.
            return MqttMessage(topic=pointset_topic, payload=b'{"pointset":{"points":{}}}')

        previous_config = {"topic": "site/ahu-1/config", "payload": '{"prev":true}', "captured": True}
        result = rollback_config(
            {
                "topic": "site/ahu-1/config",
                "payload": '{"new":true}',
                "confirmed": True,
                "use_live_broker": True,
                "broker_host": "mqtt.example.local",
                "broker_port": 1883,
                # A live republish is an authorized active operation now gated in
                # the engine core, so supply authorization to exercise the
                # publisher path.
                "authorized": True,
            },
            previous_config,
            broker_publisher=fake_publisher,
        )
        # The captured PREVIOUS payload was republished, not the forward payload.
        self.assertEqual(calls["config_payload"], '{"prev":true}')
        self.assertEqual(calls["config_topic"], "site/ahu-1/config")
        self.assertTrue(result.result_summary["rollback"])
        self.assertEqual(result.issues, [])

    def test_rollback_honours_publish_confirmation_gate(self) -> None:
        # Not confirmed => the publish gate blocks the rollback too.
        previous_config = {"topic": "site/ahu-1/config", "payload": '{"prev":true}', "captured": True}
        result = rollback_config(
            {"topic": "site/ahu-1/config", "payload": "{}", "confirmed": False},
            previous_config,
        )
        self.assertIn("publish_not_confirmed", [i.issue_type for i in result.issues])

    def test_rollback_without_captured_payload_raises(self) -> None:
        with self.assertRaises(ValueError):
            rollback_config({"confirmed": True}, {"topic": "t/config", "payload": None, "captured": False})


class RollbackProcessorTests(unittest.TestCase):
    def test_processor_marks_succeeded_when_no_issues(self) -> None:
        store = FakeRunStore()
        result = process_mqtt_config_rollback_run(
            "run_rb",
            {"topic": "site/ahu-1/config", "confirmed": True},
            previous_config={"topic": "site/ahu-1/config", "payload": '{"prev":true}', "captured": True},
            run_store=store,
            execution_mode="inline_local_fallback",
        )
        # Validate-only (no live broker) republish of a valid topic+payload =>
        # no issues, so the rollback run succeeds.
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(store.summary["rollback"])

    def test_processor_marks_failed_when_payload_missing(self) -> None:
        store = FakeRunStore()
        result = process_mqtt_config_rollback_run(
            "run_rb2",
            {"topic": "site/ahu-1/config", "confirmed": True},
            previous_config={"topic": "site/ahu-1/config", "payload": None, "captured": False},
            run_store=store,
            execution_mode="inline_local_fallback",
        )
        self.assertEqual(result["status"], "failed")


class LiveBrokerAuthorizationTests(unittest.TestCase):
    """Authorization is enforced in the engine core, not only the API route.

    A worker / direct caller cannot bypass it: a live publish/rollback without
    authorization must raise ScanNotAuthorized BEFORE the broker publisher is
    ever invoked.
    """

    def test_unauthorized_live_publish_does_not_call_publisher(self) -> None:
        calls: list[Any] = []

        def fake_publisher(*args: Any, **kwargs: Any) -> MqttMessage | None:
            calls.append((args, kwargs))
            return None

        with self.assertRaises(ScanNotAuthorized):
            validate_and_publish_config(
                {
                    "topic": "site/ahu-1/config",
                    "payload": "{}",
                    "confirmed": True,
                    "use_live_broker": True,
                    "broker_host": "mqtt.example.local",
                    # No authorization key supplied.
                },
                broker_publisher=fake_publisher,
            )
        self.assertEqual(calls, [], "publisher must never be called for an unauthorized live publish")

    def test_unauthorized_live_rollback_does_not_call_publisher(self) -> None:
        calls: list[Any] = []

        def fake_publisher(*args: Any, **kwargs: Any) -> MqttMessage | None:
            calls.append((args, kwargs))
            return None

        with self.assertRaises(ScanNotAuthorized):
            rollback_config(
                {
                    "topic": "site/ahu-1/config",
                    "payload": "{}",
                    "confirmed": True,
                    "use_live_broker": True,
                    "broker_host": "mqtt.example.local",
                },
                {"topic": "site/ahu-1/config", "payload": '{"prev":true}', "captured": True},
                broker_publisher=fake_publisher,
            )
        self.assertEqual(calls, [], "publisher must never be called for an unauthorized live rollback")

    def test_validate_only_publish_stays_unauthenticated(self) -> None:
        # No live broker => no authorization required; validate-only succeeds.
        result = validate_and_publish_config(
            {"topic": "site/ahu-1/config", "payload": "{}", "confirmed": True}
        )
        self.assertEqual(result.issues, [])


class BrokerErrorDoesNotLeakCredentialsTests(unittest.TestCase):
    """A credential-bearing broker error must never reach error_message/issues."""

    _SECRET = "password=hunter2"

    def _exploding_publisher(self) -> Any:
        def fake_publisher(*args: Any, **kwargs: Any) -> MqttMessage | None:
            raise MqttTransportError(f"connection refused redis://user:{self._SECRET}@broker:1883")

        return fake_publisher

    def test_broker_error_not_in_issue_descriptions_or_summary(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "use_live_broker": True,
                "broker_host": "mqtt.example.local",
                "authorized": True,
            },
            broker_publisher=self._exploding_publisher(),
        )
        serialized_issues = json.dumps([issue.__dict__ for issue in result.issues], default=str)
        self.assertNotIn(self._SECRET, serialized_issues)
        self.assertNotIn(self._SECRET, json.dumps(result.result_summary, default=str))
        # The coarse status label IS present so the operator still gets a hint.
        self.assertTrue(any("Live MQTT publish/subscribe failed" in i.description for i in result.issues))

    def test_processor_failure_message_is_sanitized(self) -> None:
        # An exception raised inside the engine (not a caught broker error, but a
        # hard failure) must surface only the sanitized constant in error_message.
        store = FakeRunStore()

        def boom_publisher(*args: Any, **kwargs: Any) -> MqttMessage | None:
            raise RuntimeError(f"boom {BrokerErrorDoesNotLeakCredentialsTests._SECRET}")

        result = process_mqtt_config_publish_run(
            "run_pub",
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "use_live_broker": True,
                "broker_host": "mqtt.example.local",
                "authorized": True,
            },
            run_store=store,
            execution_mode="inline_local_fallback",
            broker_publisher=boom_publisher,
            broker_reader=None,  # not testing the retained read; avoid a real socket
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(BrokerErrorDoesNotLeakCredentialsTests._SECRET, str(result["error_message"]))
        self.assertEqual(result["error_message"], "MQTT config publish failed; see server logs.")


class MultiPointConfirmTests(unittest.TestCase):
    """Confirm-back must cover every written point, not just the primary (mq9n11wi).

    These exercise the validate-only (no broker) path: the caller supplies the
    next_pointset_payload directly, so confirmation is fully dev-testable.
    """

    def test_all_points_confirmed_no_issues(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_points": [
                    {"point": "sat", "value": 22},
                    {"point": "fan", "value": True},
                ],
                "next_pointset_payload": {
                    "pointset": {"points": {"sat": {"present_value": 22}, "fan": {"present_value": True}}}
                },
            }
        )
        self.assertEqual(result.issues, [])
        confirmed = result.result_summary["expected_points"]
        self.assertEqual(len(confirmed), 2)
        self.assertTrue(all(point["confirmed"] for point in confirmed))
        self.assertEqual(result.result_summary["status"], "succeeded")

    def test_one_point_not_confirmed_raises_single_issue(self) -> None:
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_points": [
                    {"point": "sat", "value": 22},
                    {"point": "fan", "value": True},
                ],
                # sat is confirmed; fan never appears in the next pointset.
                "next_pointset_payload": {"pointset": {"points": {"sat": {"present_value": 22}}}},
            }
        )
        overrides = [i for i in result.issues if i.issue_type == "config_override_not_observed"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].point_name, "fan")
        self.assertEqual(overrides[0].observed_value, "missing")
        confirmed = {point["point"]: point["confirmed"] for point in result.result_summary["expected_points"]}
        self.assertTrue(confirmed["sat"])
        self.assertFalse(confirmed["fan"])
        self.assertEqual(result.result_summary["status"], "failed")

    def test_legacy_singular_expected_point_still_confirmed(self) -> None:
        # No expected_points list => fall back to the singular primary point,
        # exactly as before this change.
        result = validate_and_publish_config(
            {
                "topic": "site/ahu-1/config",
                "payload": "{}",
                "confirmed": True,
                "expected_point": "sat",
                "expected_value": 22,
                "next_pointset_payload": {"pointset": {"points": {"sat": {"present_value": 22}}}},
            }
        )
        self.assertEqual(result.issues, [])
        self.assertEqual(result.result_summary["observed_value"], 22)
        self.assertEqual(len(result.result_summary["expected_points"]), 1)


if __name__ == "__main__":
    unittest.main()
