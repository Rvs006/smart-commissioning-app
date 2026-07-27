#!/usr/bin/env python3
"""Exercise queued and inline hosted execution through the public HTTP API."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL = {"succeeded", "failed", "cancelled"}
PROJECT_ID = "hosted-release-acceptance"
SITE_ID = "isolated-release-lab"


class AcceptanceError(RuntimeError):
    """Raised when a hosted release invariant is not observed."""


class ApiClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise AcceptanceError(
                f"{method} {path} returned HTTP {error.code}: {detail[:500]}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise AcceptanceError(f"{method} {path} failed: {type(error).__name__}") from error
        if not isinstance(payload, dict):
            raise AcceptanceError(f"{method} {path} returned a non-object response")
        return payload


def _canonical_request() -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "site_id": SITE_ID,
        "job_type": "udmi_validation",
        "parameters": {
            "requested_from": "hosted_release_acceptance",
            "expected_schedule": {
                "asset_id": "ACCEPTANCE-1",
                "udmi_version": "1.5.2",
                "units": {},
            },
            "state_payload": {
                "version": "1.5.2",
                "timestamp": "2026-07-27T00:00:00Z",
                "system": {
                    "serial_no": "ACCEPTANCE-1",
                    "last_config": "2026-07-27T00:00:00Z",
                    "hardware": {"make": "Release", "model": "Hosted"},
                    "software": {},
                    "operation": {"operational": True},
                    "not_in_udmi_schema": True,
                },
            },
        },
    }


def _mqtt_request(capture_seconds: int) -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "site_id": SITE_ID,
        "job_type": "mqtt_discovery",
        "parameters": {
            "requested_from": "hosted_release_acceptance",
            "authorized": True,
            "broker_host": "mqtt-acceptance",
            "broker_port": 1883,
            "use_tls": False,
            "topic_filter": "release/quiet/#",
            "capture_seconds": capture_seconds,
            "max_messages": 25,
        },
    }


def _run_path(kind: str, run_id: str) -> str:
    if kind not in {"validation", "discovery"}:
        raise AcceptanceError(f"unknown run kind {kind!r}")
    return f"{kind}/runs/{run_id}"


def _create(client: ApiClient, *, kind: str, body: dict[str, object]) -> tuple[str, dict]:
    route = "validation/udmi/runs" if kind == "validation" else "discovery/mqtt/runs"
    accepted = client.request("POST", route, body)
    run_id = accepted.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AcceptanceError("accepted response omitted run_id")
    return run_id, accepted


def _wait_for_status(
    client: ApiClient,
    *,
    kind: str,
    run_id: str,
    wanted: set[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = client.request("GET", _run_path(kind, run_id))
        if latest.get("status") in wanted:
            return latest
        time.sleep(1)
    raise AcceptanceError(
        f"run {run_id} did not reach {sorted(wanted)} within {timeout:g}s; "
        f"last status={latest.get('status')!r} stage={latest.get('stage')!r}"
    )


def _execution_mode(run: dict[str, Any]) -> str | None:
    direct = run.get("execution_mode")
    summary = run.get("result_summary")
    if direct is None and isinstance(summary, dict):
        direct = summary.get("execution_mode")
    return direct if isinstance(direct, str) else None


def _assert_execution_mode(run: dict[str, Any], expected: str) -> None:
    actual = _execution_mode(run)
    if actual != expected:
        raise AcceptanceError(f"run executed in {actual!r}, expected {expected!r}")


def _assert_no_lease_failure(run: dict[str, Any]) -> None:
    text = " ".join(
        str(run.get(name) or "") for name in ("stage", "error_message", "result_summary")
    ).casefold()
    forbidden = ("lease_expired", "lease expired", "execution owner lease expired")
    if any(value in text for value in forbidden):
        raise AcceptanceError("a live hosted run was terminalized by lease expiry")


def _terminal_snapshot(run: dict[str, Any]) -> dict[str, object]:
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "stage": run.get("stage"),
        "progress_percent": run.get("progress_percent"),
        "error_message": run.get("error_message"),
        "result_summary": run.get("result_summary"),
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_run_reference(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AcceptanceError("run reference is not an object")
    run_id = payload.get("run_id")
    kind = payload.get("kind")
    if not isinstance(run_id, str) or kind not in {"validation", "discovery"}:
        raise AcceptanceError("run reference lacks run_id or kind")
    return run_id, str(kind)


def _canonical(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, _ = _create(client, kind="validation", body=_canonical_request())
    run = _wait_for_status(
        client, kind="validation", run_id=run_id, wanted=TERMINAL, timeout=args.timeout
    )
    if run.get("status") != "succeeded":
        raise AcceptanceError(f"canonical UDMI run ended {run.get('status')!r}")
    _assert_execution_mode(run, args.expected_execution_mode)
    _assert_no_lease_failure(run)
    if args.output is not None:
        _write_json(args.output, _terminal_snapshot(run))
    print(f"hosted canonical smoke: OK ({run_id})")


def _long_capture(client: ApiClient, args: argparse.Namespace) -> None:
    if args.capture_seconds <= args.minimum_running_seconds:
        raise AcceptanceError("capture seconds must exceed the minimum running observation")
    run_id, _ = _create(
        client, kind="discovery", body=_mqtt_request(args.capture_seconds)
    )
    _wait_for_status(client, kind="discovery", run_id=run_id, wanted={"running"}, timeout=60)
    observed_at = time.monotonic()
    while time.monotonic() - observed_at < args.minimum_running_seconds:
        run = client.request("GET", _run_path("discovery", run_id))
        if run.get("status") != "running":
            raise AcceptanceError(
                f"long capture left running after {time.monotonic() - observed_at:.1f}s: "
                f"{run.get('status')!r}"
            )
        _assert_no_lease_failure(run)
        time.sleep(1)
    run = _wait_for_status(
        client, kind="discovery", run_id=run_id, wanted=TERMINAL, timeout=args.timeout
    )
    if run.get("status") != "failed":
        raise AcceptanceError(f"silent capture ended {run.get('status')!r}, expected failed")
    _assert_execution_mode(run, args.expected_execution_mode)
    _assert_no_lease_failure(run)
    print(
        f"hosted long silent capture: OK ({run_id}, "
        f">={args.minimum_running_seconds:g}s running)"
    )


def _cancel_capture(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, _ = _create(client, kind="discovery", body=_mqtt_request(600))
    _wait_for_status(client, kind="discovery", run_id=run_id, wanted={"running"}, timeout=60)
    client.request("POST", f"runs/{run_id}/cancel", {})
    run = _wait_for_status(
        client, kind="discovery", run_id=run_id, wanted=TERMINAL, timeout=args.timeout
    )
    if run.get("status") != "cancelled":
        raise AcceptanceError(f"cancelled capture ended {run.get('status')!r}")
    _assert_execution_mode(run, args.expected_execution_mode)
    _assert_no_lease_failure(run)
    print(f"hosted cancellation: OK ({run_id})")


def _deferred_create(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, accepted = _create(client, kind="validation", body=_canonical_request())
    message = str(accepted.get("message") or "").casefold()
    if accepted.get("status") != "queued" or "pending automatic retry" not in message:
        raise AcceptanceError("Redis outage did not leave one durable pending dispatch")
    _write_json(args.output, {"run_id": run_id, "kind": "validation"})
    print(f"hosted deferred publication: OK ({run_id})")


def _wait_canonical(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, kind = _read_run_reference(args.input)
    run = _wait_for_status(client, kind=kind, run_id=run_id, wanted=TERMINAL, timeout=args.timeout)
    if run.get("status") != "succeeded":
        raise AcceptanceError(f"deferred canonical run ended {run.get('status')!r}")
    _assert_execution_mode(run, args.expected_execution_mode)
    _assert_no_lease_failure(run)
    print(f"hosted deferred publication recovery: OK ({run_id})")


def _worker_capture(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, _ = _create(client, kind="discovery", body=_mqtt_request(600))
    _wait_for_status(client, kind="discovery", run_id=run_id, wanted={"running"}, timeout=60)
    _write_json(args.output, {"run_id": run_id, "kind": "discovery"})
    print(f"hosted active capture started: OK ({run_id})")


def _cancel_existing_capture(client: ApiClient, args: argparse.Namespace) -> None:
    """Prove a previously-running capture survived an API restart, then stop it."""

    run_id, kind = _read_run_reference(args.input)
    if kind != "discovery":
        raise AcceptanceError("existing capture reference is not a discovery run")
    running = client.request("GET", _run_path(kind, run_id))
    if running.get("status") != "running":
        raise AcceptanceError(
            f"capture did not remain active across API restart: {running.get('status')!r}"
        )
    _assert_no_lease_failure(running)
    client.request("POST", f"runs/{run_id}/cancel", {})
    terminal = _wait_for_status(
        client, kind=kind, run_id=run_id, wanted=TERMINAL, timeout=args.timeout
    )
    if terminal.get("status") != "cancelled":
        raise AcceptanceError(
            f"capture surviving API restart ended {terminal.get('status')!r}"
        )
    _assert_execution_mode(terminal, args.expected_execution_mode)
    _assert_no_lease_failure(terminal)
    print(f"hosted active-run continuity: OK ({run_id})")


def _worker_recovery(client: ApiClient, args: argparse.Namespace) -> None:
    run_id, kind = _read_run_reference(args.input)
    run = _wait_for_status(client, kind=kind, run_id=run_id, wanted=TERMINAL, timeout=args.timeout)
    recovery_stages = {"lease_expired", "worker_heartbeat_expired"}
    if (
        run.get("status") != "failed"
        or run.get("stage") not in recovery_stages
        or not run.get("error_message")
    ):
        raise AcceptanceError(
            f"dead worker recovery ended {run.get('status')!r}/{run.get('stage')!r}"
        )
    _write_json(args.output, _terminal_snapshot(run))
    print(f"hosted dead-worker recovery: OK ({run_id})")


def _assert_snapshot(client: ApiClient, args: argparse.Namespace) -> None:
    expected = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(expected, dict) or not isinstance(expected.get("run_id"), str):
        raise AcceptanceError("terminal snapshot is invalid")
    run = client.request("GET", _run_path(args.kind, expected["run_id"]))
    if _terminal_snapshot(run) != expected:
        raise AcceptanceError("duplicate delivery changed immutable terminal evidence")
    print(f"hosted duplicate-delivery fencing: OK ({expected['run_id']})")


def _common(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    subparser.add_argument("--api-key", required=True)
    subparser.add_argument("--timeout", type=float, default=180)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    canonical = commands.add_parser("canonical")
    _common(canonical)
    canonical.add_argument("--expected-execution-mode", required=True)
    canonical.add_argument("--output", type=Path)
    canonical.set_defaults(handler=_canonical)

    long_capture = commands.add_parser("long-capture")
    _common(long_capture)
    long_capture.add_argument("--expected-execution-mode", required=True)
    long_capture.add_argument("--capture-seconds", type=int, default=70)
    long_capture.add_argument("--minimum-running-seconds", type=float, default=65)
    long_capture.set_defaults(handler=_long_capture)

    cancellation = commands.add_parser("cancel-capture")
    _common(cancellation)
    cancellation.add_argument("--expected-execution-mode", required=True)
    cancellation.set_defaults(handler=_cancel_capture)

    deferred = commands.add_parser("deferred-create")
    _common(deferred)
    deferred.add_argument("--output", type=Path, required=True)
    deferred.set_defaults(handler=_deferred_create)

    wait_canonical = commands.add_parser("wait-canonical")
    _common(wait_canonical)
    wait_canonical.add_argument("--input", type=Path, required=True)
    wait_canonical.add_argument("--expected-execution-mode", required=True)
    wait_canonical.set_defaults(handler=_wait_canonical)

    worker = commands.add_parser("worker-capture")
    _common(worker)
    worker.add_argument("--output", type=Path, required=True)
    worker.set_defaults(handler=_worker_capture)

    cancel_existing = commands.add_parser("cancel-existing")
    _common(cancel_existing)
    cancel_existing.add_argument("--input", type=Path, required=True)
    cancel_existing.add_argument("--expected-execution-mode", required=True)
    cancel_existing.set_defaults(handler=_cancel_existing_capture)

    recovery = commands.add_parser("wait-worker-recovery")
    _common(recovery)
    recovery.add_argument("--input", type=Path, required=True)
    recovery.add_argument("--output", type=Path, required=True)
    recovery.set_defaults(handler=_worker_recovery)

    snapshot = commands.add_parser("assert-snapshot")
    _common(snapshot)
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--kind", choices=("validation", "discovery"), default="discovery")
    snapshot.set_defaults(handler=_assert_snapshot)

    args = parser.parse_args(argv)
    client = ApiClient(args.base_url, args.api_key)
    try:
        args.handler(client, args)
    except (AcceptanceError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
