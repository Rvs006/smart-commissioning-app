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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--sbom", action="append", type=Path, default=[])
    parser.add_argument(
        "--gate",
        action="append",
        default=[],
        metavar="NAME=STATUS",
        help="Record a gate such as hosted_compose=passed.",
    )
    args = parser.parse_args(argv)

    if re.fullmatch(r"v\d+\.\d+\.\d+", args.version) is None:
        parser.error("--version must look like v0.1.26")
    commit = args.commit.lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        parser.error("--commit must be a full 40-character Git SHA")

    gates: dict[str, str] = {}
    for value in args.gate:
        if "=" not in value:
            parser.error(f"invalid --gate {value!r}; expected NAME=STATUS")
        name, status = value.split("=", 1)
        if not name or status not in {"passed", "failed", "skipped"}:
            parser.error(f"invalid --gate {value!r}")
        gates[name] = status

    files: list[dict[str, object]] = []
    for kind, paths in (("artifact", args.artifact), ("sbom", args.sbom)):
        for raw_path in paths:
            path = raw_path.resolve()
            if not path.is_file():
                parser.error(f"{kind} does not exist: {path}")
            files.append(
                {
                    "kind": kind,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = output_dir / "release-evidence.json"
    evidence = {
        "schema_version": "1.0",
        "release_version": args.version,
        "source_commit": commit,
        "generated_at": datetime.now(UTC).isoformat(),
        "gates": gates,
        "files": sorted(files, key=lambda item: (str(item["kind"]), str(item["name"]))),
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
