"""Deterministic, redacted raw-evidence export for persisted UDMI runs."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

EXPORT_SCHEMA_VERSION = "1.0"
EXPORT_SCHEMA_PATH = "docs/schemas/udmi-validation-export-v1.schema.json"
_REDACTED = "********"
_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "auth_header",
    "authorization",
    "bearer",
    "client_secret",
    "connection_string",
    "cookie",
    "credential",
    "database_url",
    "dsn",
    "password",
    "passphrase",
    "private_key",
    "redis_url",
    "secret",
    "session_key",
    "token",
)
_SECRET_KEY_ALIASES = {"passwd", "pwd"}
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----",
    flags=re.IGNORECASE,
)
_LONE_SURROGATE_PATTERN = re.compile(r"[\uD800-\uDFFF]")
_SAFE_FILE_PART = re.compile(r"[^A-Za-z0-9._-]+")


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _secret_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
    collapsed = normalized.replace("_", "")
    return collapsed in _SECRET_KEY_ALIASES or any(
        part in normalized or part.replace("_", "") in collapsed
        for part in _SECRET_KEY_PARTS
    )


def _unicode_safe_text(value: object) -> str:
    return _LONE_SURROGATE_PATTERN.sub(
        lambda match: f"\\u{ord(match.group(0)):04X}",
        str(value),
    )


def redact_export_value(value: object, *, key: object = "") -> object:
    """Recursively redact credential-shaped keys and private-key PEM text."""
    value = _json_value(value)
    if _secret_key(key) and value not in (None, ""):
        return _REDACTED
    if isinstance(value, dict):
        return {
            _unicode_safe_text(child_key): redact_export_value(child_value, key=child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_export_value(item) for item in value]
    if isinstance(value, str):
        if _PRIVATE_KEY_PATTERN.search(value):
            return _REDACTED
        # JSON can spell lone UTF-16 surrogates, but they are not Unicode scalar
        # values and cannot be emitted as UTF-8. Preserve their code-unit value
        # as visible escaped text so the evidence stays downloadable and usable.
        return _unicode_safe_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return f"{value} (non-standard JSON number)"
    return value


def _timestamp(value: object) -> str:
    value = _json_value(value)
    return str(value or "")


def build_validation_export(run: object) -> dict[str, object]:
    """Build the versioned envelope solely from one stored run snapshot."""
    updated_at = _timestamp(getattr(run, "updated_at", None))
    created_at = _timestamp(getattr(run, "created_at", None))
    status = str(getattr(run, "status", ""))
    issues = list(getattr(run, "issues", []) or [])
    result_summary = getattr(run, "result_summary", {})
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "schema": EXPORT_SCHEMA_PATH,
        # Stable by design: a repeat download of the same stored run produces
        # identical bytes instead of embedding wall-clock time.
        "exported_at": updated_at or created_at,
        "project_id": str(getattr(run, "project_id", "")),
        "site_id": str(getattr(run, "site_id", "")),
        "run": {
            "run_id": str(getattr(run, "run_id", "")),
            "job_type": str(getattr(run, "job_type", "")),
            "status": status,
            "stage": str(getattr(run, "stage", "")),
            "progress_percent": int(getattr(run, "progress_percent", 0) or 0),
            "created_at": created_at,
            "updated_at": updated_at,
            "partial": status in {"cancelled", "failed"},
            "error_message": redact_export_value(
                getattr(run, "error_message", None),
                key="error_message",
            ),
        },
        "result_summary": redact_export_value(result_summary),
        "issues": redact_export_value(issues),
        "evidence_limitations": [
            (
                "The full MQTT message stream is not retained. The validation snapshot "
                "stores counts, captured topics, issues, and at most the latest structured "
                "payload for each asset and payload type."
            ),
            "Non-JSON MQTT bodies are represented by validation findings rather than raw bytes.",
            "Credential-shaped fields and private-key PEM text are redacted from this export.",
            "Non-finite numeric values in legacy evidence are preserved as descriptive strings.",
            "Invalid lone UTF-16 surrogate code units are represented as escaped text.",
        ],
    }


def stable_validation_export_bytes(run: object) -> bytes:
    """Return human-readable, deterministic UTF-8 JSON for a stored run."""
    return (
        json.dumps(
            build_validation_export(run),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def validation_export_filename(run: object) -> str:
    def safe(value: object, fallback: str) -> str:
        cleaned = _SAFE_FILE_PART.sub("-", str(value or "").strip()).strip(".-_")
        return cleaned[:80] or fallback

    site = safe(getattr(run, "site_id", None), "site")
    run_id = safe(getattr(run, "run_id", None), "run")
    return f"udmi-validation-{site}-{run_id}.json"
