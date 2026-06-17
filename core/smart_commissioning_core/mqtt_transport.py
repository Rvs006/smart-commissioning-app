import json
import os
import socket
import ssl
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class MqttTransportError(RuntimeError):
    pass


# -- secret:// cert resolver hook -------------------------------------------
# Cert fields (CA / client cert / private key) may be either a plain
# filesystem path OR a ``secret://`` reference into the encrypted secret store
# (Phase 2). This module has no access to the secret store itself, so the
# owning service (the API's ConfigurationService, or the worker when the
# secrets volume is shared) registers a resolver that maps a ``secret://`` ref
# to its DECRYPTED bytes. When unset, ``secret://`` refs resolve to nothing and
# the TLS context is built without that material (same as today).

SecretResolver = Callable[[str], bytes | None]

_secret_resolver: SecretResolver | None = None


def set_secret_resolver(resolver: SecretResolver | None) -> None:
    """Register the callable that resolves a ``secret://`` ref to decrypted bytes.

    The resolver receives the full reference (e.g. ``secret://client-cert-...``)
    and returns the decrypted material as ``bytes`` (PEM text encoded as UTF-8 is
    fine) or ``None`` if it cannot resolve it. It MUST NOT raise for a missing
    secret; raising is treated the same as ``None`` (the field is skipped) so a
    resolver error never leaks into the TLS error path. Plain filesystem paths
    never reach the resolver.
    """
    global _secret_resolver
    _secret_resolver = resolver


def _resolve_secret_material(ref: str) -> bytes | None:
    """Resolve a ``secret://`` ref to decrypted bytes via the registered resolver.

    Returns ``None`` when no resolver is registered, the resolver returns
    nothing, or the resolver raises (a resolver failure must not abort the
    handshake setup with a credential-bearing exception)."""
    if _secret_resolver is None:
        return None
    try:
        material = _secret_resolver(ref)
    except Exception:
        return None
    if material is None:
        return None
    return material if isinstance(material, bytes) else bytes(material)


def _is_secret_ref(value: str | None) -> bool:
    return bool(value) and value.startswith("secret://")


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
        # Temp files materialized from secret:// cert material; removed on exit.
        self._temp_cert_files: list[str] = []

    def __enter__(self) -> "MqttClient":
        raw_socket = self.socket_factory((self.settings.host, self.settings.port), self.settings.timeout_seconds)
        raw_socket.settimeout(self.settings.timeout_seconds)
        try:
            self._socket = self._wrap_tls(raw_socket) if self.settings.use_tls else raw_socket
        finally:
            # Cert material was loaded into the SSLContext; the temp files are no
            # longer needed once wrap_socket has consumed them.
            self._cleanup_temp_cert_files()
        self._connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self._send_packet(0xE0, b"")
        except MqttTransportError:
            pass
        if self._socket:
            self._socket.close()
        self._cleanup_temp_cert_files()

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
        cancel_check: Callable[[], bool] | None = None,
    ) -> MqttMessage | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Observe cancellation promptly even mid-window (used by long /
            # indefinite captures so the Cancel control stops them quickly).
            if cancel_check is not None and cancel_check():
                return None
            remaining = max(0.1, min(self.settings.timeout_seconds, deadline - time.monotonic()))
            try:
                self._require_socket().settimeout(remaining)
                packet_type, payload = self._read_packet()
            except TimeoutError:
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
        """Build the SSLContext, resolving secret:// cert refs to decrypted material.

        CA / client-cert / private-key fields are each handled independently:

        * a plain filesystem path keeps today's behavior (loaded by path);
        * a ``secret://`` reference is resolved to decrypted bytes via the
          registered secret resolver and loaded IN MEMORY where the ssl API
          allows it (the CA via ``load_verify_locations(cadata=...)``), or via a
          transient 0600 temp file where the API requires a path
          (``load_cert_chain`` has no in-memory form). Temp files are removed by
          the caller (``__enter__``/``__exit__``) once the context has consumed
          them.

        HONESTY: this materializes + loads the cert material; the real TLS
        handshake against a live broker is on-site-untested.
        """
        context = self._build_ssl_context()
        client_cert_path = self._resolve_cert_field_to_path(self.settings.client_certificate)
        private_key_path = self._resolve_cert_field_to_path(self.settings.private_key)
        if client_cert_path:
            context.load_cert_chain(certfile=client_cert_path, keyfile=private_key_path)
        return context.wrap_socket(raw_socket, server_hostname=self.settings.host)

    def _build_ssl_context(self) -> ssl.SSLContext:
        """Create the verifying SSLContext, loading the CA from a path or secret."""
        ca_field = self.settings.ca_certificate
        if _is_secret_ref(ca_field):
            material = _resolve_secret_material(ca_field)
            context = ssl.create_default_context()
            if material is not None:
                # Prefer in-memory loading for the CA: no temp file needed.
                context.load_verify_locations(cadata=material.decode("utf-8"))
            return context
        return ssl.create_default_context(cafile=_path_if_file(ca_field))

    def _resolve_cert_field_to_path(self, value: str | None) -> str | None:
        """Return a filesystem path for a cert field, materializing secrets.

        Plain paths are returned as-is (when they exist). A ``secret://`` ref is
        resolved to decrypted bytes and written to a transient 0600 temp file
        whose path is returned; the file is tracked for cleanup. Returns ``None``
        when there is nothing loadable (no resolver / missing secret / missing
        file), preserving today's behavior for plain paths.
        """
        if _is_secret_ref(value):
            material = _resolve_secret_material(value)
            if material is None:
                return None
            return self._materialize_temp_cert(material)
        return _path_if_file(value)

    def _materialize_temp_cert(self, material: bytes) -> str:
        """Write decrypted cert material to a transient owner-only (0600) file.

        On POSIX the 0600 mode is enforced via ``os.open``; on Windows the mode
        bits only map onto the read-only attribute, so isolation there relies on
        the host ACL. The file is tracked and removed on context exit.
        """
        fd, path = tempfile.mkstemp(prefix="mqtt-tls-", suffix=".pem")
        try:
            os.write(fd, material)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._temp_cert_files.append(path)
        return path

    def _cleanup_temp_cert_files(self) -> None:
        for path in self._temp_cert_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._temp_cert_files.clear()

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
    timeout_seconds: float | None,
    max_messages: int,
    cancel_check: Callable[[], bool] | None = None,
) -> list[MqttMessage]:
    """Subscribe to ``topics`` and collect messages up to ``max_messages``.

    ``timeout_seconds`` bounds the capture window; pass ``None`` for an
    indefinite capture that runs until ``cancel_check`` returns True or the
    message cap is reached (mq9nhbzu "run until stopped"). The loop polls in
    short slices so cancellation is observed promptly in both modes.
    """
    messages: list[MqttMessage] = []
    expected_topics = set(topics)
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    with MqttClient(settings) as client:
        for topic in topics:
            client.subscribe(topic)
        while len(messages) < max_messages:
            if cancel_check is not None and cancel_check():
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                poll = max(0.1, min(1.0, remaining))
            else:
                # Indefinite: poll in 1s slices, re-checking cancel each time.
                poll = 1.0
            message = client.read_publish_any(
                expected_topics=expected_topics,
                timeout_seconds=poll,
                cancel_check=cancel_check,
            )
            if message is not None:
                messages.append(message)
            # No message this slice: keep looping (re-check cancel/deadline)
            # rather than breaking, so a quiet broker does not end the window
            # early and an indefinite capture keeps waiting.
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
