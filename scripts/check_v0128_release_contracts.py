#!/usr/bin/env python3
"""Fail-closed static contracts for the v0.1.28 release paths."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

VERSION = "v0.1.28"
PACKAGE_VERSION = "0.1.28"


def _require(text: str, value: str, message: str, failures: list[str]) -> None:
    if value not in text:
        failures.append(message)


def _read(repo: Path, relative: str, failures: list[str]) -> str:
    path = repo / relative
    if not path.is_file():
        failures.append(f"required release file is missing: {relative}")
        return ""
    return path.read_text(encoding="utf-8")


def _action_pins(workflow: str, name: str, failures: list[str]) -> None:
    count = 0
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match is None or match.group(1).startswith("./"):
            continue
        count += 1
        action = match.group(1)
        if "@" not in action or re.fullmatch(r"[0-9a-f]{40}", action.rsplit("@", 1)[1]) is None:
            failures.append(f"{name}:{line_number} action is not pinned to a full commit SHA")
    if count == 0:
        failures.append(f"{name} contains no third-party action pins")


def _exact_assignment(
    text: str, variable: str, expected: str, source: str, failures: list[str]
) -> None:
    matches = re.findall(
        rf'(?m)^{re.escape(variable)}\s*=\s*["\']([^"\']+)["\']\s*$', text
    )
    if matches != [expected]:
        failures.append(f"{source} must assign {variable} exactly once to {expected}")


def check(repo: Path) -> list[str]:
    failures: list[str] = []
    windows = _read(repo, ".github/workflows/windows-portable.yml", failures)
    hosted = _read(repo, ".github/workflows/release-gates.yml", failures)
    publisher = _read(repo, "scripts/release-portable.ps1", failures)
    image_publisher = _read(repo, "scripts/publish_docker_images.sh", failures)
    release_notes = _read(repo, "docs/release-notes-v0.1.28.md", failures)
    migration = _read(repo, "docs/migration-rollback-v0.1.28.md", failures)
    validation = _read(repo, "docs/release-validation-v0.1.28.md", failures)

    for relative in (
        "docs/sync-v2-wire-format.md",
        "docs/sync-v2-credential-scope.md",
        "docs/sync-v2-operations.md",
        "docs/docker-deployment-rollback-v0.1.28.md",
        "scripts/validate_v0128_release_evidence.py",
        "scripts/test_v0128_release_evidence.py",
        "scripts/generate_docker_image_evidence.py",
        "scripts/test_generate_docker_image_evidence.py",
        "scripts/test_release_portable_ps51.ps1",
        "core/alembic/versions/a7b8c9d0e1f2_sync_v2_immutable_evidence.py",
    ):
        _read(repo, relative, failures)

    for name, text in (("Windows workflow", windows), ("release gates", hosted)):
        _require(text, "pull_request:", f"{name} has no PR-head candidate path", failures)
        _require(
            text,
            "github.event.pull_request.head.sha",
            f"{name} is not bound to the PR head SHA",
            failures,
        )
        _require(text, "git rev-parse origin/main", f"{name} lacks exact-main dispatch proof", failures)
        _action_pins(text, name, failures)

    for value, message in (
        ("default: v0.1.28", "Windows workflow is not pinned to v0.1.28"),
        ("test_owned_run_heartbeat.py", "Windows workflow omits the shared heartbeat guard"),
        ("test_tasks_interrupt.py", "Windows workflow omits worker lifecycle parity"),
        ('- "worker/**"', "Windows workflow paths omit worker changes"),
        ("docs\\migration-rollback-v0.1.28.md", "Windows bundle omits v0.1.28 migration guidance"),
        ("validate_v0128_release_evidence.py", "Windows workflow uses the wrong evidence validator"),
        ("windows-acceptance.json", "Windows workflow omits retained portable acceptance"),
        ("ProductVersion", "Windows workflow does not verify ProductVersion"),
        (
            "test_release_portable_ps51.ps1",
            "Windows workflow omits the PowerShell 5.1 release-body regression",
        ),
    ):
        _require(windows, value, message, failures)

    for value, message in (
        ("name: v0.1.28 Release Gates", "release workflow has the wrong name"),
        ("packages: write", "release dispatch cannot publish GHCR packages"),
        ("github.event_name == 'workflow_dispatch'", "GHCR publication is not dispatch-only"),
        ("ghcr.io", "release gates do not target GHCR"),
        ("docker-image-evidence.json", "release gates omit Docker digest evidence"),
        ("generate_docker_image_evidence.py", "release gates do not verify image evidence"),
        ("SBOM.image-api.cdx.json", "release gates omit the API image SBOM"),
        ("SBOM.image-worker.cdx.json", "release gates omit the worker image SBOM"),
        ("SBOM.image-frontend.cdx.json", "release gates omit the frontend image SBOM"),
        ("org.opencontainers.image.revision", "release gates do not inspect OCI revision labels"),
        ("hosted_queue_smoke", "release gates omit queued execution"),
        ("hosted_inline", "release gates omit inline execution"),
        ("worker_recovery", "release gates omit worker termination recovery"),
        ("redis_interruption", "release gates omit Redis interruption"),
        ("sync_v2", "release gates omit Sync v2 acceptance"),
        ("frontend", "release gates omit frontend acceptance"),
        ("down -v", "release gates do not clean the disposable Compose project"),
        ("validate_v0128_release_evidence.py", "release gates use the wrong evidence validator"),
        ("SYNC_V2_WIRE_FORMAT.md", "hosted evidence omits the Sync v2 wire document"),
        ("SYNC_V2_CREDENTIAL_SCOPE.md", "hosted evidence omits credential scope guidance"),
        ("SYNC_V2_OPERATIONS.md", "hosted evidence omits receipt and compatibility guidance"),
        ("DOCKER_DEPLOYMENT_ROLLBACK.md", "hosted evidence omits Docker rollback guidance"),
        ('- "docs/release-notes-v0.1.28.md"', "release workflow paths omit release notes"),
        ('- "docs/release-validation-v0.1.28.md"', "release workflow paths omit validation guidance"),
    ):
        _require(f"{hosted}\n{image_publisher}", value, message, failures)
    if windows.count("if ($LASTEXITCODE -ne 0)") < 8:
        failures.append("Windows workflow can mask an earlier native-command failure")

    for value, message in (
        ('$ReleaseGateWorkflowName = "$Version Release Gates"', "publisher does not bind the versioned workflow name"),
        ("Test-DockerImageEvidence", "publisher does not verify immutable image evidence"),
        ("docker-image-evidence.json", "publisher omits Docker image evidence"),
        ("SYNC_V2_WIRE_FORMAT.md", "publisher omits Sync v2 wire documentation"),
        ("SYNC_V2_CREDENTIAL_SCOPE.md", "publisher omits credential scope documentation"),
        ("SYNC_V2_OPERATIONS.md", "publisher omits receipt and compatibility documentation"),
        ("DOCKER_DEPLOYMENT_ROLLBACK.md", "publisher omits Docker deployment guidance"),
        ("Assert-SignedAnnotatedTag", "publisher does not verify the signed tag"),
        ("Assert-CleanReleaseRepository", "publisher does not require a clean tracked worktree"),
        ("Assert-ReleaseCheckoutAtCommit", "publisher does not bind HEAD and publisher bytes to the release commit"),
        ("Assert-TrackedReleaseNotes", "publisher does not bind notes to the exact release commit"),
        ("-cne '.github/workflows/windows-portable.yml'", "publisher accepts a noncanonical Windows workflow path"),
        ("-cne '.github/workflows/release-gates.yml'", "publisher accepts a noncanonical release-gates path"),
        ("Published Windows zip is not byte-identical", "publisher does not compare public and workflow Windows bytes"),
        ("claimedHostedFiles", "publisher does not compare hosted workflow and public bytes"),
        ('Token = "{{$upperRole`_IMAGE}}"', "publisher does not construct image-name note tokens"),
        ('Token = "{{$upperRole`_IMAGE_DIGEST}}"', "publisher does not construct image-digest note tokens"),
        ("$Template.Contains", "publisher does not require every release-note token before replacement"),
        ("ExpectedImageReferences", "publisher does not require exact immutable image references in release bodies"),
        ("Get-TrackedReleaseNotesTemplate", "VerifyExisting does not reconstruct reviewed notes from the signed tag"),
        ("Assert-ReleaseViewUnchanged", "VerifyExisting lacks a final release-state TOCTOU check"),
        ("duplicate or case-colliding entry names", "publisher does not reject ambiguous ZIP entries"),
        ("dot-segment entry name", "publisher does not reject ZIP traversal paths"),
    ):
        _require(publisher, value, message, failures)

    for token in (
        "{{COMMIT}}",
        "{{EXE_SHA256}}",
        "{{ZIP_SHA256}}",
        "{{API_IMAGE}}",
        "{{API_IMAGE_DIGEST}}",
        "{{WORKER_IMAGE}}",
        "{{WORKER_IMAGE_DIGEST}}",
        "{{FRONTEND_IMAGE}}",
        "{{FRONTEND_IMAGE_DIGEST}}",
    ):
        _require(release_notes, token, f"release notes omit publisher token {token}", failures)

    for value, message in (
        ("a7b8c9d0e1f2", "migration guide omits the v0.1.28 head"),
        ("f6a7b8c9d0e1", "migration guide omits the v0.1.27 rollback head"),
        ("sync_credentials", "migration guide omits credential storage"),
        ("sync_delivery_state", "migration guide omits delivery state"),
        ("downgrade", "migration guide omits downgrade instructions"),
        ("Mixed-version", "migration guide omits mixed-version operation"),
    ):
        _require(migration, value, message, failures)
    _require(validation, "Every unchecked row blocks publication", "validation is not fail-closed", failures)

    for relative in ("core/pyproject.toml", "backend/pyproject.toml", "worker/pyproject.toml"):
        text = _read(repo, relative, failures)
        try:
            package = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            failures.append(f"{relative} is not valid TOML: {error}")
            continue
        if package.get("project", {}).get("version") != PACKAGE_VERSION:
            failures.append(f"{relative} project.version is not exactly {PACKAGE_VERSION}")

    frontend_package_text = _read(repo, "frontend/package.json", failures)
    frontend_lock_text = _read(repo, "frontend/package-lock.json", failures)
    try:
        frontend_package = json.loads(frontend_package_text)
    except json.JSONDecodeError as error:
        failures.append(f"frontend/package.json is not valid JSON: {error}")
        frontend_package = {}
    try:
        frontend_lock = json.loads(frontend_lock_text)
    except json.JSONDecodeError as error:
        failures.append(f"frontend/package-lock.json is not valid JSON: {error}")
        frontend_lock = {}
    if frontend_package.get("version") != PACKAGE_VERSION:
        failures.append("frontend/package.json version is not exactly 0.1.28")
    if (
        frontend_lock.get("version") != PACKAGE_VERSION
        or frontend_lock.get("packages", {}).get("", {}).get("version") != PACKAGE_VERSION
    ):
        failures.append("frontend/package-lock.json root versions are not exactly 0.1.28")

    version_assignments = (
        ("core/smart_commissioning_core/__init__.py", "__version__"),
        ("backend/app/main.py", "APP_VERSION"),
        ("backend/app/services/run_context_builder.py", "_APP_VERSION"),
        ("backend/app/services/report_artifacts.py", "REPORT_RENDERER_VERSION"),
    )
    assignment_sources: dict[str, str] = {}
    for relative, variable in version_assignments:
        source = _read(repo, relative, failures)
        assignment_sources[relative] = source
        _exact_assignment(source, variable, PACKAGE_VERSION, relative, failures)
    main_source = assignment_sources["backend/app/main.py"]
    _require(main_source, "version=APP_VERSION", "FastAPI does not use the exact APP_VERSION", failures)
    _require(
        main_source,
        '"version": app.version or APP_VERSION',
        "health metadata does not use the exact APP_VERSION fallback",
        failures,
    )

    for dockerfile in ("backend/Dockerfile", "worker/Dockerfile", "frontend/Dockerfile"):
        text = _read(repo, dockerfile, failures)
        for label in (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
        ):
            _require(text, label, f"{dockerfile} omits {label}", failures)

    return failures


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    failures = check(repo)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.28 release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
