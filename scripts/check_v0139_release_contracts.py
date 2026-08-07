#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.39 release paths."""

from __future__ import annotations

import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.39",
        package_version="0.1.39",
        release_notes_path="docs/release-notes-v0.1.39.md",
        migration_path="docs/migration-rollback-v0.1.39.md",
        validation_path="docs/release-validation-v0.1.39.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.39.md",
        evidence_validator="validate_v0139_release_evidence.py",
        evidence_test="test_v0139_release_evidence.py",
        release_contract_script="check_v0139_release_contracts.py",
    )
    release_workflow = (repo / ".github/workflows/release-gates.yml").read_text(
        encoding="utf-8"
    )
    windows_workflow = (repo / ".github/workflows/windows-portable.yml").read_text(
        encoding="utf-8"
    )
    for relative in (
        "scripts/scan_v0139_release_secrets.py",
        "scripts/test_v0139_security_scan.py",
        "scripts/test_v0139_version_identity.py",
    ):
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.39 safety file is missing: {relative}")
    scanner_commands = (
        (
            release_workflow,
            "release workflow",
            "python scripts/scan_v0139_release_secrets.py --path build/release-evidence",
        ),
        (
            windows_workflow,
            "Windows workflow",
            "python scripts/scan_v0139_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable",
        ),
    )
    for text, label, scanner_command in scanner_commands:
        if scanner_command not in text:
            failures.append(f"{label} does not execute the v0.1.39 repository secret scan")
            continue
        scan_position = text.index(scanner_command)
        upload_position = text.find("actions/upload-artifact@", scan_position + len(scanner_command))
        if upload_position < 0:
            failures.append(f"{label} does not upload an artifact after the v0.1.39 secret scan")
    hosted_evidence = release_workflow[
        release_workflow.index("  evidence:") : release_workflow.index("  promote-images:")
    ]
    hosted_scan = "python scripts/scan_v0139_release_secrets.py --path build/release-evidence"
    if not (
        hosted_evidence.index("python scripts/generate_release_evidence.py")
        < hosted_evidence.index(hosted_scan)
        < hosted_evidence.index("actions/upload-artifact@")
    ):
        failures.append("v0.1.39 hosted evidence scan is not ordered after assembly and before upload")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.39 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
