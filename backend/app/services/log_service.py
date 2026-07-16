"""Reads of the Logging & Diagnostics configuration section, plus the log
bundle / upload machinery behind the engineer-gated ``/logs`` routes.

Everything that interprets the (now real) Logging & Diagnostics fields lives
here so the rest of the app depends on named helpers rather than magic strings:

* level / retention derivation from the stored config words;
* :func:`apply_logging_settings` — re-point the live logging at the configured
  level + the rotating file handler, called from startup and from a config save
  so a changed Log Level takes effect without a restart;
* :func:`mask_log_text` — redact credential-shaped keys before a log bundle
  leaves the machine;
* :func:`build_log_bundle` — zip the local ``logs/`` directory (and NOTHING
  else — never the secrets store, DB, or import files) with masking applied;
* :func:`upload_log_bundle` — POST the bundle to a configured URL and report the
  TRUTHFUL outcome (a dead endpoint is ``no_response``, never a faked success).

Masking guarantee (stated honestly): this masks values under known
credential-shaped keys (``password``/``token``/``secret``/``api_key``/
``authorization``/``private_key``). It is NOT a DLP scanner and cannot catch a
secret embedded in free text by some future log call. The primary control is
CONTAINMENT — the bundle only ever contains files under the local logs
directory; the secrets store and the database are never zipped.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import re
import urllib.parse
import zipfile
from datetime import UTC, datetime
from typing import Literal

import httpx

from app.core.logging import configure_file_logging
from app.core.runtime import RUNTIME_ROOT

logger = logging.getLogger(__name__)

# Field names in the Logging & Diagnostics configuration section. Spelled once
# here so the config defaults, the validators, and these readers cannot drift.
LOG_LEVEL_FIELD = "Log Level"
LOG_RETENTION_FIELD = "Log Retention"
DIAGNOSTICS_FIELD = "Diagnostics Mode"
UPLOAD_URL_FIELD = "Log Upload URL"
UPLOAD_TOKEN_FIELD = "Log Upload Token"

# The local logging destination. The portable launcher anchors RUNTIME_ROOT to a
# machine-stable folder (run_smart_commissioning_app.py) and already creates
# RUNTIME_ROOT/logs for its crash logs, so app.log lands beside them.
LOG_DIR = RUNTIME_ROOT / "logs"

# Accepted Log Level words (casefolded), mapped to the logging level name.
_LEVEL_WORDS = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR"}

_DEFAULT_RETENTION_DAYS = 30


def effective_log_level(values: dict[str, str]) -> str:
    """Resolve the level name the process should log at, by precedence.

    1. Diagnostics Mode Enabled -> ``DEBUG`` (the operator asked for verbose).
    2. else the ``LOG_LEVEL`` environment variable, if set — an ops/deploy
       override that keeps every existing deployment and CI run byte-identical
       when nobody has touched the new config fields.
    3. else the stored Log Level word (``Info`` -> ``INFO``).
    4. else ``INFO``.
    """
    if str(values.get(DIAGNOSTICS_FIELD, "")).strip().casefold() == "enabled":
        return "DEBUG"
    env_level = os.environ.get("LOG_LEVEL")
    if env_level:
        return env_level.strip().upper()
    stored = str(values.get(LOG_LEVEL_FIELD, "")).strip().casefold()
    return _LEVEL_WORDS.get(stored, "INFO")


def retention_days(values: dict[str, str]) -> int:
    """Days of rotated/crash log history to keep, parsed from ``Log Retention``.

    Prefix-parses the leading integer of e.g. ``"30 days"`` (mirroring the
    configuration validator's own prefix split); an unparseable or non-positive
    value falls back to the 30-day default rather than disabling the purge.
    """
    prefix = str(values.get(LOG_RETENTION_FIELD, "")).strip().split(" ", 1)[0]
    try:
        parsed = int(prefix)
    except ValueError:
        return _DEFAULT_RETENTION_DAYS
    return parsed if parsed > 0 else _DEFAULT_RETENTION_DAYS


def apply_logging_settings(values: dict[str, str]) -> None:
    """Point the live logging at the configured level and the rotating file.

    Sets the root logger level and (re)installs the JSON file handler at
    ``LOG_DIR/app.log`` at the same level. Called from the app lifespan and after
    a configuration save so a Log Level / Diagnostics Mode change takes effect in
    the running process, not only at next boot.
    """
    level = effective_log_level(values)
    logging.getLogger().setLevel(level)
    configure_file_logging(LOG_DIR, level)


# Credential-shaped keys whose values are redacted before a bundle leaves the
# box. Matched in JSON ("key": "value") and key=value forms, case-insensitively.
_SECRET_KEY_GROUP = r"password|passwd|token|secret|api[-_]?key|authorization|private[-_]?key"
_SECRET_JSON_RE = re.compile(
    rf'("(?:{_SECRET_KEY_GROUP})"\s*:\s*")[^"]*(")',
    re.IGNORECASE,
)
_SECRET_EQ_RE = re.compile(
    rf"\b({_SECRET_KEY_GROUP})(\s*=\s*)(\S+)",
    re.IGNORECASE,
)
_MASK = "********"


def mask_log_text(text: str) -> str:
    """Redact credential-shaped values in ``text`` to ``********``.

    Handles JSON pairs (``"password": "hunter2"``) and key=value forms
    (``token=abc``). Lines with no credential-shaped key are returned
    byte-identical, so ``secret://`` references and ordinary message text pass
    through untouched (a ``secret://`` ref is already opaque, and it carries no
    ``=`` for the key=value rule to trip on).
    """
    masked = _SECRET_JSON_RE.sub(rf"\1{_MASK}\2", text)
    return _SECRET_EQ_RE.sub(rf"\1\2{_MASK}", masked)


def build_log_bundle() -> tuple[bytes, list[str]]:
    """Zip every ``*.log*`` file under :data:`LOG_DIR`, masked, in memory.

    Members are the rotating ``app.log``/``app.log.N``, ``crash-*.log`` and
    ``faulthandler-*.log`` files ONLY. The secrets store, the database, and
    uploaded import files are never touched — that containment, not the regex, is
    the primary secrets control. Returns ``(zip_bytes, member_names)``.
    """
    members: list[str] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(LOG_DIR.glob("*.log*")):
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            archive.writestr(path.name, mask_log_text(raw.decode("utf-8", errors="replace")))
            members.append(path.name)
    return buffer.getvalue(), members


@dataclasses.dataclass
class UploadOutcome:
    """Truthful terminal result of a log-bundle upload attempt.

    ``no_response`` is a real answer (the endpoint did not respond), never a
    fabricated failure or success. The token is never placed in ``detail``.
    """

    outcome: Literal["uploaded", "rejected", "no_response"]
    status_code: int | None
    detail: str
    bundle_bytes: int
    files: list[str]


def _validate_upload_url(url: str) -> None:
    """Reject an upload URL that is not http(s) or that embeds credentials.

    The token must ride the Authorization header, never the URL — so a
    ``user:pass@host`` form is refused (privacy, and it would defeat the masking
    guarantee by putting a secret somewhere we do not scrub).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Log Upload URL must be an http:// or https:// URL.")
    if not parts.netloc:
        raise ValueError("Log Upload URL must include a host.")
    if parts.username or parts.password:
        raise ValueError(
            "Log Upload URL must not embed credentials (user:pass@host). "
            "Put the bearer token in the Log Upload Token field instead."
        )


def upload_log_bundle(url: str, token: str) -> UploadOutcome:
    """POST the masked log bundle to ``url`` and report the honest outcome.

    Sends a multipart ``file`` field; the ``token`` (when non-empty) rides an
    ``Authorization: Bearer`` header and is never logged, never put in the URL,
    and never placed in the returned detail. A 2xx is ``uploaded``; a >=400 is
    ``rejected`` with the status and a truncated response snippet; a transport
    failure (no response, DNS, TLS, timeout) is ``no_response`` — no retry, no
    fabricated success. Raises :class:`ValueError` for an invalid URL.
    """
    _validate_upload_url(url)
    bundle, files = build_log_bundle()
    filename = f"smart_commissioning_logs_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = httpx.post(
            url,
            files={"file": (filename, bundle, "application/zip")},
            headers=headers,
            timeout=60.0,
        )
    except (httpx.HTTPError, OSError) as error:
        # Build detail from the exception type/message only — never from the
        # request (which could echo the URL); the token is not in either.
        return UploadOutcome(
            outcome="no_response",
            status_code=None,
            detail=f"{type(error).__name__}: {error}",
            bundle_bytes=len(bundle),
            files=files,
        )
    if response.is_success:
        return UploadOutcome(
            outcome="uploaded",
            status_code=response.status_code,
            detail=f"Server accepted the bundle ({response.status_code}).",
            bundle_bytes=len(bundle),
            files=files,
        )
    snippet = mask_log_text(response.text[:500]).strip()
    return UploadOutcome(
        outcome="rejected",
        status_code=response.status_code,
        detail=f"Server rejected the bundle ({response.status_code}): {snippet}"
        if snippet
        else f"Server rejected the bundle ({response.status_code}).",
        bundle_bytes=len(bundle),
        files=files,
    )
