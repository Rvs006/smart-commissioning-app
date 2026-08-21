"""Pure helpers for the Advanced-panel reverse proxy (``scanner_raw``).

The proxy forwards a browser iframe's requests to a vendored standalone scanner
sidecar. This module decides, per request, whether it is a read (flows free) or
a write (guarded). Fail closed: any non-GET that is not a known read-action is
treated as a write, so a future upstream write endpoint is guarded the day it
appears rather than the day someone remembers to classify it.

No FastAPI, no I/O - just string classification, so it is unit-tested directly.
"""

from __future__ import annotations

from app.services.sidecar_supervisor import BACNET_SCANNER, IP_SCANNER, MQTT_SCANNER

# URL path segment (``/scanners/{proto}/raw/...``) -> supervisor sidecar name.
SIDECAR_BY_PROTO: dict[str, str] = {
    "ip": IP_SCANNER,
    "bacnet": BACNET_SCANNER,
    "mqtt": MQTT_SCANNER,
}

# Non-GET endpoints that are still reads (no device write): register import/clear,
# re-RAG compare, the MQTT read-side controls, and BACnet's export (a POST that
# streams a zip). Matched on the normalized ``/api/...`` path; the method is
# already known non-GET when this set is consulted.
_READ_ACTION_PATHS = frozenset(
    {
        "/api/register",
        "/api/compare",
        "/api/search",
        "/api/focus",
        "/api/subscribe",
        "/api/connect",
        "/api/disconnect",
        "/api/export",
    }
)


def _normalize(subpath: str) -> str:
    """The proxied sub-path (after ``/raw/``) as a leading-slash path, query
    stripped: ``api/scan?x=1`` -> ``/api/scan``, ``""`` -> ``/``."""
    return "/" + subpath.split("?", 1)[0].strip("/")


def is_api_path(subpath: str) -> bool:
    """True for the sidecar's ``/api/*`` surface (vs its static UI assets)."""
    return _normalize(subpath).startswith("/api/")


def classify(method: str, subpath: str) -> str:
    """``"read"`` or ``"write"``.

    GET/HEAD are always reads. Every other method is a write unless the path is a
    known read-action. Static asset requests (non ``/api/``) are always reads.
    """
    if method.upper() in ("GET", "HEAD"):
        return "read"
    if not is_api_path(subpath):
        return "read"
    if _normalize(subpath) in _READ_ACTION_PATHS:
        return "read"
    return "write"
