"""Best-effort progressive device observations for the loopback-sidecar scanners.

The ``ip_scanner`` / ``bacnet_scanner`` sidecar adapters consume their vendored
SSE stream server-side and return only the final ``{rows, summary}``. To let the
native page show provisional rows WHILE a scan runs, each adapter folds its live
``device`` / ``device-update`` events into viewer-safe ``projection_v1`` device
observations and appends them through the run store's progressive-observation
sink — the SAME durable channel the sealed ip/bacnet discovery lanes use and the
frontend already folds (``projectedDeviceRecords`` / ``projection_v1``,
``ModulePage.tsx``).

Design guarantees (why this is safe to add to an inline sidecar run):

* Additive and best-effort. It NEVER changes the final persisted result — that
  still comes from the adapter's ``_map_result`` through the normal persist
  path (``run_store.replace_devices``) — and it NEVER breaks a scan. A run store
  that does not accept the observation (or any transient append error) latches
  emission off for the rest of the run, so a scan proceeds unchanged and the
  lifecycle-conflict audit is not spammed.
* Bounded. At most :data:`MAX_PROGRESSIVE_DEVICE_OBSERVATIONS` appends per run,
  so a large subnet (or a chatty enrichment stream) cannot flood the observation
  stream. The final result still lists every device; only the LIVE preview is
  capped.

The emitter validates every observation through the real
:class:`DiscoveryObservationInputV1` contract before it reaches the sink, so a
malformed device field is skipped (that one device only) rather than corrupting
the stream or disabling the preview.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from smart_commissioning_core.discovery_observations import DiscoveryObservationInputV1

logger = logging.getLogger(__name__)

# ponytail: fixed live-preview ceiling. The final result is never capped (it
# persists through ``_map_result``); only the progressive rows are. Raise it, or
# promote it to a run parameter, only if a real site needs a bigger live preview.
MAX_PROGRESSIVE_DEVICE_OBSERVATIONS = 500


class ProgressiveDeviceEmitter:
    """Append capped, viewer-safe ``projection_v1`` device observations, best-effort.

    One instance per run. ``run_store`` is the executor-owned store handed to the
    engine; when it exposes no ``append_observation`` sink (a legacy in-memory
    store, or a test double without the U2 sink) the emitter is a silent no-op,
    exactly like :attr:`EngineContext.supports_progressive_observations`.
    """

    def __init__(self, run_store: Any, *, protocol: str, entity_kind: str) -> None:
        sink = getattr(run_store, "append_observation", None)
        self._sink = sink if callable(sink) else None
        self._protocol = protocol
        self._entity_kind = entity_kind
        self._phase = "reachability" if protocol == "ip" else "enrichment"
        self._enabled = self._sink is not None
        self._positions: dict[str, int] = {}
        self._versions: dict[str, int] = {}
        self._emitted = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def emitted(self) -> int:
        return self._emitted

    def emit(self, entity_key: str, record: Mapping[str, Any]) -> None:
        """Publish one provisional device row; never raises to the caller.

        A stable ``entity_key`` folds re-observations of the same device onto one
        row (each carrying a higher ``entity_version``); the position is assigned
        once, in first-seen order, so the frontend's entity-key sort is stable.
        """

        if not self._enabled or self._emitted >= MAX_PROGRESSIVE_DEVICE_OBSERVATIONS:
            return
        position = self._positions.setdefault(entity_key, len(self._positions))
        version = self._versions.get(entity_key, 0) + 1
        try:
            observation = DiscoveryObservationInputV1(
                protocol=self._protocol,
                entity_kind=self._entity_kind,
                entity_key=entity_key,
                entity_version=version,
                event_key=f"{entity_key}:v{version}",
                phase=self._phase,
                outcome="observed",
                payload_schema_version="1.0",
                payload={
                    "projection_v1": {
                        "collection": "devices",
                        "position": position,
                        "present": True,
                        "record": dict(record),
                    }
                },
                observed_at=datetime.now(UTC),
            )
        except Exception:  # noqa: BLE001 (advisory row; a bad field must not stop the preview)
            logger.debug(
                "skipping malformed provisional device observation (%s)",
                entity_key,
                exc_info=True,
            )
            return
        try:
            self._sink(observation)  # type: ignore[misc]  (guarded: _enabled implies _sink)
        except Exception:  # noqa: BLE001 (a store that rejects sidecar rows disables the preview)
            self._enabled = False
            logger.debug(
                "progressive device observations disabled for this run", exc_info=True
            )
            return
        self._versions[entity_key] = version
        self._emitted += 1


__all__ = ["MAX_PROGRESSIVE_DEVICE_OBSERVATIONS", "ProgressiveDeviceEmitter"]
