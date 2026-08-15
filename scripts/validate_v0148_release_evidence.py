#!/usr/bin/env python3
"""Validate v0.1.48 evidence with the immutable provenance contract."""

from __future__ import annotations

import sys

import validate_v0128_release_evidence as base


def main(argv: list[str] | None = None) -> int:
    return base.main_for_version(
        argv,
        expected_version="v0.1.48",
        success_label="v0.1.48 Docker and release evidence: OK",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
