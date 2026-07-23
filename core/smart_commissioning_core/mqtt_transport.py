import json
import os
import socket
import ssl
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    # Local (source) address to bind the outbound socket to, forcing egress out a
    # chosen NIC. ``None`` (default) = OS default route, backward compatible.
    source_address: tuple[str, int] | None = None


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes
    retained: bool = False
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC), compare=False)
    # DELIVERY QoS of the inbound PUBLISH = min(publisher's QoS, our subscription
    # QoS). NOT the publisher's QoS: with a QoS-0 subscription every delivery
    # reads 0 regardless of what the publisher used. Appended LAST so every
    # existing (topic, payload[, retained, received_at]) construction is
    # unaffected. compare left default (participates in equality) — a plain
    # (topic, payload) construction defaults qos=0, matching prior behavior.
    qos: int = 0

    def json_payload(self) -> object | None:
        def reject_non_finite(value: str) -> object:
            raise ValueError(f"Non-standard JSON constant: {value}")

        try:
            return json.loads(
                self.payload.decode("utf-8"),
                parse_constant=reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None


class MqttCaptureInterrupted(MqttTransportError):
    """Broker failure that preserves messages captured before the interruption."""

    def __init__(self, messages: list[MqttMessage], cause: Exception) -> None:
        super().__init__("MQTT capture was interrupted; partial messages are available.")
        self.messages = list(messages)
        self.cause = cause


def _resolve_socket_factory(
    socket_factory: Callable[[tuple[str, int], float], socket.socket] | None,
    source_address: tuple[str, int] | None,
) -> Callable[[tuple[str, int], float], socket.socket]:
    """Pick the socket factory, binding the default one to a source interface.

    An injected ``socket_factory`` is returned untouched (callers / tests that
    supply their own factory are never wrapped). Only when no factory is injected
    AND a ``source_address`` is configured do we wrap ``socket.create_connection``
    so the outbound socket binds to that local address. With neither, this is
    exactly today's ``socket.create_connection`` default (backward compatible).
    """
    if socket_factory is not None:
        return socket_factory
    if source_address is None:
        return socket.create_connection

    def _bound_create_connection(address: tuple[str, int], timeout: float) -> socket.socket:
        return socket.create_connection(address, timeout, source_address=source_address)

    return _bound_create_connection


class MqttClient:
    def __init__(
        self,
        settings: MqttConnectionSettings,
        *,
        socket_factory: Callable[[tuple[str, int], float], socket.socket] | None = None,
    ) -> None:
        self.settings = settings
        self.socket_factory = _resolve_socket_factory(socket_factory, settings.source_address)
        self._socket: socket.socket | None = None
        self._packet_id = 1
        self._pending_messages: list[MqttMessage] = []
        self._qos2_pending_messages: dict[int, MqttMessage] = {}
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
        except (OSError, MqttTransportError):
            # A dead connection (RST/timeout) must not raise out of teardown —
            # it would discard messages already captured in this session.
            pass
        if self._socket:
            self._socket.close()
        self._cleanup_temp_cert_files()

    def publish(self, topic: str, payload: str | bytes) -> None:
        # QoS0 + non-retained BY DESIGN. The fixed 0x30 packet type carries no
        # QoS bits (QoS0) and no RETAIN flag. This tool publishes to an online,
        # already-subscribed device and immediately waits for the echoed
        # pointset, so retain (which only benefits future/reconnecting
        # subscribers) is intentionally not set — and a lingering retained
        # config could conflict with the site's real config authority. KNOWN
        # GAP: the Configuration "QoS" field drives only the subscribe/capture
        # path; this publish is hardcoded QoS0.
        payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
        packet = _encode_utf8(topic) + payload_bytes
        self._send_packet(0x30, packet)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribe_many([topic], qos)

    def subscribe_many(self, topics: list[str], qos: int = 0) -> None:
        """Subscribe all filters in one packet before retained publishes arrive.

        Brokers send each retained value immediately after acknowledging its
        subscription. Issuing one SUBSCRIBE per filter lets that PUBLISH become
        the next packet while the client is waiting for another SUBACK, causing
        an honest broker to look like a failed setup. MQTT supports multiple
        filters per SUBSCRIBE, so batch them and await one matching SUBACK.
        """
        if not topics:
            raise ValueError("At least one MQTT subscription topic is required.")
        packet_id = self._next_packet_id()
        packet = packet_id.to_bytes(2, "big") + b"".join(
            _encode_utf8(topic) + bytes([qos & 0x03]) for topic in topics
        )
        self._send_packet(0x82, packet)
        deadline = time.monotonic() + self.settings.timeout_seconds
        while time.monotonic() < deadline:
            self._require_socket().settimeout(max(0.1, deadline - time.monotonic()))
            packet_type, payload = self._read_packet()
            if packet_type & 0xF0 == 0x30:
                message = self._decode_publish(packet_type, payload)
                if message is not None:
                    self._pending_messages.append(message)
                continue
            if packet_type & 0xF0 == 0x60:
                message = self._complete_qos2(payload)
                if message is not None:
                    self._pending_messages.append(message)
                continue
            if packet_type == 0xD0:  # PINGRESP may race with a later subscribe.
                continue
            if packet_type != 0x90:
                raise MqttTransportError("MQTT broker did not acknowledge the subscription.")
            if (
                len(payload) != 2 + len(topics)
                or int.from_bytes(payload[:2], "big") != packet_id
                or any(code not in {0x00, 0x01, 0x02} for code in payload[2:])
            ):
                raise MqttTransportError("MQTT broker rejected the subscription.")
            return
        raise MqttTransportError("MQTT broker timed out acknowledging the subscription.")

    def ping(self) -> None:
        """Send an MQTT PINGREQ (0xC0) keepalive.

        The capture loop only recv()s, so on a quiet broker no bytes are sent
        after CONNECT/SUBSCRIBE; a spec-compliant broker drops a client that is
        silent for 1.5x keep_alive. Sending PINGREQ before keep_alive elapses
        keeps a long/indefinite capture connected. The PINGRESP (0xD0) reply is
        consumed and ignored by read_publish_any (it only returns PUBLISH 0x30).
        """
        self._send_packet(0xC0, b"")

    def read_publish(self, *, expected_topic: str, timeout_seconds: float) -> MqttMessage | None:
        return self.read_publish_any(expected_topics={expected_topic}, timeout_seconds=timeout_seconds)

    def read_publish_any(
        self,
        *,
        expected_topics: set[str] | None = None,
        timeout_seconds: float,
        cancel_check: Callable[[], bool] | None = None,
        capture_deadline: float | None = None,
        use_timeout_as_packet_deadline: bool = True,
    ) -> MqttMessage | None:
        pending = self._take_pending_message(expected_topics)
        if pending is not None:
            return pending
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Observe cancellation promptly even mid-window (used by long /
            # indefinite captures so the Cancel control stops them quickly).
            if cancel_check is not None and cancel_check():
                return None
            remaining = max(0.001, min(self.settings.timeout_seconds, deadline - time.monotonic()))
            try:
                self._require_socket().settimeout(remaining)
                packet_deadline = deadline if use_timeout_as_packet_deadline else capture_deadline
                packet_type, payload = self._read_packet(deadline=packet_deadline)
            except TimeoutError:
                # A quiet slice at a packet BOUNDARY (no byte of a packet consumed).
                # Keep waiting until the caller's deadline instead of abandoning the
                # whole window on the first silence — a caller asking for a 30s
                # pointset wait must not be cut to the ~5s connect-timeout slice.
                # (A timeout MID-packet is a different, real fault: _read_packet
                # raises MqttTransportError for that, which is NOT caught here.)
                continue
            if packet_type & 0xF0 == 0x60:
                message = self._complete_qos2(payload)
                if message is not None:
                    if expected_topics is None or any(
                        _topic_matches_filter(message.topic, expected)
                        for expected in expected_topics
                    ):
                        return message
                    self._pending_messages.append(message)
                continue
            if packet_type & 0xF0 != 0x30:
                continue
            message = self._decode_publish(packet_type, payload)
            if message is not None and (
                expected_topics is None
                or any(_topic_matches_filter(message.topic, expected) for expected in expected_topics)
            ):
                return message
        return None

    def _decode_publish(self, packet_type: int, payload: bytes) -> MqttMessage | None:
        if len(payload) < 2:
            raise MqttTransportError("MQTT broker returned a malformed PUBLISH packet.")
        qos = (packet_type >> 1) & 0x03
        if qos == 0x03:
            raise MqttTransportError("MQTT broker returned a PUBLISH packet with reserved QoS bits.")
        topic_length = int.from_bytes(payload[:2], "big")
        payload_offset = 2 + topic_length
        if topic_length == 0 or payload_offset > len(payload):
            raise MqttTransportError("MQTT broker returned a malformed PUBLISH topic.")
        topic = payload[2:payload_offset].decode("utf-8", errors="replace")

        packet_id: int | None = None
        if qos > 0:
            if payload_offset + 2 > len(payload):
                raise MqttTransportError("MQTT QoS PUBLISH omitted its packet identifier.")
            packet_id = int.from_bytes(payload[payload_offset : payload_offset + 2], "big")
            if packet_id == 0:
                raise MqttTransportError("MQTT QoS PUBLISH used packet identifier zero.")
            payload_offset += 2

        if qos == 1 and packet_id is not None:
            self._send_packet(0x40, packet_id.to_bytes(2, "big"))
        elif qos == 2 and packet_id is not None:
            self._send_packet(0x50, packet_id.to_bytes(2, "big"))
            if packet_id in self._qos2_pending_messages:
                return None
        message = MqttMessage(
            topic=topic,
            payload=payload[payload_offset:],
            retained=bool(packet_type & 0x01),
            qos=qos,
        )
        if qos == 2 and packet_id is not None:
            self._qos2_pending_messages[packet_id] = message
            return None
        return message

    def _complete_qos2(self, payload: bytes) -> MqttMessage | None:
        if len(payload) != 2:
            raise MqttTransportError("MQTT broker returned a malformed PUBREL packet.")
        packet_id = int.from_bytes(payload, "big")
        if packet_id == 0:
            raise MqttTransportError("MQTT PUBREL used packet identifier zero.")
        self._send_packet(0x70, payload)
        return self._qos2_pending_messages.pop(packet_id, None)

    def discard_pending_messages(self) -> None:
        self._pending_messages.clear()

    def _take_pending_message(self, expected_topics: set[str] | None) -> MqttMessage | None:
        for index, message in enumerate(self._pending_messages):
            if expected_topics is None or any(
                _topic_matches_filter(message.topic, expected) for expected in expected_topics
            ):
                return self._pending_messages.pop(index)
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
        # Plain filesystem CA path. If one is CONFIGURED but the file is
        # missing/typo'd, fail CLOSED with a clear (tls_error-classified) message
        # rather than silently falling back to the system trust store — a silent
        # fallback would quietly drop private-CA pinning with no signal. An
        # empty/absent ca_field means "no CA pinned" (the intended default) and
        # keeps the system trust store.
        if ca_field and ca_field.strip():
            resolved = _path_if_file(ca_field)
            if resolved is None:
                raise MqttTransportError(
                    "Configured CA certificate file was not found; refusing to "
                    "fall back to the system trust store."
                )
            return ssl.create_default_context(cafile=resolved)
        return ssl.create_default_context()

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

    def _read_packet(self, *, deadline: float | None = None) -> tuple[int, bytes]:
        sock = self._require_socket()
        packet_type = sock.recv(1)  # times out at a BOUNDARY -> caller may keep waiting
        if not packet_type:
            raise MqttTransportError("MQTT broker closed the connection.")
        # A packet has started. Its remaining bytes MUST be read to completion: if a
        # short poll-slice timeout fired part-way through (a multi-KB retained UDMI
        # payload split across TCP segments is the field case), returning to the poll
        # loop would leave the stream mid-packet and the next read would parse
        # payload bytes as a new fixed header — fabricating packets or aborting a
        # healthy capture. Give the remainder a full read timeout; a timeout even
        # then is a real stall, surfaced as a broker error (never silent desync).
        previous_timeout = sock.gettimeout()
        packet_timeout = max(self.settings.timeout_seconds, previous_timeout or 0.0)
        if deadline is not None:
            # A packet that begins near the capture boundary may finish, but it
            # cannot extend the operator's requested window by another full
            # connection timeout. The stream is already mid-packet, so expiry is
            # still surfaced as an honest partial-packet transport error.
            packet_timeout = min(packet_timeout, max(0.001, deadline - time.monotonic()))
        sock.settimeout(packet_timeout)
        try:
            remaining_length = _read_remaining_length(sock)
            payload = _recv_exact(sock, remaining_length)
        except TimeoutError as error:
            raise MqttTransportError(
                "MQTT broker sent a partial packet and then stopped responding."
            ) from error
        finally:
            sock.settimeout(previous_timeout)
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
    # The config publish is QoS0 + non-retained BY DESIGN: we subscribe first,
    # publish to an online, already-subscribed device, then immediately wait for
    # the echoed pointset. Retain only helps future/reconnecting subscribers and
    # is intentionally omitted so a lingering retained config cannot conflict with
    # the site's real config authority. KNOWN GAP: the Configuration "QoS" field
    # drives only the subscribe/capture path — this publish is hardcoded QoS0.
    with MqttClient(settings) as client:
        client.subscribe(pointset_topic)
        client.discard_pending_messages()
        published_at = datetime.now(UTC)
        client.publish(config_topic, config_payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            message = client.read_publish(
                expected_topic=pointset_topic,
                timeout_seconds=max(0.1, deadline - time.monotonic()),
            )
            if message is None:
                return None
            # Retained or already-buffered pointsets describe state from before
            # this write and cannot confirm that the device applied it.
            if not message.retained and message.received_at >= published_at:
                return message
        return None


def read_retained_config(
    settings: MqttConnectionSettings,
    *,
    config_topic: str,
    timeout_seconds: float,
) -> str | None:
    """Read the broker's RETAINED message on ``config_topic`` for rollback.

    A retained config message is delivered by the broker immediately on
    subscribe, so a short subscribe-and-read snapshots the device's current
    config WITHOUT publishing anything (read-only: SUBSCRIBE only). Returns the
    payload text, or None if no retained message arrives within the window. Must
    be called BEFORE a forward publish so the captured value is the prior config.
    """
    with MqttClient(settings) as client:
        client.subscribe(config_topic)
        message = client.read_publish(expected_topic=config_topic, timeout_seconds=timeout_seconds)
    if message is None:
        return None
    return message.payload.decode("utf-8", errors="replace")


def subscribe_and_capture(
    settings: MqttConnectionSettings,
    *,
    topics: list[str],
    timeout_seconds: float | None,
    max_messages: int,
    cancel_check: Callable[[], bool] | None = None,
    stop_when: Callable[[list[MqttMessage]], bool] | None = None,
    qos: int = 0,
    retain_latest: bool = False,
    on_message: Callable[[MqttMessage], None] | None = None,
) -> list[MqttMessage]:
    """Subscribe to ``topics`` and collect messages up to ``max_messages``.

    ``timeout_seconds`` bounds the capture window; pass ``None`` for an
    indefinite capture that runs until ``cancel_check`` returns True or the
    message cap is reached (mq9nhbzu "run until stopped"). The loop polls in
    short slices so cancellation is observed promptly in both modes. ``qos`` is
    the requested max subscribe QoS (0-2; broker grants min of this and publish).
    ``stop_when`` (optional) is a completion predicate called with the messages
    captured so far after each new one; returning True ends the capture.

    ``retain_latest`` (independently of ``stop_when``) bounds memory on a long /
    indefinite capture: one latest message is kept per concrete topic, so
    ``max_messages`` becomes a DISTINCT-TOPIC cap (hitting it ends the capture,
    identical to completion-mode semantics) and duplicates cannot starve quiet
    topics. The same retention is implied whenever ``stop_when`` is set. Raw
    captures (no predicate, ``retain_latest=False``) retain every message up to
    the ordinary cap.

    ``on_message`` (optional) is called with EVERY accepted message BEFORE
    dedup, including a message that merely replaces an earlier one on the same
    topic. It lets a caller keep an honest per-topic total that the deduped
    return list can no longer show under retention.
    """
    messages: list[MqttMessage] = []
    topic_positions: dict[str, int] = {}
    expected_topics = set(topics)
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    # Keepalive: ping at keep_alive/2 so a quiet broker does not drop a long /
    # indefinite capture (the loop is otherwise recv-only after SUBSCRIBE).
    ping_interval = max(1.0, settings.keep_alive / 2.0) if settings.keep_alive and settings.keep_alive > 0 else None
    last_ping = time.monotonic()
    with MqttClient(settings) as client:
        client.subscribe_many(topics, qos)
        while len(messages) < max_messages:
            if cancel_check is not None and cancel_check():
                break
            if ping_interval is not None and time.monotonic() - last_ping >= ping_interval:
                try:
                    client.ping()
                except (OSError, MqttTransportError) as error:
                    raise MqttCaptureInterrupted(messages, error) from error
                last_ping = time.monotonic()
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                poll = max(0.1, min(1.0, remaining))
            else:
                # Indefinite: poll in 1s slices, re-checking cancel each time.
                poll = 1.0
            try:
                message = client.read_publish_any(
                    expected_topics=expected_topics,
                    timeout_seconds=poll,
                    cancel_check=cancel_check,
                    capture_deadline=deadline,
                    # ``poll`` is only the quiet-boundary/cancel slice. Finite
                    # captures use their real outer deadline for a packet already
                    # in flight; indefinite captures retain the normal per-packet
                    # connection timeout.
                    use_timeout_as_packet_deadline=False,
                )
            except (OSError, MqttTransportError) as error:
                raise MqttCaptureInterrupted(messages, error) from error
            if message is not None:
                if on_message is not None:
                    # Fires for every accepted message, including one that
                    # replaces a duplicate below — keeps per-topic counts honest.
                    on_message(message)
                if stop_when is None and not retain_latest:
                    messages.append(message)
                elif message.topic in topic_positions:
                    index = topic_positions[message.topic]
                    messages[index] = message
                else:
                    topic_positions[message.topic] = len(messages)
                    messages.append(message)
                if stop_when is not None and stop_when(messages):
                    break
            # No message this slice: keep looping (re-check cancel/deadline)
            # rather than breaking, so a quiet broker does not end the window
            # early and an indefinite capture keeps waiting.
    return messages


def _encode_utf8(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 65535:
        raise MqttTransportError("MQTT string field is too long.")
    return len(data).to_bytes(2, "big") + data


def _topic_matches_filter(topic: str, topic_filter: str) -> bool:
    if topic_filter == "#":
        return True
    topic_parts = topic.split("/")
    filter_parts = topic_filter.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[index]:
            return False
    return len(topic_parts) == len(filter_parts)


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
