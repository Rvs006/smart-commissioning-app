#!/usr/bin/env python3
"""Regression tests for the sanitized v0.1.37 acceptance manifest."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import build_v0137_acceptance_manifest as builder


class AcceptanceManifestTests(unittest.TestCase):
    def test_hashes_register_without_serializing_private_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            register = Path(directory) / "private-register.csv"
            register.write_bytes(b"header\nrow\n")
            output = Path(directory) / "manifest.json"
            builder.main(
                [
                    "--register", str(register),
                    "--output", str(output),
                    "--register-revision", "revision-v0137",
                    "--register-import-id", "import-v0137",
                    "--scope", "scope-v0137",
                    "--application-commit", "a" * 40,
                    "--gate-a-run-id", "run-a",
                    "--gate-b-run-id", "run-b",
                ]
            )
            manifest = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_version"], "v0.1.37")
        self.assertEqual(manifest["register"]["expected_asset_count"], 752)
        self.assertEqual(manifest["register"]["sha256"], "1c131eede78f200a76f00be22148db81139b6ad19bb33f1eafcc0873e4147256")
        self.assertNotIn("path_name", manifest["register"])
        self.assertNotIn("private-register.csv", json.dumps(manifest))
        self.assertNotIn(str(register), json.dumps(manifest))

    def test_rejects_secret_like_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            register = Path(directory) / "register.csv"
            register.write_text("header\nrow\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                builder.main(
                    [
                        "--register", str(register),
                        "--output", str(Path(directory) / "manifest.json"),
                        "--register-revision", "revision-v0137",
                        "--register-import-id", "import-v0137",
                        "--scope", "scope-v0137",
                        "--application-commit", "a" * 40,
                        "--gate-a-run-id", "run-a",
                        "--gate-b-run-id", "run-b",
                        "--broker-endpoint-ref", "password-ref",
                    ]
                )

    def test_does_not_overwrite_existing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            register = Path(directory) / "register.csv"
            register.write_text("header\nrow\n", encoding="utf-8")
            output = Path(directory) / "manifest.json"
            output.write_text("preserve", encoding="utf-8")
            with self.assertRaises(SystemExit):
                builder.main(
                    [
                        "--register", str(register),
                        "--output", str(output),
                        "--register-revision", "revision-v0137",
                        "--register-import-id", "import-v0137",
                        "--scope", "scope-v0137",
                        "--application-commit", "a" * 40,
                        "--gate-a-run-id", "run-a",
                        "--gate-b-run-id", "run-b",
                    ]
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
