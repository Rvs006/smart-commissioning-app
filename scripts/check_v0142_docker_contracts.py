#!/usr/bin/env python3
"""Static fail-closed contracts for v0.1.42 hosted Docker acceptance."""

from __future__ import annotations

import check_v0128_docker_contracts as base


def main() -> int:
    return base.main_for_version(
        version="v0.1.42",
        release_contract_script="check_v0142_release_contracts.py",
        deployment_path="docs/docker-deployment-rollback-v0.1.42.md",
        success_label="v0.1.42 Docker release contracts: OK",
    )


if __name__ == "__main__":
    raise SystemExit(main())
