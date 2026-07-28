#!/usr/bin/env python3
"""Capture an append-only MQTT evidence timeline without embedding credentials.

The password is accepted only from ``SC_CAPTURE_MQTT_PASSWORD`` or an
interactive hidden prompt. Every received message becomes one JSONL record
containing the exact MQTT topic and receive timestamp. Existing output files
are never overwritten.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import queue
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from smart_commissioning_core.mqtt_transport import (
    MqttClient,
    MqttConnectionSettings,
    MqttMessage,
    MqttTransportError,
)


class _CaptureClient(Protocol):
    def __enter__(self) -> _CaptureClient: ...

    def __exit__(self, *_exc: object) -> None: ...

    def subscribe_many(self, topics: list[str], qos: int = 0) -> None: ...

    def read_publish_any(self, **kwargs: object) -> MqttMessage | None: ...


ClientFactory = Callable[[MqttConnectionSettings], _CaptureClient]
PasswordPrompt = Callable[[str], str]


class CaptureConfigurationError(ValueError):
    """Safe, credential-free configuration error."""


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _bool_from_env(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CaptureConfigurationError("SC_CAPTURE_MQTT_TLS must be true or false.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture every MQTT message as append-only JSONL evidence. "
            "Connection values may be supplied by SC_CAPTURE_MQTT_* environment variables."
        )
    )
    parser.add_argument("--host", help="MQTT broker host (or SC_CAPTURE_MQTT_HOST).")
    parser.add_argument("--port", type=_positive_int, help="MQTT port (or SC_CAPTURE_MQTT_PORT).")
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="MQTT filter to capture. Repeat for multiple filters.",
    )
    parser.add_argument("--output", required=True, type=Path, help="New JSONL evidence file.")
    parser.add_argument(
        "--duration-seconds",
        default=7200.0,
        type=_non_negative_float,
        help="Capture duration. Zero runs until the message limit or interruption.",
    )
    parser.add_argument(
        "--max-messages",
        default=100_000,
        type=_positive_int,
        help="Hard safety limit for retained timeline records.",
    )
    parser.add_argument("--qos", choices=(0, 1, 2), default=0, type=int)
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--tls", action="store_true", dest="use_tls", default=None)
    tls.add_argument("--no-tls", action="store_false", dest="use_tls")
    return parser


def _settings_from_args(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    password_prompt: PasswordPrompt,
) -> tuple[MqttConnectionSettings, list[str]]:
    host = str(args.host or environ.get("SC_CAPTURE_MQTT_HOST") or "").strip()
    if not host:
        raise CaptureConfigurationError(
            "Set --host or SC_CAPTURE_MQTT_HOST before starting capture."
        )
    port_value = args.port or environ.get("SC_CAPTURE_MQTT_PORT") or "8883"
    try:
        port = _positive_int(str(port_value))
    except (argparse.ArgumentTypeError, ValueError) as error:
        raise CaptureConfigurationError("The MQTT port must be a positive integer.") from error

    topics = [str(topic).strip() for topic in (args.topics or []) if str(topic).strip()]
    if not topics:
        topics = [
            topic.strip()
            for topic in environ.get("SC_CAPTURE_MQTT_TOPICS", "").split(",")
            if topic.strip()
        ]
    if not topics:
        raise CaptureConfigurationError(
            "Supply at least one --topic or set SC_CAPTURE_MQTT_TOPICS."
        )

    username = environ.get("SC_CAPTURE_MQTT_USERNAME") or None
    password = environ.get("SC_CAPTURE_MQTT_PASSWORD")
    if username and password is None:
        if not sys.stdin.isatty():
            raise CaptureConfigurationError(
                "Set SC_CAPTURE_MQTT_PASSWORD for a non-interactive authenticated capture."
            )
        password = password_prompt("MQTT password: ")
    if password and not username:
        raise CaptureConfigurationError(
            "SC_CAPTURE_MQTT_USERNAME is required when a password is supplied."
        )

    use_tls = (
        args.use_tls
        if args.use_tls is not None
        else _bool_from_env(environ.get("SC_CAPTURE_MQTT_TLS"), default=port == 8883)
    )
    settings = MqttConnectionSettings(
        host=host,
        port=port,
        client_id=f"smart-commissioning-capture-{uuid.uuid4().hex[:12]}",
        username=username,
        password=password,
        use_tls=use_tls,
        ca_certificate=environ.get("SC_CAPTURE_MQTT_CA_CERTIFICATE") or None,
        client_certificate=environ.get("SC_CAPTURE_MQTT_CLIENT_CERTIFICATE") or None,
        private_key=environ.get("SC_CAPTURE_MQTT_PRIVATE_KEY") or None,
    )
    return settings, list(dict.fromkeys(topics))


def message_record(message: MqttMessage) -> dict[str, Any]:
    try:
        payload = message.payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        payload = base64.b64encode(message.payload).decode("ascii")
        encoding = "base64"
    return {
        "received_at": message.received_at.isoformat(),
        "topic": message.topic,
        "qos": message.qos,
        "retained": message.retained,
        "payload_encoding": encoding,
        "payload": payload,
        "payload_size_bytes": len(message.payload),
        "payload_sha256": hashlib.sha256(message.payload).hexdigest(),
    }


def capture_to_jsonl(
    settings: MqttConnectionSettings,
    topics: Sequence[str],
    output_path: Path,
    *,
    duration_seconds: float,
    max_messages: int,
    qos: int,
    client_factory: ClientFactory = MqttClient,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Capture messages and return the number of append-only records written."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Claim the evidence path before connecting. Opening inside the writer
    # thread leaves a race where the network loop can start before an existing
    # output file is rejected.
    output_handle = output_path.open("x", encoding="utf-8", newline="\n")
    records: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
    writer_errors: list[BaseException] = []
    written = 0

    def write_records() -> None:
        nonlocal written
        try:
            with output_handle as handle:
                while True:
                    record = records.get()
                    if record is None:
                        return
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    handle.write("\n")
                    handle.flush()
                    written += 1
        except BaseException as error:
            writer_errors.append(error)

    writer = threading.Thread(target=write_records, name="mqtt-evidence-writer", daemon=True)
    writer.start()
    deadline = None if duration_seconds == 0 else monotonic() + duration_seconds
    accepted = 0
    try:
        with client_factory(settings) as client:
            client.subscribe_many(list(topics), qos)
            while accepted < max_messages:
                if writer_errors:
                    raise writer_errors[0]
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    break
                message = client.read_publish_any(
                    expected_topics=set(topics),
                    timeout_seconds=1.0 if remaining is None else max(0.1, min(1.0, remaining)),
                    capture_deadline=deadline,
                    use_timeout_as_packet_deadline=False,
                )
                if message is None:
                    continue
                records.put(message_record(message))
                accepted += 1
    finally:
        records.put(None)
        writer.join()
    if writer_errors:
        raise writer_errors[0]
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings, topics = _settings_from_args(
            args,
            environ=os.environ,
            password_prompt=getpass.getpass,
        )
        count = capture_to_jsonl(
            settings,
            topics,
            args.output,
            duration_seconds=args.duration_seconds,
            max_messages=args.max_messages,
            qos=args.qos,
        )
    except CaptureConfigurationError as error:
        parser.error(str(error))
    except FileExistsError:
        parser.error("The output file already exists; choose a new evidence path.")
    except (MqttTransportError, OSError):
        print(
            "MQTT capture failed. Check broker reachability, authentication, TLS, and topic ACLs.",
            file=sys.stderr,
        )
        return 1
    print(f"Capture complete: {count} message records written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
