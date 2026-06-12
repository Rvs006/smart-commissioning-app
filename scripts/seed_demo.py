#!/usr/bin/env python3
"""Seed a demo into a RUNNING Smart Commissioning App so the UI has data.

This drives the REAL HTTP API only -- it never pokes the database directly, so
the exact same script works against both deployment profiles:

* hosted compose (AUTH_MODE=api_key)   -> pass the shared key (--api-key / SC_API_KEY)
* portable / local (AUTH_MODE=local)   -> loopback, no key needed

What it seeds (all side-effect-free / no real network I/O):

1. Ensures the demo-project / demo-site configuration exists. ``GET
   /api/v1/configuration`` auto-creates the default snapshot on first read, so
   this is the idempotent "make sure config exists" step.
2. Runs a UDMI validation against the BUNDLED fixture (real, inline, no
   broker). Produces a real run with normalized issues for the UI to show.
3. Runs a DRY-RUN IP discovery (parameters.dry_run=true): previews a scan plan
   with NO packets sent and NO authorization required.

HONESTY: there is no real BACnet / Redis / Postgres / broker / Docker involved
here. The UDMI run validates a packaged JSON fixture; the discovery run is a
dry-run preview. No active scan and no live publish are ever triggered. Running
this twice is safe: it creates new run records (runs are immutable history) but
performs no destructive action and never duplicates configuration.

Uses only the Python standard library (urllib) -- no third-party deps required.

Usage:
    python scripts/seed_demo.py [--base-url URL] [--api-key KEY]

    --base-url   default http://127.0.0.1:8000 (env SC_BASE_URL)
    --api-key    default env SC_API_KEY; omit for local/loopback

Exit code: 0 on success, 1 if any seed step failed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEMO_PROJECT_ID = "demo-project"
DEMO_SITE_ID = "demo-site"
POLL_ATTEMPTS = 30
POLL_INTERVAL_S = 1.0
REQUEST_TIMEOUT_S = 15


class ApiError(RuntimeError):
    """Raised when the API returns a non-2xx status or is unreachable."""


class ApiClient:
    """Tiny stdlib HTTP client that attaches the API key when configured."""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            # Hosted profile: every request must present the shared key.
            headers["X-API-Key"] = self.api_key
        return headers

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        url = f"{self.base_url}{path}"
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            status = error.code
        except urllib.error.URLError as error:
            raise ApiError(f"{method} {path} failed to connect: {error.reason}") from error

        parsed: object
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = body
        return status, parsed

    def get(self, path: str) -> tuple[int, object]:
        return self.request("GET", path)

    def post(self, path: str, payload: dict) -> tuple[int, object]:
        return self.request("POST", path, payload)


def _expect_2xx(status: int, body: object, what: str) -> object:
    if not (200 <= status < 300):
        raise ApiError(f"{what} -> HTTP {status}: {body!r}")
    return body


def ensure_configuration(client: ApiClient) -> None:
    """Step 1: ensure the demo-project/demo-site configuration exists.

    GET /api/v1/configuration auto-creates the default snapshot for the
    requested project/site on first read, so a plain GET is the idempotent
    "ensure it exists" operation.
    """
    path = f"/api/v1/configuration?project_id={DEMO_PROJECT_ID}&site_id={DEMO_SITE_ID}"
    status, body = client.get(path)
    _expect_2xx(status, body, "GET /api/v1/configuration")
    print(f"  [ok] configuration ensured for {DEMO_PROJECT_ID}/{DEMO_SITE_ID}")


def _poll_run(client: ApiClient, collection: str, run_id: str) -> dict:
    """Poll /api/v1/{collection}/runs/{id} until terminal; return the run dict."""
    terminal = {"succeeded", "failed", "cancelled"}
    last: dict = {}
    for _ in range(POLL_ATTEMPTS):
        status, body = client.get(f"/api/v1/{collection}/runs/{run_id}")
        if 200 <= status < 300 and isinstance(body, dict):
            last = body
            if body.get("status") in terminal:
                return body
        time.sleep(POLL_INTERVAL_S)
    return last


def seed_udmi_validation(client: ApiClient) -> bool:
    """Step 2: run a UDMI validation against the bundled fixture (inline, no net)."""
    payload = {
        "project_id": DEMO_PROJECT_ID,
        "site_id": DEMO_SITE_ID,
        "job_type": "udmi_validation",
        "parameters": {"requested_from": "seed_demo"},
    }
    status, body = client.post("/api/v1/validation/udmi/runs", payload)
    _expect_2xx(status, body, "POST /api/v1/validation/udmi/runs")
    if not isinstance(body, dict) or not body.get("run_id"):
        raise ApiError(f"UDMI run create returned no run_id: {body!r}")
    run_id = body["run_id"]

    run = _poll_run(client, "validation", run_id)
    run_status = run.get("status") if isinstance(run, dict) else None
    if run_status != "succeeded":
        print(f"  [warn] UDMI validation run {run_id} status={run_status!r} (expected succeeded)")
        return False

    summary = run.get("result_summary", {}) if isinstance(run, dict) else {}
    issue_count = summary.get("issue_count", "unknown")
    expected_devices = summary.get("expected_devices", "unknown")
    print(
        f"  [ok] UDMI validation run {run_id}: succeeded "
        f"(expected_devices={expected_devices}, issue_count={issue_count})"
    )
    return True


def seed_ip_discovery_dry_run(client: ApiClient) -> bool:
    """Step 3: dry-run IP discovery (no scan, no authorization required)."""
    payload = {
        "project_id": DEMO_PROJECT_ID,
        "site_id": DEMO_SITE_ID,
        "job_type": "ip_discovery",
        "parameters": {
            "dry_run": True,
            "cidr": "192.0.2.0/30",  # TEST-NET-1, reserved/non-routable
            "ports": [47808, 1883],  # BACnet/IP, MQTT
        },
    }
    status, body = client.post("/api/v1/discovery/ip/runs", payload)
    _expect_2xx(status, body, "POST /api/v1/discovery/ip/runs")
    if not isinstance(body, dict) or not body.get("run_id"):
        raise ApiError(f"IP discovery run create returned no run_id: {body!r}")
    run_id = body["run_id"]

    run = _poll_run(client, "discovery", run_id)
    run_status = run.get("status") if isinstance(run, dict) else None
    plan = {}
    if isinstance(run, dict):
        plan = run.get("result_summary", {}).get("dry_run_plan", {}) or {}
    target_count = plan.get("target_count")
    if run_status != "succeeded" or not target_count:
        print(
            f"  [warn] dry-run IP discovery {run_id} status={run_status!r}, "
            f"target_count={target_count!r} (expected succeeded with a plan)"
        )
        return False
    print(
        f"  [ok] dry-run IP discovery run {run_id}: plan with "
        f"target_count={target_count} (no packets sent)"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed demo data into a running Smart Commissioning App via its HTTP API.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SC_BASE_URL", DEFAULT_BASE_URL),
        help=f"API base URL (default {DEFAULT_BASE_URL}, env SC_BASE_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SC_API_KEY"),
        help="API key for the hosted profile (env SC_API_KEY). Omit for local/loopback.",
    )
    args = parser.parse_args()

    client = ApiClient(args.base_url, args.api_key)
    mode = "hosted (X-API-Key)" if client.api_key else "local/loopback (no key)"
    print(f"Seeding demo data against {client.base_url}  [auth: {mode}]")
    print("-" * 50)

    # Fail fast with a clear message if the stack is not reachable / not ready.
    try:
        status, body = client.get("/api/v1/health")
        _expect_2xx(status, body, "GET /api/v1/health")
    except ApiError as error:
        print(f"  [error] API is not reachable: {error}")
        print("  Is the stack running? See docs/quickstart.md.")
        return 1

    ok = True
    try:
        ensure_configuration(client)
        ok = seed_udmi_validation(client) and ok
        ok = seed_ip_discovery_dry_run(client) and ok
    except ApiError as error:
        print(f"  [error] {error}")
        return 1

    print("-" * 50)
    if ok:
        print("Demo seed complete. Open the UI to see the configuration, the UDMI")
        print("validation run with issues, and the dry-run IP discovery plan.")
        return 0
    print("Demo seed finished with warnings (see above).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
