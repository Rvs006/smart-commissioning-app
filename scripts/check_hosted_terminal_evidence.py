#!/usr/bin/env python3
"""Prove one hosted run has exactly one coherent terminal result and seal."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from smart_commissioning_core.db.models import Run, RunResult, RunSeal
from sqlalchemy import select
from sqlalchemy.orm import Session


def validate_terminal_evidence(
    run: Run | None,
    results: Sequence[RunResult],
    seals: Sequence[RunSeal],
    *,
    expected_status: str,
    expected_execution_mode: str | None,
    expected_stages: frozenset[str] = frozenset(),
    allow_lease_expiry: bool = False,
) -> list[str]:
    failures: list[str] = []
    if run is None:
        return ["run is missing"]
    if run.status != expected_status:
        failures.append(f"run status is {run.status!r}, expected {expected_status!r}")
    if expected_stages and run.stage not in expected_stages:
        failures.append(
            f"run stage is {run.stage!r}, expected one of {sorted(expected_stages)!r}"
        )
    if run.terminal_at is None or not run.result_sha256:
        failures.append("run lacks terminal timestamp or result digest")
    lifecycle_text = " ".join((run.stage or "", run.error_message or "")).casefold()
    if not allow_lease_expiry and (
        "lease_expired" in lifecycle_text or "lease expired" in lifecycle_text
    ):
        failures.append("run was terminalized by lease expiry")
    if len(results) != 1:
        failures.append(f"run has {len(results)} terminal results, expected one")
    if len(seals) != 1:
        failures.append(f"run has {len(seals)} seals, expected one")
    if len(results) == 1:
        result = results[0]
        if result.terminal_status != expected_status:
            failures.append("terminal result status differs from the run")
        if result.result_sha256 != run.result_sha256:
            failures.append("terminal result digest differs from the run")
        result_summary = result.summary if isinstance(result.summary, dict) else {}
        if (
            expected_execution_mode is not None
            and result_summary.get("execution_mode") != expected_execution_mode
        ):
            failures.append(
                "sealed result execution mode is "
                f"{result_summary.get('execution_mode')!r}, expected "
                f"{expected_execution_mode!r}"
            )
    if len(seals) == 1:
        seal = seals[0]
        if seal.terminal_status != expected_status:
            failures.append("seal status differs from the run")
        if seal.result_sha256 != run.result_sha256:
            failures.append("seal digest differs from the run")
    return failures


def terminal_snapshot(run: Run, result: RunResult, seal: RunSeal) -> dict[str, object]:
    """Return the immutable database fields that redelivery must not change."""

    return {
        "run_id": run.id,
        "status": run.status,
        "stage": run.stage,
        "terminal_at": run.terminal_at.isoformat() if run.terminal_at else None,
        "run_result_sha256": run.result_sha256,
        "result_status": result.terminal_status,
        "result_stage": result.terminal_stage,
        "result_sha256": result.result_sha256,
        "result_created_at": result.created_at.isoformat(),
        "seal_status": seal.terminal_status,
        "seal_context_sha256": seal.context_sha256,
        "seal_result_sha256": seal.result_sha256,
        "seal_sealed_at": seal.sealed_at.isoformat(),
        "result_count": 1,
        "seal_count": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--expected-status",
        choices=("succeeded", "failed", "cancelled"),
        required=True,
    )
    parser.add_argument("--expected-execution-mode")
    parser.add_argument("--expected-stage", action="append", default=[])
    parser.add_argument("--allow-lease-expiry", action="store_true")
    parser.add_argument("--snapshot-output", type=Path)
    parser.add_argument("--snapshot-input", type=Path)
    args = parser.parse_args(argv)

    from app.core.db import get_engine  # noqa: PLC0415

    with Session(get_engine()) as session:
        run = session.get(Run, args.run_id)
        results = tuple(
            session.scalars(select(RunResult).where(RunResult.run_id == args.run_id))
        )
        seals = tuple(
            session.scalars(select(RunSeal).where(RunSeal.run_id == args.run_id))
        )
    failures = validate_terminal_evidence(
        run,
        results,
        seals,
        expected_status=args.expected_status,
        expected_execution_mode=args.expected_execution_mode,
        expected_stages=frozenset(args.expected_stage),
        allow_lease_expiry=args.allow_lease_expiry,
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    assert run is not None and len(results) == 1 and len(seals) == 1
    snapshot = terminal_snapshot(run, results[0], seals[0])
    if args.snapshot_input is not None:
        expected = json.loads(args.snapshot_input.read_text(encoding="utf-8"))
        if snapshot != expected:
            print("FAIL: immutable terminal database evidence changed after redelivery")
            return 1
    if args.snapshot_output is not None:
        args.snapshot_output.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"hosted terminal result/seal cardinality: OK ({args.run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
