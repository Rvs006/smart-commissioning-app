#!/usr/bin/env python3
"""Scan v0.1.57 source and release evidence for credential material."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import scan_v0137_release_secrets as base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--path", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    if args.path:
        # Fail closed: a requested bundle that is missing or unreadable must not
        # let the secret scan report success on zero files. os.walk over a
        # nonexistent root yields nothing, so validate every explicit input
        # before expansion and again after, in case a path vanishes mid-walk.
        missing = [
            raw for raw in args.path
            if not raw.resolve().exists() or not os.access(raw.resolve(), os.R_OK)
        ]
        if missing:
            for raw in missing:
                print(f"FAIL: requested scan path is missing or unreadable: {raw}", file=sys.stderr)
            return 1
        paths = [
            path.resolve() for raw in args.path for path in base._files(raw.resolve(), explicit=True)
        ]
        if not paths:
            joined = ", ".join(str(raw) for raw in args.path)
            print(f"FAIL: requested scan path(s) expanded to zero files: {joined}", file=sys.stderr)
            return 1
    else:
        paths = base._files(args.root.resolve(), explicit=False)
    failures = base.scan(paths)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"v0.1.57 release secret scan: OK ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
