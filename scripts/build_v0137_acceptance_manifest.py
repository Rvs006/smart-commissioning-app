#!/usr/bin/env python3
"""Build a sanitized, hash-bound v0.1.37 field-acceptance manifest.

The manifest records identities and references only. It never copies register
rows, broker settings, credentials, or device details into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    if byte_count == 0:
        raise ValueError("register must be a non-empty file")
    return digest.hexdigest()


def _safe_reference(value: str, *, name: str) -> str:
    text = value.strip()
    if not text or len(text) > 160 or any(ord(char) < 32 for char in text):
        raise ValueError(f"{name} must be a short non-empty reference")
    if any(token in text.casefold() for token in ("password", "secret", "private_key", "broker.example")):
        raise ValueError(f"{name} must be a sanitized reference, not secret material")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="v0.1.37")
    parser.add_argument("--register-revision", required=True)
    parser.add_argument("--register-import-id", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--application-commit", required=True)
    parser.add_argument("--gate-a-run-id", required=True)
    parser.add_argument("--gate-b-run-id", required=True)
    parser.add_argument("--operator-ref", default="field-operator-ref")
    parser.add_argument("--machine-ref", default="field-machine-ref")
    parser.add_argument("--broker-endpoint-ref", default="approved-broker-ref")
    parser.add_argument("--gate-a-window-seconds", type=float, default=0.0)
    parser.add_argument("--gate-b-window-seconds", type=float, default=86_400.0)
    parser.add_argument("--expected-assets", type=int, default=752)
    args = parser.parse_args(argv)

    register = args.register.resolve()
    try:
        register_sha256 = _sha256(register)
    except (OSError, ValueError) as error:
        parser.error(f"--register must point to a non-empty file: {error}")
    if args.version != "v0.1.37":
        parser.error("this manifest builder is pinned to v0.1.37")
    if re.fullmatch(r"[0-9a-fA-F]{40}", args.application_commit) is None:
        parser.error("--application-commit must be a full 40-character Git SHA")
    for name, value in (("--gate-a-run-id", args.gate_a_run_id), ("--gate-b-run-id", args.gate_b_run_id)):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) is None:
            parser.error(f"{name} must be a sanitized run reference")
    if args.expected_assets <= 0:
        parser.error("--expected-assets must be greater than zero")
    if args.gate_a_window_seconds < 0 or args.gate_b_window_seconds <= 0:
        parser.error("Gate A may be zero, but Gate B must have a positive window")

    try:
        refs = {
            "operator_ref": _safe_reference(args.operator_ref, name="--operator-ref"),
            "machine_ref": _safe_reference(args.machine_ref, name="--machine-ref"),
            "broker_endpoint_ref": _safe_reference(
                args.broker_endpoint_ref, name="--broker-endpoint-ref"
            ),
            "register_import_id": _safe_reference(
                args.register_import_id, name="--register-import-id"
            ),
            "register_revision": _safe_reference(
                args.register_revision, name="--register-revision"
            ),
            "scope": _safe_reference(args.scope, name="--scope"),
        }
    except ValueError as error:
        parser.error(str(error))

    manifest = {
        "schema_version": "1.0",
        "release_version": args.version,
        "generated_at": datetime.now(UTC).isoformat(),
        "register": {
            "sha256": register_sha256,
            "revision": refs["register_revision"],
            "import_identity": refs["register_import_id"],
            "expected_asset_count": args.expected_assets,
        },
        "approved_scope": refs["scope"],
        "application": {
            "version": args.version,
            "commit": args.application_commit.lower(),
        },
        "provenance_references": {
            "operator": refs["operator_ref"],
            "machine": refs["machine_ref"],
            "broker_endpoint": refs["broker_endpoint_ref"],
        },
        "runs": {
            "gate_a": {
                "run_id": args.gate_a_run_id,
                "window_seconds": args.gate_a_window_seconds,
            },
            "gate_b": {
                "run_id": args.gate_b_run_id,
                "window_seconds": args.gate_b_window_seconds,
            },
        },
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    except FileExistsError:
        parser.error("--output already exists; acceptance manifests are immutable")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
