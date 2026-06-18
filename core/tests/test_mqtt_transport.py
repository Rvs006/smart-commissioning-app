"""Unit tests for the hand-rolled MQTT transport keepalive (PINGREQ).

No real broker/socket: a recording fake socket captures sent bytes and a patched
_connect skips the CONNECT/CONNACK handshake. Proves the PINGREQ packet the
capture loop now sends on a quiet broker is correctly encoded (0xC0 + zero
remaining-length), so a long/indefinite capture is no longer dropped after
~keep_alive of silence. The time-based interval wiring in subscribe_and_capture
is exercised on-site against a real broker (see docs/phase5-onsite-validation.md).
"""

import unittest
from typing import Any
from unittest import mock

from smart_commissioning_core.mqtt_transport import MqttClient, MqttConnectionSettings


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


if __name__ == "__main__":
    unittest.main()
