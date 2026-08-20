"""``bacnet_scanner`` sidecar adapter engine.

Drive the vendored ``bacnet-scanner`` Node app over its loopback HTTP API and
project its scan (``{rows, summary}``) + per-asset export JSON into the shared
``EngineResult`` shape: discovered assets, ``DiscoveredDevice`` rows, then
``DiscoveredPoint`` rows in ONE ``structured_records`` list so the existing
device/point key-sniff persister splits them by table.

Ownership split matches the IP adapter: ``SidecarSupervisor`` (backend) owns the
sidecar *process* and its port; this engine speaks only to an already-running
loopback base URL the scanners route passes in. It never spawns a process, so it
stays network/process-light and unit-testable like every other engine.

HONESTY: when the sidecar is unreachable, when the frozen Source Interface does
not match any sidecar adapter, or when authorization is missing, this engine
records a real failed / unauthorized run. It NEVER fabricates rows or guesses a
NIC. Every value that reaches a persisted record is passed through
``json_safe_value`` first (the v0.1.16 raw-value lesson).

Two BACnet-specific bounds the IP adapter does not need:
  * The sidecar's ``readObjectList`` has NO server-side cancel — once a device
    enumeration starts it runs to completion. So the ``/api/export`` reader
    enforces its OWN wall-clock deadline and abandons the SSE stream (closing
    the socket) on run-cancel or deadline, halting progression to the NEXT
    device (see the driving contract §5).
  * The frozen source NIC is resolved to the sidecar's adapter index via
    ``/api/adapters`` and the run FAILS on mismatch — never a guessed index.

The transport is injectable (``sidecar_client``) so tests drive the mapping with
no HTTP and no live Node process. The built-in client speaks the driving
contract in ``scanners/vendor/bacnet-scanner`` using only stdlib.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
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

ENGINE_NAME = "bacnet_scanner"

# The vendored sidecar's register template columns (verbatim), in order. Match
# key is Device Instance (globally unique across a BACnet internetwork). The
# ``bacnet_scanner_register`` import profile uses these same names, so accepted
# register rows re-serialize straight back into the sidecar with no field
# translation. This tuple is the golden contract asserted by the tests.
REGISTER_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "Device Instance",
    "Device Name",
    "Network",
    "IP Address",
    "Vendor",
    "Model",
    "Location",
    "Expected Objects",
    "Description",
)

# RAG severity: red is a hard fault, amber a partial; green/none raise no issue.
_RAG_SEVERITY = {"red": "high", "amber": "medium"}

# Adapter-side wall-clock budget for the /api/export phase. The sidecar cannot be
# told to stop a device enumeration mid-flight (driving contract §5), so this is
# the only bound on a device whose object reads hang. Run-cancel is honoured the
# same way — by closing the SSE socket between events.
# ponytail: fixed ceiling; make it a run parameter (export_deadline_s already
# read below) if a real site's inventory legitimately needs longer.
_EXPORT_DEADLINE_S = 300.0
# The scan phase is short (the sidecar clamps its discovery window to <=20s), but
# enrichment reads add up; bound it too so a wedged read cannot hang the run.
_SCAN_DEADLINE_S = 180.0

# Transport seam: given (base_url, register_csv, scan_query, is_cancelled,
# export_deadline_s) return ``{"rows": [...], "summary": {...},
# "device_files": [<per-asset export json>, ...]}``. Injectable so tests exercise
# the mapping with no HTTP.
SidecarClient = Callable[..., dict[str, Any]]


class SidecarTransportError(RuntimeError):
    """The sidecar could not be reached, mismatched the NIC, or errored (honest fail)."""


def process_bacnet_scanner_run(
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
    """Run a sidecar-backed BACnet scan through the shared engine lifecycle.

    Same signature as ``process_ip_scanner_run``: build an :class:`EngineContext`,
    define the engine coroutine, hand both to :func:`run_engine`.

    Args:
        sidecar_base_url: loopback URL (``http://127.0.0.1:<port>``) the scanners
            route resolved from ``SidecarSupervisor``. ``None`` when the sidecar
            is unavailable — a live scan then fails honestly.
        sidecar_client: transport override for tests.
        import_loader: accepted-row loader (``ImportRepository.get_accepted_rows``)
            used to fetch the bound ``bacnet_scanner_register`` rows by import id.
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
        return await _run_bacnet_scanner(
            engine_ctx,
            base_url=sidecar_base_url,
            client=sidecar_client,
            import_loader=import_loader,
        )

    if persist_records is None:
        return run_engine(ctx, engine)
    return run_engine(ctx, engine, persist_records=persist_records)


async def _run_bacnet_scanner(
    ctx: EngineContext,
    *,
    base_url: str | None,
    client: SidecarClient | None,
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> EngineResult:
    source_ip = _source_ip(ctx.parameters)

    # DRY RUN: no I/O, enumerate the plan (safety.build_dry_run_plan convention).
    if ctx.dry_run:
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=[source_ip or ""],
            actions=[
                "sidecar-adapter-resolve",
                "sidecar-register-import",
                "sidecar-scan",
                "sidecar-export",
                "sidecar-register-clear",
            ],
            notes="No packets sent in dry run; the sidecar performs the real scan.",
        )
        return EngineResult(result_summary_extra={"dry_run_plan": plan, "devices_discovered": 0})

    # Authorization gates any real I/O (defense in depth with the route gate).
    require_scan_authorization(ctx.parameters)

    if not source_ip:
        # Never guess a NIC: without a frozen Source Interface there is no honest
        # way to pick the sidecar adapter index.
        return EngineResult(
            status_override="failed",
            error_message=(
                "No Source Interface is set for this BACnet scan. Open the "
                "Configuration page, choose your wired network adapter, and Save, "
                "then run the scan again."
            ),
        )

    if client is None:
        if base_url is None:
            return EngineResult(
                status_override="failed",
                error_message="BACnet scanner sidecar is not available on this host.",
            )
        client = _default_sidecar_client

    register_rows = _load_register_rows(ctx.parameters, import_loader)
    export_deadline_s = _positive_float(ctx.parameters.get("export_deadline_s"), default=_EXPORT_DEADLINE_S)

    try:
        payload = client(
            base_url=base_url,
            source_ip=source_ip,
            register_csv=_register_csv(register_rows),
            scan_query=_scan_query(ctx.parameters),
            is_cancelled=ctx.is_cancelled,
            export_deadline_s=export_deadline_s,
        )
    except SidecarTransportError as error:
        return EngineResult(status_override="failed", error_message=str(error))

    return _map_result(
        payload.get("rows") or [],
        payload.get("summary") or {},
        payload.get("device_files") or [],
        ctx.parameters,
    )


# --------------------------------------------------------------------------
# Pure projection: (rows, summary, device_files) -> EngineResult. No I/O.
# --------------------------------------------------------------------------


def _map_result(
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    device_files: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
) -> EngineResult:
    """Project the sidecar's scan + export output into the shared record shapes.

    Devices are appended FIRST, then points, into a single ``structured_records``
    list — the device/point key-sniff persister routes a record carrying
    ``point_id``/``device_ref`` to the points table and everything else to
    devices.
    """
    project_id = parameters.get("project_id")
    site_id = parameters.get("site_id")
    now = datetime.now(UTC).isoformat()

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    issues: list[ValidationIssueRecord] = []

    for row in rows:
        instance = row.get("instance")
        rag = row.get("rag")
        register_state = row.get("register")
        # "missing" = expected-but-not-discovered: an issue, not a device.
        is_device = register_state != "missing" and row.get("status") != "unreachable"

        if is_device:
            asset_id = f"bacnet-device-{instance}"
            discovered_assets.append(
                json_safe_value(
                    {
                        "asset_id": asset_id,
                        "device_instance": instance,
                        "address": row.get("ip"),
                        "name": row.get("name") or None,
                        "vendor": row.get("vendor") or None,
                        "model": row.get("model") or None,
                        "firmware": row.get("firmware") or None,
                        "rag": rag,
                        "register_state": register_state,
                        "last_seen_at": now,
                    }
                )
            )
            structured_records.append(
                json_safe_value(
                    {
                        "project_id": project_id,
                        "site_id": site_id,
                        "address": row.get("ip"),
                        "device_type": "bacnet_device",
                        "name": row.get("name") or None,
                        "vendor": row.get("vendor") or None,
                        "model": row.get("model") or None,
                        "attributes": {
                            "asset_id": asset_id,
                            "device_instance": instance,
                            "firmware": row.get("firmware") or None,
                            "rag": rag,
                            "register_state": register_state,
                            "network": row.get("network"),
                            "mac": row.get("mac"),
                            "vendor_id": row.get("vendorId"),
                            "system_status": row.get("systemStatus"),
                            "object_count": row.get("objectCount"),
                            "location": row.get("location"),
                            "description": row.get("description"),
                        },
                    }
                )
            )

        severity = _RAG_SEVERITY.get(str(rag))
        if severity is not None:
            issues.append(_issue_for_row(row, severity, now))

    # Points come from the per-asset export JSON, appended AFTER every device.
    for asset in device_files:
        instance = asset.get("deviceInstance")
        device_ref = f"bacnet-device-{instance}"
        for obj in asset.get("points") or []:
            structured_records.append(
                json_safe_value(
                    {
                        "device_ref": device_ref,
                        "point_id": f"{obj.get('objectType')}-{obj.get('objectInstance')}",
                        "point_name": obj.get("name") or f"{obj.get('objectType')}-{obj.get('objectInstance')}",
                        # observed_value is a JSON object (the repository column is
                        # JSON); the scalar present-value nests under "value".
                        "observed_value": {"value": json_safe_value(obj.get("presentValue"))},
                        "units": obj.get("units") or None,
                        "attributes": {
                            "object_type": obj.get("objectType"),
                            "object_instance": obj.get("objectInstance"),
                            "device_instance": instance,
                        },
                    }
                )
            )

    result_summary_extra = json_safe_value(
        {
            "devices_discovered": summary.get("discovered"),
            "register_expected": summary.get("expected"),
            "register_reachable": summary.get("expectedReachable"),
            "register_matches": summary.get("matches"),
            "register_partial": summary.get("partial"),
            "register_missing": summary.get("missing"),
            "register_rogue": summary.get("rogue"),
            "points_exported": sum(len(a.get("points") or []) for a in device_files),
            "scanner": ENGINE_NAME,
        }
    )
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        issues=issues,
        result_summary_extra=result_summary_extra,
    )


def _issue_for_row(row: Mapping[str, Any], severity: str, now: str) -> ValidationIssueRecord:
    register_state = str(row.get("register") or "none")
    instance = row.get("instance")
    label = f"device {instance}" if instance not in (None, "—") else "an expected device"
    description = _issue_description(register_state, label, row)
    return ValidationIssueRecord(
        issue_id=f"bacnet_scanner:{register_state}:{instance if instance not in (None, '') else '—'}",
        asset_id=f"bacnet-device-{instance}" if register_state != "missing" and instance not in (None, "—") else None,
        issue_type=f"bacnet_scanner_{register_state}",
        severity=severity,  # type: ignore[arg-type]  (validated by the literal)
        description=description,
        status=str(row.get("status") or ""),
        expected_value=str(row.get("expectedObjects")) if row.get("expectedObjects") is not None else None,
        observed_value=str(row.get("objectCount")) if row.get("objectCount") is not None else None,
        match_basis="device_instance",
        status_detail=f"{row.get('status') or 'unknown'}/{register_state}",
        last_seen_at=datetime.fromisoformat(now),
    )


def _issue_description(register_state: str, label: str, row: Mapping[str, Any]) -> str:
    if register_state == "missing":
        return f"Expected {label} was not discovered during the scan."
    if register_state == "partial":
        diff = row.get("objectDiff") or row.get("mismatch")
        suffix = f" ({diff})" if diff else ""
        return f"Discovered {label} partially matches its register entry{suffix}."
    if register_state == "rogue":
        return f"Unregistered (rogue) {label} responded on the network."
    return f"Discovered {label} raised an issue ({register_state})."


# --------------------------------------------------------------------------
# Register serialization + scan-query / parameter parsing.
# --------------------------------------------------------------------------


def _source_ip(parameters: Mapping[str, Any]) -> str:
    """The frozen Source Interface IPv4 (engine_dispatch sets ``source_ip``)."""
    value = parameters.get("source_ip")
    return str(value).strip() if value else ""


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _load_register_rows(
    parameters: Mapping[str, Any],
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Load the run's bound ``bacnet_scanner_register`` accepted rows, if any."""
    import_id = parameters.get("register_import_id")
    if not isinstance(import_id, str) or not import_id.strip() or import_loader is None:
        return []
    try:
        return [dict(row) for row in import_loader(import_id)]
    except FileNotFoundError:
        return []


def _register_csv(register_rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize accepted register rows to the sidecar's 9-column CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REGISTER_TEMPLATE_COLUMNS)
    for row in register_rows:
        writer.writerow([str(row.get(column, "") or "") for column in REGISTER_TEMPLATE_COLUMNS])
    return buffer.getvalue()


def _scan_query(parameters: Mapping[str, Any]) -> dict[str, str]:
    """Build the ``/api/scan`` query params (adapter index is added later).

    Optional: ``discoverMs`` (discovery window), ``timeout`` (per-read),
    ``low``/``high`` (device-instance range). The adapter never fabricates a
    range — a blank range is a global Who-Is, which is the sidecar's default.
    """
    query: dict[str, str] = {}
    for key in ("discoverMs", "timeout", "low", "high"):
        value = parameters.get(key)
        if value not in (None, ""):
            query[key] = str(value)
    return query


# --------------------------------------------------------------------------
# Built-in stdlib transport (resolve NIC -> register -> scan SSE -> export SSE).
# --------------------------------------------------------------------------


def _default_sidecar_client(
    *,
    base_url: str,
    source_ip: str,
    register_csv: str,
    scan_query: Mapping[str, str],
    is_cancelled: Callable[[], bool],
    export_deadline_s: float,
) -> dict[str, Any]:
    """Speak the driving contract over stdlib HTTP + SSE. Honest failures only."""
    base = base_url.rstrip("/")
    try:
        adapter_index = _resolve_adapter_index(base, source_ip)
        _post_register(base, register_csv)
        rows, summary, devices = _stream_scan(base, adapter_index, scan_query, is_cancelled)
        device_files = _export_devices(base, devices, is_cancelled, export_deadline_s)
    except SidecarTransportError:
        _delete_register(base)  # best-effort; never masks the primary error
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        _delete_register(base)
        raise SidecarTransportError(
            "The BACnet scanner sidecar could not be reached during the scan."
        ) from error
    _delete_register(base)
    return {"rows": rows, "summary": summary, "device_files": device_files}


def _resolve_adapter_index(base: str, source_ip: str) -> int:
    """Map the frozen Source Interface IP to the sidecar's adapter index.

    FAILS the run on mismatch — a guessed index would scan the wrong NIC, the
    exact silent-substitution this design forbids.
    """
    request = urllib.request.Request(f"{base}/api/adapters", method="GET")  # noqa: S310 (loopback)
    with urllib.request.urlopen(request, timeout=10.0) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8", "replace"))
    adapters = payload.get("adapters") or []
    for index, adapter in enumerate(adapters):
        if str(adapter.get("ip") or "").strip() == source_ip:
            return index
    raise SidecarTransportError(
        f"The selected Source Interface ({source_ip}) does not match any network "
        "adapter the BACnet scanner can see. Check the Source Interface on the "
        "Configuration page matches an adapter that is up on this machine."
    )


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


def _iter_sse_events(stream: Any, is_cancelled: Callable[[], bool], deadline: float):
    """Yield decoded ``data:`` JSON events until cancel, deadline, or EOF.

    Breaking out of the loop closes the caller's stream context, which is the
    ONLY way to cancel the sidecar (it has no stop endpoint — driving contract
    §5). The deadline is the adapter-side bound on an uninterruptible device read.
    """
    for raw in stream:
        if is_cancelled() or time.monotonic() > deadline:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[len("data:"):].strip())
        except json.JSONDecodeError:
            continue


def _stream_scan(
    base: str,
    adapter_index: int,
    scan_query: Mapping[str, str],
    is_cancelled: Callable[[], bool],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Consume ``/api/scan`` SSE; return (rows, summary, device-target list).

    Devices captured from ``device``/``device-update`` events carry the raw
    target fields (instance/ip/port/network/mac/name) the ``/api/export`` body
    needs; ``result`` carries the RAG {rows, summary}.
    """
    params = dict(scan_query)
    params["adapter"] = str(adapter_index)
    url = f"{base}/api/scan?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})  # noqa: S310
    devices: dict[Any, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    deadline = time.monotonic() + _SCAN_DEADLINE_S
    with urllib.request.urlopen(request, timeout=None) as stream:  # noqa: S310
        for event in _iter_sse_events(stream, is_cancelled, deadline):
            kind = event.get("type")
            if kind in ("device", "device-update"):
                device = event.get("device") or {}
                instance = device.get("instance")
                if instance is not None:
                    devices[instance] = {**devices.get(instance, {}), **device}
            elif kind == "result":
                rows = list(event.get("rows") or [])
                summary = dict(event.get("summary") or {})
            elif kind == "error":
                raise SidecarTransportError(
                    "The BACnet scanner sidecar reported a scan error "
                    f"({event.get('message') or 'no detail'})."
                )
            elif kind == "complete":
                break
    return rows, summary, list(devices.values())


def _export_devices(
    base: str,
    devices: Sequence[Mapping[str, Any]],
    is_cancelled: Callable[[], bool],
    export_deadline_s: float,
) -> list[dict[str, Any]]:
    """POST discovered devices to ``/api/export``; return per-asset JSON dicts.

    The ``ready`` event carries the whole ZIP base64-inline; we unzip it in
    memory and parse each ``*.json`` entry (the per-asset export shape). Bounded
    by an adapter-side deadline and abandoned on cancel — the sidecar cannot stop
    an in-flight device enumeration itself (driving contract §5).
    """
    if not devices:
        return []
    body = json.dumps({"devices": [dict(device) for device in devices]}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 (fixed loopback URL)
        f"{base}/api/export",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    deadline = time.monotonic() + export_deadline_s
    zip_b64: str | None = None
    with urllib.request.urlopen(request, timeout=None) as stream:  # noqa: S310
        for event in _iter_sse_events(stream, is_cancelled, deadline):
            kind = event.get("type")
            if kind == "ready":
                zip_b64 = event.get("base64")
                break
            if kind == "error":
                raise SidecarTransportError(
                    "The BACnet scanner sidecar reported an export error "
                    f"({event.get('message') or 'no detail'})."
                )
    if not zip_b64:
        return []  # cancelled / deadline / no ready event: honest empty point set
    return _decode_export_zip(zip_b64)


def _decode_export_zip(zip_b64: str) -> list[dict[str, Any]]:
    """Decode the base64 export ZIP and return each asset's parsed JSON dict."""
    try:
        raw = base64.b64decode(zip_b64)
        assets: list[dict[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                if not name.endswith(".json"):
                    continue
                try:
                    assets.append(json.loads(archive.read(name).decode("utf-8", "replace")))
                except (json.JSONDecodeError, KeyError):
                    continue  # skip one unreadable asset, keep the rest
        return assets
    except (ValueError, zipfile.BadZipFile):
        return []  # a corrupt archive is honest "no points", never a fake point


def _demo() -> None:
    """Assert-based self-check for the pure mapping + register serialization."""
    rows = [
        {"instance": 1001, "register": "match", "rag": "green", "status": "reachable",
         "ip": "10.0.0.11", "name": "AHU-1", "vendor": "Acme", "model": "V1", "firmware": "1.2"},
        {"instance": 1002, "register": "partial", "rag": "amber", "status": "reachable",
         "ip": "10.0.0.12", "name": "VAV-3", "objectDiff": "expected 5, found 4"},
        {"instance": 9, "register": "missing", "rag": "red", "status": "unreachable",
         "ip": "—", "expectedObjects": 5},
        {"instance": 2050, "register": "rogue", "rag": "red", "status": "rogue", "ip": "10.0.0.30"},
    ]
    summary = {"expected": 3, "discovered": 3, "expectedReachable": 2,
               "matches": 1, "partial": 1, "missing": 1, "rogue": 1}
    device_files = [
        {"deviceInstance": 1001, "points": [
            {"objectType": "analog-input", "objectInstance": 1, "name": "SAT",
             "presentValue": "18.60", "units": "degreesCelsius"},
            {"objectType": "binary-output", "objectInstance": 1, "name": "FanCmd",
             "presentValue": "active", "units": ""},
        ]},
    ]
    result = _map_result(rows, summary, device_files, {"project_id": "p", "site_id": "s"})

    # missing (unreachable) is an issue, not a device; the other 3 are devices.
    devices = [r for r in result.structured_records if "device_ref" not in r]
    points = [r for r in result.structured_records if "device_ref" in r]
    assert len(devices) == 3, devices
    assert {d["address"] for d in devices} == {"10.0.0.11", "10.0.0.12", "10.0.0.30"}, devices
    assert all(d["device_type"] == "bacnet_device" for d in devices), devices
    # devices come first, then points, in the one list.
    assert result.structured_records.index(points[0]) > max(
        result.structured_records.index(d) for d in devices
    ), "points must follow devices"
    # point identity + json-safe observed value.
    assert len(points) == 2, points
    assert points[0]["point_id"] == "analog-input-1", points[0]
    assert points[0]["observed_value"] == {"value": "18.60"}, points[0]
    assert points[0]["device_ref"] == "bacnet-device-1001", points[0]
    assert points[0]["attributes"]["object_type"] == "analog-input", points[0]
    # green raises no issue; amber + 2 reds do.
    assert len(result.issues) == 3, result.issues
    assert sorted(i.severity for i in result.issues) == ["high", "high", "medium"], result.issues
    # device attributes carry the BACnet identity, never as loose columns.
    ahu = next(d for d in devices if d["address"] == "10.0.0.11")
    assert set(ahu) <= {"project_id", "site_id", "address", "device_type", "name",
                        "vendor", "model", "attributes"}, set(ahu)
    assert ahu["attributes"]["asset_id"] == "bacnet-device-1001", ahu
    assert ahu["attributes"]["register_state"] == "match", ahu
    assert ahu["attributes"]["firmware"] == "1.2", ahu

    csv_text = _register_csv([{"Device Instance": "1001", "Device Name": "AHU-1"}])
    assert csv_text.splitlines()[0] == ",".join(REGISTER_TEMPLATE_COLUMNS), csv_text
    assert "1001" in csv_text

    assert _scan_query({"low": 1, "high": 100})["low"] == "1"
    assert _scan_query({}) == {}  # blank range -> global Who-Is (sidecar default)
    assert _source_ip({"source_ip": " 192.0.2.5 "}) == "192.0.2.5"
    assert _decode_export_zip("not-base64!!") == []  # corrupt -> honest empty
    print("bacnet_scanner_sidecar self-check OK")


if __name__ == "__main__":
    _demo()
