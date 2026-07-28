#!/usr/bin/env python3
"""Validate v0.1.28 evidence, including immutable GHCR image provenance."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import validate_v0127_release_evidence as base

V0128_HOSTED_FILES = {
    "docker-image-evidence.json": "artifact",
    "SYNC_V2_WIRE_FORMAT.md": "artifact",
    "SYNC_V2_CREDENTIAL_SCOPE.md": "artifact",
    "SYNC_V2_OPERATIONS.md": "artifact",
    "DOCKER_DEPLOYMENT_ROLLBACK.md": "artifact",
}


def _find_argument(argv: list[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _validate_docker_images(path: Path, version: str, commit: str, repository: str) -> list[str]:
    failures: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"docker-image-evidence.json is invalid: {error}"]
    if not isinstance(payload, dict):
        return ["docker-image-evidence.json root is not an object"]
    expected_root_keys = {
        "schema_version",
        "release_version",
        "source_commit",
        "registry",
        "images",
    }
    if set(payload) != expected_root_keys:
        failures.append("Docker image evidence root contains missing or unknown fields")
    expected = {
        "schema_version": "1.0",
        "release_version": version,
        "source_commit": commit,
        "registry": "ghcr.io",
    }
    for name, value in expected.items():
        if payload.get(name) != value:
            failures.append(f"Docker image evidence {name} does not match")
    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != {"api", "worker", "frontend"}:
        return [*failures, "Docker image evidence must contain exactly api, worker, and frontend"]
    expected_base = f"ghcr.io/{repository.lower()}"
    seen_names: set[str] = set()
    for role in ("api", "worker", "frontend"):
        image = images.get(role)
        if not isinstance(image, dict):
            failures.append(f"Docker image evidence {role} is not an object")
            continue
        if set(image) != {"name", "digest", "immutable_reference", "labels"}:
            failures.append(
                f"Docker image evidence {role} contains missing or unknown fields"
            )
        name = image.get("name")
        digest = image.get("digest")
        if not isinstance(name, str) or re.fullmatch(
            r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+", name
        ) is None:
            failures.append(f"Docker image evidence {role} name is invalid")
            continue
        expected_name = f"{expected_base}-{role}"
        if name != expected_name:
            failures.append(
                f"Docker image evidence {role} name must be the canonical {expected_name}"
            )
        if name in seen_names:
            failures.append(f"Docker image evidence {role} reuses another role's image name")
        seen_names.add(name)
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            failures.append(f"Docker image evidence {role} digest is invalid")
        if image.get("immutable_reference") != f"{name}@{digest}":
            failures.append(f"Docker image evidence {role} immutable reference is invalid")
        labels = image.get("labels")
        if not isinstance(labels, dict):
            failures.append(f"Docker image evidence {role} labels are missing")
            continue
        if set(labels) != {
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
        }:
            failures.append(
                f"Docker image evidence {role} labels contain missing or unknown fields"
            )
        if labels.get("org.opencontainers.image.version") != version:
            failures.append(f"Docker image evidence {role} version label does not match")
        if labels.get("org.opencontainers.image.revision") != commit:
            failures.append(f"Docker image evidence {role} revision label does not match")
        if labels.get("org.opencontainers.image.source") != f"https://github.com/{repository}":
            failures.append(f"Docker image evidence {role} source label does not match")
    return failures


def main_for_version(
    argv: list[str] | None,
    *,
    expected_version: str,
    success_label: str,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--evidence-kind", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    known, _ = parser.parse_known_args(arguments)
    if known.version != expected_version:
        parser.error(f"this validator is pinned to {expected_version}")
    commit = known.commit.lower()
    if known.evidence_kind == "hosted":
        base.REQUIRED_FILES["hosted"] = {
            **base.REQUIRED_FILES["hosted"],
            **V0128_HOSTED_FILES,
        }
        roots = [*known.search_root, known.evidence.parent]
        matches = [
            root.resolve() / "docker-image-evidence.json"
            for root in roots
            if (root.resolve() / "docker-image-evidence.json").is_file()
        ]
        unique = {str(path.resolve()): path.resolve() for path in matches}
        if len(unique) != 1:
            print(
                "FAIL: docker-image-evidence.json is missing or ambiguous across search roots",
                file=sys.stderr,
            )
            return 1
        failures = _validate_docker_images(
            next(iter(unique.values())), known.version, commit, known.repository
        )
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
    result = base.main(arguments)
    if result == 0:
        print(success_label)
    return result


def main(argv: list[str] | None = None) -> int:
    return main_for_version(
        argv,
        expected_version="v0.1.28",
        success_label="v0.1.28 Docker and release evidence: OK",
    )


if __name__ == "__main__":
    raise SystemExit(main())
