#!/usr/bin/env python3
"""Static fail-closed contracts for v0.1.28 GHCR and hosted acceptance."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _require(text: str, tokens: list[str], *, label: str, failures: list[str]) -> None:
    for token in tokens:
        if token not in text:
            failures.append(f"{label} is missing {token!r}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release-gates.yml").read_text(encoding="utf-8")
    compose = (root / "infra/docker-compose.yml").read_text(encoding="utf-8")
    inline = (root / "infra/docker-compose.inline.yml").read_text(encoding="utf-8")
    acceptance = (root / "infra/docker-compose.acceptance.yml").read_text(encoding="utf-8")
    sync_acceptance = (root / "infra/docker-compose.sync-acceptance.yml").read_text(
        encoding="utf-8"
    )
    generator = (root / "scripts/generate_docker_image_evidence.py").read_text(encoding="utf-8")
    publisher = (root / "scripts/publish_docker_images.sh").read_text(encoding="utf-8")
    promoter = (root / "scripts/promote_docker_images.sh").read_text(encoding="utf-8")
    local_image_verifier = (root / "scripts/verify_local_docker_image.py").read_text(
        encoding="utf-8"
    )
    tag_verifier = (root / "scripts/verify_signed_release_tag.py").read_text(encoding="utf-8")
    windows_workflow = (root / ".github/workflows/windows-portable.yml").read_text(
        encoding="utf-8"
    )
    hosted_frontend = (root / "scripts/hosted_frontend_interaction.mjs").read_text(
        encoding="utf-8"
    )
    hosted_frontend_check = (root / "scripts/check_hosted_frontend.py").read_text(
        encoding="utf-8"
    )
    worker_tasks = (root / "worker/app/tasks.py").read_text(encoding="utf-8")
    deployment = (root / "docs/docker-deployment-rollback-v0.1.28.md").read_text(
        encoding="utf-8"
    )
    failures: list[str] = []

    for relative in ("backend/Dockerfile", "worker/Dockerfile", "frontend/Dockerfile"):
        dockerfile = (root / relative).read_text(encoding="utf-8")
        _require(
            dockerfile,
            [
                "ARG APP_VERSION=dev",
                "ARG SOURCE_COMMIT=unknown",
                "ARG SOURCE_REPOSITORY=https://github.com/Rvs006/smart-commissioning-app",
                "org.opencontainers.image.version",
                "org.opencontainers.image.revision",
                "org.opencontainers.image.source",
            ],
            label=relative,
            failures=failures,
        )

    frontend_dockerfile = (root / "frontend/Dockerfile").read_text(encoding="utf-8")
    _require(
        frontend_dockerfile,
        [
            "FROM node:24-alpine AS build\n\nARG APP_VERSION=dev",
            'ENV VITE_APP_VERSION="${APP_VERSION}"',
            "RUN npm run build",
        ],
        label="frontend Docker build stamp",
        failures=failures,
    )

    _require(
        compose,
        [
            '${API_IMAGE:-smart-commissioning-api:local}',
            '${WORKER_IMAGE:-smart-commissioning-worker:local}',
            '${FRONTEND_IMAGE:-smart-commissioning-frontend:local}',
            'APP_VERSION: "${APP_VERSION:-dev}"',
            'SOURCE_COMMIT: "${SOURCE_COMMIT:-unknown}"',
        ],
        label="hosted Compose",
        failures=failures,
    )
    _require(
        inline,
        ['JOB_EXECUTION_MODE: "inline"', 'INLINE_RUN_ASYNC: "1"', 'profiles: ["queue-only"]'],
        label="inline Compose override",
        failures=failures,
    )
    _require(
        acceptance,
        [
            "eclipse-mosquitto:2.0.22@sha256:",
            './mosquitto-acceptance.conf:/mosquitto/config/acceptance.conf:ro',
            'worker:\n    restart: "no"',
        ],
        label="acceptance Compose override",
        failures=failures,
    )
    _require(
        sync_acceptance,
        [
            "hub-postgres:",
            "hub-api:",
            'DEPLOYMENT_ROLE: "hub"',
            'AUTH_MODE: "api_key"',
            '127.0.0.1:18080:8000',
            "hub_postgres_data:/var/lib/postgresql/data",
            "hub_report_artifacts:/app/runtime/artifacts",
            "hub_report_signing:/app/runtime/report-signing",
        ],
        label="isolated Sync v2 acceptance Compose",
        failures=failures,
    )

    _require(
        generator,
        [
            'SCHEMA_VERSION = "1.0"',
            'ROLES = ("api", "worker", "frontend")',
            '"immutable_reference"',
            '"org.opencontainers.image.version"',
            '"org.opencontainers.image.revision"',
            '"org.opencontainers.image.source"',
        ],
        label="Docker evidence generator",
        failures=failures,
    )

    _require(
        workflow,
        [
            "name: v0.1.28 Release Gates",
            "packages: write",
            "if: github.event_name == 'workflow_dispatch'",
            "publish-images:",
            "promote-images:",
            "release-accepted-images-",
            "publish_docker_images.sh",
            "promote_docker_images.sh",
            "verify_signed_release_tag.py",
            'BUILDX_NO_DEFAULT_ATTESTATIONS: "1"',
            "verify_local_docker_image.py",
            "docker-image-evidence.json",
            "SYNC_V2_WIRE_FORMAT.md",
            "SYNC_V2_CREDENTIAL_SCOPE.md",
            "SYNC_V2_OPERATIONS.md",
            "DOCKER_DEPLOYMENT_ROLLBACK.md",
            "hosted_release_acceptance.py long-capture",
            "hosted_release_acceptance.py cancel-existing",
            "hosted_release_acceptance.py wait-worker-recovery",
            "hosted_release_acceptance.py deferred-create",
            "hosted_api_restart=passed",
            "hosted_database_interruption=passed",
            "smoke_sync_v2.py",
            "hosted_sync_v2_round_trip=passed",
            "hosted_frontend_interaction.mjs",
            '--expected-version "$RELEASE_VERSION"',
            "check_hosted_mtls.py",
            "check_hosted_terminal_evidence.py",
            "duplicate_delivery_before",
            "duplicate_delivery_observed",
            'test "$duplicate_delivery_observed" -eq 1',
            "Skipping duplicate or stale delivery run_id=$killed_run_id",
            "check_hosted_frontend.py",
            "check_hosted_release_logs.py",
            "check_v0128_release_contracts.py",
            "docker_sbom_from_digest=passed",
        ],
        label="release workflow",
        failures=failures,
    )
    _require(
        worker_tasks,
        ["Skipping duplicate or stale delivery run_id=%s"],
        label="run-specific duplicate worker observation",
        failures=failures,
    )
    _require(
        hosted_frontend,
        [
            'document.querySelector(\'[title="App version"]\')',
            "version_label: versionState.text",
            "version_visible: versionState.visible",
            "bounds.width > 0",
            "app_version: options.expectedVersion",
        ],
        label="hosted frontend visible version acceptance",
        failures=failures,
    )
    _require(
        hosted_frontend_check,
        [
            'parser.add_argument("--expected-version", required=True)',
            'report.get("app_version") != expected_version',
            'route.get("version_label") != expected_version',
            'route.get("version_visible") is not True',
        ],
        label="hosted frontend version evidence validation",
        failures=failures,
    )
    _require(
        publisher,
        [
            "candidate-image-ids.env",
            "docker buildx imagetools inspect",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "Unsupported or empty manifest mediaType",
            "verify_local_docker_image.py",
            "docker push",
            "docker pull",
            "candidate_id",
            "generate_docker_image_evidence.py",
            "publishes only immutable commit-SHA tags",
        ],
        label="Docker image publisher",
        failures=failures,
    )
    _require(
        local_image_verifier,
        [
            "ALLOWED_MANIFEST_MEDIA_TYPES",
            'rootfs.get("Type") != "layers"',
            'media_type = "docker-engine-single-platform"',
            "did not resolve to exactly one local image",
        ],
        label="local Docker image verifier",
        failures=failures,
    )
    for label, text in (("release workflow", workflow), ("Docker image publisher", publisher)):
        if 'index .Descriptor "mediaType"' in text:
            failures.append(f"{label} uses the non-portable Docker Descriptor template")
    _require(
        promoter,
        [
            "docker-image-evidence.json",
            "candidate-image-ids.env",
            "docker buildx imagetools inspect",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "Unsupported or empty manifest mediaType",
            "version_present",
            "docker push",
            "docker pull",
            "docker-image-promotion.txt",
        ],
        label="Docker image promoter",
        failures=failures,
    )
    _require(
        tag_verifier,
        [
            'ref_payload.get("ref") != f"refs/tags/{version}"',
            'ref_object.get("type") != "tag"',
            'target.get("type") != "commit"',
            'target.get("sha") != expected_commit',
            'verification.get("verified") is not True',
        ],
        label="signed annotated tag verifier",
        failures=failures,
    )
    workflow_preamble = workflow.split("jobs:", 1)[0]
    if "packages: write" in workflow_preamble:
        failures.append("PR-capable workflow permissions grant package write")
    if workflow.count("packages: write") != 2:
        failures.append("only SHA publication and version promotion may grant package write")
    if workflow.count("actions/checkout@") != workflow.count("persist-credentials: false"):
        failures.append("one or more release checkouts persist Git credentials")
    hosted_position = workflow.find("  hosted:")
    publish_position = workflow.find("  publish-images:")
    evidence_position = workflow.find("  evidence:")
    promote_position = workflow.find("  promote-images:")
    if not 0 <= hosted_position < publish_position < evidence_position < promote_position:
        failures.append("Docker publication phases are not ordered around evidence validation")
    if "needs: [evidence, publish-images]" not in workflow[promote_position:]:
        failures.append("version promotion does not wait for validated evidence")
    for line in workflow.splitlines():
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match is not None and re.fullmatch(r"[^@]+@[0-9a-f]{40}", match.group(1)) is None:
            failures.append(f"release workflow action is not pinned to a 40-character SHA: {match.group(1)}")

    windows_preamble = windows_workflow.split("jobs:", 1)[0]
    if "permissions:\n  contents: read" not in windows_preamble:
        failures.append("Windows portable workflow does not explicitly use read-only contents permission")
    if windows_workflow.count("actions/checkout@") != windows_workflow.count(
        "persist-credentials: false"
    ):
        failures.append("Windows portable checkout persists Git credentials")

    _require(
        deployment,
        [
            "docker-image-evidence.json",
            "API_IMAGE=ghcr.io/rvs006/smart-commissioning-app-api@sha256:",
            "up -d --no-build",
            "infra/docker-compose.inline.yml",
            "Do not delete volumes",
        ],
        label="Docker deployment guide",
        failures=failures,
    )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.28 Docker release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
