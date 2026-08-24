#!/usr/bin/env python3
"""Validate v0.1.26 evidence, CycloneDX shape, and payload digests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def _sha256(path: Path) -> str | None:
    """SHA-256 of a file, or None if it cannot be read (OSError)."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _validate_cyclonedx(path: Path) -> list[str]:
    failures: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"{path.name}: invalid JSON: {error}"]
    if payload.get("bomFormat") != "CycloneDX":
        failures.append(f"{path.name}: bomFormat is not CycloneDX")
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
                    failures.append(
                        f"{path.name}: file component {index} lacks hashes"
                    )
            elif not component.get("version"):
                failures.append(f"{path.name}: component {index} lacks version")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    parser.add_argument("--require-gate", action="append", default=[])
    args = parser.parse_args(argv)

    failures: list[str] = []
    commit = args.commit.lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        parser.error("--commit must be a full 40-character Git SHA")
    evidence_path = args.evidence.resolve()
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"FAIL: invalid evidence JSON: {error}", file=sys.stderr)
        return 1

    if evidence.get("release_version") != args.version:
        failures.append("release version does not match")
    if evidence.get("source_commit") != commit:
        failures.append("source commit does not match exact release SHA")
    gates = evidence.get("gates")
    if not isinstance(gates, dict):
        failures.append("gates object is missing")
        gates = {}
    for gate in args.require_gate:
        if gates.get(gate) != "passed":
            failures.append(f"required gate is not passed: {gate}")

    roots = [path.resolve() for path in args.search_root]
    roots.append(evidence_path.parent)
    seen_names: set[str] = set()
    for record in evidence.get("files", []):
        if not isinstance(record, dict):
            failures.append("file record is not an object")
            continue
        name = record.get("name")
        expected = record.get("sha256")
        if not isinstance(name, str) or not isinstance(expected, str):
            failures.append("file record lacks name or sha256")
            continue
        if name in seen_names:
            failures.append(f"duplicate evidence file name: {name}")
        seen_names.add(name)
        matches = [root / name for root in roots if (root / name).is_file()]
        if not matches:
            failures.append(f"evidence payload is missing: {name}")
            continue
        payload_path = matches[0]
        actual = _sha256(payload_path)
        if actual is None:
            failures.append(f"evidence payload is unreadable: {name}")
        elif actual != expected:
            failures.append(f"SHA-256 mismatch for {name}")
        expected_size = record.get("size")
        if not isinstance(expected_size, int) or payload_path.stat().st_size != expected_size:
            failures.append(f"size mismatch for {name}")
        if record.get("kind") == "sbom":
            failures.extend(_validate_cyclonedx(payload_path))

    checksums_path = evidence_path.parent / "SHA256SUMS.txt"
    if checksums_path.is_file():
        try:
            checksum_text = checksums_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as error:
            failures.append(f"SHA256SUMS.txt is unreadable or not ASCII: {error}")
            checksum_text = ""
        checksum_records: dict[str, str] = {}
        for line in checksum_text.splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
            if match is None:
                failures.append(f"invalid SHA256SUMS line: {line!r}")
                continue
            digest, name = match.groups()
            if name in checksum_records:
                failures.append(f"duplicate SHA256SUMS entry: {name}")
            checksum_records[name] = digest
        expected_checksums = {
            record["name"]: record["sha256"]
            for record in evidence.get("files", [])
            if isinstance(record, dict)
            and isinstance(record.get("name"), str)
            and isinstance(record.get("sha256"), str)
        }
        evidence_digest = _sha256(evidence_path)
        if evidence_digest is None:
            failures.append("release evidence file is unreadable")
        else:
            expected_checksums[evidence_path.name] = evidence_digest
            if checksum_records != expected_checksums:
                failures.append("SHA256SUMS entries do not match release evidence")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("release evidence: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
