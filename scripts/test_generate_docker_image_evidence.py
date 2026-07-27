#!/usr/bin/env python3
"""Tests for immutable Docker image evidence generation."""

from __future__ import annotations

import unittest

import generate_docker_image_evidence as evidence

VERSION = "v0.1.28"
COMMIT = "a" * 40
REPOSITORY = "Rvs006/smart-commissioning-app"
SOURCE = f"https://github.com/{REPOSITORY}"


def _references() -> dict[str, str]:
    return {
        role: (
            f"ghcr.io/rvs006/smart-commissioning-app-{role}@sha256:"
            f"{index * 64}"
        )
        for role, index in (("api", "1"), ("worker", "2"), ("frontend", "3"))
    }


def _inspector(overrides: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    labels = {
        "org.opencontainers.image.version": VERSION,
        "org.opencontainers.image.revision": COMMIT,
        "org.opencontainers.image.source": SOURCE,
    }
    labels.update(overrides or {})

    def inspect(reference: str) -> list[dict]:
        return [{"RepoDigests": [reference], "Config": {"Labels": labels}}]

    return inspect


class DockerImageEvidenceTests(unittest.TestCase):
    def test_builds_exact_schema_from_pulled_digest_references(self) -> None:
        payload = evidence.build_evidence(
            release_version=VERSION,
            source_commit=COMMIT,
            repository=REPOSITORY,
            registry="ghcr.io",
            references=_references(),
            inspect_image=_inspector(),
        )
        self.assertEqual(
            set(payload),
            {"schema_version", "release_version", "source_commit", "registry", "images"},
        )
        self.assertEqual(set(payload["images"]), {"api", "worker", "frontend"})
        api = payload["images"]["api"]
        self.assertEqual(api["immutable_reference"], f"{api['name']}@{api['digest']}")

    def test_rejects_mutable_tag(self) -> None:
        references = _references()
        references["api"] = "ghcr.io/rvs006/smart-commissioning-app-api:v0.1.28"
        with self.assertRaisesRegex(evidence.ImageEvidenceError, "not immutable"):
            evidence.build_evidence(
                release_version=VERSION,
                source_commit=COMMIT,
                repository=REPOSITORY,
                registry="ghcr.io",
                references=references,
                inspect_image=_inspector(),
            )

    def test_rejects_reference_not_recorded_by_pulled_image(self) -> None:
        with self.assertRaisesRegex(evidence.ImageEvidenceError, "does not record"):
            evidence.build_evidence(
                release_version=VERSION,
                source_commit=COMMIT,
                repository=REPOSITORY,
                registry="ghcr.io",
                references=_references(),
                inspect_image=lambda _reference: [{"RepoDigests": [], "Config": {"Labels": {}}}],
            )

    def test_rejects_label_mismatch(self) -> None:
        with self.assertRaisesRegex(evidence.ImageEvidenceError, "revision"):
            evidence.build_evidence(
                release_version=VERSION,
                source_commit=COMMIT,
                repository=REPOSITORY,
                registry="ghcr.io",
                references=_references(),
                inspect_image=_inspector({"org.opencontainers.image.revision": "b" * 40}),
            )

    def test_rejects_missing_role(self) -> None:
        references = _references()
        references.pop("worker")
        with self.assertRaisesRegex(evidence.ImageEvidenceError, "exactly"):
            evidence.build_evidence(
                release_version=VERSION,
                source_commit=COMMIT,
                repository=REPOSITORY,
                registry="ghcr.io",
                references=references,
                inspect_image=_inspector(),
            )

    def test_rejects_role_bound_to_another_roles_image_name(self) -> None:
        references = _references()
        references["worker"] = references["worker"].replace(
            "smart-commissioning-app-worker", "smart-commissioning-app-api"
        )
        with self.assertRaisesRegex(evidence.ImageEvidenceError, "unique canonical name"):
            evidence.build_evidence(
                release_version=VERSION,
                source_commit=COMMIT,
                repository=REPOSITORY,
                registry="ghcr.io",
                references=references,
                inspect_image=_inspector(),
            )


if __name__ == "__main__":
    unittest.main()
