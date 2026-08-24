#!/usr/bin/env python3
"""Regression tests for the v0.1.53 evidence-validator entry point."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate_v0153_release_evidence

ROOT = Path(__file__).resolve().parents[1]

_CLEAN_SHA = "a" * 40
_EXE_NAME = "SmartCommissioningApp.exe"
_EXE_BYTES = b"portable-app-bytes"
_SCANNER_COMPONENTS = {
    "scanners/node.exe": b"node-runtime-bytes",
    "scanners/mqtt-discovery/dist/bundle.js": b"mqtt-scanner-bundle-bytes",
}


def _write_provenance(
    bundle: Path,
    *,
    write_exe: bool = True,
    exe_bytes: bytes = _EXE_BYTES,
    write_scanners: bool = True,
    **overrides: object,
) -> Path:
    if write_exe:
        (bundle / _EXE_NAME).write_bytes(exe_bytes)
    scanner_hashes = {}
    for relative, data in _SCANNER_COMPONENTS.items():
        scanner_hashes[relative] = hashlib.sha256(data).hexdigest()
        if write_scanners:
            component = bundle / relative
            component.parent.mkdir(parents=True, exist_ok=True)
            component.write_bytes(data)
    payload = {
        "schema_version": "1.0",
        "application_version": "v0.1.53",
        "portable_profile": "unified",
        "source_commit": _CLEAN_SHA,
        "source_tree_state": "clean",
        "publishable": True,
        "portable_exe_sha256": hashlib.sha256(exe_bytes).hexdigest(),
        "scanner_components_sha256": scanner_hashes,
    }
    payload.update(overrides)
    provenance = bundle / "BUILD_PROVENANCE.json"
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    (bundle / "release-evidence.json").write_text("{}", encoding="utf-8")
    return provenance


def _windows_args(bundle: Path, commit: str = _CLEAN_SHA) -> list[str]:
    return [
        "--evidence-kind", "windows",
        "--version", "v0.1.53",
        "--commit", commit,
        "--evidence", str(bundle / "release-evidence.json"),
        "--search-root", str(bundle),
    ]


class V0153EvidenceEntryPointTests(unittest.TestCase):
    def test_delegates_to_the_shared_contract_with_the_exact_version(self) -> None:
        arguments = ["--version", "v0.1.53"]
        with patch.object(
            validate_v0153_release_evidence.base, "main_for_version", return_value=0
        ) as delegate:
            self.assertEqual(validate_v0153_release_evidence.main(arguments), 0)
        delegate.assert_called_once_with(
            arguments,
            expected_version="v0.1.53",
            success_label="v0.1.53 Docker and release evidence: OK",
        )

    def test_dirty_portable_provenance_is_explicitly_non_publishable(self) -> None:
        build_script = (ROOT / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")
        manifest = (ROOT / "docs/v0.1.53-evidence-manifest.md").read_text(encoding="utf-8")
        for value in (
            '"dirty_local_non_publishable"',
            '"local-dirty-tree"',
            'publishable = ($SourceTreeState -eq "clean")',
        ):
            self.assertIn(value, build_script)
        self.assertIn("source_commit: local-dirty-tree", manifest)
        self.assertIn("source_tree_state: dirty_local_non_publishable", manifest)
        self.assertIn("publishable: false", manifest)
        self.assertIn("clean rebuild after the authorized release", manifest)


class V0153BundleProvenanceTests(unittest.TestCase):
    def _run(self, args: list[str]) -> tuple[int, object]:
        with patch.object(
            validate_v0153_release_evidence.base, "main_for_version", return_value=0
        ) as delegate:
            code = validate_v0153_release_evidence.main(args)
        return code, delegate

    def test_dirty_bundle_is_rejected_before_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(
                bundle,
                source_commit="local-dirty-tree",
                source_tree_state="dirty_local_non_publishable",
                publishable=False,
            )
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_clean_bundle_with_mismatched_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle, source_commit="b" * 40)
            code, delegate = self._run(_windows_args(bundle, commit=_CLEAN_SHA))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_missing_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "release-evidence.json").write_text("{}", encoding="utf-8")
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_invalid_utf8_provenance_is_rejected_without_traceback(self) -> None:
        # PROVENANCE-5: malformed encoding must return a controlled invalid
        # failure, not escape as an uncaught UnicodeDecodeError traceback.
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "release-evidence.json").write_text("{}", encoding="utf-8")
            (bundle / "BUILD_PROVENANCE.json").write_bytes(
                b'{"source_tree_state":"clean"}\xff'
            )
            failures = validate_v0153_release_evidence._bundle_provenance_failures(
                _windows_args(bundle)
            )
        self.assertEqual(len(failures), 1)
        self.assertIn("BUILD_PROVENANCE.json is invalid", failures[0])

    def test_clean_matching_bundle_reaches_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 0)
        delegate.assert_called_once()

    def test_tampered_executable_is_rejected_before_delegation(self) -> None:
        # REV-2: provenance records the original exe hash; the exe bytes are
        # changed afterwards. The validator must re-derive the digest and fail.
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle)
            (bundle / _EXE_NAME).write_bytes(_EXE_BYTES + b"\x00")
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_missing_executable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle, write_exe=False)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_invalid_portable_exe_sha256_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle, portable_exe_sha256=None)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_tampered_scanner_component_is_rejected_before_delegation(self) -> None:
        # PROV-6: a scanner component changed after provenance is written must be
        # caught the same way the executable is.
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle)
            (bundle / "scanners/mqtt-discovery/dist/bundle.js").write_bytes(b"tampered")
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_missing_scanner_component_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle, write_scanners=False)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_missing_scanner_component_map_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle, scanner_components_sha256=None)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_scanner_component_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(
                bundle, scanner_components_sha256={"../escape.js": "a" * 64}
            )
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 1)
        delegate.assert_not_called()

    def test_hosted_evidence_skips_the_portable_provenance_check(self) -> None:
        code, delegate = self._run(
            ["--evidence-kind", "hosted", "--version", "v0.1.53", "--commit", _CLEAN_SHA]
        )
        self.assertEqual(code, 0)
        delegate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
