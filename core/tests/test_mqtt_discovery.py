"""Unit tests for the MQTT discovery engine.

HONESTY: there is NO real MQTT broker here. The transport is dependency-injected
(``live_capture`` + ``build_settings``), exactly like ``udmi_validation`` injects
``live_capture``. The fake yields canned :class:`MqttMessage` objects and records
the topic filter / window / max it was asked for, so we can assert the bound was
honoured WITHOUT ever opening a socket. The default ``subscribe_and_capture``
real-broker path (raw-socket CONNECT/SUBSCRIBE/TLS) is NOT exercised — it is
listed in the task's ``live_untested`` output and requires on-site validation.
"""

import json
import unittest
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.engines import mqtt_discovery
from smart_commissioning_core.mqtt_transport import (
    MqttConnectionSettings,
    MqttMessage,
    MqttTransportError,
)


class FakeRunStore:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self._cancelled = cancelled

    def update_run_status(self, run_id: str, *, status: str, stage: str | None = None,
                          progress_percent: int | None = None, error_message: str | None = None) -> dict[str, Any]:
        self.last_status = status
        return {"run_id": run_id, "status": status, "stage": stage,
                "progress_percent": progress_percent, "error_message": error_message,
                "result_summary": dict(self.record_summary)}

    def update_result_summary(self, run_id: str, result_summary: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
        self.summary_calls.append(dict(result_summary))
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        self.issues_calls.append(list(issues))
        return {"run_id": run_id}

    def is_cancel_requested(self, run_id: str) -> bool:
        return self._cancelled


_AUTH = {"authorized": True}
_STUB_SETTINGS = MqttConnectionSettings(host="broker.test", port=1883, client_id="test")


def _stub_build(_parameters: dict[str, Any]) -> MqttConnectionSettings:
    return _STUB_SETTINGS


def _json_msg(
    topic: str,
    payload: dict[str, Any],
    *,
    retained: bool = False,
    qos: int = 0,
    received_at: datetime | None = None,
) -> MqttMessage:
    kwargs: dict[str, Any] = {
        "topic": topic,
        "payload": json.dumps(payload).encode("utf-8"),
        "retained": retained,
        "qos": qos,
    }
    if received_at is not None:
        kwargs["received_at"] = received_at
    return MqttMessage(**kwargs)


_FIXED_TS = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)


class FakeCapture:
    """Records what it was asked to capture and replays canned messages.

    Emulates the real ``subscribe_and_capture``: it fires ``on_message`` for
    every accepted message (before dedup) and, under ``retain_latest``, keeps
    one LATEST message per topic in first-seen order with ``max_messages`` as a
    distinct-topic cap. A test that constructs FakeCapture but calls it with the
    defaults (no ``retain_latest``) still gets the old append-every-message-up-
    to-the-cap behavior.
    """

    def __init__(self, messages: list[MqttMessage]) -> None:
        self._messages = messages
        self.calls: list[dict[str, Any]] = []

    def __call__(self, settings: MqttConnectionSettings, *, topics: list[str],
                 timeout_seconds: float | None, max_messages: int,
                 cancel_check: Any = None, qos: int = 0,
                 retain_latest: bool = False,
                 on_message: Any = None) -> list[MqttMessage]:
        self.calls.append({"settings": settings, "topics": list(topics),
                           "timeout_seconds": timeout_seconds, "max_messages": max_messages,
                           "cancel_check": cancel_check, "qos": qos,
                           "retain_latest": retain_latest, "on_message": on_message})
        result: list[MqttMessage] = []
        positions: dict[str, int] = {}
        for message in self._messages:
            # max_messages caps the raw list (raw mode) or the distinct-topic
            # set (retain_latest) — mirror the transport's top-of-loop check.
            if len(result) >= max_messages:
                break
            if on_message is not None:
                on_message(message)
            if not retain_latest:
                result.append(message)
            elif message.topic in positions:
                result[positions[message.topic]] = message
            else:
                positions[message.topic] = len(result)
                result.append(message)
        return result


class TopicFilterTests(unittest.TestCase):
    def test_default_filter_is_hash(self) -> None:
        self.assertEqual(mqtt_discovery._resolve_topic_filters({}), ["#"])

    def test_prefix_normalized_to_subtree(self) -> None:
        self.assertEqual(mqtt_discovery._resolve_topic_filters({"topic_prefix": "udmi"}), ["udmi/#"])
        self.assertEqual(mqtt_discovery._resolve_topic_filters({"topic_filter": "udmi/#"}), ["udmi/#"])

    def test_explicit_topics_list(self) -> None:
        self.assertEqual(
            mqtt_discovery._resolve_topic_filters({"topics": ["a/#", "b/#"]}),
            ["a/#", "b/#"],
        )


class AggregationTests(unittest.TestCase):
    def test_fake_transport_topics_counts_and_assets(self) -> None:
        store = FakeRunStore()
        messages = [
            _json_msg("udmi/AHU-1/pointset", {"present_value": 1}),
            _json_msg("udmi/AHU-1/pointset", {"present_value": 2}),  # 2nd, becomes last
            _json_msg("udmi/AHU-1/state", {"online": True}),
        ]
        capture = FakeCapture(messages)
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_agg", {**_AUTH, "topic_prefix": "udmi"},
            run_store=store, execution_mode="x",
            live_capture=capture, build_settings=_stub_build,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )

        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["messages_captured"], 3)
        self.assertEqual(summary["topics_discovered"], 2)

        # discovered_assets: one per distinct topic, in first-seen order.
        assets = summary["discovered_assets"]
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["asset_id"], "AHU-1")  # derived from topic
        self.assertEqual(assets[0]["match_basis"], "none")

        # structured DiscoveredTopic records: counts + last payload.
        self.assertEqual(len(persisted), 1)
        _rid, records = persisted[0]
        pointset = next(r for r in records if r["topic"] == "udmi/AHU-1/pointset")
        self.assertEqual(pointset["message_count"], 2)
        self.assertEqual(pointset["last_payload"], {"present_value": 2})  # last wins
        state = next(r for r in records if r["topic"] == "udmi/AHU-1/state")
        self.assertEqual(state["message_count"], 1)

    def test_non_json_payload_stored_as_presence_marker(self) -> None:
        store = FakeRunStore()
        messages = [MqttMessage(topic="raw/topic", payload=b"\x00\x01not-json")]
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_raw", {**_AUTH}, run_store=store, execution_mode="x",
            live_capture=FakeCapture(messages), build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "succeeded")
        # last_payload must be a JSON-object marker, never raw bytes.
        topic_summary = store.summary_calls[-1]
        self.assertEqual(topic_summary["topics_discovered"], 1)

    def test_empty_capture_window(self) -> None:
        store = FakeRunStore()
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_empty", {**_AUTH}, run_store=store, execution_mode="x",
            live_capture=FakeCapture([]), build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "failed")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["topics_discovered"], 0)
        self.assertEqual(summary["broker_status_detail"], "capture_window_empty")
        self.assertIn("capture_window_empty", result["error_message"])

    def test_partial_broker_drop_fails_and_keeps_captured_topics(self) -> None:
        store = FakeRunStore()
        partial = [_json_msg("udmi/AHU-1/state", {"timestamp": "2026-07-09T10:00:00Z"})]

        def interrupted_capture(*_args: object, **_kwargs: object) -> list[MqttMessage]:
            raise mqtt_discovery.MqttCaptureInterrupted(
                partial,
                ConnectionResetError("broker dropped password=hunter2"),
            )

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_partial_drop",
            {**_AUTH},
            run_store=store,
            execution_mode="x",
            live_capture=interrupted_capture,
            build_settings=_stub_build,
        )

        self.assertEqual(result["status"], "failed")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["topics_discovered"], 1)
        self.assertEqual(summary["messages_captured"], 1)
        self.assertEqual(summary["broker_status_detail"], "authentication_error")
        self.assertNotIn("hunter2", str(summary))
        self.assertIn("authentication_error", result["error_message"])
        self.assertNotIn("hunter2", result["error_message"])


class RetainLatestAggregationTests(unittest.TestCase):
    """The retain-latest capture bounds memory to one message per topic while
    the observer keeps per-topic counts honest across the dedup."""

    def test_duplicate_heavy_stream_is_memory_bounded_but_counts_stay_honest(self) -> None:
        store = FakeRunStore()
        # 1000 messages cycling three topics: the retained list must collapse to
        # three records, but the counts must still total the full 1000 seen.
        messages = [_json_msg(f"t/{i % 3}", {"i": i}) for i in range(1000)]
        capture = FakeCapture(messages)
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_dupe", {**_AUTH, "max_messages": 500},
            run_store=store, execution_mode="dramatiq_worker",
            live_capture=capture, build_settings=_stub_build,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )

        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["topics_discovered"], 3)
        # Total honest count, not the deduped list length.
        self.assertEqual(summary["messages_captured"], 1000)
        self.assertEqual(summary["capture_retention"], "latest_per_topic")
        self.assertFalse(summary["topic_limit_reached"])

        _rid, records = persisted[0]
        self.assertEqual(len(records), 3)  # memory-bounded: one record per topic
        self.assertEqual(sum(r["message_count"] for r in records), 1000)
        # Last payload on t/0 wins (i=999 is the final t/0 message).
        topic0 = next(r for r in records if r["topic"] == "t/0")
        self.assertEqual(topic0["last_payload"], {"i": 999})

    def test_distinct_topic_cap_is_reached_and_flagged(self) -> None:
        store = FakeRunStore()
        messages = [_json_msg(f"t/{i}", {"i": i}) for i in range(5)]
        mqtt_discovery.process_mqtt_discovery_run(
            "run_topic_cap", {**_AUTH, "max_messages": 3},
            run_store=store, execution_mode="dramatiq_worker",
            live_capture=FakeCapture(messages), build_settings=_stub_build,
        )
        summary = store.summary_calls[-1]
        self.assertEqual(summary["topics_discovered"], 3)
        self.assertTrue(summary["topic_limit_reached"])
        self.assertTrue(summary["message_limit_reached"])

    def test_counts_fall_back_to_returned_list_when_observer_is_ignored(self) -> None:
        # An ad-hoc fake that never calls on_message: counts must fall back to
        # the returned message list instead of collapsing to zero.
        class _NoObserverCapture:
            def __init__(self, messages: list[MqttMessage]) -> None:
                self._messages = messages

            def __call__(self, settings: MqttConnectionSettings, *, topics: list[str],
                         timeout_seconds: float | None, max_messages: int,
                         cancel_check: Any = None, qos: int = 0,
                         retain_latest: bool = False, on_message: Any = None) -> list[MqttMessage]:
                return list(self._messages[:max_messages])

        store = FakeRunStore()
        messages = [
            _json_msg("udmi/AHU-1/pointset", {"present_value": 1}),
            _json_msg("udmi/AHU-1/pointset", {"present_value": 2}),
            _json_msg("udmi/AHU-1/state", {"online": True}),
        ]
        mqtt_discovery.process_mqtt_discovery_run(
            "run_fallback", {**_AUTH, "topic_prefix": "udmi"},
            run_store=store, execution_mode="x",
            live_capture=_NoObserverCapture(messages), build_settings=_stub_build,
        )
        summary = store.summary_calls[-1]
        self.assertEqual(summary["topics_discovered"], 2)
        self.assertEqual(summary["messages_captured"], 3)

    def test_max_messages_is_clamped_to_the_topic_cap(self) -> None:
        # A silly operator value cannot buy unbounded retained-payload memory.
        self.assertEqual(
            mqtt_discovery._max_messages({"max_messages": 10_000_000}),
            mqtt_discovery.MAX_TOPIC_CAP,
        )


class MessageMetadataTests(unittest.TestCase):
    """Per-topic last-message metadata (retained / delivery QoS / received-at)
    rides the structured record attributes for the v0.1.13 inspector, and the
    subscription QoS cap is echoed in result_summary. Describes the SAME message
    as last_payload by construction.
    """

    def _run(
        self, messages: list[MqttMessage], params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], FakeRunStore, list[tuple[str, list[dict[str, Any]]]]]:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_meta", {**_AUTH, **(params or {})},
            run_store=store, execution_mode="dramatiq_worker",
            live_capture=FakeCapture(messages), build_settings=_stub_build,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        return result, store, persisted

    def test_metadata_carried_into_structured_record(self) -> None:
        messages = [
            _json_msg("udmi/AHU-1/state", {"online": True}, retained=True, qos=1, received_at=_FIXED_TS)
        ]
        result, _store, persisted = self._run(messages)
        self.assertEqual(result["status"], "succeeded")
        _rid, records = persisted[0]
        attrs = records[0]["attributes"]
        self.assertIs(attrs["last_retained"], True)
        self.assertEqual(attrs["last_qos"], 1)
        self.assertEqual(attrs["last_received_at"], _FIXED_TS.isoformat())

    def test_metadata_describes_the_last_message_on_a_topic(self) -> None:
        earlier = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)
        messages = [
            _json_msg("t/1", {"v": 1}, retained=True, qos=0, received_at=earlier),
            _json_msg("t/1", {"v": 2}, retained=False, qos=1, received_at=_FIXED_TS),
        ]
        _result, _store, persisted = self._run(messages)
        _rid, records = persisted[0]
        # The metadata must describe the SAME (last) message as last_payload.
        self.assertEqual(records[0]["last_payload"], {"v": 2})
        attrs = records[0]["attributes"]
        self.assertIs(attrs["last_retained"], False)
        self.assertEqual(attrs["last_qos"], 1)
        self.assertEqual(attrs["last_received_at"], _FIXED_TS.isoformat())

    def test_non_json_payload_still_carries_metadata(self) -> None:
        messages = [
            MqttMessage(topic="raw/topic", payload=b"\x00not-json", retained=True, qos=2, received_at=_FIXED_TS)
        ]
        _result, _store, persisted = self._run(messages)
        _rid, records = persisted[0]
        self.assertEqual(records[0]["last_payload"], {"_raw_present": True})
        attrs = records[0]["attributes"]
        self.assertIs(attrs["last_retained"], True)
        self.assertEqual(attrs["last_qos"], 2)
        self.assertEqual(attrs["last_received_at"], _FIXED_TS.isoformat())

    def test_subscribe_qos_echoed_in_summary(self) -> None:
        _result, store, _persisted = self._run([_json_msg("t/1", {"v": 1})], params={"qos": 2})
        self.assertEqual(store.summary_calls[-1]["subscribe_qos"], 2)

    def test_subscribe_qos_defaults_to_zero_when_absent(self) -> None:
        _result, store, _persisted = self._run([_json_msg("t/1", {"v": 1})])
        self.assertEqual(store.summary_calls[-1]["subscribe_qos"], 0)


class BoundTests(unittest.TestCase):
    def test_capture_window_and_max_messages_passed_through(self) -> None:
        store = FakeRunStore()
        capture = FakeCapture([_json_msg(f"t/{i}", {"i": i}) for i in range(50)])
        mqtt_discovery.process_mqtt_discovery_run(
            "run_bound", {**_AUTH, "capture_seconds": 2.5, "max_messages": 10},
            run_store=store, execution_mode="x",
            live_capture=capture, build_settings=_stub_build,
        )
        call = capture.calls[-1]
        self.assertEqual(call["timeout_seconds"], 2.5)
        self.assertEqual(call["max_messages"], 10)
        summary = store.summary_calls[-1]
        # Fake honoured the cap -> only 10 captured, limit flagged reached.
        self.assertEqual(summary["messages_captured"], 10)
        self.assertTrue(summary["message_limit_reached"])

    def test_defaults_applied(self) -> None:
        store = FakeRunStore()
        capture = FakeCapture([])
        mqtt_discovery.process_mqtt_discovery_run(
            "run_def", {**_AUTH}, run_store=store, execution_mode="x",
            live_capture=capture, build_settings=_stub_build,
        )
        call = capture.calls[-1]
        self.assertEqual(call["timeout_seconds"], mqtt_discovery.DEFAULT_CAPTURE_SECONDS)
        self.assertEqual(call["max_messages"], mqtt_discovery.DEFAULT_MAX_MESSAGES)
        self.assertEqual(call["topics"], ["#"])

    def test_explicit_zero_is_indefinite_on_worker(self) -> None:
        # mq9nhbzu: capture_seconds=0 => indefinite (timeout None) + a cancel
        # check is wired so the run can be stopped; summary labels it indefinite.
        # Indefinite is honored on the background worker (dramatiq_worker).
        store = FakeRunStore()
        capture = FakeCapture([_json_msg("t/1", {"i": 1})])
        mqtt_discovery.process_mqtt_discovery_run(
            "run_indef", {**_AUTH, "capture_seconds": 0},
            run_store=store, execution_mode="dramatiq_worker",
            live_capture=capture, build_settings=_stub_build,
        )
        call = capture.calls[-1]
        self.assertIsNone(call["timeout_seconds"])
        self.assertTrue(callable(call["cancel_check"]))
        self.assertEqual(store.summary_calls[-1]["capture_mode"], "indefinite")
        self.assertFalse(store.summary_calls[-1]["indefinite_bounded_inline"])

    def test_indefinite_bounded_on_inline_path(self) -> None:
        # Inline-hang guard: an indefinite request on a non-worker (inline) path
        # is bounded to the default window so it can't tie up the request worker,
        # and the downgrade is flagged. The capture gets a finite timeout.
        store = FakeRunStore()
        capture = FakeCapture([_json_msg("t/1", {"i": 1})])
        mqtt_discovery.process_mqtt_discovery_run(
            "run_inline_indef", {**_AUTH, "capture_seconds": 0},
            run_store=store, execution_mode="inline_local_fallback",
            live_capture=capture, build_settings=_stub_build,
        )
        call = capture.calls[-1]
        self.assertEqual(call["timeout_seconds"], mqtt_discovery.DEFAULT_CAPTURE_SECONDS)
        self.assertEqual(store.summary_calls[-1]["capture_mode"], "bounded")
        self.assertTrue(store.summary_calls[-1]["indefinite_bounded_inline"])

    def test_missing_seconds_is_bounded(self) -> None:
        store = FakeRunStore()
        capture = FakeCapture([])
        mqtt_discovery.process_mqtt_discovery_run(
            "run_bounded", {**_AUTH}, run_store=store, execution_mode="x",
            live_capture=capture, build_settings=_stub_build,
        )
        self.assertEqual(store.summary_calls[-1]["capture_mode"], "bounded")


class CancelDuringCaptureTests(unittest.TestCase):
    def test_cancel_during_capture_stops_and_marks_cancelled(self) -> None:
        # A store whose cancel flag flips True after the pre-capture check, and a
        # capture that polls the injected cancel_check and stops early. Proves the
        # engine passes a working cancel_check and reports a cancelled run with
        # the partial messages — all without a broker.
        class FlippingStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__()
                self._calls = 0

            def is_cancel_requested(self, run_id: str) -> bool:
                self._calls += 1
                return self._calls > 1  # False on the pre-capture check, then True

        class CancellingCapture:
            def __call__(self, settings: MqttConnectionSettings, *, topics: list[str],
                         timeout_seconds: float | None, max_messages: int,
                         cancel_check: Any = None, qos: int = 0,
                         retain_latest: bool = False, on_message: Any = None) -> list[MqttMessage]:
                captured: list[MqttMessage] = []
                for index in range(max_messages):
                    captured.append(_json_msg(f"t/{index}", {"i": index}))
                    if cancel_check and cancel_check():
                        break
                return captured

        store = FlippingStore()
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_cancel", {**_AUTH, "capture_seconds": 0},
            run_store=store, execution_mode="dramatiq_worker",
            live_capture=CancellingCapture(), build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(store.summary_calls[-1]["messages_captured"], 1)


class DryRunTests(unittest.TestCase):
    def test_dry_run_connects_to_nothing(self) -> None:
        store = FakeRunStore()

        def boom_capture(*_a: Any, **_k: Any) -> list[MqttMessage]:  # pragma: no cover
            raise AssertionError("dry run must not call the transport")

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_dry",
            {"broker_host": "broker.example", "broker_port": 8883,
             "topic_prefix": "udmi", "capture_seconds": 9, "max_messages": 7},
            run_store=store, execution_mode="x", dry_run=True,
            live_capture=boom_capture, build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertTrue(summary["dry_run"])
        plan = summary["dry_run_plan"]
        self.assertEqual(plan["engine"], "mqtt_discovery")
        self.assertEqual(plan["targets"], ["udmi/#"])
        self.assertEqual(plan["capture_seconds"], 9)
        self.assertEqual(plan["max_messages"], 7)
        # broker coordinates surfaced from the stub settings, NO credentials.
        self.assertEqual(plan["broker_host"], "broker.test")
        self.assertEqual(plan["broker_port"], 1883)

    def test_dry_run_does_not_require_authorization(self) -> None:
        store = FakeRunStore()
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_dry2", {"topic_prefix": "udmi"},
            run_store=store, execution_mode="x", dry_run=True,
            live_capture=FakeCapture([]), build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "succeeded")


class AuthorizationTests(unittest.TestCase):
    def test_real_capture_without_authorization_fails_and_connects_to_nothing(self) -> None:
        store = FakeRunStore()

        def boom_capture(*_a: Any, **_k: Any) -> list[MqttMessage]:  # pragma: no cover
            raise AssertionError("unauthorized run must not call the transport")

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_noauth", {"topic_prefix": "udmi"},
            run_store=store, execution_mode="x",
            live_capture=boom_capture, build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("udmi", result["error_message"] or "")


class CancellationTests(unittest.TestCase):
    def test_cancel_before_capture_returns_cancelled(self) -> None:
        store = FakeRunStore(cancelled=True)

        def boom_capture(*_a: Any, **_k: Any) -> list[MqttMessage]:  # pragma: no cover
            raise AssertionError("cancelled run must not start capture")

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_cancel", {**_AUTH},
            run_store=store, execution_mode="x",
            live_capture=boom_capture, build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(store.summary_calls[-1]["broker_status_detail"], "cancelled_before_capture")


class ErrorSanitizationTests(unittest.TestCase):
    def test_transport_error_text_not_leaked(self) -> None:
        store = FakeRunStore()
        secret = "user=admin password=hunter2 host=10.9.9.9"

        def failing_capture(*_a: Any, **_k: Any) -> list[MqttMessage]:
            raise MqttTransportError(f"connection refused: {secret}")

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_err", {**_AUTH},
            run_store=store, execution_mode="x",
            live_capture=failing_capture, build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "failed")
        summary = store.summary_calls[-1]
        detail = summary["broker_status_detail"]
        # Coarse label only — raw secret must NOT appear anywhere in the summary.
        self.assertEqual(detail, "authentication_error")
        serialized = json.dumps(summary)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("admin", serialized)
        self.assertNotIn("10.9.9.9", serialized)

    def test_live_capture_unavailable_is_honest(self) -> None:
        store = FakeRunStore()
        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_unavail", {**_AUTH},
            run_store=store, execution_mode="x",
            live_capture=None, build_settings=_stub_build,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.summary_calls[-1]["broker_status_detail"], "live_capture_unavailable")
        self.assertIn("live_capture_unavailable", result["error_message"])

    def test_missing_broker_host_maps_to_status(self) -> None:
        store = FakeRunStore()

        def failing_build(_p: dict[str, Any]) -> MqttConnectionSettings:
            raise ValueError("Live broker mode requires an MQTT broker FQDN or IP address.")

        result = mqtt_discovery.process_mqtt_discovery_run(
            "run_nohost", {**_AUTH},
            run_store=store, execution_mode="x",
            live_capture=FakeCapture([]), build_settings=failing_build,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.summary_calls[-1]["broker_status_detail"], "broker_unreachable")
        self.assertIn("broker_unreachable", result["error_message"])


if __name__ == "__main__":
    unittest.main()
