"""``mqtt_publish`` engine: send ONE MQTT message through the held live session,
under SCT's sealed scan contract.

A publish is a real WRITE, so it rides the same sealed-preview machinery as an
active scan: a dry run previews the EXACT bytes with no broker I/O, an admin
approves that exact preview, and the live send replays the FROZEN bytes the
preview sealed. This engine never sees un-frozen input on the live path.

HONESTY: not-connected / unreachable / a client that did not accept the publish
all record a real FAILED run carrying the sidecar's own error; the failed run IS
the evidence. Success is recorded as "accepted by the sidecar's MQTT client",
never "delivered": the sidecar returns client-accept, not a broker
acknowledgement. Broker credentials never reach the evidence — only host/port/tls.
"""

from __future__ import annotations

import hashlib
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.engines import mqtt_scanner_sidecar
from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    ThrottleConfig,
    make_cancel_checker,
    run_engine,
)
from smart_commissioning_core.engines.safety import build_dry_run_plan, require_scan_authorization
from smart_commissioning_core.run_context import json_safe_value

ENGINE_NAME = "mqtt_publish"

# Transport seam (module-level so tests monkeypatch ``mqtt_publish_sidecar._post_json``).
_post_json = mqtt_scanner_sidecar._post_json


def process_mqtt_publish_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: Any,
    execution_mode: str,
    throttle: Any = None,
    dry_run: bool = False,
    sidecar_base_url: str | None = None,
) -> Any:
    """Run a sealed MQTT publish through the shared engine lifecycle.

    NO ``persist_records``: a write is not a discovery and must never touch the
    device/topic inventory. All evidence lives in ``result_summary``.
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
        return _run_mqtt_publish(engine_ctx, base_url=sidecar_base_url)

    return run_engine(ctx, engine)


def _run_mqtt_publish(ctx: EngineContext, *, base_url: str | None) -> EngineResult:
    fields = _publish_fields(ctx.parameters)
    contract = _sealed_contract(ctx.parameters)
    if fields is None or contract is None:
        return EngineResult(
            status_override="failed",
            error_message="This publish run is missing its sealed contract; nothing was sent.",
        )
    topic, payload, qos, retain = fields
    payload_bytes = payload.encode("utf-8")
    broker = contract.get("broker") or {}

    # DRY: no socket, ever. Show the exact frozen bytes that a live send would
    # transmit through the held live session.
    if ctx.dry_run:
        plan = build_dry_run_plan(
            engine=ENGINE_NAME,
            targets=[topic],
            actions=[f"sidecar-publish:{topic}"],
            notes=(
                "No message sent in dry run; the live send transmits exactly these frozen "
                "bytes through the held MQTT live session."
            ),
            extra={
                "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                "payload_bytes": len(payload_bytes),
                "qos": qos,
                "retain": retain,
                "broker_host": broker.get("host"),
                "broker_port": broker.get("port"),
                "use_tls": broker.get("tls"),
            },
        )
        return EngineResult(result_summary_extra=json_safe_value({"dry_run_plan": plan}))

    # LIVE: authorization gate (defense in depth with the route gate).
    require_scan_authorization(ctx.parameters)

    # Belt-and-braces: the wire bytes MUST be the approved bytes. Refuse on any
    # drift from the sealed digest even against an in-process parameter mutation.
    sealed_sha = str(contract.get("payload_sha256") or "")
    if hashlib.sha256(payload_bytes).hexdigest() != sealed_sha:
        return EngineResult(
            status_override="failed",
            error_message="The payload no longer matches its approved digest; nothing was sent.",
        )

    if base_url is None:
        return EngineResult(
            status_override="failed",
            error_message="MQTT discovery sidecar is not available on this host.",
        )

    try:
        result = _post_json(base_url.rstrip("/"), "/api/publish", {"topic": topic, "payload": payload, "qos": qos, "retain": retain})
    except urllib.error.HTTPError as error:
        message = (
            "The live session is not connected to the broker. Nothing was sent."
            if error.code == 409
            else "The MQTT sidecar rejected the publish. Nothing was sent."
        )
        return EngineResult(status_override="failed", error_message=message)
    except (urllib.error.URLError, OSError, TimeoutError):
        return EngineResult(
            status_override="failed",
            error_message="The MQTT sidecar could not be reached. The message may not have been sent.",
        )

    if not (isinstance(result, Mapping) and result.get("ok") is True):
        return EngineResult(
            status_override="failed",
            error_message="The sidecar's MQTT client did not accept the publish.",
        )

    authorization = ctx.parameters.get("scan_authorization")
    authorization = authorization if isinstance(authorization, Mapping) else {}
    evidence = json_safe_value(
        {
            "scanner": ENGINE_NAME,
            "publish": {
                "topic": topic,
                "payload_sha256": sealed_sha,
                "payload_bytes": len(payload_bytes),
                "qos": qos,
                "retain": retain,
                # host/port/tls only — never credentials (v0.1.16 credential-honesty rule).
                "broker": {"host": broker.get("host"), "port": broker.get("port"), "tls": broker.get("tls")},
                "authorized_by": authorization.get("authorized_by"),
                "authorization_id": authorization.get("authorization_id"),
                "accepted_by_sidecar": True,
                "delivery_confirmed": False,
                "published_at": datetime.now(UTC).isoformat(),
            },
        }
    )
    return EngineResult(result_summary_extra=evidence)


# --------------------------------------------------------------------------
# Pure readers of the frozen publish fields + sealed contract section.
# --------------------------------------------------------------------------


def _publish_fields(parameters: Mapping[str, Any]) -> tuple[str, str, int, bool] | None:
    topic = parameters.get("topic")
    payload = parameters.get("payload")
    if not isinstance(topic, str) or not topic or not isinstance(payload, str):
        return None
    qos = parameters.get("qos")
    qos_int = qos if isinstance(qos, int) and qos in (0, 1, 2) else 0
    return topic, payload, qos_int, bool(parameters.get("retain"))


def _sealed_contract(parameters: Mapping[str, Any]) -> dict[str, Any] | None:
    contract = parameters.get("scan_contract_v1")
    if not isinstance(contract, Mapping):
        return None
    section = contract.get("mqtt_publish")
    return dict(section) if isinstance(section, Mapping) else None


def _demo() -> None:
    """Assert-based self-check for the dry plan, digest refusal, and honesty."""
    sha = hashlib.sha256(b'{"set":1}').hexdigest()
    params = {
        "topic": "site/ahu-1/config",
        "payload": '{"set":1}',
        "qos": 1,
        "retain": True,
        "scan_contract_v1": {
            "scan_contract_version": "1.0",
            "job_type": "mqtt_publish",
            "mqtt_publish": {
                "topic": "site/ahu-1/config",
                "payload_sha256": sha,
                "qos": 1,
                "retain": True,
                "broker": {"host": "broker.example.local", "port": 8883, "tls": True},
            },
        },
        "scan_authorization": {"authorized": True, "authorized_by": "eng", "authorization_id": "auth-1"},
    }

    # Dry plan echoes the sealed values, no I/O.
    dry_ctx = EngineContext(
        run_id="r", parameters=dict(params), run_store=None, execution_mode="inline",
        throttle=ThrottleConfig(), dry_run=True, _is_cancelled=lambda: False,
    )
    dry = _run_mqtt_publish(dry_ctx, base_url=None)
    plan = dry.result_summary_extra["dry_run_plan"]
    assert plan["targets"] == ["site/ahu-1/config"], plan
    assert plan["payload_sha256"] == sha and plan["qos"] == 1 and plan["retain"] is True, plan
    assert plan["broker_host"] == "broker.example.local", plan

    # Digest mismatch (payload tampered after seal) refuses without sending.
    tampered = dict(params, payload='{"set":2}')
    live_ctx = EngineContext(
        run_id="r", parameters=tampered, run_store=None, execution_mode="inline",
        throttle=ThrottleConfig(), dry_run=False, _is_cancelled=lambda: False,
    )
    refused = _run_mqtt_publish(live_ctx, base_url="http://127.0.0.1:1")
    assert refused.status_override == "failed" and "digest" in (refused.error_message or ""), refused

    # Honest failures on the transport, and a clean success carries no credentials.
    global _post_json
    original = _post_json
    try:
        ok_ctx = lambda: EngineContext(  # noqa: E731 - tiny test factory
            run_id="r", parameters=dict(params), run_store=None, execution_mode="inline",
            throttle=ThrottleConfig(), dry_run=False, _is_cancelled=lambda: False,
        )
        _post_json = lambda *_a, **_k: {"ok": True}  # noqa: E731
        good = _run_mqtt_publish(ok_ctx(), base_url="http://127.0.0.1:1")
        pub = good.result_summary_extra["publish"]
        assert pub["accepted_by_sidecar"] is True and pub["delivery_confirmed"] is False, pub
        assert pub["authorized_by"] == "eng" and "password" not in str(pub), pub

        def _raise_409(*_a, **_k):
            raise urllib.error.HTTPError("x", 409, "Conflict", {}, None)

        _post_json = _raise_409
        failed = _run_mqtt_publish(ok_ctx(), base_url="http://127.0.0.1:1")
        assert failed.status_override == "failed" and "not connected" in (failed.error_message or "").lower(), failed

        _post_json = lambda *_a, **_k: {"ok": False}  # noqa: E731
        rejected = _run_mqtt_publish(ok_ctx(), base_url="http://127.0.0.1:1")
        assert rejected.status_override == "failed", rejected
    finally:
        _post_json = original
    print("mqtt_publish_sidecar self-check OK")


if __name__ == "__main__":
    _demo()
