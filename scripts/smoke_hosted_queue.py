#!/usr/bin/env python3
"""Run one deterministic UDMI job through the hosted Redis worker."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def _request(url: str, api_key: str, *, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if body is None else "POST",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{request.method} {url} returned {error.code}: {detail}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    body = {
        "project_id": "hosted-release-smoke",
        "site_id": "hosted-release-smoke",
        "job_type": "udmi_validation",
        "parameters": {
            "requested_from": "hosted_release_gate",
            "expected_schedule": {
                "asset_id": "SMOKE-1",
                "udmi_version": "1.5.2",
                "units": {},
            },
            "state_payload": {
                "version": "1.5.2",
                "timestamp": "2026-07-26T00:00:00Z",
                "system": {
                    "serial_no": "SMOKE-1",
                    "last_config": "2026-07-26T00:00:00Z",
                    "hardware": {"make": "Release", "model": "Hosted"},
                    "software": {},
                    "operation": {"operational": True},
                    "not_in_udmi_schema": True,
                },
            },
        },
    }
    created = _request(
        f"{args.base_url.rstrip('/')}/validation/udmi/runs",
        args.api_key,
        body=body,
    )
    run_id = created.get("run_id")
    if not run_id:
        raise RuntimeError("run creation response omitted run_id")

    deadline = time.monotonic() + args.timeout
    run: dict = {}
    while time.monotonic() < deadline:
        run = _request(
            f"{args.base_url.rstrip('/')}/validation/runs/{run_id}", args.api_key
        )
        if run.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"hosted worker did not terminalize {run_id} within {args.timeout}s")

    if run.get("status") != "succeeded":
        raise RuntimeError(f"hosted UDMI smoke ended {run.get('status')}: {run.get('error_message')}")
    result_summary = run.get("result_summary")
    execution_mode = run.get("execution_mode")
    if execution_mode is None and isinstance(result_summary, dict):
        execution_mode = result_summary.get("execution_mode")
    if execution_mode != "dramatiq_worker":
        raise RuntimeError(
            f"UDMI smoke ran in {execution_mode!r}, not dramatiq_worker"
        )

    issues = _request(
        f"{args.base_url.rstrip('/')}/validation/runs/{run_id}/issues", args.api_key
    )
    canonical = [
        item
        for item in issues.get("issues", [])
        if "canonical UDMI 1.5.2 state schema" in str(item.get("description", ""))
    ]
    if not canonical:
        raise RuntimeError("hosted UDMI smoke returned no canonical schema issue")

    print(f"hosted queue smoke: OK ({run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
