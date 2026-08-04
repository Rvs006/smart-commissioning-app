#!/usr/bin/env python3
"""Tests for the v0.1.37 release-facing secret scan."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

import scan_v0137_release_secrets as scan


class V0137SecurityScanTests(unittest.TestCase):
    def test_rejects_seeded_private_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-notes.md"
            path.write_text(
                'broker_password = "seeded-private-value"\n'
                "api_key: seeded-private-value\n",
                encoding="utf-8",
            )
            failures = scan.scan([path])
        self.assertTrue(failures)

    def test_accepts_sanitized_release_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-notes.md"
            path.write_text(
                "- password: {{PASSWORD_FROM_SECRET_MANAGER}}\n"
                "- API: {{API_IMAGE}}@{{API_IMAGE_DIGEST}}\n",
                encoding="utf-8",
            )
            failures = scan.scan([path])
        self.assertEqual(failures, [])

    def test_ignores_cpe_metadata_containing_passwd_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SBOM.json"
            path.write_text(
                '{"cpe":"cpe:2.3:a:base-passwd:base-passwd:3.6.7:*:*:*:*:*:*:*"}\n',
                encoding="utf-8",
            )
            failures = scan.scan([path])
        self.assertEqual(failures, [])

    def test_scans_zip_members_including_docx_and_xlsx_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release-evidence.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", '<w:t>api_key = "seeded-private-value"</w:t>')
            failures = scan.scan([path])
        self.assertTrue(failures)

    def test_scans_tar_members(self) -> None:
        import tarfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "release.log"
            source.write_text('password = "seeded-private-value"\n', encoding="utf-8")
            path = root / "release-evidence.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                archive.add(source, arcname="release.log")
            failures = scan.scan([path])
        self.assertTrue(failures)

    def test_rejects_a_complete_pem_private_key_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.pem"
            path.write_text(
                "-----BEGIN PRIVATE KEY-----\n"
                + ("A" * 80)
                + "\n-----END PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            failures = scan.scan([path])
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
