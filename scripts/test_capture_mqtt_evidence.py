#!/usr/bin/env python3
"""Tests for the credential-safe MQTT evidence collector."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import capture_mqtt_evidence as capture
from smart_commissioning_core.mqtt_transport import MqttMessage


class _FakeClient:
    def __init__(self, _settings: object, messages: list[MqttMessage]) -> None:
        self.messages = list(messages)
        self.subscriptions: list[tuple[list[str], int]] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def subscribe_many(self, topics: list[str], qos: int = 0) -> None:
        self.subscriptions.append((topics, qos))

    def read_publish_any(self, **_kwargs: object) -> MqttMessage | None:
        return self.messages.pop(0) if self.messages else None


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


if __name__ == "__main__":
    unittest.main()
