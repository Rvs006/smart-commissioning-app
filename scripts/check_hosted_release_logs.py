#!/usr/bin/env python3
"""Fail a hosted release gate on severe, leaking, or repeated lifecycle logs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SECRET_KEYS = {"POSTGRES_PASSWORD", "REDIS_PASSWORD", "API_KEY"}
SEVERE_PATTERNS = {
    "unhandled application exception": re.compile(
        r"Unhandled exception handling|Exception in ASGI application", re.IGNORECASE
    ),
    "heartbeat thread leak": re.compile(
        r"owned-run heartbeat did not stop promptly|heartbeat thread.*(?:leak|alive)",
        re.IGNORECASE,
    ),
    "private key material": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-bearing URL": re.compile(
        r"\b(?:postgres(?:ql)?|redis|mqtts?)://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE
    ),
}
HEARTBEAT_FAILURE = re.compile(
    r"owned-run heartbeat (?:refresh failed|renewal window is low)", re.IGNORECASE
)
SQLITE_LOCK = re.compile(r"database is locked|sqlite.*lock", re.IGNORECASE)


def _secret_values(path: Path) -> list[str]:
    values: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in SECRET_KEYS and len(value) >= 8 and "CHANGE_ME" not in value:
            values.append(value)
    return values


def inspect_logs(text: str, *, secret_values: list[str]) -> list[str]:
    failures = [name for name, pattern in SEVERE_PATTERNS.items() if pattern.search(text)]
    if any(value in text for value in secret_values):
        failures.append("environment secret value")
    heartbeat_failures = len(HEARTBEAT_FAILURE.findall(text))
    if heartbeat_failures > 3:
        failures.append(f"repeated heartbeat failures ({heartbeat_failures})")
    sqlite_locks = len(SQLITE_LOCK.findall(text))
    if sqlite_locks > 3:
        failures.append(f"SQLite lock storm ({sqlite_locks})")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    failures = inspect_logs(
        args.logs.read_text(encoding="utf-8", errors="replace"),
        secret_values=_secret_values(args.env_file),
    )
    if failures:
        for failure in failures:
            print(f"FAIL: hosted logs contain {failure}")
        return 1
    print("hosted release logs: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
