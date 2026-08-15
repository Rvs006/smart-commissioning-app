#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.47 release paths."""

from __future__ import annotations

import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.47",
        package_version="0.1.47",
        release_notes_path="docs/release-notes-v0.1.47.md",
        migration_path="docs/migration-rollback-v0.1.47.md",
        validation_path="docs/release-validation-v0.1.47.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.47.md",
        evidence_validator="validate_v0147_release_evidence.py",
        evidence_test="test_v0147_release_evidence.py",
        release_contract_script="check_v0147_release_contracts.py",
    )
    for relative in (
        "scripts/scan_v0147_release_secrets.py",
        "scripts/test_v0147_security_scan.py",
        "scripts/test_v0147_version_identity.py",
    ):
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.47 safety file is missing: {relative}")
    release_workflow = (repo / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    windows_workflow = (repo / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    portable_builder = (repo / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")
    evidence_manifest = (repo / "docs/v0.1.47-evidence-manifest.md").read_text(encoding="utf-8")
    scanner_commands = (
        (release_workflow, "release workflow", "python scripts/scan_v0147_release_secrets.py --path build/release-evidence"),
        (windows_workflow, "Windows workflow", "python scripts/scan_v0147_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable"),
    )
    for text, label, command in scanner_commands:
        if command not in text:
            failures.append(f"{label} does not execute the v0.1.47 repository secret scan")
        elif text.find("actions/upload-artifact@", text.index(command) + len(command)) < 0:
            failures.append(f"{label} does not upload an artifact after the v0.1.47 secret scan")
    hosted = release_workflow[release_workflow.index("  evidence:") : release_workflow.index("  promote-images:")]
    scan = "python scripts/scan_v0147_release_secrets.py --path build/release-evidence"
    if not (
        hosted.index("python scripts/generate_release_evidence.py")
        < hosted.index(scan)
        < hosted.index("actions/upload-artifact@")
    ):
        failures.append("v0.1.47 evidence scan is not ordered after assembly and before upload")
    for value in (
        '"local-dirty-tree"',
        '"dirty_local_non_publishable"',
        'publishable = ($SourceTreeState -eq "clean")',
    ):
        if value not in portable_builder:
            failures.append(f"portable build does not enforce dirty-tree provenance: {value}")
    for value in (
        "source_commit: local-dirty-tree",
        "source_tree_state: dirty_local_non_publishable",
        "publishable: false",
        "clean rebuild after the authorized release",
    ):
        if value not in evidence_manifest:
            failures.append(f"v0.1.47 evidence manifest omits provenance requirement: {value}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.47 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
