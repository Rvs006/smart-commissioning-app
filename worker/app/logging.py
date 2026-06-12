"""Structured JSON logging for the Dramatiq worker (stdlib only).

The worker runs in its own package context (PYTHONPATH=/app/worker:/app/core)
and cannot import the backend's ``app.core.logging`` module, so it carries a
compact, self-contained copy of the same JSON line format. Keeping the output
shape identical (timestamp/level/logger/message + run_id) means worker and API
logs aggregate cleanly in one log pipeline.

Only the Python standard library is used — no third-party logging dependency.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

# Worker correlation is per-message: each actor binds the run_id it is
# processing, isolated via a contextvar.
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sct_worker_run_id",
    default=None,
)


def set_run_id(value: str | None) -> contextvars.Token:
    return _run_id_var.set(value)


def reset_run_id(token: contextvars.Token) -> None:
    _run_id_var.reset(token)


def get_run_id() -> str | None:
    return _run_id_var.get()


@contextmanager
def run_id_context(run_id: str | None) -> Iterator[None]:
    """Bind ``run_id`` for the body of an actor, then restore."""
    token = set_run_id(run_id)
    try:
        yield
    finally:
        reset_run_id(token)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.run_id = get_run_id()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat()
        payload: dict[str, object] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            payload["run_id"] = run_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_SCT_HANDLER_FLAG = "_sct_worker_json_handler"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON formatter + run-id filter on the root logger (idempotent)."""
    if isinstance(level, str):
        level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _SCT_HANDLER_FLAG, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RunIdFilter())
    setattr(handler, _SCT_HANDLER_FLAG, True)

    root.addHandler(handler)
    root.setLevel(level)
