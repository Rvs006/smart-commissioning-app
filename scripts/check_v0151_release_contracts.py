#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.51 release paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import check_v0128_release_contracts as base


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = base.check(
        repo,
        version="v0.1.51",
        package_version="0.1.51",
        release_notes_path="docs/release-notes-v0.1.51.md",
        migration_path="docs/migration-rollback-v0.1.51.md",
        validation_path="docs/release-validation-v0.1.51.md",
        docker_deployment_path="docs/docker-deployment-rollback-v0.1.51.md",
        evidence_validator="validate_v0151_release_evidence.py",
        evidence_test="test_v0151_release_evidence.py",
        release_contract_script="check_v0151_release_contracts.py",
    )
    required = (
        "scripts/scan_v0151_release_secrets.py",
        "scripts/test_v0151_security_scan.py",
        "scripts/test_v0151_version_identity.py",
        "docs/v0.1.51-evidence-manifest.md",
        "docs/v0.1.51-field-acceptance-checklist.md",
    )
    for relative in required:
        if not (repo / relative).is_file():
            failures.append(f"required v0.1.51 safety file is missing: {relative}")
    release_workflow = (repo / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    windows_workflow = (repo / ".github/workflows/windows-portable.yml").read_text(encoding="utf-8")
    portable_builder = (repo / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")
    evidence_manifest = (repo / "docs/v0.1.51-evidence-manifest.md").read_text(encoding="utf-8")
    field_checklist = (repo / "docs/v0.1.51-field-acceptance-checklist.md").read_text(
        encoding="utf-8"
    )
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    release_notes = (repo / "docs/release-notes-v0.1.51.md").read_text(encoding="utf-8")
    brief = (repo / "frontend/src/features/brief/BriefPage.tsx").read_text(encoding="utf-8")
    learning = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo / "frontend/src/features/learning").glob("*.tsx"))
        if not path.name.endswith(".test.tsx")
    )
    hosted_interaction = (repo / "scripts/hosted_frontend_interaction.mjs").read_text(
        encoding="utf-8"
    )
    hosted_checker = (repo / "scripts/check_hosted_frontend.py").read_text(encoding="utf-8")
    brief_text = " ".join(brief.split())
    learning_text = " ".join(learning.split())
    field_checklist_text = " ".join(field_checklist.split())
    for text, label, command in (
        (release_workflow, "release workflow", "python scripts/scan_v0151_release_secrets.py --path build/release-evidence"),
        (windows_workflow, "Windows workflow", "python scripts/scan_v0151_release_secrets.py --path build/Smart_Commissioning_App_Windows_Portable"),
    ):
        if command not in text:
            failures.append(f"{label} does not execute the v0.1.51 repository secret scan")
        elif text.find("actions/upload-artifact@", text.index(command) + len(command)) < 0:
            failures.append(f"{label} does not upload an artifact after the v0.1.51 secret scan")
    hosted = release_workflow[release_workflow.index("  evidence:") : release_workflow.index("  promote-images:")]
    scan = "python scripts/scan_v0151_release_secrets.py --path build/release-evidence"
    if not (hosted.index("python scripts/generate_release_evidence.py") < hosted.index(scan) < hosted.index("actions/upload-artifact@")):
        failures.append("v0.1.51 evidence scan is not ordered after assembly and before upload")
    for value in ('"local-dirty-tree"', '"dirty_local_non_publishable"', 'publishable = ($SourceTreeState -eq "clean")'):
        if value not in portable_builder:
            failures.append(f"portable build does not enforce dirty-tree provenance: {value}")
    for value in ("source_commit: local-dirty-tree", "source_tree_state: dirty_local_non_publishable", "publishable: false", "clean rebuild after the authorized release"):
        if value not in evidence_manifest:
            failures.append(f"v0.1.51 evidence manifest omits provenance requirement: {value}")
    for value in (
        "How a protected discovery run works",
        "matching sealed preview authorization",
        "authoritatively terminal",
        "same scan can be retried safely",
    ):
        if value not in brief_text:
            failures.append(f"v0.1.51 Brief omits required guidance: {value}")
    for value in (
        "Run an IP discovery",
        "Retry an IP discovery safely",
        "Final evidence is synchronising",
        "Final evidence unavailable",
        "Queued",
        "Running",
        "Succeeded",
        "Failed",
        "Cancelled",
        "This release does not add BACnet write capability",
        "blank Payload type is valid",
        "does not bundle, install, or download Nmap or Npcap",
        "An administrator must create and approve the scan authorization",
        "If you are not an administrator, ask one to approve it",
        "Troubleshooting",
    ):
        if value not in learning_text:
            failures.append(f"v0.1.51 Learning omits required guidance: {value}")
    public_guidance = f"{brief}\n{learning}"
    private_template_patterns = (
        re.compile(r"\b(?:mqtt|bacnet)[A-Za-z0-9_. -]{0,120}\.(?:html?|zip)\b", re.IGNORECASE),
        re.compile(
            r"\b[A-Za-z0-9_-]*(?:(?:template|version)[A-Za-z0-9_-]*\d{6,8}|"
            r"\d{6,8}[A-Za-z0-9_-]*(?:template|version))[A-Za-z0-9_-]*\b",
            re.IGNORECASE,
        ),
    )
    if any(pattern.search(public_guidance) for pattern in private_template_patterns):
        failures.append("public in-app guidance contains a private-looking template identifier")
    for value in (
        "Complete an explicitly approved UDMI validation run",
        "Generate both the condensed client report and the technical report",
        "that exact UDMI validation run and evidence set",
        "technical payload-manifest hashes",
        "expected redaction state",
        "condensed client report SHA-256",
        "technical report SHA-256",
    ):
        if value not in field_checklist_text:
            failures.append(f"v0.1.51 field checklist omits report evidence: {value}")
    udmi_validation = field_checklist.find("Complete an explicitly approved UDMI validation run")
    report_pair = field_checklist.find(
        "Generate both the condensed client report and the technical report"
    )
    if udmi_validation < 0 or report_pair < 0 or udmi_validation >= report_pair:
        failures.append(
            "v0.1.51 field checklist does not complete the approved UDMI run before report generation"
        )
    changelog_v0151 = changelog[changelog.index("## [0.1.51]") : changelog.index("## [0.1.50]")]
    if "SCT_REQUIRE_SCAN_AUTHORIZATION=0" not in changelog_v0151:
        failures.append("v0.1.51 changelog omits the frictionless authorization mode")
    if "SCT_REQUIRE_SCAN_AUTHORIZATION=0" not in release_notes:
        failures.append("v0.1.51 release notes omit the frictionless authorization mode")
    for value in (
        "--route '/#/brief'",
        "--route '/#/learning'",
        "--page build/browser/brief.html",
        "--page build/browser/learning.html",
    ):
        if value not in release_workflow:
            failures.append(f"release workflow omits Brief/Learning browser coverage: {value}")
    for value in (
        'name: "desktop", width: 1280',
        'name: "mobile", width: 375',
        "Product brief sections",
        "Learning setup and roles",
        "exerciseThemeToggle",
        "assertNoHorizontalOverflow",
        "clippedElements",
        "clipped_content",
        "assertClippingDetectorCatchesFixture",
        "clipping_negative_fixture",
    ):
        if value not in hosted_interaction:
            failures.append(f"hosted browser gate omits guidance interaction proof: {value}")
    if "complete guidance control set" not in hosted_checker:
        failures.append("hosted browser checker does not require every guidance control")
    if "frontend\\dist" not in portable_builder:
        failures.append("portable build does not bundle the compiled Brief and Learning source")
    for value in (
        "Prove packaged Brief and Learning guidance",
        "How a protected discovery run works",
        "Operator guides for protected scans and evidence",
        "Smart_Commissioning_App_Windows_Portable.zip",
    ):
        if value not in windows_workflow:
            failures.append(f"Windows workflow omits packaged guidance proof: {value}")
    agents = (repo / "AGENTS.md").read_bytes()
    if agents != (repo / "CLAUDE.md").read_bytes():
        failures.append("AGENTS.md and CLAUDE.md must be byte-identical")
    handoff = " ".join(agents.decode("utf-8").lower().split())
    for value in ("2026-08-21", "latest public release is v0.1.50", "v0.1.51 is the current candidate"):
        if value not in handoff:
            failures.append(f"v0.1.51 handoff omits: {value}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.51 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
