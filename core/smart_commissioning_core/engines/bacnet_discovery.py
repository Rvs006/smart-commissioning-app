"""BACnet/IP device discovery engine, behind a swappable backend abstraction.

Discovery has two phases:

    1. **Who-Is / I-Am**: broadcast a Who-Is over a device-instance range and
       collect the responding devices (instance, address, vendor, ...).
    2. **Per-device read**: for each device, read its ``object-list`` and the
       ``present-value`` of each readable point.

The engine drives a :class:`BacnetDiscoveryBackend` (an async Protocol) under
the shared :class:`~smart_commissioning_core.engines.base.Throttle`, honours
cooperative cancellation, and emits ``discovered_assets`` + DiscoveredDevice /
DiscoveredPoint records in the DiscoveryRepository row shapes.

HONESTY / TESTABILITY (read this before trusting any "it works" claim):

    * :class:`SimulatedBacnetBackend` is a deterministic in-memory fixture. It
      is the DEFAULT backend, so the engine runs end-to-end OFFLINE with NO
      network access and produces sample data for tests and demos. Results from
      it are explicitly labelled ``result_summary["backend"] == "simulated"`` so
      simulated data is never mistaken for a real scan.
    * :class:`Bacpypes3Backend` is the real BACnet/IP path. It has NEVER been
      integration-tested in this environment (there is no BACnet device or
      building network here). It REQUIRES on-site validation against real
      controllers before it can be trusted. Its ``bacpypes3`` import is lazy and
      guarded so importing this module never requires ``bacpypes3`` to be
      installed.

This module imports cleanly with only the standard library + the engine
framework; ``bacpypes3`` is an OPTIONAL extra (``pip install
smart-commissioning-core[bacnet]``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from smart_commissioning_core.engines.base import (
    EngineContext,
    EngineResult,
    Throttle,
    ThrottleConfig,
    run_engine,
    run_engine_async,
)
from smart_commissioning_core.engines.safety import (
    build_dry_run_plan,
    require_scan_authorization,
)
from smart_commissioning_core.run_store import RunStore

# Stable engine identifier used in dry-run plans / summaries.
ENGINE_NAME = "bacnet_discovery"

# Backend selector values (parameters["bacnet_backend"] / config).
BACKEND_SIMULATED = "simulated"
BACKEND_BACPYPES3 = "bacpypes3"

# BACnet device instances span 0..4194303 (22-bit). Used as the default Who-Is
# window when the caller does not narrow the range.
_BACNET_INSTANCE_MIN = 0
_BACNET_INSTANCE_MAX = 4194303


@runtime_checkable
class BacnetDiscoveryBackend(Protocol):
    """Async transport abstraction for BACnet discovery.

    All three methods are ``async`` — the engine drives them under the shared
    :class:`Throttle` (an asyncio construct), so a consistent async contract
    keeps the call sites uniform. A synchronous transport (e.g. a blocking
    library) should wrap its blocking calls in ``asyncio.to_thread`` inside
    these coroutines.

    The dict shapes are intentionally loose so a backend may carry extra
    vendor-specific keys; the engine reads a documented core subset (see
    :func:`_device_asset` / :func:`_point_record`) and stores the rest under the
    record ``attributes``.
    """

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        """Broadcast Who-Is over ``[low_limit, high_limit]`` and return devices.

        Each returned dict SHOULD carry at least:
            ``device_instance`` (int), ``address`` (str). Optional:
            ``vendor`` / ``vendor_id``, ``model``, ``name``, and any extra keys
            (stored under the device record's ``attributes``).

        ``address`` optionally targets a unicast/directed Who-Is instead of a
        full broadcast.
        """
        ...

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the readable objects (points) for ``device``.

        ``device`` is one of the dicts returned by :meth:`who_is`. Each returned
        object dict SHOULD carry at least:
            ``object_identifier`` (str, e.g. ``"analog-input,3"``). Optional:
            ``object_name``, ``object_type``, ``units``, and extra keys.
        """
        ...

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        """Read and return the ``present-value`` of ``obj`` on ``device``.

        ``device`` / ``obj`` are dicts returned by :meth:`who_is` /
        :meth:`read_object_list`. The return value is the raw decoded value
        (number, bool, str, ...) and is stored JSON-wrapped on the point record.
        """
        ...


# -- simulated backend ------------------------------------------------------


# Deterministic fixture: a handful of fake devices, each with a fixed set of
# objects and present-values. Stable across runs so tests can assert on it.
_DEFAULT_SIM_DEVICES: tuple[dict[str, Any], ...] = (
    {
        "device_instance": 1001,
        "address": "10.10.0.11:47808",
        "name": "AHU-1 Controller",
        "vendor": "Acme Controls",
        "vendor_id": 999,
        "model": "ACME-VAV-200",
        "objects": [
            {
                "object_identifier": "analog-input,1",
                "object_name": "SupplyAirTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 18.6,
            },
            {
                "object_identifier": "analog-input,2",
                "object_name": "ReturnAirTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 22.1,
            },
            {
                "object_identifier": "binary-output,1",
                "object_name": "SupplyFanCmd",
                "object_type": "binary-output",
                "units": None,
                "present_value": "active",
            },
        ],
    },
    {
        "device_instance": 1002,
        "address": "10.10.0.12:47808",
        "name": "VAV-3rd-Floor-01",
        "vendor": "Acme Controls",
        "vendor_id": 999,
        "model": "ACME-VAV-100",
        "objects": [
            {
                "object_identifier": "analog-value,10",
                "object_name": "ZoneTempSetpoint",
                "object_type": "analog-value",
                "units": "degreesCelsius",
                "present_value": 21.0,
            },
            {
                "object_identifier": "analog-input,5",
                "object_name": "ZoneTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 21.4,
            },
        ],
    },
    {
        "device_instance": 2050,
        "address": "10.10.0.30:47808",
        "name": "Chiller-Plant-Ctrl",
        "vendor": "Globex BMS",
        "vendor_id": 555,
        "model": "GLX-CH-9",
        "objects": [
            {
                "object_identifier": "analog-input,7",
                "object_name": "ChilledWaterSupplyTemp",
                "object_type": "analog-input",
                "units": "degreesCelsius",
                "present_value": 6.7,
            },
        ],
    },
)


class SimulatedBacnetBackend:
    """Deterministic in-memory BACnet backend for offline tests/demos.

    Performs NO network I/O. ``who_is`` filters the fixture by the requested
    instance range; ``read_object_list`` / ``read_present_value`` read from the
    fixture. This is the DEFAULT backend, so the engine produces sample
    ``discovered_assets`` with zero hardware — callers MUST treat the output as
    simulated (the engine stamps ``result_summary["backend"] == "simulated"``).

    A custom fixture may be supplied for richer test scenarios; it must follow
    the ``_DEFAULT_SIM_DEVICES`` shape.
    """

    backend_name = BACKEND_SIMULATED

    def __init__(self, devices: Sequence[Mapping[str, Any]] | None = None) -> None:
        source = devices if devices is not None else _DEFAULT_SIM_DEVICES
        # Deep-ish copy so callers/tests cannot mutate the shared fixture.
        self._devices: list[dict[str, Any]] = [dict(device) for device in source]

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,  # noqa: ARG002 - part of the Protocol; unused by the sim
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for device in self._devices:
            instance = device.get("device_instance")
            if not isinstance(instance, int):
                continue
            if low_limit <= instance <= high_limit:
                # Return device metadata WITHOUT the embedded objects list; the
                # engine fetches objects via read_object_list, mirroring how a
                # real backend works (Who-Is returns device info, not points).
                matched.append({k: v for k, v in device.items() if k != "objects"})
        return matched

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        fixture = self._find(device)
        objects = fixture.get("objects") if fixture else None
        if not isinstance(objects, list):
            return []
        # Strip present_value here — it is fetched separately via
        # read_present_value, matching the real ReadProperty two-step.
        return [{k: v for k, v in dict(obj).items() if k != "present_value"} for obj in objects]

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        fixture = self._find(device)
        if not fixture:
            return None
        target_id = obj.get("object_identifier")
        for candidate in fixture.get("objects", []):
            if candidate.get("object_identifier") == target_id:
                return candidate.get("present_value")
        return None

    def _find(self, device: Mapping[str, Any]) -> dict[str, Any] | None:
        instance = device.get("device_instance")
        for fixture in self._devices:
            if fixture.get("device_instance") == instance:
                return fixture
        return None


# -- real backend (UNVALIDATED — requires on-site validation) ---------------


class Bacpypes3Backend:
    """Real BACnet/IP backend using ``bacpypes3`` (Who-Is + ReadProperty).

    !!! NEVER INTEGRATION-TESTED — REQUIRES ON-SITE VALIDATION !!!

    There is no BACnet device or building network in the development/CI
    environment, so this class has NOT been exercised against real hardware. The
    ``bacpypes3`` calls below are written conservatively against the documented
    high-level async API (``Application.from_args``/``from_json``,
    ``app.who_is``, ``app.read_property``). Anything uncertain is flagged with a
    ``# UNVERIFIED:`` comment. Before relying on this path in production it MUST
    be validated on-site against real controllers; treat the data shapes below
    as best-effort until then.

    The ``bacpypes3`` import is performed lazily in :meth:`_ensure_app` (NOT at
    module import), guarded so a missing dependency raises a clear
    :class:`RuntimeError` with an install hint instead of an ImportError at an
    unexpected place. Importing this module never requires ``bacpypes3``.
    """

    backend_name = BACKEND_BACPYPES3

    def __init__(
        self,
        *,
        local_address: str | None = None,
        timeout_s: float = 5.0,
        object_list_property: str = "object-list",
    ) -> None:
        """Configure the real backend.

        Args:
            local_address: the local BACnet/IP interface (e.g.
                ``"192.168.1.10/24"`` or ``"192.168.1.10/24:47808"``). Required
                by bacpypes3 to bind a socket; passed through to the Application.
            timeout_s: per-request timeout in seconds for Who-Is / ReadProperty.
            object_list_property: property id read for the device object list
                (overridable for non-standard devices).
        """
        self._local_address = local_address
        self._timeout_s = timeout_s
        self._object_list_property = object_list_property
        self._app: Any = None  # lazily-created bacpypes3 Application

    def _ensure_app(self) -> Any:
        """Lazily import bacpypes3 and build the Application; guard ImportError.

        REQUIRES ON-SITE VALIDATION. Raises a clear RuntimeError (not a bare
        ImportError) when bacpypes3 is not installed, so selecting this backend
        without the optional dependency fails with an actionable message.
        """
        if self._app is not None:
            return self._app
        try:
            # UNVERIFIED: import paths follow the documented bacpypes3 layout.
            from bacpypes3.app import Application  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "The 'bacpypes3' backend was selected but bacpypes3 is not installed. "
                "Install the optional BACnet extra: pip install "
                "'smart-commissioning-core[bacnet]' (or: pip install bacpypes3)."
            ) from exc

        if not self._local_address:
            raise RuntimeError(
                "Bacpypes3Backend requires a local BACnet/IP address "
                "(local_address=...), e.g. '192.168.1.10/24'."
            )
        # UNVERIFIED: Application.from_json builds an app from an object list;
        # the network-port entry binds the local interface. Real deployments may
        # prefer Application.from_args / a full config. Validate on-site.
        object_list = [
            {
                "object-identifier": "device,4194303",
                "object-name": "SmartCommissioningScanner",
                "vendor-identifier": 999,
            },
            {
                "object-identifier": "network-port,1",
                "object-name": "NetworkPort-1",
                "ip-address": self._local_address,
            },
        ]
        self._app = Application.from_json(object_list)
        return self._app

    async def who_is(
        self,
        low_limit: int,
        high_limit: int,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        """REQUIRES ON-SITE VALIDATION. Broadcast Who-Is and map I-Am responses."""
        app = self._ensure_app()
        # UNVERIFIED: who_is(low_limit, high_limit, timeout=...) returns a list
        # of IAmRequest APDUs. The 'address' (directed Who-Is) parameter is not
        # passed here because the documented signature does not accept it; a
        # directed Who-Is would require a different bacpypes3 call. Validate
        # on-site before relying on directed discovery.
        i_ams = await app.who_is(low_limit=low_limit, high_limit=high_limit, timeout=self._timeout_s)
        devices: list[dict[str, Any]] = []
        for i_am in i_ams or []:
            # UNVERIFIED: iAmDeviceIdentifier is an ObjectIdentifier tuple
            # ("device", instance); pduSource is the responder address.
            try:
                instance = int(i_am.iAmDeviceIdentifier[1])
            except (AttributeError, IndexError, TypeError, ValueError):  # pragma: no cover - hardware shapes
                continue
            devices.append(
                {
                    "device_instance": instance,
                    "address": str(getattr(i_am, "pduSource", "")),
                    # UNVERIFIED: vendorID attribute name on IAmRequest.
                    "vendor_id": getattr(i_am, "vendorID", None),
                }
            )
        return devices

    async def read_object_list(self, device: Mapping[str, Any]) -> list[dict[str, Any]]:
        """REQUIRES ON-SITE VALIDATION. ReadProperty device object-list."""
        app = self._ensure_app()
        address = str(device.get("address") or "")
        instance = device.get("device_instance")
        device_object = f"device,{instance}"
        # UNVERIFIED: reading the whole object-list array in one ReadProperty may
        # exceed APDU size on large devices; production code typically reads the
        # array length then each element by index. Validate / chunk on-site.
        raw_objects = await app.read_property(address, device_object, self._object_list_property)
        objects: list[dict[str, Any]] = []
        for raw in raw_objects or []:
            # UNVERIFIED: each entry is an ObjectIdentifier; str() yields
            # "objecttype,instance". Skip the device object itself.
            object_identifier = str(raw)
            if object_identifier.startswith("device,"):
                continue
            objects.append(
                {
                    "object_identifier": object_identifier,
                    "object_type": object_identifier.split(",", 1)[0],
                }
            )
        return objects

    async def read_present_value(
        self,
        device: Mapping[str, Any],
        obj: Mapping[str, Any],
    ) -> Any:
        """REQUIRES ON-SITE VALIDATION. ReadProperty present-value of an object."""
        app = self._ensure_app()
        address = str(device.get("address") or "")
        object_identifier = str(obj.get("object_identifier") or "")
        # UNVERIFIED: present-value read; some object types (e.g. structured
        # view) have no present-value and will raise ErrorRejectAbortNack — the
        # engine catches per-point failures so one bad object does not abort.
        return await app.read_property(address, object_identifier, "present-value")

    def close(self) -> None:
        """REQUIRES ON-SITE VALIDATION. Tear down the bacpypes3 Application."""
        app = self._app
        self._app = None
        if app is not None:
            close = getattr(app, "close", None)
            if callable(close):
                close()  # UNVERIFIED: Application.close() is documented as sync.


# -- backend selection ------------------------------------------------------


def _select_backend(
    parameters: Mapping[str, Any],
    backend: BacnetDiscoveryBackend | None,
) -> BacnetDiscoveryBackend:
    """Resolve the backend to use for a run.

    Precedence: an explicitly injected ``backend`` wins (used by tests/wiring).
    Otherwise ``parameters["bacnet_backend"]`` selects ``"simulated"`` (default)
    or ``"bacpypes3"``. Unknown values fall back to simulated to stay safe and
    offline; the chosen backend's name is reflected in the result summary.
    """
    if backend is not None:
        return backend
    selector = str(parameters.get("bacnet_backend") or BACKEND_SIMULATED).lower()
    if selector == BACKEND_BACPYPES3:
        # Construct here so an unavailable bacpypes3 raises the clear RuntimeError
        # (from _ensure_app) only when the real backend is actually used.
        return Bacpypes3Backend(
            local_address=parameters.get("local_address"),
            timeout_s=float(parameters.get("connect_timeout_s") or 5.0),
        )
    return SimulatedBacnetBackend()


def _backend_name(backend: BacnetDiscoveryBackend) -> str:
    return getattr(backend, "backend_name", backend.__class__.__name__)


def _instance_range(parameters: Mapping[str, Any]) -> tuple[int, int]:
    """Return the (low, high) device-instance Who-Is window from parameters."""
    low = parameters.get("device_instance_low")
    high = parameters.get("device_instance_high")
    low_int = int(low) if isinstance(low, (int, float, str)) and str(low).strip() != "" else _BACNET_INSTANCE_MIN
    high_int = int(high) if isinstance(high, (int, float, str)) and str(high).strip() != "" else _BACNET_INSTANCE_MAX
    if low_int > high_int:
        low_int, high_int = high_int, low_int
    return max(low_int, _BACNET_INSTANCE_MIN), min(high_int, _BACNET_INSTANCE_MAX)


# -- record building --------------------------------------------------------


def _device_asset(device: Mapping[str, Any], backend_name: str) -> dict[str, Any]:
    """Map a backend device dict to a discovered_assets entry.

    The asset_id is a stable per-device key (the BACnet device instance), which
    the DiscoveredPoint rows reference via ``device_ref``.
    """
    instance = device.get("device_instance")
    return {
        "asset_id": f"bacnet-device-{instance}",
        "device_instance": instance,
        "address": device.get("address"),
        "name": device.get("name"),
        "vendor": device.get("vendor"),
        "vendor_id": device.get("vendor_id"),
        "model": device.get("model"),
        "match_basis": "bacnet_who_is",
        "backend": backend_name,
    }


def _device_record(device: Mapping[str, Any], asset_id: str) -> dict[str, Any]:
    """Map a backend device dict to a DiscoveredDevice repository row."""
    return {
        "address": device.get("address"),
        "device_type": "bacnet_device",
        "name": device.get("name"),
        "vendor": device.get("vendor"),
        "model": device.get("model"),
        "attributes": {
            "asset_id": asset_id,
            "device_instance": device.get("device_instance"),
            "vendor_id": device.get("vendor_id"),
        },
    }


def _point_record(
    device: Mapping[str, Any],
    obj: Mapping[str, Any],
    present_value: Any,
    *,
    device_ref: str,
    read_error: str | None = None,
) -> dict[str, Any]:
    """Map a backend object + present-value to a DiscoveredPoint repository row.

    ``observed_value`` is a JSON object (the repository column is JSON), so the
    raw present-value is wrapped under a ``"value"`` key; ``read_error`` records
    a per-point read failure without aborting the device.
    """
    attributes: dict[str, Any] = {
        "object_type": obj.get("object_type"),
        "device_instance": device.get("device_instance"),
    }
    if read_error is not None:
        attributes["read_error"] = read_error
    return {
        "device_ref": device_ref,
        "point_id": obj.get("object_identifier"),
        "point_name": obj.get("object_name") or obj.get("object_identifier"),
        "observed_value": {} if read_error is not None else {"value": present_value},
        "units": obj.get("units"),
        "attributes": attributes,
    }


# -- the engine -------------------------------------------------------------


async def _run_bacnet_discovery(ctx: EngineContext, backend: BacnetDiscoveryBackend) -> EngineResult:
    """Async engine body: Who-Is, then throttled per-device object/value reads."""
    parameters = ctx.parameters
    low, high = _instance_range(parameters)
    address = parameters.get("address")
    backend_name = _backend_name(backend)

    # Phase 1: Who-Is. A single broadcast — run it under one throttle slot so the
    # rate limiter spaces it like any other dispatch.
    throttle = Throttle(ctx.throttle)

    async def _who_is() -> list[dict[str, Any]]:
        async with throttle.slot():
            return await backend.who_is(low, high, address)

    devices = await _who_is()

    discovered_assets: list[dict[str, Any]] = []
    device_records: list[dict[str, Any]] = []
    point_records: list[dict[str, Any]] = []

    # Phase 2: per device, read the object list then each present-value. Each
    # device is a throttled unit; run_throttled honours cancellation between
    # dispatches and returns partial results if cancelled mid-scan.
    def _make_device_unit(device: Mapping[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def _unit() -> dict[str, Any]:
            objects = await backend.read_object_list(device)
            points: list[dict[str, Any]] = []
            asset_id = f"bacnet-device-{device.get('device_instance')}"
            for obj in objects:
                # Per-point read errors must not abort the whole device scan.
                try:
                    value = await backend.read_present_value(device, obj)
                    points.append(_point_record(device, obj, value, device_ref=asset_id))
                except Exception:  # noqa: BLE001 - record a read failure, keep scanning
                    points.append(
                        _point_record(
                            device,
                            obj,
                            None,
                            device_ref=asset_id,
                            read_error="present_value_read_failed",
                        )
                    )
            return {"device": device, "asset_id": asset_id, "points": points}

        return _unit

    device_units = [_make_device_unit(device) for device in devices]
    per_device_results = await throttle.run_throttled(device_units, ctx)

    cancelled = ctx.is_cancelled()
    for entry in per_device_results:
        device = entry["device"]
        asset_id = entry["asset_id"]
        asset = _device_asset(device, backend_name)
        asset["point_count"] = len(entry["points"])
        discovered_assets.append(asset)
        device_records.append(_device_record(device, asset_id))
        point_records.extend(entry["points"])

    structured_records = device_records + point_records

    summary_extra: dict[str, Any] = {
        "backend": backend_name,
        "device_instance_low": low,
        "device_instance_high": high,
        "device_count": len(discovered_assets),
        "point_count": len(point_records),
    }
    if cancelled:
        summary_extra["partial"] = True

    return EngineResult(
        discovered_assets=discovered_assets,
        structured_records=structured_records,
        result_summary_extra=summary_extra,
        status_override="cancelled" if cancelled else None,
    )


def _dry_run_result(ctx: EngineContext, backend: BacnetDiscoveryBackend) -> EngineResult:
    """Build the dry-run plan WITHOUT contacting the network (no Who-Is)."""
    low, high = _instance_range(ctx.parameters)
    address = ctx.parameters.get("address")
    plan = build_dry_run_plan(
        engine=ENGINE_NAME,
        targets=[{"device_instance_low": low, "device_instance_high": high, "address": address}],
        actions=["bacnet-who-is-broadcast", "read-property:object-list", "read-property:present-value"],
        notes=(
            "Dry run: no Who-Is broadcast emitted. Would scan the device-instance "
            f"range [{low}, {high}] via the '{_backend_name(backend)}' backend."
        ),
        extra={"backend": _backend_name(backend), "interface": address},
    )
    return EngineResult(result_summary_extra={"backend": _backend_name(backend), "dry_run_plan": plan})


def make_bacnet_discovery_engine(
    backend: BacnetDiscoveryBackend | None = None,
) -> Callable[[EngineContext], Awaitable[EngineResult]]:
    """Return an async engine callable bound to a backend (for ``run_engine``).

    The returned callable enforces scan authorization, performs a dry-run plan
    when ``ctx.dry_run`` is set (NO broadcast), and otherwise drives the backend
    Who-Is + per-device reads under the throttle. The backend is resolved per
    call from ``ctx.parameters`` unless one is injected here.
    """

    async def _engine(ctx: EngineContext) -> EngineResult:
        # A dry run is side-effect free (it emits NO Who-Is broadcast), so a
        # preview is allowed without authorization — matching the IP/MQTT engines
        # and the safety module's documented dry-run convention. A real scan
        # (BACnet Who-Is is a broadcast that can disrupt fragile field buses)
        # still requires explicit authorization, gated AFTER the dry-run branch.
        chosen = _select_backend(ctx.parameters, backend)
        if ctx.dry_run:
            return _dry_run_result(ctx, chosen)
        require_scan_authorization(ctx.parameters)
        return await _run_bacnet_discovery(ctx, chosen)

    return _engine


def process_bacnet_discovery_run(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: RunStore,
    execution_mode: str,
    backend: BacnetDiscoveryBackend | None = None,
    throttle: ThrottleConfig | None = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Synchronous processor entrypoint mirroring the other ``process_*_run`` jobs.

    Builds the :class:`EngineContext`, selects the backend (default
    :class:`SimulatedBacnetBackend`, OFFLINE), and drives the engine via
    :func:`run_engine`. Authorization is enforced inside the engine; on an
    unauthorized run the framework records a sanitized ``failed`` status.

    Args:
        run_id / parameters / run_store / execution_mode: standard run context.
        backend: optional explicit backend (else selected from parameters;
            default simulated). Inject :class:`SimulatedBacnetBackend` for
            offline use; select ``parameters["bacnet_backend"] = "bacpypes3"``
            (or pass a :class:`Bacpypes3Backend`) for the real, UNVALIDATED path.
        throttle: optional :class:`ThrottleConfig` (defaults applied otherwise).
        dry_run: when True, returns the planned Who-Is window WITHOUT broadcasting.
        persist_records: optional structured-record persister (e.g. backed by
            DiscoveryRepository); defaults to the framework no-op.
        is_cancelled: optional cooperative-cancellation checker.

    Returns the terminal run record from the run store.
    """
    ctx = _build_context(
        run_id=run_id,
        parameters=parameters,
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle,
        dry_run=dry_run,
        is_cancelled=is_cancelled,
    )
    engine = make_bacnet_discovery_engine(backend)
    if persist_records is not None:
        return run_engine(ctx, engine, persist_records=persist_records)
    return run_engine(ctx, engine)


async def process_bacnet_discovery_run_async(
    run_id: str,
    parameters: dict[str, Any],
    *,
    run_store: RunStore,
    execution_mode: str,
    backend: BacnetDiscoveryBackend | None = None,
    throttle: ThrottleConfig | None = None,
    dry_run: bool = False,
    persist_records: Callable[[str, Sequence[dict[str, Any]]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Async variant of :func:`process_bacnet_discovery_run` (for event-loop callers)."""
    ctx = _build_context(
        run_id=run_id,
        parameters=parameters,
        run_store=run_store,
        execution_mode=execution_mode,
        throttle=throttle,
        dry_run=dry_run,
        is_cancelled=is_cancelled,
    )
    engine = make_bacnet_discovery_engine(backend)
    if persist_records is not None:
        return await run_engine_async(ctx, engine, persist_records=persist_records)
    return await run_engine_async(ctx, engine)


def _build_context(
    *,
    run_id: str,
    parameters: dict[str, Any],
    run_store: RunStore,
    execution_mode: str,
    throttle: ThrottleConfig | None,
    dry_run: bool,
    is_cancelled: Callable[[], bool] | None,
) -> EngineContext:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "parameters": parameters,
        "run_store": run_store,
        "execution_mode": execution_mode,
        "throttle": throttle or ThrottleConfig(),
        "dry_run": dry_run,
    }
    if is_cancelled is not None:
        kwargs["_is_cancelled"] = is_cancelled
    return EngineContext(**kwargs)
