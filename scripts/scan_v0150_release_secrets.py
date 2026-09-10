#!/usr/bin/env python3
"""Scan v0.1.50 source and release evidence for credential material."""

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
        # Fail closed: a requested bundle that is missing, unreadable, or empty
        # must not let the secret scan report success. os.walk over a missing
        # root yields nothing, so validate each explicit input on its own before
        # and after expansion; a populated path must not mask an empty one.
        missing = [
            raw for raw in args.path
            if not raw.resolve().exists() or not os.access(raw.resolve(), os.R_OK)
        ]
        if missing:
            for raw in missing:
                print(f"FAIL: requested scan path is missing or unreadable: {raw}", file=sys.stderr)
            return 1
        paths: list[Path] = []
        empty = []
        for raw in args.path:
            expanded = [path.resolve() for path in base._files(raw.resolve(), explicit=True)]
            if not expanded:
                empty.append(raw)
            paths.extend(expanded)
        if empty:
            for raw in empty:
                print(f"FAIL: requested scan path expanded to zero files: {raw}", file=sys.stderr)
            return 1
    else:
        paths = base._files(args.root.resolve(), explicit=False)
    failures = base.scan(paths)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"v0.1.50 release secret scan: OK ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
