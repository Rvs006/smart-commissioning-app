#!/usr/bin/env python3
"""Record immutable GHCR image digests and OCI labels from pulled images."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROLES = ("api", "worker", "frontend")
SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9_.-]+/[a-z0-9_.-]+")


class ImageEvidenceError(RuntimeError):
    """Raised when a registry reference or pulled image cannot prove provenance."""


def _parse_role_values(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ImageEvidenceError(f"invalid --image {value!r}; expected ROLE=NAME@DIGEST")
        role, reference = value.split("=", 1)
        if role not in ROLES:
            raise ImageEvidenceError(f"unsupported image role {role!r}")
        if role in parsed:
            raise ImageEvidenceError(f"duplicate image role {role!r}")
        parsed[role] = reference
    if set(parsed) != set(ROLES):
        missing = ", ".join(sorted(set(ROLES) - set(parsed))) or "none"
        raise ImageEvidenceError(f"images must contain exactly api, worker, frontend; missing {missing}")
    return parsed


def _split_reference(reference: str, *, registry: str) -> tuple[str, str]:
    if reference.count("@") != 1:
        raise ImageEvidenceError(f"image reference is not immutable: {reference!r}")
    name, digest = reference.split("@", 1)
    if _NAME_RE.fullmatch(name) is None or not name.startswith(f"{registry}/"):
        raise ImageEvidenceError(f"image name is outside {registry}: {name!r}")
    if _DIGEST_RE.fullmatch(digest) is None:
        raise ImageEvidenceError(f"image digest is invalid: {digest!r}")
    return name, digest


def _docker_inspect(reference: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["docker", "image", "inspect", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ImageEvidenceError(
            f"docker image inspect failed for {reference!r} (exit {completed.returncode})"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ImageEvidenceError(f"docker inspect returned invalid JSON for {reference!r}") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ImageEvidenceError(f"docker inspect returned an unexpected record for {reference!r}")
    return payload


def build_evidence(
    *,
    release_version: str,
    source_commit: str,
    repository: str,
    registry: str,
    references: dict[str, str],
    inspect_image: Callable[[str], list[dict[str, Any]]] = _docker_inspect,
) -> dict[str, object]:
    """Build the fail-closed evidence document from locally pulled digest refs."""

    if _VERSION_RE.fullmatch(release_version) is None:
        raise ImageEvidenceError("release version must look like v0.1.30")
    source_commit = source_commit.lower()
    if _COMMIT_RE.fullmatch(source_commit) is None:
        raise ImageEvidenceError("source commit must be a full lowercase Git SHA")
    if registry != "ghcr.io":
        raise ImageEvidenceError("registry must be ghcr.io")
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+", repository) is None:
        raise ImageEvidenceError("repository must be an owner/repository slug")
    if set(references) != set(ROLES):
        raise ImageEvidenceError("images must contain exactly api, worker, frontend")

    expected_source = f"https://github.com/{repository}"
    images: dict[str, object] = {}
    seen_names: set[str] = set()
    for role in ROLES:
        reference = references[role]
        name, digest = _split_reference(reference, registry=registry)
        expected_name = f"{registry}/{repository.lower()}-{role}"
        if name != expected_name or name in seen_names:
            raise ImageEvidenceError(
                f"{role} image must use its unique canonical name {expected_name!r}"
            )
        seen_names.add(name)
        inspected = inspect_image(reference)[0]
        repo_digests = inspected.get("RepoDigests")
        if not isinstance(repo_digests, list) or reference not in repo_digests:
            raise ImageEvidenceError(
                f"pulled {role} image does not record the requested immutable reference"
            )
        config = inspected.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            raise ImageEvidenceError(f"pulled {role} image has no OCI labels")
        required_labels = {
            "org.opencontainers.image.version": release_version,
            "org.opencontainers.image.revision": source_commit,
            "org.opencontainers.image.source": expected_source,
        }
        for label, expected in required_labels.items():
            if labels.get(label) != expected:
                raise ImageEvidenceError(f"pulled {role} image label {label!r} does not match")
        images[role] = {
            "name": name,
            "digest": digest,
            "immutable_reference": reference,
            "labels": required_labels,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "release_version": release_version,
        "source_commit": source_commit,
        "registry": registry,
        "images": images,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--registry", default="ghcr.io")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="ROLE=NAME@DIGEST",
        help="Repeat once for api, worker, and frontend.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        references = _parse_role_values(args.image)
        evidence = build_evidence(
            release_version=args.version,
            source_commit=args.source_commit,
            repository=args.repository,
            registry=args.registry,
            references=references,
        )
    except ImageEvidenceError as error:
        parser.error(str(error))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
