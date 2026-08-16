#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.49 release paths."""

from __future__ import annotations

import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.49",
        package_version="0.1.49",
        release_notes_path="docs/release-notes-v0.1.49.md",
        migration_path="docs/migration-rollback-v0.1.49.md",
        validation_path="docs/release-validation-v0.1.49.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.49.md",
        evidence_validator="validate_v0149_release_evidence.py",
        evidence_test="test_v0149_release_evidence.py",
        release_contract_script="check_v0149_release_contracts.py",
    )
    required = (
        "scripts/scan_v0149_release_secrets.py",
        "scripts/test_v0149_security_scan.py",
        "scripts/test_v0149_version_identity.py",
        "docs/v0.1.49-evidence-manifest.md",
        "docs/v0.1.49-field-acceptance-checklist.md",
    )
    for relative in required:
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.49 safety file is missing: {relative}")
    release_workflow = (repo / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    windows_workflow = (repo / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    portable_builder = (repo / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")
    evidence_manifest = (repo / "docs/v0.1.49-evidence-manifest.md").read_text(encoding="utf-8")
    for text, label, command in (
        (release_workflow, "release workflow", "python scripts/scan_v0149_release_secrets.py --path build/release-evidence"),
        (windows_workflow, "Windows workflow", "python scripts/scan_v0149_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable"),
    ):
        if command not in text:
            failures.append(f"{label} does not execute the v0.1.49 repository secret scan")
        elif text.find("actions/upload-artifact@", text.index(command) + len(command)) < 0:
            failures.append(f"{label} does not upload an artifact after the v0.1.49 secret scan")
    hosted = release_workflow[release_workflow.index("  evidence:") : release_workflow.index("  promote-images:")]
    scan = "python scripts/scan_v0149_release_secrets.py --path build/release-evidence"
    if not (hosted.index("python scripts/generate_release_evidence.py") < hosted.index(scan) < hosted.index("actions/upload-artifact@")):
        failures.append("v0.1.49 evidence scan is not ordered after assembly and before upload")
    for value in ('"local-dirty-tree"', '"dirty_local_non_publishable"', 'publishable = ($SourceTreeState -eq "clean")'):
        if value not in portable_builder:
            failures.append(f"portable build does not enforce dirty-tree provenance: {value}")
    for value in ("source_commit: local-dirty-tree", "source_tree_state: dirty_local_non_publishable", "publishable: false", "clean rebuild after the authorized release"):
        if value not in evidence_manifest:
            failures.append(f"v0.1.49 evidence manifest omits provenance requirement: {value}")
    agents = (repo / "AGENTS.md").read_bytes()
    if agents != (repo / "CLAUDE.md").read_bytes():
        failures.append("AGENTS.md and CLAUDE.md must be byte-identical")
    for value in ("2026-08-16", "latest public release is v0.1.48", "v0.1.49 is the current candidate"):
        if value not in agents.decode("utf-8"):
            failures.append(f"v0.1.49 handoff omits: {value}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.49 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
