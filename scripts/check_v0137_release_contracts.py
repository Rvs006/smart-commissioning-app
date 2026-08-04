#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.37 release paths."""

from __future__ import annotations

import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.37",
        package_version="0.1.37",
        release_notes_path="docs/release-notes-v0.1.37.md",
        migration_path="docs/migration-rollback-v0.1.37.md",
        validation_path="docs/release-validation-v0.1.37.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.37.md",
        evidence_validator="validate_v0137_release_evidence.py",
        evidence_test="test_v0137_release_evidence.py",
        release_contract_script="check_v0137_release_contracts.py",
    )
    publisher = (repo / "scripts/release-portable.ps1").read_text(encoding="utf-8")
    release_workflow = (repo / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    windows_workflow = (repo / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    for relative in (
        "scripts/scan_v0137_release_secrets.py",
        "scripts/test_v0137_security_scan.py",
        "scripts/test_v0137_version_identity.py",
    ):
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.37 safety file is missing: {relative}")
    for required in ("PrepareOnly", "PREPARE ONLY OK", "no GitHub release was created or published"):
        if required not in publisher:
            failures.append(f"publisher omits prepare-only contract: {required}")
    if "python scripts/scan_v0137_release_secrets.py" not in release_workflow:
        failures.append("release workflow does not execute the v0.1.37 repository secret scan")
    if "python scripts/scan_v0137_release_secrets.py --path build/release-evidence" not in release_workflow:
        failures.append("release workflow does not scan assembled hosted release evidence before upload")
    if "python scripts/scan_v0137_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable" not in windows_workflow:
        failures.append("Windows workflow does not scan the portable bundle before upload")
    hosted_evidence = release_workflow[release_workflow.index("  evidence:") : release_workflow.index("  promote-images:")]
    hosted_scan = "python scripts/scan_v0137_release_secrets.py --path build/release-evidence"
    if not (
        hosted_evidence.index("python scripts/generate_release_evidence.py")
        < hosted_evidence.index(hosted_scan)
        < hosted_evidence.index("actions/upload-artifact@")
    ):
        failures.append("hosted evidence scan is not ordered after assembly and before upload")
    if not (
        windows_workflow.index("python scripts/generate_release_evidence.py")
        < windows_workflow.index("python scripts/scan_v0137_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable")
        < windows_workflow.index("name: Upload bundle artifact")
    ):
        failures.append("Windows evidence scan is not ordered after assembly and before upload")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.37 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
