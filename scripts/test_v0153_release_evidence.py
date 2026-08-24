#!/usr/bin/env python3
"""Regression tests for the v0.1.53 evidence-validator entry point."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate_v0153_release_evidence

ROOT = Path(__file__).resolve().parents[1]

_CLEAN_SHA = "a" * 40


def _write_provenance(bundle: Path, **overrides: object) -> Path:
    payload = {
        "schema_version": "1.0",
        "application_version": "v0.1.53",
        "portable_profile": "unified",
        "source_commit": _CLEAN_SHA,
        "source_tree_state": "clean",
        "publishable": True,
        "portable_exe_sha256": None,
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

    def test_clean_matching_bundle_reaches_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            _write_provenance(bundle)
            code, delegate = self._run(_windows_args(bundle))
        self.assertEqual(code, 0)
        delegate.assert_called_once()

    def test_hosted_evidence_skips_the_portable_provenance_check(self) -> None:
        code, delegate = self._run(
            ["--evidence-kind", "hosted", "--version", "v0.1.53", "--commit", _CLEAN_SHA]
        )
        self.assertEqual(code, 0)
        delegate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
