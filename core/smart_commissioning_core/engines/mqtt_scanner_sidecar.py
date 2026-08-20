"""``mqtt_scanner`` sidecar adapter engine.

Drive the vendored ``mqtt-discovery`` Node app over its loopback HTTP API for a
bounded capture window and project its export archive (``_manifest.json`` +
per-topic payloads) into the shared ``EngineResult`` shape (discovered assets +
``DiscoveredTopic`` records + validation issues).

Ownership split mirrors ``ip_scanner_sidecar``: ``SidecarSupervisor`` (backend)
owns the sidecar *process* and its port; this engine only speaks to an
already-running loopback base URL that the scanners route passes in. It never
spawns a process, so it stays network/process-light and unit-testable.

Capture sequence (read-only — never ``/api/publish`` or ``/api/config``):
    connect -> (register) -> poll status for a bounded window -> export -> disconnect.

CREDENTIAL HONESTY: broker credentials and certificate material are resolved at
run time through the SAME provider-aware path the live MQTT lane uses
(``mqtt_settings.build_mqtt_connection_settings`` + its ``secret://`` resolver).
Secrets are NEVER frozen into ``parameters`` and NEVER copied into
``result_summary`` — only host / port / tls / counts are surfaced.

RUN HONESTY: when the sidecar is unreachable, the broker connect is refused, or
the capture window sees nothing, this engine records a real failed run. It never
fabricates topics. Every value that reaches a persisted record is passed through
``json_safe_value`` first (the v0.1.16 raw-value lesson).

The transport is injectable (``sidecar_client``) so tests drive the mapping with
no HTTP and no live Node process. The built-in client speaks the driving
contract in ``scanners/vendor/mqtt-discovery`` using only stdlib.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import time
import urllib.error
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
from smart_commissioning_core.mqtt_settings import (
    MqttSettingsError,
    build_mqtt_connection_settings,
    parse_int,
)
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import json_safe_value

ENGINE_NAME = "mqtt_scanner"

# The vendored sidecar's register template columns (verbatim), in order — the
# 10-column MQTT register CSV (udmi.js importRegister / server.js
# generateRegisterCsv). Accepted register rows re-serialize straight back into
# the sidecar with no field translation. This tuple is the golden contract.
REGISTER_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "Asset",
    "Topic",
    "Type",
    "Point",
    "Unit",
    "Data Type",
    "Schema",
    "Site",
    "Location",
    "Description",
)

DEFAULT_CAPTURE_SECONDS = 60.0
# Hard ceiling on a single bounded capture. A read-only broker sweep never needs
# longer, and the cap stops a mistyped parameter from pinning the sidecar open.
MAX_CAPTURE_SECONDS = 900.0

DEFAULT_ROOT_FILTER = "#"

# Transport seam. Injectable so tests exercise the mapping with no HTTP.
SidecarClient = Callable[..., dict[str, Any]]


class SidecarTransportError(RuntimeError):
    """The sidecar could not be reached or refused the connect (honest fail)."""


def process_mqtt_scanner_run(
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
    """Run a sidecar-backed MQTT capture through the shared engine lifecycle.

    Same signature as ``process_ip_scanner_run``: build an :class:`EngineContext`,
    define the engine coroutine, hand both to :func:`run_engine`.

    Args:
        sidecar_base_url: loopback URL (``http://127.0.0.1:<port>``) the scanners
            route resolved from ``SidecarSupervisor``. ``None`` when the sidecar
            is unavailable — a live capture then fails honestly.
        sidecar_client: transport override for tests.
        import_loader: accepted-row loader used to fetch the bound register rows
            by import id.
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
        return await _run_mqtt_scanner(
            engine_ctx,
            base_url=sidecar_base_url,
            client=sidecar_client,
            import_loader=import_loader,
        )

    if persist_records is None:
        return run_engine(ctx, engine)
    return run_engine(ctx, engine, persist_records=persist_records)


async def _run_mqtt_scanner(
    ctx: EngineContext,
    *,
    base_url: str | None,
    client: SidecarClient | None,
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> EngineResult:
    root_filter = _root_filter(ctx.parameters)
    capture_seconds = _capture_seconds(ctx.parameters)

    # DRY RUN: no I/O, enumerate the plan. build_mqtt_connection_settings is pure
    # (no socket) so host/port/tls are safe to surface, but credentials are not.
    if ctx.dry_run:
        host: str | None = None
        port: int | None = None
        use_tls: bool | None = None
        try:
            settings = build_mqtt_connection_settings(ctx.parameters)
            host, port, use_tls = settings.host, settings.port, settings.use_tls
        except (MqttSettingsError, ValueError):
            host = _safe_str(ctx.parameters.get("broker_host"))
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=[root_filter],
            actions=["sidecar-connect", f"sidecar-subscribe:{root_filter}", "sidecar-export", "sidecar-disconnect"],
            notes="No broker connection opened in dry run; the sidecar performs the real capture.",
            extra={
                "broker_host": host,
                "broker_port": port,
                "use_tls": use_tls,
                "capture_seconds": capture_seconds,
            },
        )
        return EngineResult(result_summary_extra={"dry_run_plan": plan, "topics_discovered": 0})

    # Authorization gates any real I/O (defense in depth with the route gate).
    require_scan_authorization(ctx.parameters)

    if client is None:
        if base_url is None:
            return EngineResult(
                status_override="failed",
                error_message="MQTT discovery sidecar is not available on this host.",
            )
        client = _default_sidecar_client

    # Resolve broker connection settings (host/port/tls/creds) via the SAME
    # provider + secret resolver the live MQTT lane uses. A missing host is an
    # honest configuration failure, not an empty success.
    try:
        connect_config = _connect_config(ctx.parameters, root_filter)
    except (MqttSettingsError, ValueError) as error:
        return EngineResult(
            status_override="failed",
            error_message=_failure_message("broker_not_configured" if isinstance(error, MqttSettingsError) else "invalid_parameters"),
        )

    register_rows = _load_register_rows(ctx.parameters, import_loader)
    register_csv = _register_csv(register_rows) if register_rows else None

    start = time.monotonic()

    def progress(stats: Mapping[str, Any]) -> None:
        # Run progress only (percent), never a progressive observation — the
        # protocol CHECK allows only ip/bacnet. Best-effort: a store hiccup must
        # never abort the capture.
        elapsed = time.monotonic() - start
        percent = 15 + int(min(elapsed / capture_seconds, 1.0) * 80) if capture_seconds else 15
        try:
            ctx.run_store.update_run_status(
                ctx.run_id, status="running", stage="mqtt_capture", progress_percent=percent
            )
        except Exception:  # noqa: BLE001 (progress is advisory)
            pass

    try:
        payload = client(
            base_url=base_url,
            connect_config=connect_config,
            register_csv=register_csv,
            capture_seconds=capture_seconds,
            is_cancelled=ctx.is_cancelled,
            progress=progress,
        )
    except SidecarTransportError as error:
        return EngineResult(status_override="failed", error_message=str(error))

    manifest = payload.get("manifest") or {}
    payloads = payload.get("payloads") or {}
    return _map_manifest(manifest, payloads, register_rows, ctx.parameters, cancelled=ctx.is_cancelled())


# --------------------------------------------------------------------------
# Pure projection: (manifest, payloads) -> EngineResult. No I/O, unit-testable.
# --------------------------------------------------------------------------


def _map_manifest(
    manifest: Mapping[str, Any],
    payloads: Mapping[str, Mapping[str, Any]],
    register_rows: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    *,
    cancelled: bool = False,
) -> EngineResult:
    """Project the sidecar's export archive into the shared record shapes."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    manifest_assets = list(manifest.get("assets") or [])
    # topic -> its owning asset entry (manifest is authoritative for identity).
    topic_to_asset: dict[str, Mapping[str, Any]] = {}
    for asset in manifest_assets:
        for topic in asset.get("topics") or []:
            topic_to_asset[str(topic)] = asset

    discovered_assets: list[dict[str, Any]] = []
    for asset in manifest_assets:
        matched = bool(asset.get("matched"))
        discovered_assets.append(
            json_safe_value(
                {
                    "asset_id": asset.get("asset"),
                    "hostname": None,
                    "observed_ports": [],
                    "match_basis": "register" if matched else "none",
                    "status_detail": f"mqtt asset ({len(asset.get('topics') or [])} topics)",
                    "last_seen_at": now_iso,
                }
            )
        )

    structured_records: list[dict[str, Any]] = []
    for position, topic in enumerate(payloads):
        asset = topic_to_asset.get(str(topic), {})
        entry = payloads[topic] or {}
        structured_records.append(
            json_safe_value(
                {
                    "topic": topic,
                    "message_count": _message_count(entry),
                    "last_payload": _as_payload_dict(entry.get("raw")),
                    "attributes": {
                        "device_ref": asset.get("asset"),
                        "schema": asset.get("schema"),
                        "site": asset.get("site"),
                        "room": asset.get("room"),
                        "gateway_id": asset.get("gatewayId"),
                        "matched": bool(asset.get("matched")),
                        "position": position,
                    },
                }
            )
        )

    issues = _issues_from_comparison(manifest_assets, register_rows, now)

    result_summary_extra = json_safe_value(
        {
            "topics_discovered": manifest.get("topicCount", len(payloads)),
            "assets_discovered": manifest.get("assetCount", len(manifest_assets)),
            "register_expected": _expected_asset_count(register_rows),
            "capture_mode": "bounded",
            "scanner": ENGINE_NAME,
            "broker_status_detail": (
                "cancelled" if cancelled else ("messages_captured" if payloads else "capture_window_empty")
            ),
        }
    )

    # Empty capture fails honestly: nothing on the wire is a real "no result",
    # not a silent success (unless the operator cancelled it).
    empty = not payloads
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        issues=issues,
        result_summary_extra=result_summary_extra,
        status_override=("cancelled" if cancelled else ("failed" if empty else None)),
        error_message=(_failure_message("capture_window_empty") if empty and not cancelled else None),
    )


def _issues_from_comparison(
    manifest_assets: Sequence[Mapping[str, Any]],
    register_rows: Sequence[Mapping[str, Any]],
    now: datetime,
) -> list[ValidationIssueRecord]:
    """Matched/missing/extra -> issues. Missing asset high; missing point medium; extra low.

    Mirrors udmi.js comparePoints: expected (register) vs live (manifest points),
    both run through ``_norm_point``; matching is on the uppercased asset key.
    """
    expected = _expected_points_by_asset(register_rows)
    live_keys = {_asset_key(a.get("asset")) for a in manifest_assets}
    issues: list[ValidationIssueRecord] = []

    # Expected assets never seen live -> high (the device is absent).
    for key in expected:
        if key not in live_keys:
            issues.append(
                ValidationIssueRecord(
                    issue_id=f"mqtt_scanner:missing_asset:{key}",
                    asset_id=key,
                    issue_type="mqtt_scanner_missing_asset",
                    severity="high",
                    description=f"Expected asset {key} published no topics during the capture.",
                    match_basis="asset",
                    last_seen_at=now,
                )
            )

    for asset in manifest_assets:
        key = _asset_key(asset.get("asset"))
        exp = expected.get(key)
        if not exp:
            continue  # rogue / unregistered asset raises no register issue
        live = {_norm_point(p.get("name")) for p in (asset.get("points") or [])}
        for name in sorted(exp - live):
            issues.append(
                ValidationIssueRecord(
                    issue_id=f"mqtt_scanner:missing_point:{key}:{name}",
                    asset_id=key,
                    issue_type="mqtt_scanner_missing_point",
                    severity="medium",
                    description=f"Asset {key} is missing expected point '{name}'.",
                    point_name=name,
                    match_basis="point",
                    last_seen_at=now,
                )
            )
        for name in sorted(live - exp):
            issues.append(
                ValidationIssueRecord(
                    issue_id=f"mqtt_scanner:extra_point:{key}:{name}",
                    asset_id=key,
                    issue_type="mqtt_scanner_extra_point",
                    severity="low",
                    description=f"Asset {key} published unexpected point '{name}'.",
                    point_name=name,
                    match_basis="point",
                    last_seen_at=now,
                )
            )
    return issues


def _as_payload_dict(raw: Any) -> dict[str, Any]:
    """last_payload is ALWAYS a dict — wrap non-JSON / scalars like mqtt_discovery."""
    if raw is None:
        return {"_raw_present": True}
    text = raw if isinstance(raw, str) else str(raw)
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return {"_raw_present": True}
    if isinstance(decoded, dict):
        return decoded
    return {"_value": decoded}


def _message_count(entry: Mapping[str, Any]) -> int:
    # ponytail: the export only carries the topic's rolling history (<=8 payloads,
    # HISTORY_PER_TOPIC), so this is a lower bound on real traffic, not the live
    # per-topic total. At least 1 (the latest payload is always present).
    count = entry.get("history_count") or 0
    try:
        return max(int(count), 1)
    except (TypeError, ValueError):
        return 1


# --------------------------------------------------------------------------
# Register + parameter parsing.
# --------------------------------------------------------------------------


def _norm_point(value: Any) -> str:
    """Point match key — mirrors udmi.js normPoint (case/separator insensitive)."""
    return re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())


def _asset_key(value: Any) -> str:
    """Asset match key — mirrors server.js assetKey (uppercase-exact)."""
    return str(value or "").strip().upper()


def _expected_points_by_asset(register_rows: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Group register rows into {asset_key: {norm_point, ...}} (presence-only asset kept)."""
    grouped: dict[str, set[str]] = {}
    for row in register_rows:
        key = _asset_key(row.get("Asset"))
        if not key:
            continue
        points = grouped.setdefault(key, set())
        point = _norm_point(row.get("Point"))
        if point:
            points.add(point)
    return grouped


def _expected_asset_count(register_rows: Sequence[Mapping[str, Any]]) -> int:
    return len(_expected_points_by_asset(register_rows))


def _load_register_rows(
    parameters: Mapping[str, Any],
    import_loader: Callable[[str], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Load the run's bound register accepted rows, if any."""
    import_id = parameters.get("register_import_id")
    if not isinstance(import_id, str) or not import_id.strip() or import_loader is None:
        return []
    try:
        return [dict(row) for row in import_loader(import_id)]
    except FileNotFoundError:
        return []


def _register_csv(register_rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize accepted register rows to the sidecar's 10-column CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REGISTER_TEMPLATE_COLUMNS)
    for row in register_rows:
        writer.writerow([str(row.get(column, "") or "") for column in REGISTER_TEMPLATE_COLUMNS])
    return buffer.getvalue()


def _root_filter(parameters: Mapping[str, Any]) -> str:
    """The subscription filter(s) for /api/connect (comma-split by the sidecar)."""
    topics = parameters.get("topics")
    if isinstance(topics, (list, tuple)) and topics:
        joined = ",".join(str(topic).strip() for topic in topics if str(topic).strip())
        if joined:
            return joined
    single = (
        parameters.get("root_filter")
        or parameters.get("rootFilter")
        or parameters.get("topic_filter")
        or parameters.get("topic_prefix")
    )
    text = str(single or "").strip()
    return text or DEFAULT_ROOT_FILTER


def _capture_seconds(parameters: Mapping[str, Any]) -> float:
    """Bounded capture window: default 60s, floor 1s, hard cap 900s."""
    raw = parameters.get("capture_seconds")
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CAPTURE_SECONDS
    if not math.isfinite(seconds) or seconds <= 0:
        return DEFAULT_CAPTURE_SECONDS
    return max(1.0, min(seconds, MAX_CAPTURE_SECONDS))


def _connect_config(parameters: Mapping[str, Any], root_filter: str) -> dict[str, Any]:
    """Build the /api/connect body from the resolved MQTT connection settings.

    Credentials + certificate material come from
    ``build_mqtt_connection_settings`` (provider + ``secret://`` resolver), never
    frozen into ``parameters``. Only non-empty fields are sent so the sidecar
    falls back to its own defaults (e.g. omitted username -> anonymous).
    """
    settings = build_mqtt_connection_settings(dict(parameters))
    config: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "tls": settings.use_tls,
        "rootFilter": root_filter,
        "qos": parse_int(parameters.get("qos"), default=0) if parameters.get("qos") is not None else 0,
        "clientId": settings.client_id,
    }
    if settings.username:
        config["username"] = settings.username
    if settings.password:
        config["password"] = settings.password
    if settings.ca_certificate:
        config["ca"] = settings.ca_certificate
    if settings.client_certificate:
        config["cert"] = settings.client_certificate
    if settings.private_key:
        config["key"] = settings.private_key
    reject = parameters.get("reject_unauthorized")
    if reject is None:
        reject = parameters.get("validate_cert")
    if reject is not None:
        config["rejectUnauthorized"] = bool(reject)
    return config


# --------------------------------------------------------------------------
# Built-in stdlib transport (connect -> poll -> export -> disconnect).
# --------------------------------------------------------------------------


def _default_sidecar_client(
    *,
    base_url: str,
    connect_config: Mapping[str, Any],
    register_csv: str | None,
    capture_seconds: float,
    is_cancelled: Callable[[], bool],
    progress: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Speak the driving contract over stdlib HTTP. Honest failures only."""
    base = base_url.rstrip("/")
    connected = False
    try:
        if register_csv:
            _post_json(base, "/api/register", {"csv": register_csv})
        status = _post_json(base, "/api/connect", dict(connect_config))
        if isinstance(status, Mapping) and status.get("ok") is False:
            raise SidecarTransportError("The MQTT broker connection was refused during the capture.")
        connected = True
        _capture_window(base, capture_seconds, is_cancelled, progress)
        manifest, payloads = _export_archive(base)
    except urllib.error.HTTPError as error:
        # A non-2xx from connect (502) or register — reached the sidecar, but the
        # broker/handshake failed. Honest failure, no raw error text surfaced.
        raise SidecarTransportError(
            "The MQTT discovery sidecar refused the broker connection during the capture."
        ) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise SidecarTransportError(
            "The MQTT discovery sidecar could not be reached during the capture."
        ) from error
    finally:
        if connected:
            _post_disconnect(base)  # always release the broker session, even on error
    return {"manifest": manifest, "payloads": payloads}


def _capture_window(
    base: str,
    capture_seconds: float,
    is_cancelled: Callable[[], bool],
    progress: Callable[[Mapping[str, Any]], None],
) -> None:
    """Poll /api/status every ~1s until the window closes or a cancel lands."""
    deadline = time.monotonic() + capture_seconds
    while time.monotonic() < deadline:
        if is_cancelled():
            return
        try:
            status = _get_json(base, "/api/status")
            if isinstance(status, Mapping):
                progress(status.get("stats") or {})
        except (urllib.error.URLError, OSError):
            pass  # a transient status blip must not abort a live capture
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))


def _export_archive(base: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """GET /api/export-archive; unzip in-memory. 409 => empty capture (honest)."""
    request = urllib.request.Request(f"{base}/api/export-archive", headers={"Accept": "application/zip"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310
            data = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 409:
            return {}, {}  # "Nothing discovered yet" — an empty capture, not a transport error
        raise
    return _read_export_zip(data)


def _read_export_zip(data: bytes) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Parse the export ZIP: _manifest.json + per-topic latest payloads/history."""
    manifest: dict[str, Any] = {}
    latest: dict[str, str] = {}
    history_counts: dict[str, int] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name == "_manifest.json":
                try:
                    manifest = json.loads(archive.read(name).decode("utf-8", "replace"))
                except ValueError:
                    manifest = {}
                continue
            if ".history/" in name:
                base_topic = name.split(".history/", 1)[0]
                history_counts[base_topic] = history_counts.get(base_topic, 0) + 1
                continue
            if name.endswith(".json"):
                topic = name[: -len(".json")]  # zip path mirrors the topic tree
                latest[topic] = archive.read(name).decode("utf-8", "replace")
    payloads = {
        topic: {"raw": raw, "history_count": history_counts.get(topic, 0)}
        for topic, raw in latest.items()
    }
    return manifest, payloads


def _post_json(base: str, path: str, body: Mapping[str, Any]) -> Any:
    request = urllib.request.Request(  # noqa: S310 (fixed loopback URL)
        f"{base}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15.0) as response:  # noqa: S310
        return _read_json_response(response)


def _get_json(base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base}{path}", timeout=10.0) as response:  # noqa: S310
        return _read_json_response(response)


def _post_disconnect(base: str) -> None:
    try:
        request = urllib.request.Request(f"{base}/api/disconnect", data=b"", method="POST")  # noqa: S310
        with urllib.request.urlopen(request, timeout=10.0):  # noqa: S310
            return
    except (urllib.error.URLError, OSError):
        return  # cleanup is best-effort; never mask the primary error


def _read_json_response(response: Any) -> Any:
    try:
        return json.loads(response.read().decode("utf-8", "replace"))
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# Failure messaging.
# --------------------------------------------------------------------------

_FAILURE_HINTS = {
    "broker_not_configured": (
        " No MQTT broker is configured. Enter the broker FQDN or IP address on "
        "the Configuration page and save it."
    ),
    "capture_window_empty": (
        " The capture window closed with no messages. Check the broker, the "
        "topic filter, and that devices are publishing."
    ),
}


def _failure_message(status_detail: str) -> str:
    return f"MQTT discovery failed ({status_detail}).{_FAILURE_HINTS.get(status_detail, '')}"


def _safe_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _demo() -> None:
    """Assert-based self-check for the pure mapping + register serialization."""
    manifest = {
        "tool": "SCA MQTT Discovery",
        "version": "2.0.0",
        "assetCount": 2,
        "topicCount": 2,
        "assets": [
            {
                "asset": "AHU-01", "matched": True, "schema": "udmi-v2",
                "site": "S", "room": "Plant", "gatewayId": "GW-1",
                "topics": ["udmi/site/x/ahu/01/events/pointset"],
                "points": [{"name": "supply_air_temp", "value": 14.2, "unit": "degC"}],
            },
            {
                "asset": "VAV-09", "matched": False, "schema": "json",
                "site": "", "room": "", "gatewayId": "",
                "topics": ["site/x/vav/09/state"], "points": [],
            },
        ],
    }
    payloads = {
        "udmi/site/x/ahu/01/events/pointset": {"raw": '{"version":"1","data":{}}', "history_count": 3},
        "site/x/vav/09/state": {"raw": "not-json", "history_count": 0},
    }
    register_rows = [
        {"Asset": "AHU-01", "Point": "Supply Air Temp"},   # matches (normPoint)
        {"Asset": "AHU-01", "Point": "return_air_temp"},   # missing -> medium
        {"Asset": "PMP-07", "Point": "pump_speed"},        # asset absent -> high
    ]
    result = _map_manifest(manifest, payloads, register_rows, {"project_id": "p", "site_id": "s"})

    assert len(result.discovered_assets) == 2, result.discovered_assets
    assert len(result.structured_records) == 2, result.structured_records
    # last_payload is ALWAYS a dict: JSON object stays, non-JSON is wrapped.
    by_topic = {r["topic"]: r for r in result.structured_records}
    assert by_topic["udmi/site/x/ahu/01/events/pointset"]["last_payload"] == {"version": "1", "data": {}}
    assert by_topic["site/x/vav/09/state"]["last_payload"] == {"_raw_present": True}
    assert by_topic["udmi/site/x/ahu/01/events/pointset"]["message_count"] == 3
    assert by_topic["site/x/vav/09/state"]["message_count"] == 1  # floor
    # only fixed keys + attributes at the top level.
    assert set(by_topic["site/x/vav/09/state"]) == {"topic", "message_count", "last_payload", "attributes"}

    severities = sorted(i.severity for i in result.issues)
    assert severities == ["high", "medium"], severities  # missing asset high, missing point medium
    kinds = {i.issue_type for i in result.issues}
    assert kinds == {"mqtt_scanner_missing_asset", "mqtt_scanner_missing_point"}, kinds

    # empty capture fails honestly.
    empty = _map_manifest({"assets": []}, {}, [], {})
    assert empty.status_override == "failed", empty
    # cancelled beats empty-fail.
    cancelled = _map_manifest({"assets": []}, {}, [], {}, cancelled=True)
    assert cancelled.status_override == "cancelled", cancelled

    csv_text = _register_csv([{"Asset": "AHU-01", "Point": "supply_air_temp", "Unit": "degC"}])
    assert csv_text.splitlines()[0] == ",".join(REGISTER_TEMPLATE_COLUMNS)
    assert "AHU-01" in csv_text
    assert len(REGISTER_TEMPLATE_COLUMNS) == 10

    assert _root_filter({"topics": ["a/#", "b/#"]}) == "a/#,b/#"
    assert _root_filter({}) == "#"
    assert _capture_seconds({"capture_seconds": 9999}) == MAX_CAPTURE_SECONDS
    assert _capture_seconds({}) == DEFAULT_CAPTURE_SECONDS
    assert _norm_point("Supply Air Temp") == "supply_air_temp"
    print("mqtt_scanner_sidecar self-check OK")


if __name__ == "__main__":
    _demo()
