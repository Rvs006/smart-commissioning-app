#!/usr/bin/env python3
"""Tests for the credential-safe MQTT evidence collector."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import capture_mqtt_evidence as capture
from cryptography.fernet import Fernet
from smart_commissioning_core.mqtt_transport import MqttMessage


class _FakeClient:
    def __init__(self, _settings: object, messages: list[MqttMessage]) -> None:
        self.messages = list(messages)
        self.subscriptions: list[tuple[list[str], int]] = []
        self.pings = 0

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def subscribe_many(self, topics: list[str], qos: int = 0) -> None:
        self.subscriptions.append((topics, qos))

    def ping(self) -> None:
        self.pings += 1

    def read_publish_any(self, **_kwargs: object) -> MqttMessage | None:
        return self.messages.pop(0) if self.messages else None


class _FailingClient(_FakeClient):
    def read_publish_any(self, **_kwargs: object) -> MqttMessage | None:
        raise RuntimeError("transport stopped")


class CaptureMqttEvidenceTests(unittest.TestCase):
    def test_settings_read_password_from_environment_without_cli_argument(self) -> None:
        args = argparse.Namespace(
            host="broker.example",
            port=8883,
            topics=["site/#"],
            use_tls=True,
        )
        prompt_calls: list[str] = []
        settings, topics = capture._settings_from_args(
            args,
            environ={
                "SC_CAPTURE_MQTT_USERNAME": "capture-user",
                "SC_CAPTURE_MQTT_PASSWORD": "test-secret-value",
            },
            password_prompt=lambda label: prompt_calls.append(label) or "prompted",
        )

        self.assertEqual(settings.password, "test-secret-value")
        self.assertEqual(settings.username, "capture-user")
        self.assertEqual(topics, ["site/#"])
        self.assertEqual(prompt_calls, [])
        self.assertNotIn("password", vars(args))

    def test_interactive_password_uses_hidden_prompt(self) -> None:
        args = argparse.Namespace(
            host="broker.example",
            port=1883,
            topics=["site/#"],
            use_tls=False,
        )
        with patch.object(capture.sys.stdin, "isatty", return_value=True):
            settings, _topics = capture._settings_from_args(
                args,
                environ={"SC_CAPTURE_MQTT_USERNAME": "capture-user"},
                password_prompt=lambda _label: "prompted-secret",
            )

        self.assertEqual(settings.password, "prompted-secret")

    def test_saved_app_values_use_saved_qos_and_keep_a_separate_client_id(self) -> None:
        settings, qos = capture._settings_from_saved_values(
            {
                "MQTT Broker FQDN or IP Address": "10.191.160.4",
                "Port": "8883",
                "Use TLS": "Enabled",
                "QoS": "1 - At least once",
                "Keep Alive Interval": "60",
                "MQTT Username": "capture-user",
                "MQTT Password": "stored-password",
            },
            {
                "CA Certificate": "secret://ca-certificate-example",
                "Client Certificate": "secret://client-certificate-example",
                "Private Key": "secret://private-key-example",
            },
        )

        self.assertEqual(settings.host, "10.191.160.4")
        self.assertEqual(settings.port, 8883)
        self.assertTrue(settings.use_tls)
        self.assertEqual(settings.keep_alive, 60)
        self.assertEqual(qos, 1)
        self.assertTrue(settings.client_id.startswith("smart-commissioning-evidence-"))
        self.assertEqual(settings.ca_certificate, "secret://ca-certificate-example")
        self.assertEqual(settings.client_certificate, "secret://client-certificate-example")
        self.assertEqual(settings.private_key, "secret://private-key-example")

    def test_saved_app_runtime_points_only_at_the_requested_data_directory(self) -> None:
        environment: dict[str, str] = {}
        data_root = Path("C:/field-data")
        capture._configure_saved_app_runtime(data_root, environment)

        self.assertEqual(environment["SMART_COMMISSIONING_RUNTIME_ROOT"], str(data_root))
        self.assertEqual(environment["SMART_COMMISSIONING_SECRETS_ROOT"], str(data_root / "secrets"))
        self.assertEqual(
            environment["DATABASE_URL"],
            f"sqlite:///{(data_root / 'smart_commissioning.db').as_posix()}",
        )

    def test_saved_app_loader_reads_configuration_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "secrets"
            secrets.mkdir()
            key = Fernet.generate_key()
            (secrets / ".secret_store_key").write_bytes(key)
            fernet = Fernet(key)
            references = {
                "CA Certificate": "secret://test-ca",
                "Client Certificate": "secret://test-client",
                "Private Key": "secret://test-private-key",
                "MQTT Password": "secret://test-password",
            }
            for field_name, reference in references.items():
                name = reference.removeprefix("secret://")
                contents = b"stored-password" if field_name == "MQTT Password" else b"placeholder"
                (secrets / f"{name}.pem").write_bytes(fernet.encrypt(contents))

            payload = {
                "mqtt": {
                    "values": {
                        "MQTT Broker FQDN or IP Address": "10.191.160.4",
                        "Port": "8883",
                        "Use TLS": "Enabled",
                        "QoS": "1 - At least once",
                        "Keep Alive Interval": "60",
                        "MQTT Username": "",
                        "MQTT Password": references["MQTT Password"],
                    }
                },
                "certificates": {
                    "values": {
                        "CA Certificate": references["CA Certificate"],
                        "Client Certificate": references["Client Certificate"],
                        "Private Key": references["Private Key"],
                    }
                },
            }
            payload_text = json.dumps(payload, separators=(",", ":"))
            database = root / "smart_commissioning.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE configuration_snapshots (
                        project_id TEXT,
                        site_id TEXT,
                        version INTEGER,
                        payload TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO configuration_snapshots VALUES (?, ?, ?, ?)",
                    ("demo-project", "demo-site", 1, payload_text),
                )
                connection.commit()
            finally:
                connection.close()

            with patch.dict(os.environ, {}, clear=False):
                mqtt, certificates = capture._saved_app_values(
                    root,
                    project_id="demo-project",
                    site_id="demo-site",
                    environ=os.environ,
                )
                import app.services as app_services

                resolved_ca = app_services._resolve_secret(references["CA Certificate"])

            connection = sqlite3.connect(database)
            try:
                stored_payload = connection.execute(
                    "SELECT payload FROM configuration_snapshots"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(mqtt["MQTT Password"], "stored-password")
        self.assertEqual(certificates["Private Key"], "secret://test-private-key")
        self.assertEqual(resolved_ca, b"placeholder")
        self.assertEqual(stored_payload, payload_text)

    def test_capture_preserves_repeated_topic_messages_and_exact_topic(self) -> None:
        received_at = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        messages = [
            MqttMessage("site/device/events/pointset", b'{"value":1}', received_at=received_at),
            MqttMessage("site/device/events/pointset", b'{"value":2}', received_at=received_at),
        ]
        fake = _FakeClient(None, messages)
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            count = capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=60,
                max_messages=2,
                qos=1,
                client_factory=lambda _settings: fake,
                monotonic=lambda: 0.0,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(count, 2)
        self.assertEqual([row["topic"] for row in rows], ["site/device/events/pointset"] * 2)
        self.assertEqual([row["payload"] for row in rows], ['{"value":1}', '{"value":2}'])
        self.assertEqual(rows[0]["received_at"], received_at.isoformat())
        self.assertEqual(fake.subscriptions, [(["site/#"], 1)])
        self.assertNotIn("username", rows[0])
        self.assertNotIn("password", rows[0])

    def test_existing_evidence_file_is_not_overwritten(self) -> None:
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            output.write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                capture.capture_to_jsonl(
                    settings,
                    ["site/#"],
                    output,
                    duration_seconds=1,
                    max_messages=1,
                    qos=0,
                    client_factory=lambda _settings: _FakeClient(None, []),
                    monotonic=lambda: 0.0,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve")

    def test_capture_sends_keepalives_during_a_long_run(self) -> None:
        fake = _FakeClient(
            None,
            [
                MqttMessage("site/device/state", b"{}"),
                MqttMessage("site/device/metadata", b"{}"),
            ],
        )
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
            keep_alive=60,
        )
        clock_values = iter(range(0, 1_000, 31))

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            count = capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=600,
                max_messages=2,
                qos=0,
                client_factory=lambda _settings: fake,
                monotonic=lambda: next(clock_values),
            )

        self.assertEqual(count, 2)
        self.assertGreaterEqual(fake.pings, 1)

    def test_capture_reports_start_only_after_subscription_is_acknowledged(self) -> None:
        fake = _FakeClient(None, [MqttMessage("site/device/state", b"{}")])
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        started: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            count = capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=60,
                max_messages=1,
                qos=1,
                client_factory=lambda _settings: fake,
                monotonic=lambda: 0.0,
                on_started=lambda: started.append("acknowledged"),
            )

        self.assertEqual(count, 1)
        self.assertEqual(started, ["acknowledged"])
        self.assertEqual(fake.subscriptions, [(["site/#"], 1)])

    def test_manifest_capture_appends_a_completed_terminal_marker(self) -> None:
        fake = _FakeClient(None, [MqttMessage("site/device/state", b"{}")])
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {
                            "sha256": "a" * 64,
                            "revision": "revision-ref",
                            "import_identity": "import-ref",
                        },
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            count = capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=60,
                max_messages=1,
                qos=0,
                client_factory=lambda _settings: fake,
                monotonic=lambda: 0.0,
                manifest_path=manifest,
                run_id="run-ref",
                require_provenance=True,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(count, 1)
        self.assertEqual(rows[-1]["record_type"], "capture_terminal")
        self.assertEqual(rows[-1]["termination_reason"], "message_cap")
        self.assertFalse(rows[-1]["window_completed"])
        self.assertEqual(rows[-1]["run_id"], "run-ref")
        self.assertEqual(rows[-1]["register_sha256"], "a" * 64)

    def test_acceptance_rejects_a_manifest_from_another_release(self) -> None:
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.36",
                        "register": {
                            "sha256": "a" * 64,
                            "revision": "revision-ref",
                            "import_identity": "import-ref",
                        },
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(capture.CaptureConfigurationError):
                capture.capture_to_jsonl(
                    settings,
                    ["site/#"],
                    root / "capture.jsonl",
                    duration_seconds=60,
                    max_messages=1,
                    qos=0,
                    client_factory=lambda _settings: _FakeClient(None, []),
                    monotonic=lambda: 0.0,
                    manifest_path=manifest,
                    run_id="run-ref",
                    require_provenance=True,
                )

    def test_capture_rejects_an_oversized_payload_with_a_terminal_marker(self) -> None:
        fake = _FakeClient(None, [MqttMessage("site/device/state", b"12345")])
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {
                            "sha256": "a" * 64,
                            "revision": "revision-ref",
                            "import_identity": "import-ref",
                        },
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            with self.assertRaises(capture.CaptureResourceLimitError):
                capture.capture_to_jsonl(
                    settings,
                    ["site/#"],
                    output,
                    duration_seconds=60,
                    max_messages=1,
                    qos=0,
                    client_factory=lambda _settings: fake,
                    monotonic=lambda: 0.0,
                    manifest_path=manifest,
                    run_id="run-ref",
                    require_provenance=True,
                    max_payload_bytes=4,
                )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows[-1]["termination_reason"], "payload_size_cap")
        self.assertFalse(rows[-1]["window_completed"])

    def test_indefinite_capture_uses_backstop_and_reports_progress(self) -> None:
        fake = _FakeClient(None, [])
        settings = capture.MqttConnectionSettings(
            host="broker.example",
            port=1883,
            client_id="capture-test",
        )
        progress: list[int] = []
        clock_values = iter([0.0, 31.0, 172_801.0, 172_802.0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {
                            "sha256": "a" * 64,
                            "revision": "revision-ref",
                            "import_identity": "import-ref",
                        },
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            count = capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=0,
                max_messages=10,
                qos=0,
                client_factory=lambda _settings: fake,
                monotonic=lambda: next(clock_values),
                manifest_path=manifest,
                run_id="run-ref",
                require_provenance=True,
                on_progress=progress.append,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(count, 0)
        self.assertEqual(progress, [0, 0])
        self.assertEqual(rows[-1]["termination_reason"], "backstop_elapsed")
        self.assertFalse(rows[-1]["window_completed"])

    def test_terminal_marker_reports_window_elapsed(self) -> None:
        settings = capture.MqttConnectionSettings(host="broker.example", port=1883, client_id="capture-test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {"sha256": "a" * 64, "revision": "revision-ref", "import_identity": "import-ref"},
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            clock_values = iter([0.0, 2.0])
            capture.capture_to_jsonl(
                settings,
                ["site/#"],
                output,
                duration_seconds=1,
                max_messages=10,
                qos=0,
                client_factory=lambda _settings: _FakeClient(None, []),
                monotonic=lambda: next(clock_values),
                manifest_path=manifest,
                run_id="run-ref",
                require_provenance=True,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            started = rows[0]
            ended = rows[-2]
            marker = rows[-1]
        self.assertEqual(started["record_type"], "capture_window_started")
        self.assertEqual(ended["record_type"], "capture_window_ended")
        self.assertTrue(started["started_at"])
        self.assertTrue(ended["ended_at"])
        self.assertEqual(marker["window_started_at"], started["started_at"])
        self.assertEqual(marker["window_ended_at"], ended["ended_at"])
        self.assertEqual(marker["termination_reason"], "window_elapsed")
        self.assertTrue(marker["window_completed"])

    def test_terminal_marker_reports_aggregate_cap(self) -> None:
        settings = capture.MqttConnectionSettings(host="broker.example", port=1883, client_id="capture-test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {"sha256": "a" * 64, "revision": "revision-ref", "import_identity": "import-ref"},
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            with self.assertRaises(capture.CaptureResourceLimitError):
                capture.capture_to_jsonl(
                    settings,
                    ["site/#"],
                    output,
                    duration_seconds=60,
                    max_messages=10,
                    qos=0,
                    client_factory=lambda _settings: _FakeClient(
                        None, [MqttMessage("site/device/state", b"{}")]
                    ),
                    monotonic=lambda: 0.0,
                    manifest_path=manifest,
                    run_id="run-ref",
                    require_provenance=True,
                    max_total_bytes=1,
                )
            marker = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(marker["termination_reason"], "aggregate_bytes_cap")
        self.assertFalse(marker["window_completed"])

    def test_terminal_marker_reports_broker_interruption(self) -> None:
        settings = capture.MqttConnectionSettings(host="broker.example", port=1883, client_id="capture-test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "release_version": "v0.1.37",
                        "register": {"sha256": "a" * 64, "revision": "revision-ref", "import_identity": "import-ref"},
                        "application": {"version": "v0.1.37", "commit": "b" * 40},
                        "approved_scope": "scope-ref",
                        "runs": {"gate_a": {"run_id": "run-ref"}, "gate_b": {"run_id": "run-b-ref"}},
                    }
                ),
                encoding="utf-8",
            )
            output = root / "capture.jsonl"
            with self.assertRaises(RuntimeError):
                capture.capture_to_jsonl(
                    settings,
                    ["site/#"],
                    output,
                    duration_seconds=60,
                    max_messages=10,
                    qos=0,
                    client_factory=lambda _settings: _FailingClient(None, []),
                    monotonic=lambda: 0.0,
                    manifest_path=manifest,
                    run_id="run-ref",
                    require_provenance=True,
                )
            marker = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(marker["termination_reason"], "broker_interruption")
        self.assertFalse(marker["window_completed"])


if __name__ == "__main__":
    unittest.main()
