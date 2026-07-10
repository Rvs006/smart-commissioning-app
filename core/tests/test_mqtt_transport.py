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
    """Partial-capture honesty: a broker drop mid-capture returns what was
    already captured (the honesty layer then names the still-missing topics),
    while connect/subscribe failures — before any message could exist — raise.
    """

    def test_mid_capture_transport_error_returns_partial_messages(self) -> None:
        captured = MqttMessage(topic="ASSET-1/events/pointset", payload=b"{}")
        for error in (ConnectionResetError("broker dropped"), MqttTransportError("broker closed the connection")):
            with self.subTest(error=type(error).__name__):
                fake = _FakeCaptureClient([captured, error])
                with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings, fake=fake: fake):
                    messages = mqtt_transport.subscribe_and_capture(
                        _settings(),
                        topics=["ASSET-1/#", "ASSET-2/#"],
                        timeout_seconds=None,  # indefinite capture, 4-of-5 style
                        max_messages=5,
                    )
                self.assertEqual(messages, [captured])

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
            def subscribe(self, topic: str, qos: int = 0) -> None:
                raise MqttTransportError("MQTT broker rejected the subscription.")

        with mock.patch.object(mqtt_transport, "MqttClient", lambda _settings: FailingSubscribe([])):
            with self.assertRaises(MqttTransportError):
                mqtt_transport.subscribe_and_capture(
                    _settings(), topics=["ASSET-1/#"], timeout_seconds=5, max_messages=5
                )


if __name__ == "__main__":
    unittest.main()
