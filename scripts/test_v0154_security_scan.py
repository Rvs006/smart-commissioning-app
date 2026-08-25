#!/usr/bin/env python3
"""Tests for the v0.1.54 release-facing secret scan."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import scan_v0154_release_secrets as scan


class V0154SecurityScanTests(unittest.TestCase):
    def test_default_command_scans_the_release_source_tree(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = root / "README.md"
        with (
            patch.object(scan.base, "_files", return_value=[expected]) as files,
            patch.object(scan.base, "scan", return_value=[]) as scan_files,
        ):
            self.assertEqual(scan.main([]), 0)
        files.assert_called_once_with(root, explicit=False)
        scan_files.assert_called_once_with([expected])

    def test_directory_path_is_expanded_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "release-evidence.json"
            with (
                patch.object(scan.base, "_files", return_value=[expected]) as files,
                patch.object(scan.base, "scan", return_value=[]) as scan_files,
            ):
                self.assertEqual(scan.main(["--path", str(root)]), 0)
        files.assert_called_once_with(root.resolve(), explicit=True)
        self.assertEqual([path.name for path in scan_files.call_args.args[0]], [expected.name])

    def test_rejects_seeded_private_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-notes.md"
            path.write_text('broker_password = "seeded-private-value"\n', encoding="utf-8")
            self.assertTrue(scan.base.scan([path]))

    def test_accepts_sanitized_release_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-notes.md"
            path.write_text("- password: {{PASSWORD_FROM_SECRET_MANAGER}}\n", encoding="utf-8")
            self.assertEqual(scan.base.scan([path]), [])

    def test_scans_zip_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-evidence.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", 'api_key = "seeded-private-value"')
            self.assertTrue(scan.base.scan([path]))

    def test_scans_credentials_inside_a_nested_archive(self) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("notes.txt", "broker_password = seeded-private-value")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-evidence.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nested-private.zip", inner.getvalue())
            self.assertTrue(scan.base.scan([path]))

    def test_bundle_archive_skips_test_and_binary_members(self) -> None:
        # A packaged bundle carries test fixtures (synthetic markers by design)
        # and binaries; the archive scan must skip them like the directory walk,
        # while still catching a real secret in a text member.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SmartCommissioningApp-windows-portable.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "backend/tests/test_sync.py", 'password = "seeded-test-marker"'
                )
                archive.writestr(
                    "_internal/libpq-abc.dll", b"\x00secret_key = deadbeefcafebabe\x01"
                )
                archive.writestr("scanners/node.exe", b"MZ\x00\x00")
                archive.writestr("README_FIRST.txt", "broker_password = real-leak-value")
            failures = scan.base.scan([path])
        self.assertEqual(len(failures), 1)
        self.assertIn("README_FIRST.txt", failures[0])

    def test_rejects_json_credential_in_directory_and_zip(self) -> None:
        # A quoted-key JSON credential must be caught in both the assembled
        # evidence directory and the packaged ZIP (BF-INTEGRATION-2 false negative).
        payload = '{"password": "hunter2-secret-value"}'
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "release-evidence.json"
            json_path.write_text(payload + "\n", encoding="utf-8")
            self.assertTrue(scan.base.scan([json_path]))
            zip_path = Path(directory) / "release-evidence.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("evidence/release-evidence.json", payload)
            self.assertTrue(scan.base.scan([zip_path]))

    def test_accepts_json_field_name_maps_and_label_descriptions(self) -> None:
        # A password key mapped to a plain field name or a human description is a
        # schema/label, not a secret literal, and must not flood false positives.
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "aliases.json"
            alias.write_text('{"mqtt_password": "password"}\n', encoding="utf-8")
            self.assertEqual(scan.base.scan([alias]), [])
            label = Path(directory) / "labels.json"
            label.write_text(
                '{"MQTT Password": "Broker password (stored masked)."}\n',
                encoding="utf-8",
            )
            self.assertEqual(scan.base.scan([label]), [])

    def test_flags_credential_values_without_a_keyword(self) -> None:
        # Value shape alone must not suppress: letter-only, uppercase, underscore,
        # a passphrase, and an identifier under a credential key are all reported
        # (REV-2-passphrase-scan). Only a value that spells out a credential
        # keyword with no digit (a field name or a label) is suppressed.
        for payload in (
            '{"password": "abcdefghijk"}',
            '{"password": "ABCDEFGHIJK"}',
            '{"password": "___________"}',
            '{"password": "correct horse battery staple"}',
            '{"password": "SomeIdentifier"}',
            'api_key = "correct horse battery staple"',
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "release-evidence.json"
                path.write_text(payload + "\n", encoding="utf-8")
                self.assertTrue(scan.base.scan([path]), f"missed: {payload}")

    def test_rejects_pretty_printed_json_credential_across_lines(self) -> None:
        # Pretty-printed JSON splits the key and its quoted value onto separate
        # physical lines; the per-line matcher never sees both (REV-1). The
        # multi-line pass must catch it, while a field name or placeholder split
        # the same way stays clean.
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "release-evidence.json"
            secret.write_text('{\n  "password":\n  "hunter2secret"\n}\n', encoding="utf-8")
            self.assertTrue(scan.base.scan([secret]))
            field_name = Path(directory) / "aliases.json"
            field_name.write_text('{\n  "mqtt_password":\n  "password"\n}\n', encoding="utf-8")
            self.assertEqual(scan.base.scan([field_name]), [])
            placeholder = Path(directory) / "template.json"
            placeholder.write_text(
                '{\n  "password":\n  "{{FROM_SECRET_MANAGER}}"\n}\n', encoding="utf-8"
            )
            self.assertEqual(scan.base.scan([placeholder]), [])

    def test_clean_nested_archive_is_accepted(self) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("readme.txt", "ordinary release notes with no secrets")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-evidence.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nested-clean.zip", inner.getvalue())
            self.assertEqual(scan.base.scan([path]), [])


if __name__ == "__main__":
    unittest.main()
