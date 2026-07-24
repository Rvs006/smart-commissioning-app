"""Deterministic, cross-platform names for generated report artifacts."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

REPORT_FORMAT_EXTENSIONS = {
    "docx": "docx",
    "pdf": "pdf",
    "xlsx": "xlsx",
    "zip": "zip",
}

_WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*')
_MULTIPLE_UNDERSCORES = re.compile(r"_+")
_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _truncate_utf8(value: str, byte_limit: int) -> str:
    """Return a valid UTF-8 prefix no longer than ``byte_limit`` bytes."""

    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore").rstrip(" ._")


def _safe_component(value: object, *, fallback: str, byte_limit: int) -> str:
    """Make one readable filename component safe on Windows and in ZIP files."""

    text = unicodedata.normalize("NFKC", str(value) if value is not None else "")
    characters: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if (
            character in _WINDOWS_FORBIDDEN
            or character.isspace()
            or category in {"Cc", "Cs"}
            or ord(character) in {0xFFFE, 0xFFFF}
        ):
            characters.append("_")
        elif character.isalnum() or character in {"-", "_", ".", "(", ")"}:
            characters.append(character)
        else:
            characters.append("_")
    component = _MULTIPLE_UNDERSCORES.sub("_", "".join(characters)).strip(" ._")
    if component in {"", ".", ".."}:
        component = fallback
    component = _truncate_utf8(component, byte_limit)
    return component or fallback


def build_report_file_name(
    report_type: str,
    run_id: str,
    output_format: str,
    *,
    report_title: object = None,
    custom_title: bool = False,
) -> str:
    """Build the persisted/downloaded name for one report.

    Runs created before v0.1.25 have no explicit ``report_title_custom`` marker.
    They deliberately retain the historical ``<type>_<run-id>.<ext>`` name even
    when a title happens to be present in their stored parameters.
    """

    extension = REPORT_FORMAT_EXTENSIONS.get(output_format, "zip")
    safe_type = _safe_component(report_type, fallback="report", byte_limit=64)
    safe_run_id = _safe_component(run_id, fallback="report", byte_limit=80)
    stem = (
        _safe_component(report_title, fallback=safe_type, byte_limit=112)
        if custom_title
        else safe_type
    )
    return f"{stem}_{safe_run_id}.{extension}"


def report_content_disposition(file_name: str) -> str:
    """Return an RFC 6266 attachment header with ASCII and UTF-8 filenames."""

    decomposed = unicodedata.normalize("NFKD", file_name)
    ascii_name = decomposed.encode("ascii", errors="ignore").decode("ascii")
    ascii_name = _ASCII_UNSAFE.sub("_", ascii_name).strip(" ._") or "report"
    ascii_name = _MULTIPLE_UNDERSCORES.sub("_", ascii_name)
    encoded_name = quote(file_name, safe="")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{encoded_name}"
    )
