#!/usr/bin/env python3
"""Tests for the v0.1.52 release-facing secret scan."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import scan_v0152_release_secrets as scan


class V0152SecurityScanTests(unittest.TestCase):
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
