"""MQTT discovery engine: an authorized, bounded broker topic sweep.

What this engine does
---------------------
Connects to an MQTT broker (settings derived from the run ``parameters`` plus
the configuration-values provider, via
:func:`smart_commissioning_core.mqtt_settings.build_mqtt_connection_settings`),
SUBSCRIBEs to a wildcard topic filter (default ``"#"``; commonly narrowed to a
prefix such as ``"udmi/#"``), and captures published messages for a bounded
window (``capture_seconds``) up to a ``max_messages`` cap. It then aggregates
the capture into:

* distinct topics, each with its **message count** and **last payload**, and
* a ``discovered_assets`` entry per topic (or per device derived from the
  topic) in the ``DiscoveryAssetObservation`` shape the API reads, plus
* a structured ``DiscoveredTopic`` row (``topic``, ``message_count``,
  ``last_payload``) for the DiscoveryRepository.

The actual capture is dependency-injected (``live_capture`` parameter) exactly
like ``udmi_validation.validate_udmi_full_report`` injects ``live_capture`` —
the production default is
:func:`smart_commissioning_core.mqtt_transport.subscribe_and_capture` (the
hand-rolled MQTT 3.1.1 client over raw sockets). Tests inject a fake that
yields canned ``MqttMessage`` objects, so NO real broker is required.

Safety / honesty
----------------
* The real capture is gated by :func:`safety.require_scan_authorization`.
* Under ``ctx.dry_run`` the engine connects to NOTHING: it returns the planned
  broker host/port, topic filter, and window as ``dry_run_plan``.
* Cancellation is honoured: if cancellation is requested before the capture
  starts the engine returns immediately; the bounded ``capture_seconds`` /
  ``max_messages`` limits keep any single capture short so a cancel observed
  after a window stops the run promptly.
* CREDENTIAL SANITIZATION: broker username/password and certificate material
  are NEVER copied into ``result_summary``, ``dry_run_plan``, or issue text.
  Only host/port/topic-filter/window/counts are surfaced. Transport error text
  is mapped to a coarse status label (e.g. ``"broker_unreachable"``) rather
  than echoed, because raw socket/TLS errors can contain hostnames or auth
  detail.

ON-SITE VALIDATION REQUIRED: the aggregation, asset/record building,
authorization, dry-run planning, cancellation, capture-bound enforcement, and
error sanitization are all unit-tested against an injected FAKE transport (see
``core/tests/test_mqtt_discovery.py``). What CANNOT be exercised here is a
capture against a REAL broker: the raw-socket CONNECT/SUBSCRIBE handshake, TLS,
auth, and live device payload topology. That path (the default
``subscribe_and_capture``) is listed in the task's ``live_untested`` output and
must be validated on site against the actual broker.
"""

from collections.abc import Callable, Sequence
from typing import Any

from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.mqtt_settings import (
    build_mqtt_connection_settings,
    parse_int,
)
from smart_commissioning_core.mqtt_transport import (
    MqttConnectionSettings,
    MqttMessage,
    MqttTransportError,
    subscribe_and_capture,
)

ENGINE_NAME = "mqtt_discovery"

DEFAULT_TOPIC_FILTER = "#"
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_MAX_MESSAGES = 500

# The capture callable: same positional/keyword shape as
# mqtt_transport.subscribe_and_capture, so the real client is the default and a
# fake can be injected for tests.
LiveCapture = Callable[..., list[MqttMessage]]


def _resolve_topic_filters(parameters: dict[str, Any]) -> list[str]:
    """Return the wildcard topic filter(s) to subscribe to.

    Accepts ``topic_filter`` / ``topic_prefix`` (single string) or ``topics``
    (list). Defaults to ``"#"`` (all topics). A bare prefix like ``"udmi"`` is
    normalized to ``"udmi/#"`` so it behaves as a subtree wildcard.
    """
    topics = parameters.get("topics")
    if isinstance(topics, (list, tuple)) and topics:
        return [str(topic).strip() for topic in topics if str(topic).strip()]

    single = parameters.get("topic_filter") or parameters.get("topic_prefix")
    if single:
        text = str(single).strip()
        if not text:
            return [DEFAULT_TOPIC_FILTER]
        # If the operator gave a plain prefix with no MQTT wildcard, make it a
        # subtree filter so we actually capture everything underneath it.
        if "#" not in text and "+" not in text:
            text = text.rstrip("/") + "/#"
        return [text]

    return [DEFAULT_TOPIC_FILTER]


def _capture_seconds(parameters: dict[str, Any]) -> float:
    from smart_commissioning_core.mqtt_settings import parse_float

    return parse_float(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS)


def _max_messages(parameters: dict[str, Any]) -> int:
    return parse_int(parameters.get("max_messages"), default=DEFAULT_MAX_MESSAGES)


def _broker_status_detail(error: Exception) -> str:
    """Map a transport error to a coarse, credential-free status label.

    Mirrors udmi_validation._broker_error_status. NEVER returns the raw error
    text (which can contain hostnames / auth detail).
    """
    text = str(error).casefold()
    if "tls" in text or "certificate" in text or "ssl" in text:
        return "tls_error"
    if (
        "username" in text
        or "password" in text
        or "authorised" in text
        or "authorized" in text
    ):
        return "authentication_error"
    if "timed out" in text or "timeout" in text:
        return "broker_timeout"
    return "broker_unreachable"


def _device_ref_from_topic(topic: str) -> str | None:
    """Best-effort device identifier derived from a UDMI-style topic.

    UDMI device topics look like ``<prefix>/<DEVICE_ID>/<message_type>`` (e.g.
    ``udmi/AHU-1/pointset``). When the topic has at least two segments we treat
    the second-to-last meaningful segment heuristic: return the segment
    immediately before a known UDMI message-type suffix, else the first
    non-prefix segment. Returns None when we cannot confidently derive one.

    This is intentionally conservative — topic structure is site-specific, so
    real device derivation requires on-site validation of the broker's topic
    convention.
    """
    parts = [segment for segment in topic.split("/") if segment]
    if len(parts) < 2:
        return None
    udmi_suffixes = {"state", "pointset", "events", "metadata", "config", "system"}
    if parts[-1] in udmi_suffixes and len(parts) >= 2:
        return parts[-2]
    return None


def process_mqtt_discovery_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: Any,
    execution_mode: str,
    throttle: Any = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    live_capture: LiveCapture | None = subscribe_and_capture,
    build_settings: Callable[[dict[str, Any]], MqttConnectionSettings] = build_mqtt_connection_settings,
) -> Any:
    """Run an MQTT discovery capture through the shared engine lifecycle.

    Mirrors the existing ``process_*_run`` processors. The wiring agent calls
    this from the worker / inline fallback with a real ``run_store`` and a
    ``persist_records`` backed by DiscoveryRepository.replace_topics.

    Args:
        run_id, parameters, run_store, execution_mode, throttle, dry_run:
            standard processor inputs.
        persist_records: structured-record persister; defaults to a no-op.
        live_capture: injectable capture callable matching
            ``subscribe_and_capture``'s signature. Default is the real raw-socket
            client; pass ``None`` to signal capture is unavailable in this
            context (the run records a sanitized status rather than crashing).
            Tests inject a fake yielding canned messages.
        build_settings: injectable settings builder (default: the real provider-
            aware ``build_mqtt_connection_settings``). Tests can inject a stub so
            no configuration provider is required.

    Returns whatever ``run_store.update_run_status`` returns for the terminal flip.
    """
    from smart_commissioning_core.engines.base import EngineContext as _Ctx
    from smart_commissioning_core.engines.base import ThrottleConfig as _ThrottleConfig
    from smart_commissioning_core.engines.base import run_engine as _run_engine

    is_cancelled = _make_cancel_checker(run_store, run_id)
    ctx = _Ctx(
        run_id=run_id,
        parameters=dict(parameters or {}),
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle or _ThrottleConfig(),
        dry_run=dry_run,
        _is_cancelled=is_cancelled,
    )

    def engine(engine_ctx: EngineContext) -> EngineResult:
        return _run_mqtt_discovery(
            engine_ctx,
            live_capture=live_capture,
            build_settings=build_settings,
        )

    persister = persist_records or _noop_records
    return _run_engine(ctx, engine, persist_records=persister)


def _noop_records(_run_id: str, _records: Sequence[dict[str, Any]]) -> None:
    """Default structured-record persister: does nothing."""


def _make_cancel_checker(run_store: Any, run_id: str) -> Callable[[], bool]:
    """Build a cancellation checker from a (possibly) cancellable run store."""
    checker = getattr(run_store, "is_cancel_requested", None)
    if not callable(checker):
        return lambda: False

    def _check() -> bool:
        try:
            return bool(checker(run_id))
        except Exception:
            return False

    return _check


def _run_mqtt_discovery(
    ctx: EngineContext,
    *,
    live_capture: LiveCapture | None,
    build_settings: Callable[[dict[str, Any]], MqttConnectionSettings],
) -> EngineResult:
    """The engine body: plan (dry run) or capture + aggregate (real run).

    Note: this is a SYNC engine because the underlying transport
    (``subscribe_and_capture``) is blocking-socket based. ``run_engine`` accepts
    sync engines, and the worker runs each engine in its own context, so this
    does not block an event loop in production wiring.
    """
    topic_filters = _resolve_topic_filters(ctx.parameters)
    capture_seconds = _capture_seconds(ctx.parameters)
    max_messages = _max_messages(ctx.parameters)

    # DRY RUN: describe the broker/topic/window plan; connect to NOTHING.
    # We do NOT call build_settings against a real provider here unless we can
    # do so without I/O — build_mqtt_connection_settings is pure (no socket),
    # so it is safe to surface host/port, but we still strip credentials.
    if ctx.dry_run:
        host: str | None = None
        port: int | None = None
        use_tls: bool | None = None
        try:
            settings = build_settings(ctx.parameters)
            host, port, use_tls = settings.host, settings.port, settings.use_tls
        except Exception:
            # Settings may be incomplete in a dry run; that is fine — we still
            # return the requested topic/window plan without leaking anything.
            host = _safe_str(ctx.parameters.get("broker_host"))
            port = _safe_int(ctx.parameters.get("broker_port"))
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=topic_filters,
            actions=[f"subscribe:{flt}" for flt in topic_filters],
            notes="No broker connection opened in dry run.",
            extra={
                # Credential-free broker coordinates only.
                "broker_host": host,
                "broker_port": port,
                "use_tls": use_tls,
                "capture_seconds": capture_seconds,
                "max_messages": max_messages,
            },
        )
        return EngineResult(
            result_summary_extra={
                "dry_run_plan": plan,
                "topics_discovered": 0,
                "messages_captured": 0,
            }
        )

    # REAL CAPTURE: authorization gates any broker connection.
    require_scan_authorization(ctx.parameters)

    if ctx.is_cancelled():
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": "cancelled_before_capture",
            },
            status_override="cancelled",
        )

    if live_capture is None:
        # Honest: capture is not available in this execution context (e.g. an
        # API process with no broker egress). Do not pretend it worked.
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": "live_capture_unavailable",
            }
        )

    try:
        settings = build_settings(ctx.parameters)
    except (ValueError, MqttTransportError) as error:
        # build error (e.g. missing host). Surface a coarse, credential-free
        # label; run_engine still marks this succeeded (no devices found) so
        # the operator can see the status_detail.
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": _broker_status_detail(error),
            }
        )

    try:
        messages = live_capture(
            settings,
            topics=topic_filters,
            timeout_seconds=capture_seconds,
            max_messages=max_messages,
        )
    except (MqttTransportError, OSError, ValueError) as error:
        # NEVER surface raw error text — map to a coarse status only.
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": _broker_status_detail(error),
            }
        )

    return _aggregate_capture(
        messages,
        topic_filters=topic_filters,
        capture_seconds=capture_seconds,
        max_messages=max_messages,
        project_id=ctx.parameters.get("project_id"),
        site_id=ctx.parameters.get("site_id"),
        cancelled=ctx.is_cancelled(),
    )


def _aggregate_capture(
    messages: Sequence[MqttMessage],
    *,
    topic_filters: Sequence[str],
    capture_seconds: float,
    max_messages: int,
    project_id: Any,
    site_id: Any,
    cancelled: bool,
) -> EngineResult:
    """Aggregate captured messages into topics + assets + structured records.

    Topics are emitted in first-seen order. For each topic: message_count is
    the number of messages observed, last_payload is the JSON of the LAST
    message on that topic (or a ``{"_raw_present": True}`` marker when the
    payload is non-JSON, so we never store undecodable bytes).
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    last_payload: dict[str, Any] = {}

    for message in messages:
        topic = message.topic
        if topic not in counts:
            counts[topic] = 0
            order.append(topic)
        counts[topic] += 1
        decoded = message.json_payload()
        if isinstance(decoded, dict):
            last_payload[topic] = decoded
        elif decoded is None:
            # Non-JSON / undecodable payload: store a presence marker, not bytes.
            last_payload[topic] = {"_raw_present": True}
        else:
            # JSON scalar/list — wrap so the column (a JSON object) stays a dict.
            last_payload[topic] = {"_value": decoded}

    discovered_assets: list[dict[str, Any]] = []
    structured_records: list[dict[str, Any]] = []
    for position, topic in enumerate(order):
        device_ref = _device_ref_from_topic(topic)
        discovered_assets.append(
            {
                "asset_id": device_ref,
                "hostname": None,
                "observed_ports": [],
                "match_basis": "none",
                "status_detail": f"mqtt topic ({counts[topic]} msg)",
            }
        )
        structured_records.append(
            {
                "topic": topic,
                "message_count": counts[topic],
                "last_payload": last_payload.get(topic, {}),
                "attributes": {
                    "device_ref": device_ref,
                    "position": position,
                },
            }
        )

    extra: dict[str, Any] = {
        "topics_discovered": len(order),
        "messages_captured": len(messages),
        "topic_filters": list(topic_filters),
        "capture_seconds": capture_seconds,
        "max_messages": max_messages,
        "broker_status_detail": (
            "messages_captured" if messages else "capture_window_empty"
        ),
        "message_limit_reached": len(messages) >= max_messages,
    }

    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        result_summary_extra=extra,
        status_override="cancelled" if cancelled else None,
    )


def _safe_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
