#!/usr/bin/env python3
"""Tests for the v0.1.40 release-facing secret scan."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import scan_v0140_release_secrets as scan


class V0140SecurityScanTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
