"""Backup and restore for the edge (SQLite) deployment.

A backup is a single zip *bundle* containing:

  * ``db/smart_commissioning.db`` — a CONSISTENT copy of the SQLite database,
    taken via SQLite's online backup API (never a naive mid-write file copy).
  * ``secrets/*`` — the secret store: encrypted PEMs, the ``.secret_store_key``,
    and the evidence ``.evidence_signing_key`` (so signatures stay verifiable
    after a restore).
  * ``imports/files/*`` — uploaded import source files referenced by import
    records.
  * ``manifest.json`` — bundle metadata: versions, a caller-supplied
    ``created_at`` timestamp, the SHA-256 of every member, and a detached
    Ed25519 signature over the canonical manifest body (via integrity.py).

Restore verifies the manifest signature and every member hash BEFORE writing
anything, then restores into a target runtime root, refusing to overwrite an
existing populated root unless ``force=True``.

Honesty / infra boundary:
  * The SQLite online-backup path runs fully in-process and is unit-tested.
  * Postgres (the hub) is NOT handled in-process here — see decisions. The hub
    story is ``pg_dump``; this module raises a clear error for non-SQLite URLs
    rather than pretending to back Postgres up.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from smart_commissioning_core import __version__ as core_version
from smart_commissioning_core.integrity import (
    SigningKey,
    cryptography_available,
    sha256_bytes,
    verify_bytes,
)
from sqlalchemy.engine import make_url

# Members inside the bundle.
_DB_MEMBER = "db/smart_commissioning.db"
_MANIFEST_MEMBER = "manifest.json"
_SECRETS_PREFIX = "secrets/"
_IMPORTS_PREFIX = "imports/files/"

_BUNDLE_FORMAT_VERSION = 1
# Fixed zip member timestamp so a given input yields reproducible bytes.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class BackupError(RuntimeError):
    """Backup/restore failure (unsupported backend, verification, overwrite)."""


@dataclass(frozen=True)
class BackupSources:
    """Filesystem inputs for a backup bundle (all optional except the DB URL)."""

    database_url: str
    secrets_root: Path | None = None
    imports_files_root: Path | None = None


def _sqlite_path(database_url: str) -> Path:
    """Return the on-disk SQLite file path, or raise for a non-SQLite backend."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise BackupError(
            "In-process backup supports SQLite only. For Postgres (hub) use "
            "pg_dump; see backup_service module docstring / decisions."
        )
    database = url.database
    if not database or database == ":memory:":
        raise BackupError("Cannot back up an in-memory or path-less SQLite database.")
    return Path(database)


def _consistent_sqlite_bytes(source_path: Path) -> bytes:
    """Return a consistent snapshot of the SQLite DB via the online backup API.

    The online backup copies a transactionally consistent image even while the
    source is being written (it is NOT a naive byte copy). The snapshot is taken
    into a temporary on-disk database and read back as bytes.
    """
    if not source_path.exists():
        raise BackupError(f"SQLite database not found: {source_path}")

    snapshot_path = source_path.with_suffix(source_path.suffix + ".backup-tmp")
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    try:
        return snapshot_path.read_bytes()
    finally:
        snapshot_path.unlink(missing_ok=True)


def _iter_dir_members(root: Path | None, prefix: str) -> list[tuple[str, bytes]]:
    """Return (member_name, bytes) for every regular file under ``root``.

    Member names are ``prefix`` + the path relative to ``root`` (POSIX form), so
    the bundle layout is stable and restore can map members back to files.
    """
    members: list[tuple[str, bytes]] = []
    if root is None or not root.exists():
        return members
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        members.append((prefix + relative, path.read_bytes()))
    return members


# Manifest fields populated *by* signing; excluded from the signed body so the
# canonical bytes are identical at sign time and verify time.
_SIGNATURE_FIELDS = ("signature", "public_key_pem", "public_key_fingerprint")


def _canonical_manifest_body(manifest: dict[str, object]) -> bytes:
    """Deterministic JSON of the manifest body that the signature covers."""
    body = {key: value for key, value in manifest.items() if key not in _SIGNATURE_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def create_backup_bundle(
    sources: BackupSources,
    *,
    created_at: datetime,
    signing_key: SigningKey | None = None,
) -> bytes:
    """Build and return the backup bundle zip bytes.

    ``created_at`` is caller-supplied (the CLI/endpoint passes the wall clock)
    so the timestamp is explicit and testable. ``signing_key`` signs the
    manifest; when omitted and ``cryptography`` is available a transient key is
    NOT used — pass a persisted key (see app.services.reports_integrity) so the
    signature is verifiable later. When ``cryptography`` is unavailable the
    manifest is unsigned (signature=None) and restore will reject it unless
    ``allow_unsigned`` is set.
    """
    members: list[tuple[str, bytes]] = [
        (_DB_MEMBER, _consistent_sqlite_bytes(_sqlite_path(sources.database_url))),
    ]
    members.extend(_iter_dir_members(sources.secrets_root, _SECRETS_PREFIX))
    members.extend(_iter_dir_members(sources.imports_files_root, _IMPORTS_PREFIX))

    manifest: dict[str, object] = {
        "bundle_format_version": _BUNDLE_FORMAT_VERSION,
        "core_version": core_version,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "members": {name: sha256_bytes(payload) for name, payload in members},
        "signature_algorithm": "ed25519",
        "signature": None,
        "public_key_pem": None,
        "public_key_fingerprint": None,
    }

    if signing_key is not None and cryptography_available():
        signature = signing_key.sign(_canonical_manifest_body(manifest))
        manifest["signature"] = base64.b64encode(signature).decode("ascii")
        manifest["public_key_pem"] = signing_key.public_key_pem()
        manifest["public_key_fingerprint"] = signing_key.public_key_fingerprint()

    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    out_buffer = _write_zip([*members, (_MANIFEST_MEMBER, manifest_bytes)])
    return out_buffer


def _write_zip(members: list[tuple[str, bytes]]) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, payload in sorted(members, key=lambda item: item[0]):
            info = ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


@dataclass(frozen=True)
class RestoreTarget:
    """Filesystem destinations for a restore (mirrors BackupSources)."""

    database_path: Path
    secrets_root: Path
    imports_files_root: Path


def verify_bundle(bundle_bytes: bytes, *, allow_unsigned: bool = False) -> dict[str, object]:
    """Verify the manifest signature and every member hash; return the manifest.

    Raises :class:`BackupError` on any mismatch. ``allow_unsigned`` permits a
    bundle whose manifest carries no signature (e.g. produced without crypto).
    """
    with ZipFile(_readonly_buffer(bundle_bytes)) as archive:
        names = set(archive.namelist())
        if _MANIFEST_MEMBER not in names:
            raise BackupError("Bundle is missing manifest.json.")
        manifest = json.loads(archive.read(_MANIFEST_MEMBER).decode("utf-8"))

        signature_b64 = manifest.get("signature")
        public_key_pem = manifest.get("public_key_pem")
        if signature_b64 and public_key_pem:
            if not cryptography_available():
                raise BackupError("Bundle is signed but cryptography is unavailable to verify it.")
            signature = base64.b64decode(signature_b64)
            if not verify_bytes(_canonical_manifest_body(manifest), signature, public_key_pem):
                raise BackupError("Manifest signature is invalid (bundle tampered or wrong key).")
        elif not allow_unsigned:
            raise BackupError("Bundle manifest is unsigned; pass allow_unsigned to restore it.")

        declared = manifest.get("members")
        if not isinstance(declared, dict):
            raise BackupError("Manifest has no members map.")
        for name, expected_hash in declared.items():
            if name not in names:
                raise BackupError(f"Bundle is missing declared member: {name}")
            if sha256_bytes(archive.read(name)) != expected_hash:
                raise BackupError(f"Member hash mismatch (tampered): {name}")
    return manifest


def restore_backup_bundle(
    bundle_bytes: bytes,
    target: RestoreTarget,
    *,
    force: bool = False,
    allow_unsigned: bool = False,
) -> dict[str, object]:
    """Verify then restore a bundle into ``target``; return the manifest.

    Refuses to overwrite an existing populated database file unless
    ``force=True``. Verification (signature + member hashes) runs first, so a
    tampered bundle never writes a single byte.
    """
    manifest = verify_bundle(bundle_bytes, allow_unsigned=allow_unsigned)

    if target.database_path.exists() and target.database_path.stat().st_size > 0 and not force:
        raise BackupError(
            f"Refusing to overwrite existing database at {target.database_path}; pass force=True."
        )

    target.database_path.parent.mkdir(parents=True, exist_ok=True)
    target.secrets_root.mkdir(parents=True, exist_ok=True)
    target.imports_files_root.mkdir(parents=True, exist_ok=True)

    with ZipFile(_readonly_buffer(bundle_bytes)) as archive:
        # Map (and path-traversal check) every member BEFORE writing any byte, so
        # a single escaping member name aborts the whole restore without leaving
        # a partial write on disk.
        planned: list[tuple[Path, bytes]] = []
        for name in sorted(archive.namelist()):
            if name == _MANIFEST_MEMBER:
                continue
            destination = _member_destination(name, target)
            if destination is None:
                continue
            planned.append((destination, archive.read(name)))

        for destination, payload in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    return manifest


def _ensure_within(destination: Path, root: Path, name: str) -> Path:
    """Resolve ``destination`` and assert it stays within ``root``.

    Defense-in-depth against zip path traversal: a crafted member name (e.g.
    ``secrets/../../etc/passwd``) must never resolve to a path outside its
    intended restore root, even when the manifest is unsigned. Raises
    :class:`BackupError` (so nothing is written) when it escapes.
    """
    resolved_root = root.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination != resolved_root and not resolved_destination.is_relative_to(
        resolved_root
    ):
        raise BackupError(f"Bundle member escapes its restore root (path traversal): {name}")
    return destination


def _member_destination(name: str, target: RestoreTarget) -> Path | None:
    """Map a bundle member name back to its restore destination path.

    Each mapped path is resolved and checked to stay within its intended root
    (the DB target dir, the secrets root, or the imports-files root) so a member
    name containing ``../`` cannot write outside the restore target.
    """
    if name == _DB_MEMBER:
        return _ensure_within(target.database_path, target.database_path.parent, name)
    if name.startswith(_SECRETS_PREFIX):
        destination = target.secrets_root / name[len(_SECRETS_PREFIX):]
        return _ensure_within(destination, target.secrets_root, name)
    if name.startswith(_IMPORTS_PREFIX):
        destination = target.imports_files_root / name[len(_IMPORTS_PREFIX):]
        return _ensure_within(destination, target.imports_files_root, name)
    return None


def _readonly_buffer(data: bytes):  # noqa: ANN202 - tiny BytesIO factory
    from io import BytesIO

    return BytesIO(data)
