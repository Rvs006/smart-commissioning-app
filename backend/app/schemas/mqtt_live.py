"""Schemas for the MQTT live session (M4a): a held broker connection streamed
to the browser through the sidecar, distinct from a bounded capture run.

A live session is not a job, so these do not live in ``jobs.py``. The sidecar's
``connection``/``stats`` objects are passed through as opaque dicts (they carry
no secrets) rather than re-modelled here, so a sidecar field addition does not
need a schema change; the frontend types pin the shape instead.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MqttLiveConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    # Legacy consent gate: a live broker subscribe is real network I/O.
    authorized: bool = False
    # Subscription filter (comma-split by the sidecar); None -> the sidecar's "#".
    # The browser NEVER sends broker host/credentials; those resolve server-side.
    root_filter: str | None = Field(default=None, max_length=512)
    qos: int | None = Field(default=None, ge=0, le=2)
    # Replace an existing session held by another operator.
    take_over: bool = False


class MqttLiveSessionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    owner: str
    project_id: str
    site_id: str
    since: datetime


class MqttLiveConnectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    session: MqttLiveSessionInfo
    # The sidecar's ``status`` (connection) object, passed through verbatim.
    connection: dict[str, object] = Field(default_factory=dict)


class MqttLiveStatusResponse(BaseModel):
    # populate_by_name so the route can construct with ``register_summary`` while
    # the wire key stays ``register`` (the sidecar's own name); pydantic warns if
    # a field is literally named ``register`` (it shadows a BaseModel attr).
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    session: MqttLiveSessionInfo | None
    sidecar_available: bool
    connection: dict[str, object] | None = None
    stats: dict[str, object] | None = None
    register_summary: dict[str, object] | None = Field(default=None, alias="register")


class MqttLiveDisconnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None releases whatever is held (an operator Stop must always be safe).
    session_id: str | None = None


class MqttLiveDisconnectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    released: bool
