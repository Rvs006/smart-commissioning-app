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
  ``last_payload``, plus per-message metadata in ``attributes``:
  ``last_retained`` / ``last_qos`` / ``last_received_at``) for the
  DiscoveryRepository.

Message-metadata honesty (load-bearing — see ``_aggregate_capture``):
``last_received_at`` is THIS TOOL'S receive clock, never a broker publish time
(MQTT 3.1.1 carries no publish timestamp on the wire); ``last_retained=True``
means a broker retained-value replay whose publish time is unknown; and
``last_qos`` is the DELIVERY QoS capped by our subscription QoS
(``subscribe_qos`` in ``result_summary``), not the publisher's QoS.

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
    make_cancel_checker,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.mqtt_settings import (
    _broker_error_status,
    build_mqtt_connection_settings,
    parse_capture_seconds,
    parse_int,
)
from smart_commissioning_core.mqtt_transport import (
    MqttCaptureInterrupted,
    MqttConnectionSettings,
    MqttMessage,
    MqttTransportError,
    subscribe_and_capture,
)

ENGINE_NAME = "mqtt_discovery"

DEFAULT_TOPIC_FILTER = "#"
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_MAX_MESSAGES = 500
# With retain-latest the message cap bounds DISTINCT TOPICS, not raw messages
# (mqtt_transport.subscribe_and_capture keeps one latest payload per topic), so a
# large operator max_messages can no longer buy unbounded payload memory — but
# each retained topic still holds one decoded JSON object, so clamp the topic
# count. Worst-case memory ~= distinct_topics x largest payload.
MAX_TOPIC_CAP = 10_000

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


def _capture_seconds(parameters: dict[str, Any]) -> float | None:
    """Capture window in seconds, or None for an indefinite capture (mq9nhbzu).

    Missing => default window (back-compat); explicit 0 / blank / negative =>
    indefinite. Parsing lives in ``mqtt_settings.parse_capture_seconds`` so the
    UDMI validation capture shares the exact same convention.
    """
    return parse_capture_seconds(parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS)


def _max_messages(parameters: dict[str, Any]) -> int:
    # Clamp to MAX_TOPIC_CAP: under retain-latest this is a distinct-topic cap,
    # so an outsized operator value would only enlarge the retained-payload set.
    return min(parse_int(parameters.get("max_messages"), default=DEFAULT_MAX_MESSAGES), MAX_TOPIC_CAP)


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

    is_cancelled = make_cancel_checker(run_store, run_id)
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

    # persist_records None -> run_engine's own _noop_persister default.
    if persist_records is None:
        return _run_engine(ctx, engine)
    return _run_engine(ctx, engine, persist_records=persist_records)


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

    # Inline-hang guard (mq9nhbzu): an indefinite capture (capture_seconds <= 0
    # => None) blocks until the message cap or cancellation. That is safe on the
    # background worker, but on the inline / in-request execution path it would
    # tie up the request worker for the whole capture, so bound it to the default
    # window there and flag why so the UI/operator can see the downgrade. Run
    # indefinite captures on the worker (DEPLOYMENT/JOB_EXECUTION via Dramatiq).
    indefinite_bounded_inline = capture_seconds is None and ctx.execution_mode != "dramatiq_worker"
    if indefinite_bounded_inline:
        capture_seconds = DEFAULT_CAPTURE_SECONDS

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
                "capture_mode": "indefinite" if capture_seconds is None else "bounded",
                "indefinite_bounded_inline": indefinite_bounded_inline,
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
            },
            status_override="cancelled" if ctx.is_cancelled() else "failed",
            error_message=(
                None if ctx.is_cancelled() else _mqtt_failure_message("live_capture_unavailable")
            ),
        )

    try:
        settings = build_settings(ctx.parameters)
    except (ValueError, MqttTransportError) as error:
        # Settings-build error (e.g. missing host) classifies as
        # broker_not_configured, pointing the operator at the Configuration page
        # rather than the network. Fail the run either way so it cannot be
        # mistaken for an empty successful discovery.
        broker_status_detail = _broker_error_status(error)
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": broker_status_detail,
            },
            status_override="cancelled" if ctx.is_cancelled() else "failed",
            error_message=(
                None if ctx.is_cancelled() else _mqtt_failure_message(broker_status_detail)
            ),
        )

    # Retain-latest bounds memory on a long capture (one payload per topic) while
    # the observer keeps an honest per-topic total across the dedup: it fires for
    # every accepted message BEFORE retention drops a duplicate, so counts survive
    # even a MqttCaptureInterrupted (the observer already ran per message).
    observed_counts: dict[str, int] = {}
    messages_observed = 0

    def _observe(message: MqttMessage) -> None:
        nonlocal messages_observed
        observed_counts[message.topic] = observed_counts.get(message.topic, 0) + 1
        messages_observed += 1

    # Requested subscription QoS (0-2). This is the DELIVERY-QoS CAP: the broker
    # grants min(this, publisher's QoS), so every message's delivery QoS can be
    # at most this value. Surfaced in result_summary so the frontend can state
    # the cap honestly next to a per-message delivery QoS.
    subscribe_qos = parse_int(ctx.parameters.get("qos"), default=0)

    capture_error_status: str | None = None
    try:
        messages = live_capture(
            settings,
            topics=topic_filters,
            timeout_seconds=capture_seconds,
            max_messages=max_messages,
            cancel_check=ctx.is_cancelled,
            qos=subscribe_qos,
            retain_latest=True,
            on_message=_observe,
        )
    except MqttCaptureInterrupted as error:
        messages = error.messages
        capture_error_status = _broker_error_status(error.cause)
    except (MqttTransportError, OSError, ValueError) as error:
        # NEVER surface raw error text — map to a coarse status only.
        broker_status_detail = _broker_error_status(error)
        return EngineResult(
            result_summary_extra={
                "topics_discovered": 0,
                "messages_captured": 0,
                "broker_status_detail": broker_status_detail,
            },
            status_override="cancelled" if ctx.is_cancelled() else "failed",
            error_message=(
                None if ctx.is_cancelled() else _mqtt_failure_message(broker_status_detail)
            ),
        )

    return _aggregate_capture(
        messages,
        topic_filters=topic_filters,
        capture_seconds=capture_seconds,
        max_messages=max_messages,
        project_id=ctx.parameters.get("project_id"),
        site_id=ctx.parameters.get("site_id"),
        cancelled=ctx.is_cancelled(),
        indefinite_bounded_inline=indefinite_bounded_inline,
        capture_error_status=capture_error_status,
        observed_counts=observed_counts,
        messages_observed=messages_observed,
        subscribe_qos=subscribe_qos,
    )


def _aggregate_capture(
    messages: Sequence[MqttMessage],
    *,
    topic_filters: Sequence[str],
    capture_seconds: float | None,
    max_messages: int,
    project_id: Any,
    site_id: Any,
    cancelled: bool,
    indefinite_bounded_inline: bool = False,
    capture_error_status: str | None = None,
    observed_counts: dict[str, int] | None = None,
    messages_observed: int = 0,
    subscribe_qos: int = 0,
) -> EngineResult:
    """Aggregate captured messages into topics + assets + structured records.

    Topics are emitted in first-seen order. For each topic: message_count is
    the number of messages observed, last_payload is the JSON of the LAST
    message on that topic (or a ``{"_raw_present": True}`` marker when the
    payload is non-JSON, so we never store undecodable bytes).

    Per-topic message metadata (``last_retained`` / ``last_qos`` /
    ``last_received_at``) describes the SAME message as ``last_payload`` by
    construction. Honesty caveats the frontend must preserve:

    * ``last_received_at`` is OUR receive clock (``MqttMessage.received_at``),
      stamped at packet-decode time — NOT a broker publish time. MQTT 3.1.1
      carries no publish timestamp on the wire, so a publish time cannot be
      shown at all.
    * ``last_retained=True`` means the broker REPLAYED a stored retained value
      on subscribe; its received_at is the replay moment and says nothing about
      when the value was originally published (could be days earlier).
    * ``last_qos`` is the DELIVERY QoS = min(publisher's QoS, our subscription
      QoS). With the default subscription QoS 0 every delivery reads 0
      regardless of the publisher; ``subscribe_qos`` records the cap so the
      frontend can say so.

    Under retain-latest the transport deduplicates to one message per topic and
    ``observed_counts``/``messages_observed`` carry the honest per-topic and
    total counts across that dedup. When ``observed_counts`` is empty (an ad-hoc
    fake that never called ``on_message``) we fall back to counting the returned
    message list so those callers still report a truthful, non-zero count.
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    last_payload: dict[str, Any] = {}
    last_meta: dict[str, dict[str, Any]] = {}
    observed = observed_counts or {}

    for message in messages:
        topic = message.topic
        if topic not in counts:
            counts[topic] = 0
            order.append(topic)
        # Prefer the observer's real per-topic total (survives dedup); else
        # increment per message so a raw/no-observer capture stays honest.
        counts[topic] = observed.get(topic, counts[topic] + 1)
        decoded = message.json_payload()
        if isinstance(decoded, dict):
            last_payload[topic] = decoded
        elif decoded is None:
            # Non-JSON / undecodable payload: store a presence marker, not bytes.
            last_payload[topic] = {"_raw_present": True}
        else:
            # JSON scalar/list — wrap so the column (a JSON object) stays a dict.
            last_payload[topic] = {"_value": decoded}
        # Metadata for the SAME (last) message as last_payload. received_at is our
        # receive clock (not a publish time); retained flags a broker replay.
        last_meta[topic] = {
            "last_retained": message.retained,
            "last_qos": message.qos,
            "last_received_at": message.received_at.isoformat(),
        }

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
                    # Per-message metadata for the last message on this topic.
                    # Rides the free-form attributes JSON column (no migration).
                    **last_meta.get(topic, {}),
                },
            }
        )

    # Under retain-latest ``messages`` is one entry per distinct topic, so
    # len(messages) is the distinct-topic count; messages_captured reports the
    # true total the observer saw (fallback to the list for no-observer fakes).
    messages_captured = max(messages_observed, len(messages))
    extra: dict[str, Any] = {
        "topics_discovered": len(order),
        "messages_captured": messages_captured,
        "topic_filters": list(topic_filters),
        "capture_seconds": capture_seconds,
        "capture_mode": "indefinite" if capture_seconds is None else "bounded",
        "indefinite_bounded_inline": indefinite_bounded_inline,
        "max_messages": max_messages,
        "capture_retention": "latest_per_topic",
        # The requested subscription QoS = the delivery-QoS cap. The frontend
        # states it beside a per-message delivery QoS so QoS 0 (the default,
        # which forces every delivery to read 0) is never mistaken for the
        # publisher's QoS.
        "subscribe_qos": subscribe_qos,
        "topic_limit_reached": len(order) >= max_messages,
        "broker_status_detail": (
            "cancelled"
            if cancelled
            else capture_error_status or ("messages_captured" if messages else "capture_window_empty")
        ),
        # NOTE: under retain-latest len(messages) IS the distinct-topic count, so
        # this key now tracks the topic cap (same value as topic_limit_reached).
        # Kept for API consumers; capture_retention/topic_limit_reached make the
        # new semantics explicit. Nothing in the frontend reads this key.
        "message_limit_reached": len(messages) >= max_messages,
    }

    failure_status = capture_error_status or ("capture_window_empty" if not messages else None)
    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        result_summary_extra=extra,
        status_override=(
            "cancelled"
            if cancelled
            else ("failed" if capture_error_status or not messages else None)
        ),
        error_message=(
            _mqtt_failure_message(failure_status)
            if failure_status and not cancelled
            else None
        ),
    )


# Operator guidance for statuses whose remedy is the Configuration page, not
# the network. Appended to the label so the frontend can keep echoing ONE
# engine-authored string. Credential-free: never names the configured host.
_FAILURE_HINTS = {
    "broker_not_configured": (
        " No MQTT broker is configured — enter the broker FQDN or IP address"
        " on the Configuration page and save it."
    ),
    "dns_resolution_failed": (
        " The configured broker hostname did not resolve in DNS — check the"
        " broker FQDN or IP address on the Configuration page."
    ),
}


def _mqtt_failure_message(status_detail: str) -> str:
    """Credential-free operator reason for a self-diagnosed failed run."""
    return f"MQTT discovery failed ({status_detail}).{_FAILURE_HINTS.get(status_detail, '')}"


def _safe_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
