#!/usr/bin/env python3
"""Capture an append-only MQTT evidence timeline without exposing credentials.

The default mode reads connection values from ``SC_CAPTURE_MQTT_*`` environment
variables and accepts a password only from the environment or a hidden prompt.
``--from-saved-app`` instead reads the same user's saved portable-app
configuration and resolves its encrypted TLS material in memory. Every received
message becomes one JSONL record containing the exact MQTT topic and receive
timestamp. Existing output files are never overwritten.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, MutableMapping, Sequence
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

    def ping(self) -> None: ...

    def read_publish_any(self, **kwargs: object) -> MqttMessage | None: ...


ClientFactory = Callable[[MqttConnectionSettings], _CaptureClient]
PasswordPrompt = Callable[[str], str]
CaptureStartedCallback = Callable[[], None]

DEFAULT_PROJECT_ID = "demo-project"
DEFAULT_SITE_ID = "demo-site"


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


def _topics_from_args(args: argparse.Namespace, environ: Mapping[str, str]) -> list[str]:
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
    return list(dict.fromkeys(topics))


def _qos_from_value(value: object, *, source: str) -> int:
    text = str(value or "").strip()
    try:
        qos = int(text.split(maxsplit=1)[0])
    except (IndexError, ValueError) as error:
        raise CaptureConfigurationError(f"The {source} QoS value must be 0, 1, or 2.") from error
    if qos not in {0, 1, 2}:
        raise CaptureConfigurationError(f"The {source} QoS value must be 0, 1, or 2.")
    return qos


def _tls_from_saved_value(value: object) -> bool:
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    raise CaptureConfigurationError(
        "The saved app TLS setting must be Enabled or Disabled."
    )


def _saved_app_data_root(
    explicit_data_root: Path | None,
    environ: Mapping[str, str],
) -> Path:
    if explicit_data_root is not None:
        return explicit_data_root.expanduser()
    configured_root = str(environ.get("SMART_COMMISSIONING_DATA_DIR") or "").strip()
    if configured_root:
        return Path(configured_root).expanduser()
    local_app_data = str(environ.get("LOCALAPPDATA") or "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "SmartCommissioning"


def _configure_saved_app_runtime(
    data_root: Path,
    environ: MutableMapping[str, str],
) -> None:
    """Point imported app services at one existing portable-app data directory.

    This only prepares process-local environment variables. The caller checks
    for the existing database first so the diagnostic cannot seed a new app
    configuration by accident.
    """
    root = data_root.expanduser()
    environ["SMART_COMMISSIONING_RUNTIME_ROOT"] = str(root)
    environ["SMART_COMMISSIONING_SECRETS_ROOT"] = str(root / "secrets")
    environ["SMART_COMMISSIONING_ARTIFACTS_ROOT"] = str(root / "artifacts")
    environ["SMART_COMMISSIONING_REPORT_SIGNING_ROOT"] = str(root / "report-signing")
    environ["DATABASE_URL"] = f"sqlite:///{(root / 'smart_commissioning.db').as_posix()}"


def _saved_app_values(
    data_root: Path,
    *,
    project_id: str,
    site_id: str,
    environ: MutableMapping[str, str] = os.environ,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Load saved MQTT values and register the app's in-memory secret resolver.

    No secret material is returned to the command line, written to JSONL, or
    printed. Importing ``app.services`` registers its resolver so the transport
    can load the app's encrypted ``secret://`` certificate references only while
    building the TLS context.
    """
    root = data_root.expanduser()
    database_path = root / "smart_commissioning.db"
    if not database_path.is_file():
        raise CaptureConfigurationError(
            "The saved app data file was not found. Run this as the same Windows "
            "user as the app, or supply its data directory with --data-root."
        )
    _configure_saved_app_runtime(data_root, environ)
    try:
        # Import side effect: registers the encrypted certificate resolver with
        # the shared MQTT transport. Do not replace this with a direct secret
        # file read: the transport owns the temporary TLS-file lifecycle.
        import app.services  # noqa: F401
    except CaptureConfigurationError:
        raise
    except Exception as error:
        raise CaptureConfigurationError(
            "This capture executable could not load the saved-app TLS support."
        ) from error

    # A read-only SQLite connection is deliberate. ConfigurationService.load()
    # can migrate a legacy snapshot, which would be inappropriate for a
    # diagnostic companion. The app's data remains untouched here.
    connection: sqlite3.Connection | None = None
    try:
        read_only_uri = f"{database_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(read_only_uri, uri=True)
        try:
            row = connection.execute(
                """
                SELECT payload
                FROM configuration_snapshots
                WHERE project_id = ? AND site_id = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (project_id, site_id),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise CaptureConfigurationError(
            "Could not read the saved app configuration database. Keep the app "
            "open and try again after its current operation finishes."
        ) from error
    if row is None:
        raise CaptureConfigurationError(
            "No saved MQTT configuration was found for the selected project and site."
        )
    raw_payload = row[0]
    try:
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureConfigurationError(
            "The saved app configuration has an unreadable format."
        ) from error
    if not isinstance(payload, Mapping):
        raise CaptureConfigurationError("The saved app configuration has an unreadable format.")

    def section_values(section_name: str) -> dict[str, object]:
        section = payload.get(section_name)
        if not isinstance(section, Mapping):
            raise CaptureConfigurationError(
                f"The saved app configuration has no {section_name} section."
            )
        values = section.get("values")
        if not isinstance(values, Mapping):
            raise CaptureConfigurationError(
                f"The saved app configuration has no {section_name} values."
            )
        return dict(values)

    mqtt_values = section_values("mqtt")
    certificate_values = section_values("certificates")
    secrets_root = root / "secrets"

    def require_existing_secret(reference: str) -> None:
        name = reference.removeprefix("secret://").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise CaptureConfigurationError("The saved app secret reference is invalid.")
        if not (secrets_root / f"{name}.pem").is_file():
            raise CaptureConfigurationError(
                "The saved app has a required TLS credential reference whose local "
                "material is unavailable."
            )

    references = [
        str(certificate_values.get(field_name) or "")
        for field_name in ("CA Certificate", "Client Certificate", "Private Key")
    ]
    password_reference = str(mqtt_values.get("MQTT Password") or "")
    if password_reference.startswith("secret://"):
        references.append(password_reference)
    if any(reference.startswith("secret://") for reference in references):
        if not (secrets_root / ".secret_store_key").is_file():
            raise CaptureConfigurationError(
                "The saved app TLS credential store is incomplete."
            )
        for reference in references:
            if reference.startswith("secret://"):
                require_existing_secret(reference)

    # Password-kind values are stored encrypted. Resolve only a configured
    # password into this process's memory; it is never logged, serialized, or
    # passed through an environment variable. Certificate/key references stay
    # opaque and are resolved later by app.services' registered TLS resolver.
    if password_reference.startswith("secret://"):
        try:
            from app.services.configuration_service import read_secret_material

            mqtt_values["MQTT Password"] = read_secret_material(password_reference)
        except Exception as error:
            raise CaptureConfigurationError(
                "The saved app MQTT password could not be read from its local credential store."
            ) from error
    return mqtt_values, certificate_values


def _settings_from_saved_values(
    mqtt_values: Mapping[str, object],
    certificate_values: Mapping[str, object],
) -> tuple[MqttConnectionSettings, int]:
    host = str(mqtt_values.get("MQTT Broker FQDN or IP Address") or "").strip()
    if not host:
        raise CaptureConfigurationError("The saved app MQTT broker host is empty.")
    try:
        port = _positive_int(str(mqtt_values.get("Port") or ""))
    except (argparse.ArgumentTypeError, ValueError) as error:
        raise CaptureConfigurationError(
            "The saved app MQTT port must be a positive integer."
        ) from error
    try:
        keep_alive = _positive_int(str(mqtt_values.get("Keep Alive Interval") or "60"))
    except (argparse.ArgumentTypeError, ValueError) as error:
        raise CaptureConfigurationError(
            "The saved app MQTT keep-alive must be a positive integer."
        ) from error

    username = str(mqtt_values.get("MQTT Username") or "").strip() or None
    password = str(mqtt_values.get("MQTT Password") or "") or None
    if username and username.startswith("secret://"):
        raise CaptureConfigurationError("The saved app MQTT username could not be resolved.")
    if password and password.startswith("secret://"):
        raise CaptureConfigurationError("The saved app MQTT password could not be resolved.")

    settings = MqttConnectionSettings(
        host=host,
        port=port,
        # Deliberately never reuse the app's configured client ID. Reusing it
        # could disconnect the validation run that this companion is meant to
        # observe independently.
        client_id=f"smart-commissioning-evidence-{uuid.uuid4().hex[:12]}",
        keep_alive=keep_alive,
        username=username,
        password=password,
        use_tls=_tls_from_saved_value(mqtt_values.get("Use TLS")),
        ca_certificate=str(certificate_values.get("CA Certificate") or "") or None,
        client_certificate=str(certificate_values.get("Client Certificate") or "") or None,
        private_key=str(certificate_values.get("Private Key") or "") or None,
    )
    return settings, _qos_from_value(mqtt_values.get("QoS"), source="saved app")


def _settings_from_saved_app(
    args: argparse.Namespace,
    *,
    environ: MutableMapping[str, str] = os.environ,
) -> tuple[MqttConnectionSettings, int]:
    data_root = _saved_app_data_root(getattr(args, "data_root", None), environ)
    mqtt_values, certificate_values = _saved_app_values(
        data_root,
        project_id=str(getattr(args, "project_id", DEFAULT_PROJECT_ID) or DEFAULT_PROJECT_ID),
        site_id=str(getattr(args, "site_id", DEFAULT_SITE_ID) or DEFAULT_SITE_ID),
        environ=environ,
    )
    settings, qos = _settings_from_saved_values(mqtt_values, certificate_values)
    if getattr(args, "qos", None) is not None:
        qos = int(args.qos)
    return settings, qos


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
        "--from-saved-app",
        action="store_true",
        help=(
            "Read broker, authentication, and TLS settings from the same user's "
            "saved portable-app data without printing any secret material."
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help=(
            "Portable-app data directory. Defaults to "
            "%%LOCALAPPDATA%%\\SmartCommissioning."
        ),
    )
    parser.add_argument(
        "--project-id",
        default=DEFAULT_PROJECT_ID,
        help="Saved app project ID used with --from-saved-app.",
    )
    parser.add_argument(
        "--site-id",
        default=DEFAULT_SITE_ID,
        help="Saved app site ID used with --from-saved-app.",
    )
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
    parser.add_argument(
        "--keep-alive",
        type=_positive_int,
        help="MQTT keep-alive in seconds (or SC_CAPTURE_MQTT_KEEP_ALIVE; default 60).",
    )
    parser.add_argument(
        "--qos",
        choices=(0, 1, 2),
        default=None,
        type=int,
        help="Subscription QoS. Defaults to saved app QoS with --from-saved-app, otherwise 0.",
    )
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

    topics = _topics_from_args(args, environ)

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

    keep_alive_value = (
        getattr(args, "keep_alive", None)
        or environ.get("SC_CAPTURE_MQTT_KEEP_ALIVE")
        or "60"
    )
    try:
        keep_alive = _positive_int(str(keep_alive_value))
    except (argparse.ArgumentTypeError, ValueError) as error:
        raise CaptureConfigurationError(
            "The MQTT keep-alive must be a positive integer."
        ) from error

    use_tls = (
        args.use_tls
        if args.use_tls is not None
        else _bool_from_env(environ.get("SC_CAPTURE_MQTT_TLS"), default=port == 8883)
    )
    settings = MqttConnectionSettings(
        host=host,
        port=port,
        client_id=f"smart-commissioning-capture-{uuid.uuid4().hex[:12]}",
        keep_alive=keep_alive,
        username=username,
        password=password,
        use_tls=use_tls,
        ca_certificate=environ.get("SC_CAPTURE_MQTT_CA_CERTIFICATE") or None,
        client_certificate=environ.get("SC_CAPTURE_MQTT_CLIENT_CERTIFICATE") or None,
        private_key=environ.get("SC_CAPTURE_MQTT_PRIVATE_KEY") or None,
    )
    return settings, topics


def _qos_from_args(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    if getattr(args, "qos", None) is not None:
        return int(args.qos)
    configured = environ.get("SC_CAPTURE_MQTT_QOS")
    if configured:
        return _qos_from_value(configured, source="SC_CAPTURE_MQTT")
    return 0


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
    on_started: CaptureStartedCallback | None = None,
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
    ping_interval = max(1.0, settings.keep_alive / 2.0)
    last_ping_at = monotonic()
    accepted = 0
    try:
        with client_factory(settings) as client:
            client.subscribe_many(list(topics), qos)
            if on_started is not None:
                on_started()
            while accepted < max_messages:
                if writer_errors:
                    raise writer_errors[0]
                now = monotonic()
                if now - last_ping_at >= ping_interval:
                    client.ping()
                    last_ping_at = monotonic()
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
        if args.from_saved_app:
            if any(
                value is not None
                for value in (args.host, args.port, args.use_tls)
            ):
                raise CaptureConfigurationError(
                    "Do not combine --from-saved-app with --host, --port, --tls, or --no-tls."
                )
            settings, qos = _settings_from_saved_app(args, environ=os.environ)
            topics = _topics_from_args(args, os.environ)
        else:
            settings, topics = _settings_from_args(
                args,
                environ=os.environ,
                password_prompt=getpass.getpass,
            )
            qos = _qos_from_args(args, os.environ)
        count = capture_to_jsonl(
            settings,
            topics,
            args.output,
            duration_seconds=args.duration_seconds,
            max_messages=args.max_messages,
            qos=qos,
            on_started=lambda: print(
                "Capture started: MQTT subscription acknowledged. Start the app validation now."
            ),
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
