#!/usr/bin/env python3
"""Adversarial unit tests for v0.1.27 release evidence."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import generate_release_evidence
import validate_v0127_release_evidence

VERSION = "v0.1.27"
COMMIT = "a" * 40
REPOSITORY = "example/repo"
RUN_ID = "12345"
RUN_ATTEMPT = "2"
ARTIFACT_NAME = "SmartCommissioningApp-windows-portable"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sbom(name: str) -> bytes:
    return (
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": name,
                        "version": VERSION,
                    }
                },
                "components": [
                    {"type": "library", "name": f"{name}-dependency", "version": "1"}
                ],
            }
        )
        + "\n"
    ).encode()


class ReleaseEvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        root.mkdir(parents=True)
        files = {
            "SmartCommissioningApp.exe": b"portable executable bytes",
            "MIGRATION_ROLLBACK.md": b"# Migration and rollback\n",
            "windows-acceptance.json": b'{"status":"passed"}\n',
            "SBOM.python.cdx.json": _sbom("python"),
            "SBOM.npm.cdx.json": _sbom("npm"),
        }
        for name, content in files.items():
            (root / name).write_bytes(content)
        generator_args = [
            "--version",
            VERSION,
            "--product-version",
            VERSION,
            "--commit",
            COMMIT,
            "--output-dir",
            str(root),
            "--evidence-kind",
            "windows",
            "--repository",
            REPOSITORY,
            "--workflow-artifact-name",
            ARTIFACT_NAME,
            "--workflow-name",
            "Windows Portable Bundle",
            "--workflow-run-id",
            RUN_ID,
            "--workflow-run-attempt",
            RUN_ATTEMPT,
            "--workflow-run-url",
            f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
            "--workflow-event",
            "workflow_dispatch",
            "--artifact",
            str(root / "SmartCommissioningApp.exe"),
            "--artifact",
            str(root / "MIGRATION_ROLLBACK.md"),
            "--artifact",
            str(root / "windows-acceptance.json"),
            "--sbom",
            str(root / "SBOM.python.cdx.json"),
            "--sbom",
            str(root / "SBOM.npm.cdx.json"),
            "--gate",
            "windows_build=passed",
            "--test",
            "portable_readiness=passed",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generate_release_evidence.main(generator_args), 0)
        return root / "release-evidence.json"

    def _validator_args(self, evidence: Path) -> list[str]:
        return [
            "--evidence",
            str(evidence),
            "--version",
            VERSION,
            "--product-version",
            VERSION,
            "--commit",
            COMMIT,
            "--evidence-kind",
            "windows",
            "--repository",
            REPOSITORY,
            "--workflow-artifact-name",
            ARTIFACT_NAME,
            "--workflow-name",
            "Windows Portable Bundle",
            "--workflow-run-id",
            RUN_ID,
            "--workflow-run-attempt",
            RUN_ATTEMPT,
            "--workflow-event",
            "workflow_dispatch",
            "--require-gate",
            "windows_build",
            "--require-test",
            "portable_readiness",
        ]

    def _validate(self, evidence: Path, extra: list[str] | None = None) -> int:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return validate_v0127_release_evidence.main(
                [*self._validator_args(evidence), *(extra or [])]
            )

    def _rewrite_manifest(self, evidence_path: Path) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        entries: list[tuple[str, str]] = []
        records = evidence.get("files")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                name = record.get("name")
                digest = record.get("sha256")
                if isinstance(name, str) and isinstance(digest, str):
                    entries.append((name, digest))
        entries.append((evidence_path.name, _sha256(evidence_path)))
        (evidence_path.parent / "SHA256SUMS.txt").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in sorted(entries)),
            encoding="ascii",
        )

    def _mutation_is_rejected(self, mutate) -> None:  # type: ignore[no-untyped-def]
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(Path(temporary) / "bundle")
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            mutate(payload, evidence.parent)
            evidence.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            self._rewrite_manifest(evidence)
            self.assertEqual(self._validate(evidence), 1)

    def test_strict_fixture_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(Path(temporary) / "bundle")
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(payload["workflow"]["run_attempt"], 2)
            self.assertEqual(payload["workflow"]["artifact_name"], ARTIFACT_NAME)
            self.assertEqual(self._validate(evidence), 0)

    def test_rejects_empty_files_metadata(self) -> None:
        self._mutation_is_rejected(lambda payload, _root: payload.__setitem__("files", []))

    def test_rejects_missing_checksum_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(Path(temporary) / "bundle")
            (evidence.parent / "SHA256SUMS.txt").unlink()
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_traversal_and_absolute_names(self) -> None:
        for unsafe in ("..\\escape.exe", "C:\\outside\\escape.exe", "/tmp/escape.exe"):
            with self.subTest(name=unsafe):
                self._mutation_is_rejected(
                    lambda payload, _root, value=unsafe: payload["files"][0].__setitem__(
                        "name", value
                    )
                )

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            evidence = self._fixture(parent / "bundle")
            migration = evidence.parent / "MIGRATION_ROLLBACK.md"
            outside = parent / "outside.md"
            outside.write_bytes(migration.read_bytes())
            migration.unlink()
            try:
                migration.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"symlink creation is unavailable: {error}")
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_wrong_required_filename_or_kind(self) -> None:
        self._mutation_is_rejected(
            lambda payload, _root: next(
                record
                for record in payload["files"]
                if record["name"] == "SBOM.python.cdx.json"
            ).__setitem__("kind", "artifact")
        )
        self._mutation_is_rejected(
            lambda payload, _root: next(
                record
                for record in payload["files"]
                if record["name"] == "SBOM.python.cdx.json"
            ).__setitem__("name", "renamed-python.cdx.json")
        )

    def test_rejects_wrong_workflow_host_repo_run_attempt_and_artifact(self) -> None:
        mutations = {
            "host": lambda workflow: workflow.__setitem__(
                "run_url", f"https://evil.example/{REPOSITORY}/actions/runs/{RUN_ID}"
            ),
            "repository": lambda workflow: workflow.__setitem__(
                "repository", "attacker/repo"
            ),
            "run": lambda workflow: workflow.__setitem__("run_id", "99999"),
            "attempt": lambda workflow: workflow.__setitem__("run_attempt", 99),
            "artifact": lambda workflow: workflow.__setitem__(
                "artifact_name", "ambiguous-artifact"
            ),
        }
        for label, mutation in mutations.items():
            with self.subTest(field=label):
                self._mutation_is_rejected(
                    lambda payload, _root, change=mutation: change(payload["workflow"])
                )

    def test_rejects_null_product_version(self) -> None:
        self._mutation_is_rejected(
            lambda payload, _root: payload.__setitem__("product_version", None)
        )

    def test_rejects_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._fixture(Path(temporary) / "bundle")
            (evidence.parent / "SmartCommissioningApp.exe").write_bytes(b"")
            self.assertEqual(self._validate(evidence), 1)

    def test_rejects_ambiguous_payload_across_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            evidence = self._fixture(parent / "bundle")
            second = parent / "second"
            second.mkdir()
            for name in validate_v0127_release_evidence.REQUIRED_FILES["windows"]:
                shutil.copy2(evidence.parent / name, second / name)
            self.assertEqual(
                self._validate(evidence, ["--search-root", str(second)]),
                1,
            )

    def test_rejects_duplicate_file_record(self) -> None:
        self._mutation_is_rejected(
            lambda payload, _root: payload["files"].append(dict(payload["files"][0]))
        )

    def test_generator_requires_product_version_and_rejects_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one = root / "one" / "same.bin"
            two = root / "two" / "same.bin"
            one.parent.mkdir()
            two.parent.mkdir()
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            args = [
                "--version",
                VERSION,
                "--commit",
                COMMIT,
                "--output-dir",
                str(root / "out"),
                "--evidence-kind",
                "windows",
                "--repository",
                REPOSITORY,
                "--workflow-artifact-name",
                ARTIFACT_NAME,
                "--workflow-name",
                "Windows Portable Bundle",
                "--workflow-run-id",
                RUN_ID,
                "--workflow-run-attempt",
                RUN_ATTEMPT,
                "--workflow-run-url",
                f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}",
                "--workflow-event",
                "workflow_dispatch",
                "--artifact",
                str(one),
                "--artifact",
                str(two),
                "--gate",
                "windows_build=passed",
                "--test",
                "portable_readiness=passed",
            ]
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    generate_release_evidence.main(args)
                with self.assertRaises(SystemExit):
                    generate_release_evidence.main(
                        ["--product-version", VERSION, *args]
                    )

    def test_generator_retains_v0126_schema_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "legacy.bin"
            artifact.write_bytes(b"legacy v0.1.26 bytes")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    generate_release_evidence.main(
                        [
                            "--version",
                            "v0.1.26",
                            "--commit",
                            COMMIT,
                            "--output-dir",
                            str(root),
                            "--artifact",
                            str(artifact),
                            "--gate",
                            "windows_build=passed",
                        ]
                    ),
                    0,
                )
            payload = json.loads(
                (root / "release-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertNotIn("workflow", payload)
            self.assertNotIn("product_version", payload)


if __name__ == "__main__":
    unittest.main()
