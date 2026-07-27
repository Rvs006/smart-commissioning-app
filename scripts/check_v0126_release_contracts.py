#!/usr/bin/env python3
"""Static release-contract checks for v0.1.26 deployment and packaging.

This deliberately uses the standard library. It runs on developer machines
that do not have Docker or a YAML parser, while ``docker compose config``
remains the authoritative syntax check in CI and the release runbook.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


def _require(text: str, pattern: str, description: str, failures: list[str]) -> None:
    if re.search(pattern, text, flags=re.MULTILINE | re.DOTALL) is None:
        failures.append(description)


def check(repo: Path) -> list[str]:
    failures: list[str] = []

    compose = (repo / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    _require(compose, r'JOB_EXECUTION_MODE:\s*["\']?queue', "hosted mode is not queue-only", failures)
    _require(
        compose,
        r'SMART_COMMISSIONING_SECRETS_ROOT:\s*["\']?/app/runtime/secrets',
        "hosted API and worker do not share one secrets-root setting",
        failures,
    )
    _require(
        compose,
        r"secret_store:/app/runtime/secrets:ro",
        "worker secret store is not mounted read-only",
        failures,
    )
    _require(
        compose,
        r"report_signing:/app/runtime/report-signing",
        "API-only report signing volume is missing",
        failures,
    )
    worker_block = compose.split("\n  worker:", 1)[-1].split("\n  postgres:", 1)[0]
    if "report_signing:" in worker_block:
        failures.append("worker must not mount the API report-signing volume")
    if "report_artifacts:" in worker_block:
        failures.append("worker must not mount the API report-artifact volume")
    for fragment, description in (
        (r"alembic_version", "worker readiness does not prove schema access"),
        (r"\.secret_store_key", "worker readiness does not prove secret-key access"),
        (r"bacpypes3", "worker readiness does not prove BACnet import"),
    ):
        _require(compose, fragment, description, failures)

    worker_dockerfile = (repo / "worker" / "Dockerfile").read_text(encoding="utf-8")
    _require(
        worker_dockerfile,
        r'["\']?\./core\[bacnet\]["\']?',
        "hosted worker image does not install the BACnet extra",
        failures,
    )

    launcher_path = repo / "packaging" / "windows_portable" / "run_smart_commissioning_app.py"
    launcher = launcher_path.read_text(encoding="utf-8")
    try:
        ast.parse(launcher, filename=str(launcher_path))
    except SyntaxError as error:
        failures.append(f"portable launcher is not valid Python: {error}")
    for fragment, description in (
        (
            'SMART_COMMISSIONING_RUNTIME_ROOT"] = str(runtime_root)',
            "portable runtime root is not stable",
        ),
        (
            'SMART_COMMISSIONING_SECRETS_ROOT"] = str(runtime_root / "secrets")',
            "portable secrets are outside runtime root",
        ),
        (
            'SMART_COMMISSIONING_ARTIFACTS_ROOT"] = str(runtime_root / "artifacts")',
            "portable artifacts are outside runtime root",
        ),
        (
            'SMART_COMMISSIONING_REPORT_SIGNING_ROOT"] = str(',
            "portable report signing is not anchored below the runtime root",
        ),
        ('"JOB_EXECUTION_MODE", "inline"', "portable execution is not inline"),
    ):
        if fragment not in launcher:
            failures.append(description)

    settings = (repo / "backend" / "app" / "core" / "config.py").read_text(encoding="utf-8")
    dispatcher = (repo / "backend" / "app" / "services" / "run_dispatch.py").read_text(encoding="utf-8")
    for name, text in (
        ("Compose", compose),
        ("portable launcher", launcher),
        ("settings", settings),
        ("dispatcher", dispatcher),
    ):
        if "ALLOW_INLINE_WORKER_FALLBACK" in text or "allow_inline_worker_fallback" in text:
            failures.append(f"{name} still exposes the removed queue-to-inline fallback")
    _require(
        dispatcher,
        r"except JobQueueUnavailable.*pending automatic retry",
        "queue publication failure is not retained as a durable pending dispatch",
        failures,
    )

    required_docs = (
        "docs/migration-rollback-v0.1.26.md",
        "docs/mqtt-client-id-and-acl.md",
        "docs/release-notes-v0.1.26.md",
        "docs/release-validation-v0.1.26.md",
        "docs/sync-v2-v0.1.27.md",
    )
    for relative in required_docs:
        if not (repo / relative).is_file():
            failures.append(f"required release document is missing: {relative}")

    release_workflow = repo / ".github" / "workflows" / "release-gates.yml"
    if not release_workflow.is_file():
        failures.append("exact-SHA hosted release workflow is missing")
    else:
        workflow = release_workflow.read_text(encoding="utf-8")
        for fragment, description in (
            ("release_sha", "release workflow has no explicit SHA input"),
            ("git rev-parse HEAD", "release workflow does not verify checkout SHA"),
            ("commits/$RELEASE_SHA/pulls", "release workflow does not bind the SHA to a merged PR"),
            ("docker compose", "release workflow does not validate hosted Compose"),
            ("backup_rollback_smoke.py", "release workflow does not smoke backup/rollback"),
            ("SBOM.image-worker.cdx.json", "release workflow omits hosted image SBOMs"),
            ("http://127.0.0.1:8080/", "release workflow does not smoke the hosted frontend"),
        ):
            if fragment not in workflow:
                failures.append(description)
        _require(
            workflow,
            r"uses:\s*anchore/sbom-action@[0-9a-f]{40}\b",
            "release workflow does not invoke Anchore from an immutable commit",
            failures,
        )
        for line_number, line in enumerate(workflow.splitlines(), start=1):
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match is None or match.group(1).startswith("./"):
                continue
            action = match.group(1)
            if "@" not in action or re.fullmatch(
                r"[0-9a-f]{40}", action.rsplit("@", 1)[1]
            ) is None:
                failures.append(
                    f"release workflow line {line_number} uses a mutable action ref"
                )

    publisher = (repo / "scripts" / "release-portable.ps1").read_text(encoding="utf-8")
    for fragment, description in (
        ("ReleaseGateRunId", "publisher does not require hosted release-gate evidence"),
        ("Get-ReleaseGateRunInfo", "publisher does not bind hosted evidence to the release SHA"),
        ("release-evidence.windows.json", "publisher does not preserve Windows evidence"),
        ("SBOM.image-worker.cdx.json", "publisher does not attach hosted image SBOMs"),
    ):
        if fragment not in publisher:
            failures.append(description)

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    failures = check(args.repo.resolve())
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.26 deployment and release contracts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
