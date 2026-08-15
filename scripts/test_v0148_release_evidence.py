#!/usr/bin/env python3
"""Regression tests for the v0.1.48 evidence-validator entry point."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import validate_v0148_release_evidence

ROOT = Path(__file__).resolve().parents[1]


class V0148EvidenceEntryPointTests(unittest.TestCase):
    def test_delegates_to_the_shared_contract_with_the_exact_version(self) -> None:
        arguments = ["--version", "v0.1.48"]
        with patch.object(
            validate_v0148_release_evidence.base, "main_for_version", return_value=0
        ) as delegate:
            self.assertEqual(validate_v0148_release_evidence.main(arguments), 0)
        delegate.assert_called_once_with(
            arguments,
            expected_version="v0.1.48",
            success_label="v0.1.48 Docker and release evidence: OK",
        )

    def test_dirty_portable_provenance_is_explicitly_non_publishable(self) -> None:
        build_script = (ROOT / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")
        manifest = (ROOT / "docs/v0.1.48-evidence-manifest.md").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
