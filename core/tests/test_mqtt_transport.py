"""Unit tests for the hand-rolled MQTT transport keepalive (PINGREQ).

No real broker/socket: a recording fake socket captures sent bytes and a patched
_connect skips the CONNECT/CONNACK handshake. Proves the PINGREQ packet the
capture loop now sends on a quiet broker is correctly encoded (0xC0 + zero
remaining-length), so a long/indefinite capture is no longer dropped after
~keep_alive of silence. The time-based interval wiring in subscribe_and_capture
is exercised on-site against a real broker (see docs/phase5-onsite-validation.md).
"""

import socket
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from smart_commissioning_core import mqtt_transport
from smart_commissioning_core.mqtt_transport import (
    MqttClient,
    MqttConnectionSettings,
    MqttMessage,
    MqttTransportError,
)


class RecordingSocket:
    def __init__(self) -> None:
        self.sent = bytearray()

    def settimeout(self, _t: float) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def close(self) -> None:
        pass


def _settings(**overrides: Any) -> MqttConnectionSettings:
    base = dict(host="broker.test", port=1883, client_id="test-client", use_tls=False)
    base.update(overrides)
    return MqttConnectionSettings(**base)


class PingTests(unittest.TestCase):
    def test_ping_sends_pingreq_packet(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        # Skip the real CONNECT/CONNACK handshake; we only assert PINGREQ bytes.
        with mock.patch.object(MqttClient, "_connect", lambda _self: None):
            with client:
                client.ping()
        # PINGREQ = control byte 0xC0 followed by a zero remaining-length byte.
        self.assertIn(b"\xc0\x00", bytes(sock.sent))


class PublishFilterTests(unittest.TestCase):
    def test_read_publish_any_accepts_wildcard_subscription_filter(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        topic = "MNVRHS-09-R09-LIGH-LT0399/events/pointset"
        payload = len(topic).to_bytes(2, "big") + topic.encode("utf-8") + b'{"ok":true}'

        for expected_filter in ("#", "MNVRHS-09-R09-LIGH-LT0399/#"):
            with self.subTest(expected_filter=expected_filter):
                with mock.patch.object(client, "_read_packet", return_value=(0x30, payload)):
                    message = client.read_publish_any(
                        expected_topics={expected_filter},
                        timeout_seconds=1,
                    )
                self.assertIsNotNone(message)
                self.assertEqual(message.topic, topic)

    def test_read_publish_preserves_the_mqtt_retain_flag(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        topic = "site/asset/state"
        payload = len(topic).to_bytes(2, "big") + topic.encode("utf-8") + b"{}"

        with mock.patch.object(client, "_read_packet", return_value=(0x31, payload)):
            retained = client.read_publish_any(expected_topics={topic}, timeout_seconds=1)
        with mock.patch.object(client, "_read_packet", return_value=(0x30, payload)):
            live = client.read_publish_any(expected_topics={topic}, timeout_seconds=1)

        self.assertTrue(retained.retained)
        self.assertFalse(live.retained)

    def test_qos1_publish_strips_packet_id_and_sends_puback(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        topic = "site/asset/state"
        packet_id = b"\x12\x34"
        payload = len(topic).to_bytes(2, "big") + topic.encode() + packet_id + b'{"ok":true}'

        with mock.patch.object(client, "_read_packet", return_value=(0x33, payload)):
            message = client.read_publish_any(expected_topics={topic}, timeout_seconds=1)

        self.assertEqual(message.json_payload(), {"ok": True})
        self.assertTrue(message.retained)
        self.assertIn(b"\x40\x02\x12\x34", bytes(sock.sent))

    def test_qos2_publish_completes_pubrec_pubrel_pubcomp_handshake(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        first_topic = "site/asset/state"
        second_topic = "site/asset/metadata"
        packet_id = b"\x45\x67"
        qos2_payload = (
            len(first_topic).to_bytes(2, "big")
            + first_topic.encode()
            + packet_id
            + b'{"state":true}'
        )
        qos0_payload = len(second_topic).to_bytes(2, "big") + second_topic.encode() + b"{}"

        with mock.patch.object(
            client,
            "_read_packet",
            side_effect=[(0x34, qos2_payload), (0x62, packet_id), (0x30, qos0_payload)],
        ) as read_packet:
            first = client.read_publish_any(expected_topics={first_topic}, timeout_seconds=1)
            # The application payload is not exposed until PUBREL has arrived
            # and PUBCOMP has been sent, so capture cannot stop mid-handshake.
            self.assertEqual(read_packet.call_count, 2)
            self.assertIn(b"\x70\x02\x45\x67", bytes(sock.sent))
            self.assertEqual(client._qos2_pending_messages, {})
            second = client.read_publish_any(expected_topics={second_topic}, timeout_seconds=1)

        self.assertEqual(first.json_payload(), {"state": True})
        self.assertEqual(second.topic, second_topic)
        self.assertIn(b"\x50\x02\x45\x67", bytes(sock.sent))
        self.assertIn(b"\x70\x02\x45\x67", bytes(sock.sent))


class ConfigPublishConfirmationTests(unittest.TestCase):
    def test_confirmation_ignores_retained_and_pre_publish_pointsets(self) -> None:
        topic = "site/asset/events/pointset"
        now = datetime.now(UTC)
        retained = MqttMessage(
            topic,
            b'{"source":"retained"}',
            retained=True,
            received_at=now + timedelta(minutes=1),
        )
        pre_publish = MqttMessage(
            topic,
            b'{"source":"before"}',
            received_at=now - timedelta(minutes=1),
        )
        fresh = MqttMessage(
            topic,
            b'{"source":"after"}',
            received_at=now + timedelta(minutes=1),
        )

        class FakeClient:
            def __init__(self) -> None:
                self.messages = [retained, pre_publish, fresh]
                self.actions: list[str] = []

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *_exc: object) -> None:
                pass

            def subscribe(self, _topic: str) -> None:
                self.actions.append("subscribe")

            def discard_pending_messages(self) -> None:
                self.actions.append("discard")

            def publish(self, _topic: str, _payload: str) -> None:
                self.actions.append("publish")

            def read_publish(self, **_kwargs: object) -> MqttMessage | None:
                self.actions.append("read")
                return self.messages.pop(0) if self.messages else None

        client = FakeClient()
        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: client):
            message = mqtt_transport.publish_config_and_wait_for_pointset(
                _settings(),
                config_topic="site/asset/config",
                config_payload='{"set":true}',
                pointset_topic=topic,
                timeout_seconds=1,
            )

        self.assertIs(message, fresh)
        self.assertEqual(client.actions[:3], ["subscribe", "discard", "publish"])
        self.assertEqual(client.actions.count("read"), 3)


class BatchSubscribeTests(unittest.TestCase):
    def test_all_topics_are_subscribed_before_retained_payloads_are_read(self) -> None:
        sock = RecordingSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        topics = ["a/state", "a/metadata", "a/events/pointset"]
        topic = topics[0]
        retained_publish = len(topic).to_bytes(2, "big") + topic.encode() + b'{"ok":true}'

        with mock.patch.object(
            client,
            "_read_packet",
            # MQTT 3.1.1 permits a matching retained PUBLISH before SUBACK.
            side_effect=[(0x31, retained_publish), (0x90, b"\x00\x01\x01\x01\x01")],
        ):
            client.subscribe_many(topics, qos=1)
            message = client.read_publish_any(expected_topics=set(topics), timeout_seconds=1)

        self.assertEqual(message.topic, topic)
        self.assertTrue(message.retained)
        # One MQTT SUBSCRIBE packet contains all three filters; no retained
        # PUBLISH can be mistaken for the next per-topic SUBACK.
        self.assertEqual(bytes(sock.sent).count(b"\x82"), 1)


class SourceAddressTests(unittest.TestCase):
    def test_source_address_binds_default_socket_factory(self) -> None:
        # With source_address set and NO injected factory, the client must wrap
        # socket.create_connection to pass that source_address. Capture the call
        # by patching create_connection on the transport module's socket.
        captured: dict[str, Any] = {}
        recording = RecordingSocket()

        def fake_create_connection(address: tuple[str, int], timeout: float, *,
                                   source_address: tuple[str, int] | None = None) -> RecordingSocket:
            captured["address"] = address
            captured["timeout"] = timeout
            captured["source_address"] = source_address
            return recording

        settings = _settings(source_address=("192.168.1.10", 0))
        client = MqttClient(settings)  # no socket_factory -> default is wrapped
        with mock.patch.object(socket, "create_connection", fake_create_connection):
            with mock.patch.object(MqttClient, "_connect", lambda _self: None):
                with client:
                    pass
        self.assertEqual(captured["address"], ("broker.test", 1883))
        self.assertEqual(captured["source_address"], ("192.168.1.10", 0))

    def test_injected_factory_ignores_source_address(self) -> None:
        # An injected socket_factory must NEVER be wrapped, even when
        # source_address is set: the caller's factory wins unchanged.
        sock = RecordingSocket()
        captured: dict[str, Any] = {}

        def factory(addr: tuple[str, int], timeout: float) -> RecordingSocket:
            captured["addr"] = addr
            return sock

        settings = _settings(source_address=("192.168.1.10", 0))
        client = MqttClient(settings, socket_factory=factory)
        with mock.patch.object(MqttClient, "_connect", lambda _self: None):
            with client:
                pass
        self.assertEqual(captured["addr"], ("broker.test", 1883))
        self.assertIs(client._socket, sock)


class _FakeCaptureClient:
    """Context-manager stand-in for MqttClient inside subscribe_and_capture.

    ``reads`` scripts read_publish_any: an MqttMessage (or None) is returned,
    an Exception instance is raised — letting a test drop the broker mid-loop.
    """

    def __init__(self, reads: list[Any]) -> None:
        self._reads = reads
        self.subscribed: list[tuple[str, int]] = []

    def __enter__(self) -> "_FakeCaptureClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    def subscribe_many(self, topics: list[str], qos: int = 0) -> None:
        self.subscribed.extend((topic, qos) for topic in topics)

    def ping(self) -> None:
        pass

    def read_publish_any(
        self,
        *,
        expected_topics: set[str] | None = None,
        timeout_seconds: float,
        cancel_check: Any = None,
    ) -> MqttMessage | None:
        item = self._reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ExitTeardownTests(unittest.TestCase):
    """__exit__ must never raise on a dead connection: the DISCONNECT sendall
    failing with an OSError (RST/timeout) would otherwise propagate out of the
    with-block in subscribe_and_capture and discard the partial messages the
    session already captured.
    """

    def test_exit_swallows_os_error_from_disconnect_and_still_closes(self) -> None:
        class DeadSocket(RecordingSocket):
            def __init__(self) -> None:
                super().__init__()
                self.closed = False

            def sendall(self, data: bytes) -> None:
                raise ConnectionResetError("connection reset by broker")

            def close(self) -> None:
                self.closed = True

        sock = DeadSocket()
        client = MqttClient(_settings(), socket_factory=lambda _addr, _t: sock)
        client._socket = sock
        client.__exit__(None, None, None)  # must not raise
        self.assertTrue(sock.closed)


class SubscribeAndCaptureFailureTests(unittest.TestCase):
    """A broker drop surfaces honestly without discarding partial evidence."""

    def test_mid_capture_transport_error_carries_partial_messages(self) -> None:
        captured = MqttMessage(topic="ASSET-1/events/pointset", payload=b"{}")
        for error in (ConnectionResetError("broker dropped"), MqttTransportError("broker closed the connection")):
            with self.subTest(error=type(error).__name__):
                fake = _FakeCaptureClient([captured, error])
                with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings, fake=fake: fake):
                    with self.assertRaises(mqtt_transport.MqttCaptureInterrupted) as raised:
                        mqtt_transport.subscribe_and_capture(
                            _settings(),
                            topics=["ASSET-1/#", "ASSET-2/#"],
                            timeout_seconds=None,
                            max_messages=5,
                        )
                self.assertEqual(raised.exception.messages, [captured])
                self.assertIs(raised.exception.cause, error)

    def test_keepalive_failure_carries_partial_messages(self) -> None:
        captured = MqttMessage(topic="ASSET-1/state", payload=b"{}")
        error = ConnectionResetError("broker dropped during ping")

        class FailingPingClient(_FakeCaptureClient):
            def ping(self) -> None:
                raise error

        fake = FailingPingClient([captured])
        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: fake):
            with mock.patch.object(mqtt_transport.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
                with self.assertRaises(mqtt_transport.MqttCaptureInterrupted) as raised:
                    mqtt_transport.subscribe_and_capture(
                        _settings(keep_alive=2),
                        topics=["ASSET-1/#", "ASSET-2/#"],
                        timeout_seconds=None,
                        max_messages=5,
                    )

        self.assertEqual(raised.exception.messages, [captured])
        self.assertIs(raised.exception.cause, error)

    def test_connect_failure_still_raises(self) -> None:
        class FailingConnect:
            def __enter__(self) -> "FailingConnect":
                raise OSError("connection refused")

            def __exit__(self, *_exc: object) -> None:
                pass

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: FailingConnect()):
            with self.assertRaises(OSError):
                mqtt_transport.subscribe_and_capture(
                    _settings(), topics=["ASSET-1/#"], timeout_seconds=5, max_messages=5
                )

    def test_subscribe_failure_still_raises(self) -> None:
        class FailingSubscribe(_FakeCaptureClient):
            def subscribe_many(self, topics: list[str], qos: int = 0) -> None:
                raise MqttTransportError("MQTT broker rejected the subscription.")

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: FailingSubscribe([])):
            with self.assertRaises(MqttTransportError):
                mqtt_transport.subscribe_and_capture(
                    _settings(), topics=["ASSET-1/#"], timeout_seconds=5, max_messages=5
                )


class SubscribeAndCaptureCompletionTests(unittest.TestCase):
    def test_duplicates_cannot_exhaust_a_completion_driven_capture(self) -> None:
        first = MqttMessage(topic="ASSET-1/state", payload=b'{"n":1}')
        latest_first = MqttMessage(topic="ASSET-1/state", payload=b'{"n":3}')
        second = MqttMessage(topic="ASSET-2/state", payload=b'{"n":2}')
        fake = _FakeCaptureClient([first, first, latest_first, second])

        def complete(messages: list[MqttMessage]) -> bool:
            return {message.topic for message in messages} == {first.topic, second.topic}

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: fake):
            messages = mqtt_transport.subscribe_and_capture(
                _settings(),
                topics=[first.topic, second.topic],
                timeout_seconds=None,
                max_messages=3,
                stop_when=complete,
            )

        self.assertEqual(messages, [latest_first, second])

    def test_completion_capture_stops_at_the_distinct_topic_cap(self) -> None:
        captured = [MqttMessage(topic=f"ASSET-{index}/state", payload=b"{}") for index in range(4)]
        expected = captured[:3]
        fake = _FakeCaptureClient(list(captured))

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: fake):
            messages = mqtt_transport.subscribe_and_capture(
                _settings(),
                topics=["+/state"],
                timeout_seconds=None,
                max_messages=3,
                stop_when=lambda _messages: False,
            )

        self.assertEqual(messages, expected)

    def test_raw_capture_still_stops_at_the_message_cap(self) -> None:
        first = MqttMessage(topic="ASSET-1/state", payload=b"{}")
        second = MqttMessage(topic="ASSET-2/state", payload=b"{}")
        fake = _FakeCaptureClient([first, first, first, second])

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: fake):
            messages = mqtt_transport.subscribe_and_capture(
                _settings(),
                topics=[first.topic, second.topic],
                timeout_seconds=None,
                max_messages=3,
            )

        self.assertEqual(messages, [first, first, first])


if __name__ == "__main__":
    unittest.main()
