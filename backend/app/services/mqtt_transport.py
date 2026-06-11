import json
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class MqttTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class MqttConnectionSettings:
    host: str
    port: int
    client_id: str
    keep_alive: int = 60
    username: str | None = None
    password: str | None = None
    use_tls: bool = False
    ca_certificate: str | None = None
    client_certificate: str | None = None
    private_key: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes

    def json_payload(self) -> object | None:
        try:
            return json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


class MqttClient:
    def __init__(
        self,
        settings: MqttConnectionSettings,
        *,
        socket_factory: Callable[[tuple[str, int], float], socket.socket] | None = None,
    ) -> None:
        self.settings = settings
        self.socket_factory = socket_factory or socket.create_connection
        self._socket: socket.socket | None = None
        self._packet_id = 1

    def __enter__(self) -> "MqttClient":
        raw_socket = self.socket_factory((self.settings.host, self.settings.port), self.settings.timeout_seconds)
        raw_socket.settimeout(self.settings.timeout_seconds)
        self._socket = self._wrap_tls(raw_socket) if self.settings.use_tls else raw_socket
        self._connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self._send_packet(0xE0, b"")
        except MqttTransportError:
            pass
        if self._socket:
            self._socket.close()

    def publish(self, topic: str, payload: str | bytes) -> None:
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        packet = _encode_utf8(topic) + payload_bytes
        self._send_packet(0x30, packet)

    def subscribe(self, topic: str) -> None:
        packet_id = self._next_packet_id()
        packet = packet_id.to_bytes(2, "big") + _encode_utf8(topic) + b"\x00"
        self._send_packet(0x82, packet)
        packet_type, payload = self._read_packet()
        if packet_type != 0x90:
            raise MqttTransportError("MQTT broker did not acknowledge the subscription.")
        if len(payload) < 3 or payload[2] == 0x80:
            raise MqttTransportError("MQTT broker rejected the subscription.")

    def read_publish(self, *, expected_topic: str, timeout_seconds: float) -> MqttMessage | None:
        return self.read_publish_any(expected_topics={expected_topic}, timeout_seconds=timeout_seconds)

    def read_publish_any(
        self,
        *,
        expected_topics: set[str] | None = None,
        timeout_seconds: float,
    ) -> MqttMessage | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.1, min(self.settings.timeout_seconds, deadline - time.monotonic()))
            try:
                self._require_socket().settimeout(remaining)
                packet_type, payload = self._read_packet()
            except TimeoutError:
                return None
            except socket.timeout:
                return None
            if packet_type & 0xF0 != 0x30 or len(payload) < 2:
                continue
            topic_length = int.from_bytes(payload[:2], "big")
            topic = payload[2 : 2 + topic_length].decode("utf-8", errors="replace")
            message_payload = payload[2 + topic_length :]
            if expected_topics is None or topic in expected_topics:
                return MqttMessage(topic=topic, payload=message_payload)
        return None

    def _connect(self) -> None:
        flags = 0x02
        payload = _encode_utf8(self.settings.client_id)
        if self.settings.username is not None:
            flags |= 0x80
            if self.settings.password is not None:
                flags |= 0x40
        variable_header = _encode_utf8("MQTT") + b"\x04" + bytes([flags]) + self.settings.keep_alive.to_bytes(2, "big")
        if self.settings.username is not None:
            payload += _encode_utf8(self.settings.username)
            if self.settings.password is not None:
                payload += _encode_utf8(self.settings.password)
        self._send_packet(0x10, variable_header + payload)
        packet_type, response = self._read_packet()
        if packet_type != 0x20 or len(response) != 2:
            raise MqttTransportError("MQTT broker returned an invalid CONNACK packet.")
        if response[1] != 0:
            errors = {
                1: "unsupported protocol level",
                2: "client identifier rejected",
                3: "broker unavailable",
                4: "bad username or password",
                5: "not authorised",
            }
            raise MqttTransportError(f"MQTT broker rejected the connection: {errors.get(response[1], response[1])}.")

    def _wrap_tls(self, raw_socket: socket.socket) -> socket.socket:
        context = ssl.create_default_context(cafile=_path_if_file(self.settings.ca_certificate))
        client_cert = _path_if_file(self.settings.client_certificate)
        private_key = _path_if_file(self.settings.private_key)
        if client_cert:
            context.load_cert_chain(certfile=client_cert, keyfile=private_key)
        return context.wrap_socket(raw_socket, server_hostname=self.settings.host)

    def _next_packet_id(self) -> int:
        packet_id = self._packet_id
        self._packet_id = 1 if packet_id >= 65535 else packet_id + 1
        return packet_id

    def _send_packet(self, packet_type: int, payload: bytes) -> None:
        self._require_socket().sendall(bytes([packet_type]) + _encode_remaining_length(len(payload)) + payload)

    def _read_packet(self) -> tuple[int, bytes]:
        sock = self._require_socket()
        packet_type = sock.recv(1)
        if not packet_type:
            raise MqttTransportError("MQTT broker closed the connection.")
        remaining_length = _read_remaining_length(sock)
        payload = _recv_exact(sock, remaining_length)
        return packet_type[0], payload

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise MqttTransportError("MQTT socket is not connected.")
        return self._socket


def publish_config_and_wait_for_pointset(
    settings: MqttConnectionSettings,
    *,
    config_topic: str,
    config_payload: str,
    pointset_topic: str,
    timeout_seconds: float,
) -> MqttMessage | None:
    with MqttClient(settings) as client:
        client.subscribe(pointset_topic)
        client.publish(config_topic, config_payload)
        return client.read_publish(expected_topic=pointset_topic, timeout_seconds=timeout_seconds)


def subscribe_and_capture(
    settings: MqttConnectionSettings,
    *,
    topics: list[str],
    timeout_seconds: float,
    max_messages: int,
) -> list[MqttMessage]:
    messages: list[MqttMessage] = []
    expected_topics = set(topics)
    with MqttClient(settings) as client:
        for topic in topics:
            client.subscribe(topic)
        deadline = time.monotonic() + timeout_seconds
        while len(messages) < max_messages and time.monotonic() < deadline:
            message = client.read_publish_any(
                expected_topics=expected_topics,
                timeout_seconds=max(0.1, deadline - time.monotonic()),
            )
            if message is None:
                break
            messages.append(message)
    return messages


def _encode_utf8(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 65535:
        raise MqttTransportError("MQTT string field is too long.")
    return len(data).to_bytes(2, "big") + data


def _encode_remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        encoded.append(digit)
        if length == 0:
            return bytes(encoded)


def _read_remaining_length(sock: socket.socket) -> int:
    multiplier = 1
    value = 0
    while True:
        digit = sock.recv(1)
        if not digit:
            raise MqttTransportError("MQTT broker closed the connection while reading packet length.")
        value += (digit[0] & 127) * multiplier
        if (digit[0] & 128) == 0:
            return value
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise MqttTransportError("MQTT packet length is malformed.")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise MqttTransportError("MQTT broker closed the connection while reading packet payload.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _path_if_file(value: str | None) -> str | None:
    if not value or value.startswith("secret://"):
        return None
    path = Path(value).expanduser()
    return str(path) if path.exists() else None
