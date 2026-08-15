#!/usr/bin/env python3
"""Scan v0.1.46 source and release evidence for credential material."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import scan_v0137_release_secrets as base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", required=True)
    args = parser.parse_args(argv)
    paths = [Path(value) for value in args.path]
    failures = base.scan(paths)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"v0.1.46 release secret scan: OK ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
