#!/usr/bin/env python3
"""Validate v0.1.53 evidence with the immutable provenance contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import validate_v0128_release_evidence as base

_BUNDLE_EXE_NAME = "SmartCommissioningApp.exe"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_provenance_failures(argv: list[str] | None) -> list[str]:
    """Cross-check the portable BUILD_PROVENANCE.json for Windows evidence.

    A dirty, non-publishable bundle records source_commit=local-dirty-tree and
    source_tree_state=dirty_local_non_publishable. The strict evidence validator
    only inspects the caller-supplied evidence JSON, so without this a dirty
    bundle paired with a clean release SHA would pass. Fail closed: for Windows
    evidence the provenance file must exist, be clean and publishable, and pin
    the requested release SHA and version.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--evidence-kind")
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--search-root", type=Path, action="append", default=[])
    known, _ = parser.parse_known_args(arguments)
    if known.evidence_kind != "windows":
        return []
    if known.evidence is None or known.commit is None:
        return ["release provenance requires --evidence and --commit"]
    roots = [*known.search_root, known.evidence.parent]
    matches = {
        str(candidate): candidate
        for root in roots
        if (candidate := (root.resolve() / "BUILD_PROVENANCE.json")).is_file()
    }
    if len(matches) != 1:
        return ["BUILD_PROVENANCE.json is missing or ambiguous across search roots"]
    provenance_path = next(iter(matches.values()))
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"BUILD_PROVENANCE.json is invalid: {error}"]
    if not isinstance(provenance, dict):
        return ["BUILD_PROVENANCE.json root is not an object"]
    failures: list[str] = []
    if provenance.get("source_tree_state") != "clean":
        failures.append("portable bundle is not from a clean tree (non-publishable)")
    if provenance.get("publishable") is not True:
        failures.append("portable bundle provenance is not marked publishable")
    if str(provenance.get("source_commit", "")).lower() != known.commit.lower():
        failures.append("portable bundle source_commit does not match the release SHA")
    if known.version is not None and provenance.get("application_version") != known.version:
        failures.append("portable bundle application_version does not match the release version")
    # Bind the packaged executable to the build-time provenance attestation. The
    # strict evidence and SHA256SUMS checks only compare metadata files that are
    # regenerated from the bundle, so a tampered exe reconciled into that metadata
    # would pass. portable_exe_sha256 is written by build.ps1 from the freshly
    # built exe, so re-deriving and comparing it fails closed on post-build tamper.
    recorded_exe_sha = provenance.get("portable_exe_sha256")
    if not (isinstance(recorded_exe_sha, str) and re.fullmatch(r"[0-9a-f]{64}", recorded_exe_sha)):
        failures.append("portable bundle provenance has no valid portable_exe_sha256")
    else:
        exe_path = provenance_path.parent / _BUNDLE_EXE_NAME
        if not exe_path.is_file():
            failures.append(f"portable bundle executable {_BUNDLE_EXE_NAME} is missing")
        elif _file_sha256(exe_path) != recorded_exe_sha:
            failures.append("packaged executable does not match provenance portable_exe_sha256")
    return failures


def main(argv: list[str] | None = None) -> int:
    failures = _bundle_provenance_failures(argv)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return base.main_for_version(
        argv,
        expected_version="v0.1.53",
        success_label="v0.1.53 Docker and release evidence: OK",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
