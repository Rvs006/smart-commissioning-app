"""Shared engine framework for Smart Commissioning discovery/validation engines.

This package is the FOUNDATION the concrete discovery/validation engines build
on. It deliberately depends only on the standard library plus the existing
``smart_commissioning_core`` contracts (``RunStore`` protocol,
``ValidationIssueRecord``) so importing it never pulls in network/hardware
dependencies.

Public contract (consumed verbatim by the wiring agent and frontend):

- ``base``: ``EngineContext``, ``ThrottleConfig``, ``Throttle``, ``EngineResult``,
  ``run_engine`` / ``run_engine_async``, and the ``StructuredRecordPersister`` /
  ``CancelChecker`` / ``EngineCallable`` typing aliases.
- ``safety``: ``require_scan_authorization`` (active-scan authorization gate),
  ``ScanNotAuthorized``, ``is_authorized``, and ``build_dry_run_plan`` (the
  dry-run convention helper).

NOTE ON HONESTY / ON-SITE VALIDATION: nothing in this package opens a socket or
talks to a real BMS/OT device. Concrete engines that touch real
hardware/network must (a) document that they require on-site validation,
(b) guard imports so absence of a transport never crashes, and (c) be listed in
the task's ``live_untested`` output. This framework is fully unit-testable
against in-memory fakes and is exercised by ``core/tests/test_engines.py``.
"""

from smart_commissioning_core.engines.base import (
    CancelChecker,
    EngineCallable,
    EngineContext,
    EngineResult,
    StructuredRecordPersister,
    Throttle,
    ThrottleConfig,
    run_engine,
    run_engine_async,
)
from smart_commissioning_core.engines.comparison import (
    build_mapping_validation_engine,
    process_mapping_validation_run,
    validate_mapping,
)
from smart_commissioning_core.engines.comparison_common import (
    DiscoveryLoader,
    ImportLoader,
    Tolerance,
)
from smart_commissioning_core.engines.ip_scan import process_ip_discovery_run
from smart_commissioning_core.engines.mqtt_discovery import process_mqtt_discovery_run
from smart_commissioning_core.engines.point_validation import (
    build_bacnet_validation_engine,
    process_bacnet_validation_run,
    validate_bacnet_points,
)
from smart_commissioning_core.engines.safety import (
    ScanNotAuthorized,
    build_dry_run_plan,
    is_authorized,
    require_scan_authorization,
)

__all__ = [
    "CancelChecker",
    "DiscoveryLoader",
    "EngineCallable",
    "EngineContext",
    "EngineResult",
    "ImportLoader",
    "ScanNotAuthorized",
    "StructuredRecordPersister",
    "Throttle",
    "ThrottleConfig",
    "Tolerance",
    "build_bacnet_validation_engine",
    "build_dry_run_plan",
    "build_mapping_validation_engine",
    "is_authorized",
    "process_bacnet_validation_run",
    "process_ip_discovery_run",
    "process_mapping_validation_run",
    "process_mqtt_discovery_run",
    "require_scan_authorization",
    "run_engine",
    "run_engine_async",
    "validate_bacnet_points",
    "validate_mapping",
]
