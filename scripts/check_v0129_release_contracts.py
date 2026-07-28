#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.29 release paths."""

from __future__ import annotations

import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.29",
        package_version="0.1.29",
        release_notes_path="docs/release-notes-v0.1.29.md",
        migration_path="docs/migration-rollback-v0.1.29.md",
        validation_path="docs/release-validation-v0.1.29.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.29.md",
        evidence_validator="validate_v0129_release_evidence.py",
        evidence_test="test_v0129_release_evidence.py",
        release_contract_script="check_v0129_release_contracts.py",
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.29 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
