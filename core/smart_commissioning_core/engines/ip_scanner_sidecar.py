"""``ip_scanner`` sidecar adapter engine.

Drive the vendored ``network-ip-scanner`` Node app over its loopback HTTP API
and project its ``{rows, summary}`` output into the shared ``EngineResult``
shape (discovered assets + ``DiscoveredDevice`` records + validation issues).

Ownership split: ``SidecarSupervisor`` (backend) owns the sidecar *process* and
its port. This engine only speaks to an already-running loopback base URL that
the scanners route passes in — it never spawns a process, so it stays
network/process-light and unit-testable like every other engine.

HONESTY: when the sidecar is unreachable, mid-scan-dead, or authorization is
missing, this engine records a real failed / unauthorized run. It never
fabricates rows. Every value that reaches a persisted record is passed through
``json_safe_value`` first (the v0.1.16 raw-value lesson) so a hostile field can
never fail the persist step.

The transport is injectable (``sidecar_client``) so tests drive the mapping with
no HTTP and no live Node process. The built-in client speaks the driving
contract in ``scanners/vendor/network-ip-scanner`` using only stdlib.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    ThrottleConfig,
    make_cancel_checker,
    run_engine,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import json_safe_value

ENGINE_NAME = "ip_scanner"

# The vendored sidecar's register template columns (verbatim), in order. The
# ``ip_scanner_register`` import profile uses these same names, so a run's
# accepted register rows re-serialize straight back into the sidecar with no
# field translation. This tuple is the golden contract asserted by the tests.
REGISTER_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "IP Address",
    "Hostname",
    "Type",
    "Vendor",
    "Model",
    "Expected Ports",
    "Project",
    "Location",
    "Description",
)

# RAG severity: red is a hard fault, amber a partial; green/none raise no issue.
_RAG_SEVERITY = {"red": "high", "amber": "medium"}

# Transport seam: given (base_url, register_rows, scan_query, is_cancelled)
# return the sidecar's ``{"rows": [...], "summary": {...}}``. Injectable so
# tests exercise the mapping with no HTTP.
SidecarClient = Callable[..., dict[str, Any]]


class SidecarTransportError(RuntimeError):
    """The sidecar could not be reached or returned a scan error (honest fail)."""


def process_ip_scanner_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: Any,
    execution_mode: str,
    throttle: Any = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    sidecar_base_url: str | None = None,
    sidecar_client: SidecarClient | None = None,
    import_loader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> Any:
    """Run a sidecar-backed IP scan through the shared engine lifecycle.

    Mirrors ``process_ip_discovery_run``: build an :class:`EngineContext`,
    define the engine coroutine, hand both to :func:`run_engine`.

    Args:
        sidecar_base_url: loopback URL (``http://127.0.0.1:<port>``) the
            scanners route resolved from ``SidecarSupervisor``. ``None`` when the
            sidecar is unavailable — a live scan then fails honestly.
        sidecar_client: transport override for tests.
        import_loader: accepted-row loader (``ImportRepository.get_accepted_rows``)
            used to fetch the bound ``ip_scanner_register`` rows by import id.
    """
    is_cancelled = make_cancel_checker(run_store, run_id)
    ctx = EngineContext(
        run_id=run_id,
        parameters=dict(parameters or {}),
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle or ThrottleConfig(),
        dry_run=dry_run,
        _is_cancelled=is_cancelled,
    )

    async def engine(engine_ctx: EngineContext) -> EngineResult:
        return await _run_ip_scanner(
            engine_ctx,
            base_url=sidecar_base_url,
            client=sidecar_client,
            import_loader=import_loader,
        )

    if persist_records is None:
        return run_engine(ctx, engine)
    return run_engine(ctx, engine, persist_records=persist_records)


async def _run_ip_scanner(
    ctx: EngineContext,
    *,
    base_url: str | None,
    client: SidecarClient | None,
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> EngineResult:
    scan_query = _scan_query(ctx.parameters)

    # DRY RUN: no I/O, enumerate the plan (safety.build_dry_run_plan convention).
    if ctx.dry_run:
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=[scan_query.get("start", ""), scan_query.get("end", "")],
            actions=["sidecar-register-import", "sidecar-scan", "sidecar-register-clear"],
            notes="No packets sent in dry run; the sidecar performs the real scan.",
        )
        return EngineResult(result_summary_extra={"dry_run_plan": plan, "hosts_scanned": 0})

    # Authorization gates any real I/O (defense in depth with the route gate).
    require_scan_authorization(ctx.parameters)

    if not scan_query.get("start"):
        return EngineResult(
            status_override="failed",
            error_message="No scan range was provided (a start IP is required).",
        )

    if client is None:
        if base_url is None:
            # Honest failure: the sidecar is not available on this host.
            return EngineResult(
                status_override="failed",
                error_message="IP scanner sidecar is not available on this host.",
            )
        client = _default_sidecar_client

    register_rows = _load_register_rows(ctx.parameters, import_loader)

    try:
        payload = client(
            base_url=base_url,
            register_rows=register_rows,
            scan_query=scan_query,
            is_cancelled=ctx.is_cancelled,
        )
    except SidecarTransportError as error:
        return EngineResult(status_override="failed", error_message=str(error))

    rows = payload.get("rows") or []
    summary = payload.get("summary") or {}
    return _map_result(rows, summary, ctx.parameters)


# --------------------------------------------------------------------------
# Pure projection: {rows, summary} -> EngineResult. No I/O, unit-testable.
# --------------------------------------------------------------------------


def _map_result(
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> EngineResult:
    """Project the sidecar's compare() output into the shared record shapes."""
    project_id = parameters.get("project_id")
    site_id = parameters.get("site_id")
    now = datetime.now(UTC).isoformat()

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    issues: list[ValidationIssueRecord] = []

    for row in rows:
        register = row.get("register")
        rag = row.get("rag")
        ip = row.get("ip")
        # "missing" = expected-but-unreachable: it is an issue, not a device.
        is_device = register != "missing" and row.get("status") != "unreachable"

        if is_device:
            open_ports = list(row.get("openPorts") or [])
            discovered_assets.append(
                json_safe_value(
                    {
                        "asset_id": None,
                        "ip_address": ip,
                        "mac_address": row.get("mac"),
                        "hostname": row.get("discoveredHostname") or row.get("hostname"),
                        "observed_ports": open_ports,
                        "match_basis": "ip",
                        "status_detail": _status_detail(row),
                        "last_seen_at": now,
                    }
                )
            )
            structured_records.append(
                json_safe_value(
                    {
                        "project_id": project_id,
                        "site_id": site_id,
                        "address": ip,
                        # rogue rows carry no type -> the neutral ip_host default.
                        "device_type": row.get("type") or "ip_host",
                        "name": row.get("discoveredHostname") or row.get("hostname") or None,
                        "vendor": row.get("vendor") or None,
                        "model": row.get("model") or None,
                        # Everything the fixed device columns cannot hold nests
                        # under attributes (repositories.py device-column rule).
                        "attributes": {
                            "rag": rag,
                            "register": register,
                            "status": row.get("status"),
                            "hostname_status": row.get("hostnameStatus"),
                            "hostname_src": row.get("hostnameSrc"),
                            "expected_hostname": row.get("expectedHostname"),
                            "mac_address": row.get("mac"),
                            "latency": row.get("latency"),
                            "open_ports": open_ports,
                            "open_tcp": list(row.get("openTcp") or []),
                            "open_udp": list(row.get("openUdp") or []),
                            "services": list(row.get("services") or []),
                            "expected_ports": list(row.get("expectedPorts") or []),
                            "missing_ports": list(row.get("missingPorts") or []),
                            "extra_ports": list(row.get("extraPorts") or []),
                            "banner": row.get("banner"),
                            "discovered_by": row.get("discoveredBy"),
                            "project": row.get("project"),
                            "location": row.get("location"),
                            "description": row.get("description"),
                        },
                    }
                )
            )

        severity = _RAG_SEVERITY.get(str(rag))
        if severity is not None:
            issues.append(_issue_for_row(row, severity, now))

    result_summary_extra = json_safe_value(
        {
            "hosts_scanned": summary.get("reachable"),
            "hosts_responsive": summary.get("expectedReachable"),
            "register_expected": summary.get("expected"),
            "register_matches": summary.get("matches"),
            "register_partial": summary.get("partial"),
            "register_missing": summary.get("missing"),
            "register_rogue": summary.get("rogue"),
            "scanner": ENGINE_NAME,
        }
    )
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        issues=issues,
        result_summary_extra=result_summary_extra,
    )


def _status_detail(row: Mapping[str, Any]) -> str | None:
    status = row.get("status")
    register = row.get("register")
    if status is None and register is None:
        return None
    return f"{status or 'unknown'}/{register or 'none'}"


def _issue_for_row(row: Mapping[str, Any], severity: str, now: str) -> ValidationIssueRecord:
    register = str(row.get("register") or "none")
    ip = row.get("ip")
    expected = _join_ports(row.get("expectedPorts"))
    observed = _join_ports(row.get("openPorts"))
    description = _issue_description(register, ip, row)
    return ValidationIssueRecord(
        issue_id=f"ip_scanner:{register}:{ip or '—'}",
        asset_id=None,
        issue_type=f"ip_scanner_{register}",
        severity=severity,  # type: ignore[arg-type]  (validated by the literal)
        description=description,
        status=str(row.get("status") or ""),
        expected_value=expected or None,
        observed_value=observed or None,
        match_basis="ip",
        status_detail=_status_detail(row),
        last_seen_at=datetime.fromisoformat(now),
    )


def _issue_description(register: str, ip: Any, row: Mapping[str, Any]) -> str:
    label = ip or "an expected device"
    if register == "missing":
        return f"Expected device {label} was not reachable during the scan."
    if register == "partial":
        missing = _join_ports(row.get("missingPorts"))
        extra = _join_ports(row.get("extraPorts"))
        detail = []
        if missing:
            detail.append(f"missing ports {missing}")
        if extra:
            detail.append(f"unexpected ports {extra}")
        suffix = f" ({'; '.join(detail)})" if detail else ""
        return f"Device {label} partially matches its register entry{suffix}."
    if register == "rogue":
        return f"Unregistered (rogue) device {label} responded on the network."
    return f"Device {label} raised an issue ({register})."


def _join_ports(value: Any) -> str:
    if not value:
        return ""
    return ",".join(str(item) for item in value)


# --------------------------------------------------------------------------
# Register serialization + scan-query parsing.
# --------------------------------------------------------------------------


def _load_register_rows(
    parameters: Mapping[str, Any],
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Load the run's bound ``ip_scanner_register`` accepted rows, if any."""
    import_id = parameters.get("register_import_id")
    if not isinstance(import_id, str) or not import_id.strip() or import_loader is None:
        return []
    try:
        return [dict(row) for row in import_loader(import_id)]
    except FileNotFoundError:
        return []


def register_rows_from_devices(devices: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Project persisted ip_scanner device dicts into ip_scanner_register rows.

    Parity port of the vendored app's Save-as-Register: each responding device's
    actual open ports become its Expected Ports, and its RESOLVED hostname (never
    an unverified expected one) becomes Hostname. Rows are keyed by
    ``REGISTER_TEMPLATE_COLUMNS`` so ``_register_csv(register_rows_from_devices(...))``
    yields exactly the sidecar's register CSV, which the import profile then
    accepts and re-serialises back into the sidecar on the next scan (round-trip).

    Every cell is a ``str`` by construction, so the values are json-safe without a
    ``json_safe_value`` wrap; the import pipeline re-parses them from CSV anyway.
    Devices the scan could not reach are skipped defensively (``_map_result``
    already keeps ``missing``/``unreachable`` rows out of the persisted set, so a
    register only ever lists devices that actually answered).
    """

    def _cell(value: Any) -> str:
        return "" if value is None else str(value)

    rows: list[dict[str, str]] = []
    for device in devices:
        attributes = device.get("attributes") or {}
        if attributes.get("register") == "missing" or attributes.get("status") == "unreachable":
            continue
        device_type = device.get("device_type") or ""
        # An unverified name came from the register's EXPECTED hostname, not an
        # observation; recording it as the new register's Hostname would launder
        # an expectation into "observed truth", so blank it (mirrors the vendored app).
        hostname = "" if attributes.get("hostname_status") == "unverified" else _cell(device.get("name"))
        open_ports = attributes.get("open_ports") or []
        rows.append(
            {
                "IP Address": _cell(device.get("address")),
                "Hostname": hostname,
                # rogue / plain hosts carry the neutral ip_host type -> blank.
                "Type": "" if device_type in ("", "ip_host") else device_type,
                "Vendor": _cell(device.get("vendor")),
                "Model": _cell(device.get("model")),
                "Expected Ports": ";".join(str(port) for port in open_ports),
                "Project": _cell(attributes.get("project")),
                "Location": _cell(attributes.get("location")),
                "Description": _cell(attributes.get("description")),
            }
        )
    return rows


def _register_csv(register_rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize accepted register rows to the sidecar's 9-column CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REGISTER_TEMPLATE_COLUMNS)
    for row in register_rows:
        writer.writerow([str(row.get(column, "") or "") for column in REGISTER_TEMPLATE_COLUMNS])
    return buffer.getvalue()


def _scan_query(parameters: Mapping[str, Any]) -> dict[str, str]:
    """Build the ``/api/scan`` query params from the run parameters.

    Accepts ``start_ip``/``end_ip`` (SCT convention) or ``start``/``end`` (the
    sidecar's own names). Only ``start`` is required by the sidecar.
    """
    query: dict[str, str] = {}
    start = parameters.get("start_ip") or parameters.get("start")
    end = parameters.get("end_ip") or parameters.get("end")
    if start:
        query["start"] = str(start)
    if end:
        query["end"] = str(end)
    for key in ("timeout", "udpPorts", "concurrency"):
        value = parameters.get(key)
        if value not in (None, ""):
            query[key] = str(value)
    return query


# --------------------------------------------------------------------------
# Built-in stdlib transport (register import -> SSE scan -> register clear).
# --------------------------------------------------------------------------


def _default_sidecar_client(
    *,
    base_url: str,
    register_rows: Sequence[Mapping[str, Any]],
    scan_query: Mapping[str, str],
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Speak the driving contract over stdlib HTTP + SSE. Honest failures only."""
    base = base_url.rstrip("/")
    try:
        _post_register(base, _register_csv(register_rows))
        payload = _stream_scan(base, scan_query, is_cancelled)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise SidecarTransportError(
            "The IP scanner sidecar could not be reached during the scan."
        ) from error
    finally:
        _delete_register(base)  # best-effort; never masks the primary error
    return payload


def _post_register(base: str, csv_text: str) -> None:
    request = urllib.request.Request(  # noqa: S310 (fixed loopback URL)
        f"{base}/api/register",
        data=csv_text.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "text/csv; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10.0):  # noqa: S310
        return


def _delete_register(base: str) -> None:
    try:
        request = urllib.request.Request(f"{base}/api/register", method="DELETE")  # noqa: S310
        with urllib.request.urlopen(request, timeout=5.0):  # noqa: S310
            return
    except (urllib.error.URLError, OSError):
        return  # cleanup is best-effort


def _stream_scan(
    base: str,
    scan_query: Mapping[str, str],
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Consume the SSE ``/api/scan`` stream; return the final {rows, summary}.

    Checks cooperative cancellation between events and closes the stream on
    cancel. The server injects a ``result`` event (compare output) then
    ``complete``; ``error`` raises.
    """
    url = f"{base}/api/scan?{urllib.parse.urlencode(dict(scan_query))}"
    result: dict[str, Any] = {"rows": [], "summary": {}}
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=None) as stream:  # noqa: S310
        for raw in stream:
            if is_cancelled():
                break  # closing the stream context cancels the server side
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "result":
                result = {"rows": event.get("rows") or [], "summary": event.get("summary") or {}}
            elif kind == "error":
                raise SidecarTransportError(
                    "The IP scanner sidecar reported a scan error."
                )
            elif kind == "complete":
                break
    return result


def _demo() -> None:
    """Assert-based self-check for the pure mapping + register serialization."""
    rows = [
        {"ip": "192.0.2.10", "register": "match", "rag": "green", "status": "reachable",
         "hostname": "h-a", "openPorts": [80, 443], "expectedPorts": [80, 443]},
        {"ip": "192.0.2.11", "register": "partial", "rag": "amber", "status": "reachable",
         "hostname": "h-b", "openPorts": [80], "expectedPorts": [80, 443], "missingPorts": [443]},
        {"ip": "192.0.2.12", "register": "missing", "rag": "red", "status": "unreachable",
         "expectedPorts": [102], "openPorts": []},
        {"ip": "192.0.2.99", "register": "rogue", "rag": "red", "status": "rogue",
         "openPorts": [23], "extraPorts": [23]},
    ]
    summary = {"expected": 3, "reachable": 3, "expectedReachable": 2,
               "matches": 1, "partial": 1, "missing": 1, "rogue": 1}
    result = _map_result(rows, summary, {"project_id": "p", "site_id": "s"})

    # missing (unreachable) is an issue, not a device; the other 3 are devices.
    assert len(result.discovered_assets) == 3, result.discovered_assets
    assert len(result.structured_records) == 3, result.structured_records
    addresses = {r["address"] for r in result.structured_records}
    assert "192.0.2.12" not in addresses, addresses
    # green raises no issue; amber + 2 reds do.
    assert len(result.issues) == 3, result.issues
    severities = sorted(i.severity for i in result.issues)
    assert severities == ["high", "high", "medium"], severities
    # rogue device falls back to the neutral device_type.
    rogue = next(r for r in result.structured_records if r["address"] == "192.0.2.99")
    assert rogue["device_type"] == "ip_host", rogue
    # extra keys nest under attributes, never as loose device columns.
    assert set(rogue) <= {"project_id", "site_id", "address", "device_type", "name",
                          "vendor", "model", "attributes"}, set(rogue)

    csv_text = _register_csv([
        {"IP Address": "192.0.2.10", "Hostname": "h-a", "Expected Ports": "80;443"},
    ])
    header = csv_text.splitlines()[0]
    assert header == ",".join(REGISTER_TEMPLATE_COLUMNS), header
    assert "192.0.2.10" in csv_text

    # Save-as-register: persisted devices -> ip_scanner_register rows. Each row
    # carries the 9 golden columns; open ports become Expected Ports; an
    # unverified hostname is blanked; unreachable/missing rows are dropped.
    devices = [
        {"address": "192.0.2.10", "name": "h-a", "vendor": "Acme", "model": "X1",
         "device_type": "server", "attributes": {"open_ports": [80, 443], "hostname_status": "match",
                                                  "project": "P1", "location": "Rack 1", "description": "web"}},
        {"address": "192.0.2.11", "name": "expected-b", "vendor": None, "model": None,
         "device_type": "ip_host", "attributes": {"open_ports": [], "hostname_status": "unverified"}},
        {"address": "192.0.2.12", "name": None, "device_type": "ip_host",
         "attributes": {"register": "missing", "status": "unreachable", "open_ports": []}},
    ]
    reg = register_rows_from_devices(devices)
    assert len(reg) == 2, reg  # unreachable/missing dropped
    assert set(reg[0]) == set(REGISTER_TEMPLATE_COLUMNS), reg[0]
    assert reg[0]["IP Address"] == "192.0.2.10" and reg[0]["Expected Ports"] == "80;443", reg[0]
    assert reg[0]["Type"] == "server" and reg[0]["Hostname"] == "h-a", reg[0]
    assert reg[0]["Project"] == "P1" and reg[0]["Location"] == "Rack 1", reg[0]
    assert reg[1]["Hostname"] == "", reg[1]  # unverified name never laundered in
    assert reg[1]["Type"] == "" and reg[1]["Expected Ports"] == "", reg[1]
    # The projection round-trips through the sidecar's own register CSV.
    assert _register_csv(reg).splitlines()[0] == ",".join(REGISTER_TEMPLATE_COLUMNS)

    # scan-query accepts both naming conventions; start is mandatory upstream.
    assert _scan_query({"start_ip": "192.0.2.1", "end_ip": "192.0.2.9"})["start"] == "192.0.2.1"
    assert "end" not in _scan_query({"start": "10.0.0.1"})  # end omitted -> single-host
    print("ip_scanner_sidecar self-check OK")


if __name__ == "__main__":
    _demo()
