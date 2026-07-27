#!/usr/bin/env python3
"""Generate exact-commit release evidence and SHA-256 checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _statuses(
    parser: argparse.ArgumentParser,
    values: list[str],
    *,
    option: str,
) -> dict[str, str]:
    """Parse repeatable NAME=STATUS records without accepting duplicate names."""

    records: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            parser.error(f"invalid {option} {value!r}; expected NAME=STATUS")
        name, status = value.split("=", 1)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            parser.error(f"invalid {option} name {name!r}")
        if status not in {"passed", "failed", "skipped"}:
            parser.error(f"invalid {option} {value!r}")
        if name in records:
            parser.error(f"duplicate {option} name {name!r}")
        records[name] = status
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--sbom", action="append", type=Path, default=[])
    parser.add_argument("--product-version")
    parser.add_argument("--evidence-kind", choices=("windows", "hosted"))
    parser.add_argument("--repository", help="GitHub owner/repository")
    parser.add_argument("--workflow-artifact-name")
    parser.add_argument("--workflow-name")
    parser.add_argument("--workflow-run-id")
    parser.add_argument("--workflow-run-attempt", type=int)
    parser.add_argument("--workflow-run-url")
    parser.add_argument("--workflow-event")
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        metavar="NAME=STATUS",
        help="Record a test group such as backend_unittest=passed.",
    )
    parser.add_argument(
        "--gate",
        action="append",
        default=[],
        metavar="NAME=STATUS",
        help="Record a gate such as hosted_compose=passed.",
    )
    args = parser.parse_args(argv)

    version_match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", args.version)
    if version_match is None:
        parser.error("--version must look like v0.1.27")
    strict_v0127 = tuple(map(int, version_match.groups())) >= (0, 1, 27)
    commit = args.commit.lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        parser.error("--commit must be a full 40-character Git SHA")
    strict_options = {
        "--product-version": args.product_version,
        "--evidence-kind": args.evidence_kind,
        "--repository": args.repository,
        "--workflow-artifact-name": args.workflow_artifact_name,
        "--workflow-name": args.workflow_name,
        "--workflow-run-id": args.workflow_run_id,
        "--workflow-run-attempt": args.workflow_run_attempt,
        "--workflow-run-url": args.workflow_run_url,
        "--workflow-event": args.workflow_event,
    }
    if strict_v0127:
        for option, value in strict_options.items():
            if value is None:
                parser.error(f"{option} is required for v0.1.27 and later")
    if args.product_version is not None and args.product_version != args.version:
        parser.error("--product-version must equal --version")
    artifact_name = args.workflow_artifact_name
    if strict_v0127:
        assert args.repository is not None
        assert artifact_name is not None
        assert args.workflow_run_id is not None
        assert args.workflow_run_attempt is not None
        if re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+",
            args.repository,
        ) is None:
            parser.error("--repository must be a GitHub owner/repository slug")
        if (
            not artifact_name
            or artifact_name != artifact_name.strip()
            or len(artifact_name) > 255
            or "/" in artifact_name
            or "\\" in artifact_name
            or any(ord(character) < 32 for character in artifact_name)
        ):
            parser.error("--workflow-artifact-name is invalid")
        if re.fullmatch(r"[1-9]\d*", args.workflow_run_id) is None:
            parser.error("--workflow-run-id must be a positive integer")
        if args.workflow_run_attempt < 1:
            parser.error("--workflow-run-attempt must be positive")
        expected_run_url = (
            f"https://github.com/{args.repository}/actions/runs/{args.workflow_run_id}"
        )
        if args.workflow_run_url != expected_run_url:
            parser.error(
                "--workflow-run-url must exactly match --repository and --workflow-run-id"
            )

    gates = _statuses(parser, args.gate, option="--gate")
    tests = _statuses(parser, args.test, option="--test")
    if strict_v0127 and not gates:
        parser.error("at least one --gate is required")
    if strict_v0127 and not tests:
        parser.error("at least one --test is required")

    files: list[dict[str, object]] = []
    names: set[str] = set()
    for kind, paths in (("artifact", args.artifact), ("sbom", args.sbom)):
        for raw_path in paths:
            path = raw_path.resolve()
            if not path.is_file():
                parser.error(f"{kind} does not exist: {path}")
            if strict_v0127 and path.stat().st_size <= 0:
                parser.error(f"{kind} is empty: {path}")
            if path.name in {"release-evidence.json", "SHA256SUMS.txt"}:
                parser.error(f"reserved release-evidence filename: {path.name}")
            if path.name in names:
                parser.error(f"duplicate release-evidence filename: {path.name}")
            names.add(path.name)
            files.append(
                {
                    "kind": kind,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    if strict_v0127 and not files:
        parser.error("at least one --artifact or --sbom is required")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "release-evidence.json"
    if strict_v0127:
        evidence = {
            "schema_version": "1.1",
            "evidence_kind": args.evidence_kind,
            "release_version": args.version,
            "product_version": args.product_version,
            "source_commit": commit,
            "generated_at": datetime.now(UTC).isoformat(),
            "workflow": {
                "name": args.workflow_name,
                "repository": args.repository,
                "artifact_name": artifact_name,
                "run_id": args.workflow_run_id,
                "run_attempt": args.workflow_run_attempt,
                "run_url": args.workflow_run_url,
                "event": args.workflow_event,
            },
            "gates": gates,
            "tests": tests,
            "files": sorted(
                files, key=lambda item: (str(item["kind"]), str(item["name"]))
            ),
        }
    else:
        evidence = {
            "schema_version": "1.0",
            "release_version": args.version,
            "source_commit": commit,
            "generated_at": datetime.now(UTC).isoformat(),
            "gates": gates,
            "files": sorted(
                files, key=lambda item: (str(item["kind"]), str(item["name"]))
            ),
        }
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    checksum_entries = [*files, {
        "kind": "evidence",
        "name": evidence_path.name,
        "size": evidence_path.stat().st_size,
        "sha256": sha256(evidence_path),
    }]
    checksums_path = output_dir / "SHA256SUMS.txt"
    lines = [
        f"{item['sha256']}  {item['name']}"
        for item in sorted(checksum_entries, key=lambda item: str(item["name"]))
    ]
    checksums_path.write_text("\n".join(lines) + "\n", encoding="ascii")

    print(evidence_path)
    print(checksums_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
