"""Compatibility alias for the shared owned-run heartbeat guard."""

from smart_commissioning_core.owned_run_heartbeat import OwnedRunHeartbeat

InlineRunHeartbeat = OwnedRunHeartbeat

__all__ = ["InlineRunHeartbeat"]
