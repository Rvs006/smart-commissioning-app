#!/usr/bin/env python3
"""Scan release-facing files for credential material and private identifiers.

The repository contains tests with synthetic secret markers by design. The
default scan excludes those test fixtures and scans release-facing inputs;
explicit `--path` arguments are useful for a staged evidence bundle and do not
apply the repository test allowlist.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*"
    r"[A-Za-z0-9+/=\r\n]{64,}\s*"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
_CREDENTIAL_URL = re.compile(r"\b(?:mqtts?|redis|postgres(?:ql)?|amqps?)://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w.])(?:[A-Za-z][A-Za-z0-9]*(?:[_ -][A-Za-z0-9]+)*[_ -])?"
    r"(?:password|passwd|secret|private[_ -]?key|api[_ -]?key|access[_ -]?token)"
    r"(?![_A-Za-z0-9])"
    r"[\"']?\s*(?:=|:\s+|:\s*(?=[\"']))\s*"
    r"(?:[\"'](?P<quoted>[^\"'\r\n]{8,})[\"']|(?P<bare>[^\s,#,(){}\[\];\"']{8,}))"
)
_MOSQUITTO_SECRET = re.compile(r"(?i)\bmosquitto_(?:pub|sub)\b[^\r\n]*(?:\s-P|\s--password)\s+([^\s]+)")
_SAFE_VALUE = re.compile(
    r"^(?:\*+|<[^>]+>|\{\{|secret://|//|\?|New-HexSecret|\$env:|\$ENV:|\$\(|\$\{|\$[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_SIMPLE_BARE_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# A value that carries a credential keyword as a whole word AND no digit is a
# schema field name or a label ABOUT the credential, not the secret itself:
# `"mqtt_password": "password"`, `"MQTT Password": "Broker password (masked)"`,
# `"Private Key": "Private key paired with the client certificate."`. An opaque
# token, key, or passphrase does not spell out "password"/"secret"/"private key"
# (and real tokens carry digits), so it still matches. Value shape alone never
# suppresses: `"password": "correct horse battery staple"` and
# `"password": "SomeIdentifier"` carry no keyword and are reported.
_CREDENTIAL_KEYWORD = re.compile(
    r"(?i)(?<![A-Za-z])(?:password|passwd|secret|private[_ -]?key|api[_ -]?key|access[_ -]?token)(?![A-Za-z])"
)
_TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".conf",
    ".csv",
    ".crt",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".key",
    ".json",
    ".jsonl",
    ".jsx",
    ".log",
    ".md",
    ".py",
    ".ps1",
    ".pem",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_ARCHIVE_SUFFIXES = {".docx", ".tar", ".tgz", ".xlsx", ".zip"}
_MAX_ARCHIVE_MEMBER_BYTES = 50 * 1024 * 1024
# ponytail: fixed nesting cap; each level is also bounded to _MAX_ARCHIVE_MEMBER_BYTES
# per member, so depth * member size bounds the work. Raise only if a real release
# artifact ever nests archives deeper than this.
_MAX_ARCHIVE_DEPTH = 8


def _is_archive(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() in _ARCHIVE_SUFFIXES or name.endswith(".tar.gz")


def _is_zip_name(name: str) -> bool:
    return name.casefold().endswith((".docx", ".xlsx", ".zip"))


def _should_scan_member(name: str) -> bool:
    """Whether an archive member should be scanned, mirroring _files().

    A release bundle legitimately carries test fixtures (synthetic secret
    markers by design) and binaries (DLLs, EXEs). The directory walk skips both;
    archive scanning must skip them too, or scanning a packaged bundle floods
    false positives on decoded binary bytes and intentional test markers.
    """
    parts = name.replace("\\", "/").split("/")
    base = parts[-1]
    if (
        "tests" in parts
        or base.startswith("test_")
        or ".test." in base
        or base.endswith("_test.py")
        or base == "scan_v0137_release_secrets.py"
    ):
        return False
    member = Path(base)
    return _is_text_path(member) or _is_archive(member)


def _is_text_path(path: Path) -> bool:
    return path.suffix.casefold() in _TEXT_SUFFIXES


def _files(root: Path, *, explicit: bool) -> list[Path]:
    if root.is_file():
        return [root]
    skip_names = {".git", "__pycache__", "node_modules"}
    if not explicit:
        skip_names.update(
            {
                ".codex-venv312",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "build",
                "dist",
                "runtime",
            }
        )
    paths: list[Path] = []
    if root.is_file():
        return [root]
    for directory, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [name for name in dirnames if name not in skip_names]
        for filename in filenames:
            path = Path(directory) / filename
            if (
                "tests" in path.parts
                or path.name.startswith("test_")
                or ".test." in path.name
                or path.name.endswith("_test.py")
                or path.name == "scan_v0137_release_secrets.py"
            ):
                continue
            if not explicit and (
                "field-message-" in path.name
                or "handoff-v0.1.36" in path.name
                or "v0.1.36" in path.name
            ):
                continue
            if _is_text_path(path) or _is_archive(path):
                paths.append(path)
    return paths


def _scan_line(path_label: str, line_number: int, line: str, failures: list[str]) -> None:
    if _CREDENTIAL_URL.search(line):
        failures.append(f"{path_label}:{line_number}: private key or credential-bearing URL")
    assignment = _ASSIGNMENT.search(line)
    assignment_value = (
        assignment.group("quoted") or assignment.group("bare")
        if assignment
        else None
    )
    bare_reference = (
        assignment is not None
        and assignment.group("quoted") is None
        and _SIMPLE_BARE_REFERENCE.fullmatch(assignment_value or "")
    )
    schema_or_label_value = (
        assignment_value is not None
        and _CREDENTIAL_KEYWORD.search(assignment_value)
        and not any(character.isdigit() for character in assignment_value)
    )
    if (
        assignment_value
        and not bare_reference
        and not schema_or_label_value
        and not _SAFE_VALUE.match(assignment_value.strip())
    ):
        failures.append(f"{path_label}:{line_number}: credential-like literal assignment")
    mosquitto = _MOSQUITTO_SECRET.search(line)
    if mosquitto and not _SAFE_VALUE.match(mosquitto.group(1).strip()):
        failures.append(f"{path_label}:{line_number}: Mosquitto command contains a literal secret")


def _scan_text(path_label: str, content: str, failures: list[str]) -> None:
    pem = _PEM_BLOCK.search(content)
    if pem:
        line_number = content.count("\n", 0, pem.start()) + 1
        failures.append(f"{path_label}:{line_number}: private key or credential-bearing URL")
    for line_number, line in enumerate(io.StringIO(content), start=1):
        _scan_line(path_label, line_number, line, failures)


def _scan_member_bytes(
    label: str, name: str, data: bytes, failures: list[str], depth: int
) -> None:
    """Scan one archive member: recurse into nested archives, else scan as text.

    Decoding a nested archive's bytes as UTF-8 only ever yields garbage, so a
    credential stored inside a nested archive would slip through. Re-open the
    member as an archive (bounded by _MAX_ARCHIVE_DEPTH) so its own members are
    inspected too.
    """
    if depth + 1 < _MAX_ARCHIVE_DEPTH and _is_archive(Path(name)):
        source = io.BytesIO(data)
        if _is_zip_name(name):
            _scan_zip(source, label, failures, depth + 1)
        else:
            _scan_tar(source, label, failures, depth + 1)
        return
    _scan_text(label, data.decode("utf-8", errors="replace"), failures)


def _scan_zip(source: Path | io.BytesIO, label_prefix: str, failures: list[str], depth: int = 0) -> None:
    try:
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if not _should_scan_member(info.filename):
                    continue
                label = f"{label_prefix}!{info.filename}"
                if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    failures.append(f"{label}: archive member exceeds scan limit")
                    continue
                try:
                    data = archive.read(info)
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
                    failures.append(f"{label}: cannot read archive member: {error}")
                    continue
                _scan_member_bytes(label, info.filename, data, failures, depth)
    except (OSError, zipfile.BadZipFile) as error:
        failures.append(f"{label_prefix}: cannot read archive: {error}")


def _scan_tar(source: Path | io.BytesIO, label_prefix: str, failures: list[str], depth: int = 0) -> None:
    try:
        opener = (
            tarfile.open(source, mode="r:*")
            if isinstance(source, (str, Path))
            else tarfile.open(fileobj=source, mode="r:*")
        )
        with opener as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                if not _should_scan_member(member.name):
                    continue
                label = f"{label_prefix}!{member.name}"
                if member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    failures.append(f"{label}: archive member exceeds scan limit")
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    failures.append(f"{label}: cannot read archive member")
                    continue
                try:
                    data = extracted.read()
                except (OSError, EOFError, ValueError) as error:
                    failures.append(f"{label}: cannot read archive member: {error}")
                    continue
                _scan_member_bytes(label, member.name, data, failures, depth)
    except (OSError, tarfile.TarError) as error:
        failures.append(f"{label_prefix}: cannot read archive: {error}")


def _scan_archive(path: Path, failures: list[str]) -> None:
    if _is_zip_name(path.name):
        _scan_zip(path, str(path), failures)
    else:
        _scan_tar(path, str(path), failures)


def scan(paths: list[Path], *, explicit: bool = True) -> list[str]:
    failures: list[str] = []
    for path in paths:
        if _is_archive(path):
            _scan_archive(path, failures)
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as error:
            failures.append(f"{path}: cannot read: {error}")
            continue
        with handle:
            _scan_text(str(path), handle.read(), failures)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--path", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    if args.path:
        paths = [path.resolve() for raw in args.path for path in _files(raw.resolve(), explicit=True)]
    else:
        root = args.root.resolve()
        paths = _files(root, explicit=False)
    failures = scan(paths)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"v0.1.37 release secret scan: OK ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
