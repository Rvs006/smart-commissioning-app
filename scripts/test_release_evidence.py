#!/usr/bin/env python3
"""Regression tests for the standalone ci-path evidence validator.

Covers the fail-closed handling added for unreadable/undecodable inputs:
_sha256 returning None on OSError, and the JSON/ascii reads not escaping as
tracebacks.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import validate_release_evidence

_COMMIT = "a" * 40


def _write_evidence(bundle: Path, payload_bytes: bytes = b"payload") -> Path:
    (bundle / "payload.bin").write_bytes(payload_bytes)
    evidence = {
        "release_version": "v0.1.53",
        "source_commit": _COMMIT,
        "gates": {},
        "files": [
            {
                "name": "payload.bin",
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
                "size": len(payload_bytes),
            }
        ],
    }
    evidence_path = bundle / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return evidence_path


def _args(evidence_path: Path) -> list[str]:
    return ["--evidence", str(evidence_path), "--version", "v0.1.53", "--commit", _COMMIT]


class ReleaseEvidenceHardeningTests(unittest.TestCase):
    def test_sha256_returns_none_on_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "f.bin"
            target.write_bytes(b"x")
            real_open = Path.open

            def denied(self, *args, **kwargs):
                if self == target:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_open(self, *args, **kwargs)

            original = Path.open
            Path.open = denied
            try:
                self.assertIsNone(validate_release_evidence._sha256(target))
            finally:
                Path.open = original

    def test_unreadable_payload_is_a_controlled_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            evidence_path = _write_evidence(bundle)
            target = (bundle / "payload.bin").resolve()
            real_open = Path.open

            def denied(self, *args, **kwargs):
                if self == target:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_open(self, *args, **kwargs)

            original = Path.open
            Path.open = denied
            try:
                code = validate_release_evidence.main(_args(evidence_path))
            finally:
                Path.open = original
        self.assertEqual(code, 1)

    def test_invalid_utf8_evidence_is_a_controlled_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            evidence_path.write_bytes(b'{"release_version":"v0.1.53"}\xff')
            code = validate_release_evidence.main(_args(evidence_path))
        self.assertEqual(code, 1)

    def test_non_ascii_checksums_is_a_controlled_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            evidence_path = _write_evidence(bundle)
            (bundle / "SHA256SUMS.txt").write_bytes(b"\xff\xfe not ascii\n")
            code = validate_release_evidence.main(_args(evidence_path))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
