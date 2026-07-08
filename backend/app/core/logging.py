"""Structured JSON logging and request correlation for the API.

This module uses ONLY the Python standard library (``logging``, ``json``,
``uuid``, ``contextvars``) — no third-party logging dependency.

What it provides:

* :func:`configure_logging` — installs a :class:`JsonLogFormatter` on the root
  logger so every record is emitted as a single JSON line carrying a timestamp,
  level, logger name, message, and the current ``request_id`` correlation value
  when it is set.
* Contextvar-based correlation helpers (:func:`set_request_id`,
  :func:`get_request_id`) plus a :class:`CorrelationIdFilter` that injects the
  value onto each record.
* :func:`new_request_id` — a stdlib ``uuid4`` generator used by the request-id
  middleware when no inbound ``X-Request-ID`` header is present.

The correlation value is stored in :mod:`contextvars`, so it is isolated per
asyncio task and never leaks across concurrent requests.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import uuid

# -- correlation context ----------------------------------------------------

# Default is None (not set). The formatter/filter render a missing value as
# absent rather than the literal string "None".
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sct_request_id",
    default=None,
)

# Header used to accept/propagate the correlation id across services.
REQUEST_ID_HEADER = "X-Request-ID"


def new_request_id() -> str:
    """Return a fresh request id (stdlib uuid4 hex)."""
    return uuid.uuid4().hex


def set_request_id(value: str | None) -> contextvars.Token:
    """Bind ``value`` as the current request id; returns a reset token."""
    return _request_id_var.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    return _request_id_var.get()


# -- logging filter + formatter ---------------------------------------------


class CorrelationIdFilter(logging.Filter):
    """Inject the current request_id onto every log record.

    Always returns True (it never filters records out); it only annotates them
    so the formatter can render the correlation field.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = get_request_id()
        return True


# Standard LogRecord attributes that the formatter consumes directly; anything
# else attached to the record (via ``extra=``) is emitted under the JSON body.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "request_id",
    },
)


class JsonLogFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Always emits: ``timestamp`` (UTC ISO 8601), ``level``, ``logger``,
    ``message``. Emits ``request_id`` only when it is set on the record (the
    :class:`CorrelationIdFilter` sets it, possibly to ``None``). Exception info
    is rendered under ``exc_info`` as formatted text. Any extra fields attached
    via ``logger.info(..., extra={...})`` are merged in.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat()
        payload: dict[str, object] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id

        # Merge user-supplied extras (skip reserved/internal attributes and any
        # private dunder fields).
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            payload.setdefault(key, value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str)


# Marker attribute so configure_logging is idempotent: re-running it (e.g. a
# test harness re-importing the app) replaces the handler instead of stacking.
_SCT_HANDLER_FLAG = "_sct_json_handler"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON formatter + correlation filter on the root logger.

    Idempotent: a previously installed SCT handler is removed first so repeated
    calls (test harness, worker import) do not duplicate log lines. The level
    may be an int or a level name; an unknown name falls back to INFO.
    """
    if isinstance(level, str):
        level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    root = logging.getLogger()

    for existing in list(root.handlers):
        if getattr(existing, _SCT_HANDLER_FLAG, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(CorrelationIdFilter())
    setattr(handler, _SCT_HANDLER_FLAG, True)

    root.addHandler(handler)
    root.setLevel(level)
