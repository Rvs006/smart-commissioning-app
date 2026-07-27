#!/usr/bin/env python3
"""Strictly validate v0.1.27 workflow identity, payloads, and checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PureWindowsPath

REQUIRED_FILES: dict[str, dict[str, str]] = {
    "windows": {
        "SmartCommissioningApp.exe": "artifact",
        "MIGRATION_ROLLBACK.md": "artifact",
        "windows-acceptance.json": "artifact",
        "SBOM.python.cdx.json": "sbom",
        "SBOM.npm.cdx.json": "sbom",
    },
    "hosted": {
        "MIGRATION_ROLLBACK.md": "artifact",
        "SBOM.python.cdx.json": "sbom",
        "SBOM.npm.cdx.json": "sbom",
        "SBOM.image-api.cdx.json": "sbom",
        "SBOM.image-worker.cdx.json": "sbom",
        "SBOM.image-frontend.cdx.json": "sbom",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_basename(value: object) -> bool:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    windows_path = PureWindowsPath(value)
    return (
        not Path(value).is_absolute()
        and not windows_path.is_absolute()
        and not windows_path.drive
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_cyclonedx(path: Path) -> list[str]:
    failures: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path.name}: invalid CycloneDX JSON: {error}"]
    if not isinstance(payload, dict) or payload.get("bomFormat") != "CycloneDX":
        failures.append(f"{path.name}: bomFormat is not CycloneDX")
        return failures
    if payload.get("specVersion") not in {"1.4", "1.5", "1.6"}:
        failures.append(f"{path.name}: unsupported or missing specVersion")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("component"), dict):
        failures.append(f"{path.name}: metadata.component is missing")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        failures.append(f"{path.name}: components is empty")
    else:
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                failures.append(f"{path.name}: component {index} is not an object")
                continue
            for field in ("type", "name"):
                if not component.get(field):
                    failures.append(f"{path.name}: component {index} lacks {field}")
            if component.get("type") == "file":
                hashes = component.get("hashes")
                if not isinstance(hashes, list) or not hashes:
                    failures.append(f"{path.name}: file component {index} lacks hashes")
            elif not component.get("version"):
                failures.append(f"{path.name}: component {index} lacks version")
    return failures


def _passed_records(
    evidence: dict[str, object],
    field: str,
    required: list[str],
) -> list[str]:
    failures: list[str] = []
    records = evidence.get(field)
    if not isinstance(records, dict) or not records:
        return [f"{field} metadata is missing or empty"]
    for name, status in records.items():
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            failures.append(f"{field} contains invalid name: {name!r}")
        if status not in {"passed", "failed", "skipped"}:
            failures.append(f"{field} contains invalid status for {name!r}")
    for name in required:
        if records.get(name) != "passed":
            failures.append(f"required {field} record is not passed: {name}")
    return failures


def _resolved_roots(evidence_path: Path, values: list[Path]) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_root in [*values, evidence_path.parent]:
        root = raw_root.resolve()
        key = os.path.normcase(str(root))
        if key not in seen:
            roots.append(root)
            seen.add(key)
    return roots


def _find_payload(name: str, roots: list[Path]) -> tuple[Path | None, list[str]]:
    failures: list[str] = []
    matches: dict[str, Path] = {}
    for root in roots:
        candidate = root / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            failures.append(f"evidence payload cannot be resolved: {name}: {error}")
            continue
        if not _within(resolved, root):
            failures.append(f"evidence payload escapes search root through a symlink: {name}")
            continue
        if not resolved.is_file():
            failures.append(f"evidence payload is not a regular file: {name}")
            continue
        matches[os.path.normcase(str(resolved))] = resolved
    if not matches:
        failures.append(f"evidence payload is missing: {name}")
        return None, failures
    if len(matches) != 1:
        failures.append(f"evidence payload is ambiguous across search roots: {name}")
        return None, failures
    return next(iter(matches.values())), failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--evidence-kind", choices=("windows", "hosted"), required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--workflow-event", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-artifact-name", required=True)
    parser.add_argument("--product-version", required=True)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    parser.add_argument("--require-gate", action="append", default=[])
    parser.add_argument("--require-test", action="append", default=[])
    args = parser.parse_args(argv)

    if re.fullmatch(r"v\d+\.\d+\.\d+", args.version) is None:
        parser.error("--version must look like v0.1.27")
    if args.product_version != args.version:
        parser.error("--product-version must equal --version")
    commit = args.commit.lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        parser.error("--commit must be a full 40-character Git SHA")
    if re.fullmatch(r"[1-9]\d*", args.workflow_run_id) is None:
        parser.error("--workflow-run-id must be a positive integer")
    if args.workflow_run_attempt < 1:
        parser.error("--workflow-run-attempt must be positive")
    if re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+",
        args.repository,
    ) is None:
        parser.error("--repository must be a GitHub owner/repository slug")
    if not _safe_basename(args.workflow_artifact_name):
        parser.error("--workflow-artifact-name is invalid")

    failures: list[str] = []
    try:
        evidence_path = args.evidence.resolve(strict=True)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"FAIL: invalid evidence JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(evidence, dict):
        print("FAIL: evidence root is not an object", file=sys.stderr)
        return 1

    if evidence.get("schema_version") != "1.1":
        failures.append("schema_version is not 1.1")
    if evidence.get("evidence_kind") != args.evidence_kind:
        failures.append("evidence kind does not match")
    if evidence.get("release_version") != args.version:
        failures.append("release version does not match")
    if evidence.get("product_version") != args.product_version:
        failures.append("ProductVersion metadata does not match")
    if evidence.get("source_commit") != commit:
        failures.append("source commit does not match exact release SHA")

    workflow = evidence.get("workflow")
    expected_run_url = (
        f"https://github.com/{args.repository}/actions/runs/{args.workflow_run_id}"
    )
    if not isinstance(workflow, dict):
        failures.append("workflow metadata is missing")
    else:
        expected_fields: dict[str, object] = {
            "name": args.workflow_name,
            "repository": args.repository,
            "artifact_name": args.workflow_artifact_name,
            "run_id": args.workflow_run_id,
            "run_attempt": args.workflow_run_attempt,
            "run_url": expected_run_url,
            "event": args.workflow_event,
        }
        for field, expected in expected_fields.items():
            if workflow.get(field) != expected:
                failures.append(f"workflow {field.replace('_', ' ')} does not match")

    failures.extend(_passed_records(evidence, "gates", args.require_gate))
    failures.extend(_passed_records(evidence, "tests", args.require_test))

    roots = _resolved_roots(evidence_path, args.search_root)
    records = evidence.get("files")
    required = REQUIRED_FILES[args.evidence_kind]
    valid_records: dict[str, dict[str, object]] = {}
    if not isinstance(records, list) or not records:
        failures.append("files metadata is missing or empty")
        records = []
    for record in records:
        if not isinstance(record, dict):
            failures.append("file record is not an object")
            continue
        name = record.get("name")
        if not _safe_basename(name):
            failures.append(f"unsafe evidence file name: {name!r}")
            continue
        assert isinstance(name, str)
        if name in valid_records:
            failures.append(f"duplicate evidence file name: {name}")
            continue
        valid_records[name] = record
        if name not in required:
            failures.append(f"unexpected evidence file name: {name}")
        expected_kind = required.get(name)
        if record.get("kind") != expected_kind:
            failures.append(f"evidence kind does not match required filename: {name}")
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            failures.append(f"invalid SHA-256 record for {name}")
        expected_size = record.get("size")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0:
            failures.append(f"invalid non-positive size record for {name}")

    missing = sorted(set(required) - set(valid_records))
    for name in missing:
        failures.append(f"required evidence file is missing: {name}")

    resolved_payloads: dict[str, Path] = {}
    for name, record in valid_records.items():
        if name not in required:
            continue
        path, path_failures = _find_payload(name, roots)
        failures.extend(path_failures)
        if path is None:
            continue
        resolved_payloads[name] = path
        size = path.stat().st_size
        if size <= 0:
            failures.append(f"evidence payload is empty: {name}")
        if record.get("size") != size:
            failures.append(f"size mismatch for {name}")
        if record.get("sha256") != _sha256(path):
            failures.append(f"SHA-256 mismatch for {name}")
        # SBOM validation is selected from the required filename, never from the
        # caller-controlled `kind` field.
        if required[name] == "sbom":
            failures.extend(_validate_cyclonedx(path))

    checksums_path = evidence_path.parent / "SHA256SUMS.txt"
    checksum_records: dict[str, str] = {}
    if not checksums_path.exists() and not checksums_path.is_symlink():
        failures.append("SHA256SUMS.txt is missing")
    else:
        try:
            resolved_checksums = checksums_path.resolve(strict=True)
            if not _within(resolved_checksums, evidence_path.parent):
                failures.append("SHA256SUMS.txt escapes the evidence directory")
            elif not resolved_checksums.is_file() or resolved_checksums.stat().st_size <= 0:
                failures.append("SHA256SUMS.txt is not a non-empty regular file")
            else:
                for line in resolved_checksums.read_text(encoding="ascii").splitlines():
                    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
                    if match is None or not _safe_basename(match.group(2)):
                        failures.append(f"invalid SHA256SUMS line: {line!r}")
                        continue
                    digest, name = match.groups()
                    if name in checksum_records:
                        failures.append(f"duplicate SHA256SUMS entry: {name}")
                    checksum_records[name] = digest
        except (OSError, UnicodeError) as error:
            failures.append(f"cannot read SHA256SUMS.txt: {error}")

    expected_checksums = {
        name: str(record.get("sha256")) for name, record in valid_records.items()
    }
    expected_checksums[evidence_path.name] = _sha256(evidence_path)
    if checksum_records != expected_checksums:
        failures.append("SHA256SUMS entries do not exactly match release evidence")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("v0.1.27 strict release evidence: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
