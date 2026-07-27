#!/usr/bin/env python3
"""Regression tests for the v0.1.28 hosted evidence validator."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_release_evidence
import validate_v0128_release_evidence

VERSION = "v0.1.28"
COMMIT = "a" * 40
REPOSITORY = "Rvs006/smart-commissioning-app"
RUN_ID = "12345"
ARTIFACT_NAME = f"{VERSION}-release-evidence-{COMMIT}"


def _sbom() -> bytes:
    return (
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {"component": {"type": "application", "name": "fixture"}},
                "components": [{"type": "library", "name": "fixture", "version": "1"}],
            }
        )
        + "\n"
    ).encode()


class V0128EvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path, *, mutate=None, omit: str | None = None) -> Path:  # type: ignore[no-untyped-def]
        root.mkdir(parents=True)
        artifacts = {
            "MIGRATION_ROLLBACK.md": b"migration\n",
            "SYNC_V2_WIRE_FORMAT.md": b"wire\n",
            "SYNC_V2_CREDENTIAL_SCOPE.md": b"scope\n",
            "SYNC_V2_OPERATIONS.md": b"receipts and compatibility\n",
            "DOCKER_DEPLOYMENT_ROLLBACK.md": b"deploy and rollback\n",
        }
        images = {}
        for role in ("api", "worker", "frontend"):
            name = f"ghcr.io/rvs006/smart-commissioning-app-{role}"
            digest = "sha256:" + ({"api": "1", "worker": "2", "frontend": "3"}[role] * 64)
            images[role] = {
                "name": name,
                "digest": digest,
                "immutable_reference": f"{name}@{digest}",
                "labels": {
                    "org.opencontainers.image.version": VERSION,
                    "org.opencontainers.image.revision": COMMIT,
                    "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
                },
            }
        docker = {
            "schema_version": "1.0",
            "release_version": VERSION,
            "source_commit": COMMIT,
            "registry": "ghcr.io",
            "images": images,
        }
        if mutate is not None:
            mutate(docker)
        artifacts["docker-image-evidence.json"] = (json.dumps(docker) + "\n").encode()
        artifact_paths: list[Path] = []
        for name, content in artifacts.items():
            if name == omit:
                continue
            path = root / name
            path.write_bytes(content)
            artifact_paths.append(path)
        sbom_paths: list[Path] = []
        for name in (
            "SBOM.python.cdx.json",
            "SBOM.npm.cdx.json",
            "SBOM.image-api.cdx.json",
            "SBOM.image-worker.cdx.json",
            "SBOM.image-frontend.cdx.json",
        ):
            path = root / name
            path.write_bytes(_sbom())
            sbom_paths.append(path)
        args = [
            "--version", VERSION,
            "--product-version", VERSION,
            "--commit", COMMIT,
            "--output-dir", str(root),
            "--evidence-kind", "hosted",
            "--repository", REPOSITORY,
            "--workflow-artifact-name", ARTIFACT_NAME,
            "--workflow-name", "v0.1.28 Release Gates",
            "--workflow-run-id", RUN_ID,
            "--workflow-run-attempt", "1",
            "--workflow-run-url", f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
            "--workflow-event", "workflow_dispatch",
            "--gate", "hosted_compose=passed",
            "--test", "docker_oci_labels=passed",
        ]
        for path in artifact_paths:
            args.extend(("--artifact", str(path)))
        for path in sbom_paths:
            args.extend(("--sbom", str(path)))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generate_release_evidence.main(args), 0)
        return root / "release-evidence.json"

    def _validate(self, evidence: Path) -> int:
        args = [
            "--evidence", str(evidence),
            "--version", VERSION,
            "--product-version", VERSION,
            "--commit", COMMIT,
            "--evidence-kind", "hosted",
            "--repository", REPOSITORY,
            "--workflow-artifact-name", ARTIFACT_NAME,
            "--workflow-name", "v0.1.28 Release Gates",
            "--workflow-run-id", RUN_ID,
            "--workflow-run-attempt", "1",
            "--workflow-event", "workflow_dispatch",
            "--require-gate", "hosted_compose",
            "--require-test", "docker_oci_labels",
        ]
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return validate_v0128_release_evidence.main(args)

    def test_accepts_complete_exact_sha_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(self._validate(self._fixture(Path(temporary) / "evidence")), 0)

    def test_rejects_mutable_or_invalid_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence",
                mutate=lambda value: value["images"]["api"].__setitem__("digest", "latest"),
            )
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_wrong_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence",
                mutate=lambda value: value["images"]["worker"]["labels"].__setitem__(
                    "org.opencontainers.image.revision", "b" * 40
                ),
            )
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_missing_release_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence", omit="SYNC_V2_OPERATIONS.md"
            )
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_extra_image_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence",
                mutate=lambda value: value["images"].__setitem__("mutable", {}),
            )
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_roles_that_reuse_one_image_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence",
                mutate=lambda value: value["images"]["worker"].update(
                    {
                        "name": value["images"]["api"]["name"],
                        "immutable_reference": (
                            value["images"]["api"]["name"]
                            + "@"
                            + value["images"]["worker"]["digest"]
                        ),
                    }
                ),
            )
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_unknown_secret_like_image_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(
                Path(temporary) / "evidence",
                mutate=lambda value: value["images"]["api"].__setitem__(
                    "private_key", "PLAINTEXT-SECRET"
                ),
            )
            self.assertEqual(self._validate(evidence), 1)


if __name__ == "__main__":
    unittest.main()
