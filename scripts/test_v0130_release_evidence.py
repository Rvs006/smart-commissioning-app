#!/usr/bin/env python3
"""Regression tests for the v0.1.30 evidence-validator entry point."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import validate_v0130_release_evidence


class V0130EvidenceEntryPointTests(unittest.TestCase):
    def test_delegates_to_the_shared_contract_with_the_exact_version(self) -> None:
        arguments = ["--version", "v0.1.30"]
        with patch.object(
            validate_v0130_release_evidence.base,
            "main_for_version",
            return_value=0,
        ) as delegate:
            self.assertEqual(validate_v0130_release_evidence.main(arguments), 0)

        delegate.assert_called_once_with(
            arguments,
            expected_version="v0.1.30",
            success_label="v0.1.30 Docker and release evidence: OK",
        )


if __name__ == "__main__":
    unittest.main()
