#!/usr/bin/env python3
"""Fail a release when an Nmap/Npcap component or acquisition hook is bundled."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

_FORBIDDEN_BASENAMES = frozenset(
    {
        "nmap.exe",
        "nmap.dtd",
        "nmap.xsl",
        "nmap.xsd",
        "nmap-mac-prefixes",
        "nmap-os-db",
        "nmap-payloads",
        "nmap-protocols",
        "nmap-rpc",
        "nmap-service-probes",
        "nmap-services",
        "packet.dll",
        "wpcap.dll",
        "npf.sys",
    }
)
_FORBIDDEN_NAME = re.compile(r"^(?:npcap(?:[-_.].*)?|nmap(?:[-_.].*)?\.(?:exe|dll|sys)|.+\.nse)$")
_FORBIDDEN_SBOM_COMPONENT = re.compile(r"(?:^|[-_/.])(?:nmap|npcap)(?:$|[-_/.])")
_FORBIDDEN_PAYLOAD_MARKERS = (
    b"https://nmap.org/dist/",
    b"https://nmap.org/download",
    b"choco install nmap",
    b"winget install",
    b"-verb runas",
    b"shellexecuteexw",
)
_FORBIDDEN_PAYLOAD_MARKER_FORMS = tuple(
    (marker, marker.decode("ascii").encode("utf-16le"))
    for marker in _FORBIDDEN_PAYLOAD_MARKERS
)
_PAYLOAD_MARKER_OVERLAP_BYTES = (
    max(len(encoded) for forms in _FORBIDDEN_PAYLOAD_MARKER_FORMS for encoded in forms) - 1
)
_READ_CHUNK_BYTES = 1024 * 1024


def _forbidden_component_name(value: str) -> bool:
    basename = PurePosixPath(value.replace("\\", "/")).name.lower()
    return basename in _FORBIDDEN_BASENAMES or bool(_FORBIDDEN_NAME.fullmatch(basename))


def _matching_marker(chunks: Iterable[bytes]) -> bytes | None:
    previous = b""
    for chunk in chunks:
        lowered = previous + chunk.lower()
        for marker, encoded_forms in zip(
            _FORBIDDEN_PAYLOAD_MARKERS,
            _FORBIDDEN_PAYLOAD_MARKER_FORMS,
            strict=True,
        ):
            if any(encoded in lowered for encoded in encoded_forms):
                return marker
        previous = lowered[-_PAYLOAD_MARKER_OVERLAP_BYTES:]
    return None


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            yield chunk


def _archive_findings(path: Path) -> list[str]:
    findings: list[str] = []
    if not zipfile.is_zipfile(path):
        return findings
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            if _forbidden_component_name(member.filename):
                findings.append(f"forbidden archive component: {path.name}!{member.filename}")
            with archive.open(member) as handle:
                marker = _matching_marker(iter(lambda: handle.read(_READ_CHUNK_BYTES), b""))
            if marker is not None:
                findings.append(
                    f"forbidden acquisition or elevation marker: {path.name}!{member.filename}"
                )
    return findings


def _sbom_findings(path: Path) -> list[str]:
    if not path.is_file():
        return [f"required SBOM is missing: {path.name}"]
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [f"SBOM is unreadable: {path.name}"]
    components = document.get("components")
    if not isinstance(components, list):
        return [f"SBOM has no component list: {path.name}"]
    findings: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            findings.append(f"SBOM has an invalid component: {path.name}")
            continue
        identities = (
            str(component.get(field, "")).lower()
            for field in ("name", "group", "purl", "bom-ref")
        )
        if any(_FORBIDDEN_SBOM_COMPONENT.search(identity) for identity in identities):
            findings.append(f"forbidden SBOM component: {path.name}")
    return findings


def scan_distribution(
    bundle_root: Path,
    *,
    sbom_paths: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    """Return deterministic findings without exposing bundled file contents."""

    root = Path(bundle_root)
    if not root.is_dir():
        return ("portable bundle directory is missing",)
    findings: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if path.is_symlink():
            findings.append(f"portable bundle contains a link: {path.relative_to(root).as_posix()}")
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _forbidden_component_name(relative):
            findings.append(f"forbidden bundled component: {relative}")
        marker = _matching_marker(_file_chunks(path))
        if marker is not None:
            findings.append(f"forbidden acquisition or elevation marker: {relative}")
        findings.extend(_archive_findings(path))
    for sbom_path in sbom_paths:
        findings.extend(_sbom_findings(Path(sbom_path)))
    return tuple(sorted(set(findings)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--sbom", action="append", type=Path, default=[])
    arguments = parser.parse_args()
    findings = scan_distribution(arguments.bundle_dir, sbom_paths=tuple(arguments.sbom))
    if findings:
        for finding in findings:
            print(f"FAIL: {finding}")
        return 1
    print("PASS: portable distribution contains no Nmap/Npcap component or acquisition hook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
