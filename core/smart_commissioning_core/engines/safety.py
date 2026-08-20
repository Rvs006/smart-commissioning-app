"""Scan-safety guards for active-scan engines.

These engines (IP discovery, BACnet discovery, MQTT discovery, MQTT config
publish) actively probe a network. Run against a live building management /
operational-technology network, an unauthorized scan can disrupt controllers,
trip alarms, or flood a broker. This module makes "did a human authorize this
scan?" an explicit, testable precondition, and defines the dry-run convention
that lets an engine enumerate exactly what it *would* do without emitting a
single packet.

AUTHORIZATION CONTRACT (the wiring agent and frontend depend on this verbatim):

    A run's ``parameters`` dict authorizes an active scan when EITHER:

      (a) ``parameters["authorized"] is True``                 (shorthand), OR
      (b) ``parameters["scan_authorization"]`` is a dict with
          ``authorized is True`` AND a non-empty string ``authorized_by``.

    Form (b) is the preferred audit-friendly shape:

        {
            "scan_authorization": {
                "authorized": True,
                "authorized_by": "jane.engineer@acme.example",
                # optional, free-form, stored as-is for the audit trail:
                "authorized_at": "2026-06-12T09:00:00+00:00",
                "note": "Commissioning window, floor 3 VLAN only.",
            }
        }

    Anything else (missing key, ``authorized`` falsey, ``scan_authorization``
    present but without an ``authorized_by``) is treated as NOT authorized and
    raises :class:`ScanNotAuthorized`.

    Rationale: a bare boolean is easy for an automated caller to set
    accidentally, so the richer form additionally records *who* authorized it.
    We accept the shorthand because the existing frontend / inline-fallback
    paths may only have a boolean to offer; the wiring agent should prefer the
    full form for anything that reaches a real network.

DRY-RUN CONVENTION (see :func:`build_dry_run_plan`):

    When ``ctx.dry_run`` is True an active engine must NOT open any socket or
    send any packet. Instead it enumerates the concrete targets/plan it would
    execute and returns it under ``result_summary_extra["dry_run_plan"]``.
    :func:`build_dry_run_plan` produces the canonical shape for that value.
"""

import os
from collections.abc import Mapping, Sequence
from typing import Any

# A deployment MAY opt out of the authorization ceremony (the "authorized"
# checkbox and the sealed two-person approval) by setting this to a falsy value.
# The public default is enforced, so existing deployments, tests, and the shared
# source are unchanged; a portable build sets it to "0" to run frictionless.
# Evidence recording is independent of this flag and is never affected.
_AUTHORIZATION_ENV_VAR = "SCT_REQUIRE_SCAN_AUTHORIZATION"
_FALSY = frozenset({"0", "false", "off", "no", ""})


def authorization_enforced() -> bool:
    """Whether active-scan / device-write authorization is enforced (default True).

    Read at call time so a deployment (or a test) can flip it via the
    ``SCT_REQUIRE_SCAN_AUTHORIZATION`` environment variable without a restart of
    import-time state. Absent or truthy -> enforced; ``0``/``false``/``off``/
    ``no``/empty -> frictionless.
    """
    raw = os.environ.get(_AUTHORIZATION_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY


class ScanNotAuthorized(RuntimeError):
    """Raised by :func:`require_scan_authorization` for an unauthorized active scan.

    Carries no parameter contents so the message is safe to surface to the API
    / frontend without leaking credentials embedded in ``parameters``.
    """


def _coerce_authorization(parameters: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """Return ``(authorized, authorized_by)`` derived from ``parameters``.

    Pure / side-effect free; never opens a socket. ``authorized_by`` is only
    populated from the structured ``scan_authorization`` form.
    """
    if not isinstance(parameters, Mapping):
        return False, None

    # Form (b): structured authorization with an explicit authorizer.
    scan_authorization = parameters.get("scan_authorization")
    if isinstance(scan_authorization, Mapping):
        authorized = scan_authorization.get("authorized") is True
        authorized_by = scan_authorization.get("authorized_by")
        if authorized and isinstance(authorized_by, str) and authorized_by.strip():
            return True, authorized_by

    # Form (a): bare boolean shorthand.
    if parameters.get("authorized") is True:
        return True, None

    return False, None


def is_authorized(parameters: Mapping[str, Any] | None) -> bool:
    """Return True if ``parameters`` authorizes an active scan (non-raising).

    Convenience predicate for callers that want to branch rather than catch
    :class:`ScanNotAuthorized`.

    In a frictionless deployment (:func:`authorization_enforced` is False) this
    is always True: the gate is disabled, so every legacy-gated caller
    (sidecar scans, config publish) and every engine guard passes. The sealed
    route helper handles its own frictionless branch.
    """
    if not authorization_enforced():
        return True
    authorized, _ = _coerce_authorization(parameters)
    return authorized


def require_scan_authorization(parameters: Mapping[str, Any] | None) -> None:
    """Assert an active scan is authorized; raise :class:`ScanNotAuthorized` otherwise.

    Active-scan engines MUST call this before contacting any target (and before
    building a dry-run plan is optional — a dry run is side-effect free, so it
    is safe to allow even unauthorized callers to *preview* the plan; concrete
    engines decide, but production wiring should gate the real scan here).

    See the module docstring for the exact accepted ``parameters`` shapes.
    """
    if not is_authorized(parameters):
        raise ScanNotAuthorized(
            "Active scan is not authorized. Provide parameters['authorized'] = True "
            "or parameters['scan_authorization'] = {'authorized': True, "
            "'authorized_by': '<who>'} before running an active-scan engine."
        )


def build_dry_run_plan(
    *,
    engine: str,
    targets: Sequence[Any],
    actions: Sequence[str] | None = None,
    notes: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical ``dry_run_plan`` payload for an active engine.

    Returns a JSON-serializable dict describing what the engine *would* do
    without performing any I/O. Engines place this under
    ``result_summary_extra["dry_run_plan"]`` (see :class:`EngineResult`).

    Args:
        engine: stable engine identifier, e.g. ``"ip_discovery"``.
        targets: the concrete units the engine would contact (addresses, topic
            filters, BACnet device instance ranges, ...). Stored verbatim, so
            pass already-sanitized, JSON-friendly values.
        actions: optional human-readable description of the operations per
            target (e.g. ``["tcp-connect:47808", "whois-broadcast"]``).
        notes: optional free-form operator note.
        extra: optional engine-specific fields merged into the plan.

    The shape is intentionally generic so the three discovery engines and the
    config-publish engine reuse it without per-vendor fields.
    """
    plan: dict[str, Any] = {
        "engine": engine,
        "dry_run": True,
        "target_count": len(targets),
        "targets": list(targets),
        "actions": list(actions) if actions is not None else [],
    }
    if notes is not None:
        plan["notes"] = notes
    if extra:
        plan.update(dict(extra))
    return plan
