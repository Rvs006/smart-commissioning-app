"""Run-owned, write-once storage for protected raw evidence.

Only this module knows filesystem locations. Public callers receive opaque
artifact identifiers and verified metadata; provider adapters receive private
paths only for the lifetime of a local process invocation.
"""

from __future__ import annotations

import getpass
import hashlib
import ipaddress
import os
import re
import stat
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from smart_commissioning_core.db.engine import query_session_factory, session_factory
from smart_commissioning_core.db.models import (
    RawEvidenceArtifact,
    RawEvidenceDownloadAudit,
    Run,
)
from smart_commissioning_core.engines.ip.nmap_runner import NmapRawXmlArtifactV1
from sqlalchemy import and_, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError as SqlIntegrityError

from app.core.runtime import ARTIFACTS_ROOT

RAW_EVIDENCE_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
RAW_EVIDENCE_TARGET_LIST_MAX_BYTES = 256 * 1024

_ARTIFACT_ID_RE = re.compile(r"^artifact_[0-9a-f]{32}$")
_ARTIFACT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+$")
_EXECUTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]{0,254}$")
_AUDIT_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]{0,254}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DELETE_QUARANTINE_RE = re.compile(
    r"^\.delete-(artifact_[0-9a-f]{32})-[0-9a-f]{32}\.tmp$"
)


class RawEvidenceError(RuntimeError):
    """Base class for sanitized protected-evidence failures."""


class RawEvidenceNotFoundError(RawEvidenceError):
    """The requested artifact is absent from the owning run."""


class RawEvidenceIntegrityError(RawEvidenceError):
    """Stored bytes or metadata no longer match their immutable record."""


class RawEvidenceSecurityError(RawEvidenceError):
    """A path, ACL, or reparse check failed closed."""


class RawEvidenceLimitError(RawEvidenceError):
    """A bounded evidence spool exceeded its approved byte ceiling."""


class RawEvidenceIncompleteError(RawEvidenceError):
    """Partial evidence was presented as authority for normalized claims."""


class RawEvidenceConflictError(RawEvidenceError):
    """An immutable artifact identity conflicts with existing evidence."""


@dataclass(frozen=True)
class RawEvidenceArtifactDescriptor:
    artifact_id: str
    run_id: str
    project_id: str
    site_id: str
    artifact_type: str
    media_type: str
    sha256: str
    size_bytes: int
    capture_complete: bool
    producer_executor_id: str
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True)
class RawEvidenceBackupRecord:
    descriptor: RawEvidenceArtifactDescriptor
    payload: bytes


@dataclass(frozen=True)
class RawEvidenceReconciliation:
    orphan_storage_keys: tuple[str, ...]
    missing_artifact_ids: tuple[str, ...]
    tampered_artifact_ids: tuple[str, ...]
    staged_temp_keys: tuple[str, ...]
    recovered_delete_artifact_ids: tuple[str, ...]
    deleted_orphan_count: int


class RawEvidenceFilesystemSecurity(Protocol):
    """Injected owner-only ACL and reparse seam for platform verification."""

    def secure_directory(self, path: Path) -> None: ...

    def secure_file(self, path: Path) -> None: ...

    def is_reparse_point(self, path: Path) -> bool: ...


class OwnerOnlyFilesystemSecurity:
    """Apply and verify owner-only permissions on POSIX or Windows."""

    def secure_directory(self, path: Path) -> None:
        if os.name == "nt":
            self._secure_windows(path, directory=True)
            return
        try:
            os.chmod(path, stat.S_IRWXU)
            if stat.S_IMODE(path.stat().st_mode) != stat.S_IRWXU:
                raise OSError("directory mode was not owner-only")
        except OSError as error:
            raise RawEvidenceSecurityError(
                "Raw evidence directory permissions could not be restricted."
            ) from error

    def secure_file(self, path: Path) -> None:
        if os.name == "nt":
            self._secure_windows(path, directory=False)
            return
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            if stat.S_IMODE(path.stat().st_mode) != stat.S_IRUSR | stat.S_IWUSR:
                raise OSError("file mode was not owner-only")
        except OSError as error:
            raise RawEvidenceSecurityError(
                "Raw evidence file permissions could not be restricted."
            ) from error

    def is_reparse_point(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return True
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)

    @staticmethod
    def _secure_windows(path: Path, *, directory: bool) -> None:
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not system_root:
            raise RawEvidenceSecurityError(
                "Windows owner-only ACL tooling is unavailable."
            )
        executable = Path(system_root) / "System32" / "icacls.exe"
        username = os.environ.get("USERNAME") or getpass.getuser()
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}\\{username}" if domain else username
        grant = f"{principal}:(OI)(CI)F" if directory else f"{principal}:F"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        commands = (
            [str(executable), str(path), "/reset"],
            [str(executable), str(path), "/inheritance:r", "/grant:r", grant],
        )
        try:
            for command in commands:
                subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=10,
                    creationflags=creation_flags,
                )
        except (OSError, subprocess.SubprocessError) as error:
            raise RawEvidenceSecurityError(
                "Raw evidence ACLs could not be restricted to the runtime owner."
            ) from error


class RawEvidenceSpool:
    """One bounded temporary capture that publishes exactly once on finalize."""

    def __init__(
        self,
        store: RawEvidenceArtifactStore,
        *,
        temporary_path: Path,
        handle: object,
        run_id: str,
        project_id: str,
        site_id: str,
        artifact_type: str,
        media_type: str,
        producer_executor_id: str,
        max_bytes: int,
        created_at: datetime,
    ) -> None:
        self._store = store
        self._temporary_path = temporary_path
        self._handle = handle
        self._run_id = run_id
        self._project_id = project_id
        self._site_id = site_id
        self._artifact_type = artifact_type
        self._media_type = media_type
        self._producer_executor_id = producer_executor_id
        self._max_bytes = max_bytes
        self._created_at = created_at
        self._digest = hashlib.sha256()
        self._size_bytes = 0
        self._overflowed = False
        self._finalized: NmapRawXmlArtifactV1 | None = None
        self._lock = threading.Lock()

    def write(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("Raw evidence spool writes require bytes.")
        with self._lock:
            if self._finalized is not None or self._handle is None:
                raise RawEvidenceConflictError("Raw evidence spool is already finalized.")
            if self._size_bytes + len(payload) > self._max_bytes:
                self._overflowed = True
                raise RawEvidenceLimitError("Raw evidence exceeded its approved byte limit.")
            if not payload:
                return
            self._handle.write(payload)
            self._digest.update(payload)
            self._size_bytes += len(payload)

    def finalize(self, *, capture_complete: bool) -> NmapRawXmlArtifactV1:
        with self._lock:
            if type(capture_complete) is not bool:
                raise TypeError("capture_complete must be a boolean")
            if self._finalized is not None:
                return self._finalized
            if self._handle is None:
                raise RawEvidenceConflictError("Raw evidence spool cannot be finalized.")
            handle = self._handle
            self._handle = None
            try:
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            descriptor = self._store._seal_spool(
                temporary_path=self._temporary_path,
                run_id=self._run_id,
                project_id=self._project_id,
                site_id=self._site_id,
                artifact_type=self._artifact_type,
                media_type=self._media_type,
                producer_executor_id=self._producer_executor_id,
                sha256=self._digest.hexdigest(),
                size_bytes=self._size_bytes,
                capture_complete=capture_complete and not self._overflowed,
                created_at=self._created_at,
            )
            self._finalized = NmapRawXmlArtifactV1(
                artifact_id=descriptor.artifact_id,
                sha256=descriptor.sha256,
                size_bytes=descriptor.size_bytes,
                capture_complete=descriptor.capture_complete,
            )
            return self._finalized

    def abort(self) -> None:
        """Close and remove an unpublished spool."""

        with self._lock:
            if self._finalized is not None:
                return
            handle = self._handle
            self._handle = None
            if handle is not None:
                handle.close()
            self._store._remove_staged_file(self._temporary_path)


class RawEvidenceArtifactStore:
    """Database-bound protected artifact store with no public path surface."""

    def __init__(
        self,
        engine: Engine,
        *,
        root: Path | None = None,
        security: RawEvidenceFilesystemSecurity | None = None,
    ) -> None:
        self.engine = engine
        self.root = Path(root or (ARTIFACTS_ROOT / "raw-evidence")).absolute()
        self.security = security or OwnerOnlyFilesystemSecurity()
        self._sessions = session_factory(engine)
        self._query_sessions = query_session_factory(engine)

    def create_spool(
        self,
        *,
        run_id: str,
        artifact_type: str,
        media_type: str,
        producer_executor_id: str,
        max_bytes: int,
        created_at: datetime | None = None,
    ) -> RawEvidenceSpool:
        """Create one owner-only bounded spool for an existing run."""

        self._validate_metadata(
            run_id=run_id,
            artifact_type=artifact_type,
            media_type=media_type,
            producer_executor_id=producer_executor_id,
            max_bytes=max_bytes,
        )
        captured_at = _aware_utc(created_at or datetime.now(UTC), field="created_at")
        project_id, site_id = self._load_run_scope(run_id)
        _, staging_root, _ = self._ensure_roots()
        temporary_path = staging_root / f".spool-{uuid4().hex}.tmp"
        self._assert_contained(temporary_path, require_exists=False)
        descriptor = os.open(
            temporary_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        handle = os.fdopen(descriptor, "wb", buffering=0)
        try:
            self.security.secure_file(temporary_path)
            self._assert_contained(temporary_path, require_exists=True)
        except Exception:
            handle.close()
            temporary_path.unlink(missing_ok=True)
            raise
        return RawEvidenceSpool(
            self,
            temporary_path=temporary_path,
            handle=handle,
            run_id=run_id,
            project_id=project_id,
            site_id=site_id,
            artifact_type=artifact_type,
            media_type=media_type,
            producer_executor_id=producer_executor_id,
            max_bytes=max_bytes,
            created_at=captured_at,
        )

    def import_bytes(
        self,
        *,
        run_id: str,
        artifact_type: str,
        media_type: str,
        payload: bytes,
        capture_complete: bool,
        producer_executor_id: str,
        max_bytes: int = RAW_EVIDENCE_DEFAULT_MAX_BYTES,
        created_at: datetime | None = None,
    ) -> RawEvidenceArtifactDescriptor:
        """Persist exact bounded bytes through the same spool used by providers."""

        if not isinstance(payload, bytes):
            raise TypeError("Raw evidence imports require bytes.")
        spool = self.create_spool(
            run_id=run_id,
            artifact_type=artifact_type,
            media_type=media_type,
            producer_executor_id=producer_executor_id,
            max_bytes=max_bytes,
            created_at=created_at,
        )
        try:
            spool.write(payload)
            artifact = spool.finalize(capture_complete=capture_complete)
        except Exception:
            spool.abort()
            raise
        return self.get_descriptor(run_id=run_id, artifact_id=artifact.artifact_id)

    def get_descriptor(
        self,
        *,
        run_id: str,
        artifact_id: str,
    ) -> RawEvidenceArtifactDescriptor:
        row = self._load_row(run_id=run_id, artifact_id=artifact_id)
        return _descriptor(row)

    def read_verified(
        self,
        *,
        run_id: str,
        artifact_id: str,
    ) -> tuple[RawEvidenceArtifactDescriptor, bytes]:
        """Read exact bytes after scope, containment, size, and digest checks."""

        row = self._load_row(run_id=run_id, artifact_id=artifact_id)
        descriptor = _descriptor(row)
        target = self._path_from_storage_key(row.storage_relpath)
        payload = self._read_exact_file(
            target,
            expected_size=descriptor.size_bytes,
            expected_sha256=descriptor.sha256,
        )
        return descriptor, payload

    def read_for_normalization(
        self,
        *,
        run_id: str,
        artifact_id: str,
    ) -> tuple[RawEvidenceArtifactDescriptor, bytes]:
        """Return verified bytes only when the capture may support claims."""

        descriptor, payload = self.read_verified(run_id=run_id, artifact_id=artifact_id)
        if not descriptor.capture_complete:
            raise RawEvidenceIncompleteError(
                "Incomplete raw evidence cannot authorize normalized claims."
            )
        return descriptor, payload

    def read_for_download(
        self,
        *,
        run_id: str,
        artifact_id: str,
        audit_actor: str,
    ) -> tuple[RawEvidenceArtifactDescriptor, bytes]:
        """Verify a scoped download and durably audit it before serving bytes."""

        if not _AUDIT_ACTOR_RE.fullmatch(audit_actor):
            raise ValueError("audit_actor must be a stable server-derived identity")
        descriptor, payload = self.read_verified(run_id=run_id, artifact_id=artifact_id)
        with self._sessions.begin() as session:
            session.add(
                RawEvidenceDownloadAudit(
                    audit_id=f"rawdl_{uuid4().hex}",
                    artifact_id=descriptor.artifact_id,
                    run_id=descriptor.run_id,
                    project_id=descriptor.project_id,
                    site_id=descriptor.site_id,
                    artifact_sha256=descriptor.sha256,
                    size_bytes=descriptor.size_bytes,
                    downloaded_by=audit_actor,
                    downloaded_at=datetime.now(UTC),
                )
            )
        return descriptor, payload

    def iter_backup_records(self) -> Iterator[RawEvidenceBackupRecord]:
        """Stream verified, path-free records for whole-runtime backup."""

        with self._query_sessions() as session:
            identities = tuple(
                session.execute(
                    select(RawEvidenceArtifact.run_id, RawEvidenceArtifact.artifact_id)
                    .order_by(RawEvidenceArtifact.run_id, RawEvidenceArtifact.artifact_id)
                ).all()
            )
        for run_id, artifact_id in identities:
            descriptor, payload = self.read_verified(
                run_id=run_id,
                artifact_id=artifact_id,
            )
            yield RawEvidenceBackupRecord(descriptor=descriptor, payload=payload)

    def restore_backup_record(
        self,
        record: RawEvidenceBackupRecord,
    ) -> RawEvidenceArtifactDescriptor:
        """Restore one exact record and reapply owner-only ACLs before publish."""

        descriptor = record.descriptor
        self._validate_backup_record(record)
        project_id, site_id = self._load_run_scope(descriptor.run_id)
        if (project_id, site_id) != (descriptor.project_id, descriptor.site_id):
            raise RawEvidenceIntegrityError(
                "Restored raw evidence does not match the owning run scope."
            )
        try:
            existing = self.get_descriptor(
                run_id=descriptor.run_id,
                artifact_id=descriptor.artifact_id,
            )
        except RawEvidenceNotFoundError:
            existing = None
        if existing is not None:
            if existing != descriptor:
                raise RawEvidenceConflictError(
                    "Restored raw evidence conflicts with an existing artifact."
                )
            self.verify_restored_artifact(record)
            return existing

        temporary_path = self._stage_exact_bytes(record.payload)
        try:
            restored = self._seal_spool(
                temporary_path=temporary_path,
                run_id=descriptor.run_id,
                project_id=descriptor.project_id,
                site_id=descriptor.site_id,
                artifact_type=descriptor.artifact_type,
                media_type=descriptor.media_type,
                producer_executor_id=descriptor.producer_executor_id,
                sha256=descriptor.sha256,
                size_bytes=descriptor.size_bytes,
                capture_complete=descriptor.capture_complete,
                created_at=descriptor.created_at,
                artifact_id=descriptor.artifact_id,
                sealed_at=descriptor.sealed_at,
            )
        except Exception:
            self._remove_staged_file(temporary_path)
            raise
        if restored != descriptor:
            raise RawEvidenceIntegrityError(
                "Restored raw evidence metadata changed during publication."
            )
        return self.verify_restored_artifact(record)

    def verify_restored_artifact(
        self,
        record: RawEvidenceBackupRecord,
    ) -> RawEvidenceArtifactDescriptor:
        self._validate_backup_record(record)
        descriptor, payload = self.read_verified(
            run_id=record.descriptor.run_id,
            artifact_id=record.descriptor.artifact_id,
        )
        if descriptor != record.descriptor or payload != record.payload:
            raise RawEvidenceIntegrityError(
                "Restored raw evidence does not match its backup record."
            )
        return descriptor

    def verify_restored_store(self) -> tuple[RawEvidenceArtifactDescriptor, ...]:
        """Reapply ACLs and verify every restored database/file binding."""

        reconciliation = self.reconcile_orphans()
        if (
            reconciliation.orphan_storage_keys
            or reconciliation.missing_artifact_ids
            or reconciliation.tampered_artifact_ids
            or reconciliation.staged_temp_keys
        ):
            raise RawEvidenceIntegrityError(
                "Restored raw evidence contains unverified lifecycle state."
            )
        with self._query_sessions() as session:
            identities = tuple(
                session.execute(
                    select(RawEvidenceArtifact.run_id, RawEvidenceArtifact.artifact_id)
                    .order_by(RawEvidenceArtifact.run_id, RawEvidenceArtifact.artifact_id)
                ).all()
            )
        verified: list[RawEvidenceArtifactDescriptor] = []
        for run_id, artifact_id in identities:
            row = self._load_row(run_id=run_id, artifact_id=artifact_id)
            target = self._path_from_storage_key(row.storage_relpath)
            self.security.secure_directory(target.parent)
            self.security.secure_file(target)
            descriptor, _ = self.read_verified(
                run_id=run_id,
                artifact_id=artifact_id,
            )
            verified.append(descriptor)
        return tuple(verified)

    def delete_verified(self, *, run_id: str, artifact_id: str) -> bool:
        """Verify, quarantine, then coordinate row and file deletion."""

        try:
            row = self._load_row(run_id=run_id, artifact_id=artifact_id)
        except RawEvidenceNotFoundError:
            return False
        descriptor = _descriptor(row)
        target = self._path_from_storage_key(row.storage_relpath)
        self._read_exact_file(
            target,
            expected_size=descriptor.size_bytes,
            expected_sha256=descriptor.sha256,
        )
        _, staging_root, _ = self._ensure_roots()
        quarantine = staging_root / f".delete-{descriptor.artifact_id}-{uuid4().hex}.tmp"
        self._assert_contained(quarantine, require_exists=False)
        os.replace(target, quarantine)
        self.security.secure_file(quarantine)
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    delete(RawEvidenceArtifact).where(
                        RawEvidenceArtifact.artifact_id == descriptor.artifact_id,
                        RawEvidenceArtifact.run_id == descriptor.run_id,
                        RawEvidenceArtifact.sha256 == descriptor.sha256,
                    )
                )
                if result.rowcount != 1:
                    raise RawEvidenceConflictError(
                        "Raw evidence changed during coordinated deletion."
                    )
        except Exception:
            os.replace(quarantine, target)
            self.security.secure_file(target)
            raise
        try:
            quarantine.unlink()
        except OSError as error:
            raise RawEvidenceConflictError(
                "Raw evidence deletion requires reconciliation."
            ) from error
        return True

    def reconcile_orphans(self, *, delete_orphans: bool = False) -> RawEvidenceReconciliation:
        """Compare protected files with database authority without trusting paths."""

        objects_root, staging_root, _ = self._ensure_roots()
        with self._query_sessions() as session:
            rows = tuple(
                session.execute(select(RawEvidenceArtifact)).scalars().all()
            )
        expected = {row.storage_relpath: row for row in rows}
        expected_by_id = {row.artifact_id: row for row in rows}
        present: set[str] = set()
        orphan_keys: list[str] = []
        missing_ids: list[str] = []
        tampered_ids: list[str] = []
        recovered_ids: list[str] = []
        delete_quarantines = sorted(staging_root.glob(".delete-*.tmp"))
        for quarantine in delete_quarantines:
            self._assert_contained(quarantine, require_exists=True)
            match = _DELETE_QUARANTINE_RE.fullmatch(quarantine.name)
            row = expected_by_id.get(match.group(1)) if match is not None else None
            if row is None:
                orphan_keys.append(quarantine.relative_to(self.root).as_posix())
                continue
            target = self._path_from_storage_key(row.storage_relpath, require_exists=False)
            if target.exists():
                orphan_keys.append(quarantine.relative_to(self.root).as_posix())
                continue
            try:
                self._read_exact_file(
                    quarantine,
                    expected_size=row.size_bytes,
                    expected_sha256=row.sha256,
                )
                os.replace(quarantine, target)
                self.security.secure_file(target)
                self._read_exact_file(
                    target,
                    expected_size=row.size_bytes,
                    expected_sha256=row.sha256,
                )
                recovered_ids.append(row.artifact_id)
            except RawEvidenceError:
                tampered_ids.append(row.artifact_id)
        for target in sorted(objects_root.rglob("*.bin")):
            self._assert_contained(target, require_exists=True)
            key = target.relative_to(self.root).as_posix()
            present.add(key)
            if key not in expected:
                orphan_keys.append(key)
        for key, row in expected.items():
            if key not in present:
                missing_ids.append(row.artifact_id)
                continue
            try:
                self._read_exact_file(
                    self._path_from_storage_key(key),
                    expected_size=row.size_bytes,
                    expected_sha256=row.sha256,
                )
            except RawEvidenceIntegrityError:
                tampered_ids.append(row.artifact_id)
        staged = tuple(
            path.relative_to(self.root).as_posix()
            for path in sorted(staging_root.glob(".spool-*.tmp"))
        )
        deleted = 0
        if delete_orphans:
            for key in orphan_keys:
                target = self._path_from_internal_key(key)
                self._assert_contained(target, require_exists=True)
                target.unlink()
                deleted += 1
        return RawEvidenceReconciliation(
            orphan_storage_keys=tuple(sorted(orphan_keys)),
            missing_artifact_ids=tuple(sorted(missing_ids)),
            tampered_artifact_ids=tuple(sorted(set(tampered_ids))),
            staged_temp_keys=staged,
            recovered_delete_artifact_ids=tuple(sorted(recovered_ids)),
            deleted_orphan_count=deleted,
        )

    def for_nmap_run(
        self,
        *,
        run_id: str,
        producer_executor_id: str,
    ) -> NmapRunPrivateArtifactsAdapter:
        """Bind this store to the current NmapPrivateArtifacts protocol."""

        return NmapRunPrivateArtifactsAdapter(
            self,
            run_id=run_id,
            producer_executor_id=producer_executor_id,
        )

    def _seal_spool(
        self,
        *,
        temporary_path: Path,
        run_id: str,
        project_id: str,
        site_id: str,
        artifact_type: str,
        media_type: str,
        producer_executor_id: str,
        sha256: str,
        size_bytes: int,
        capture_complete: bool,
        created_at: datetime,
        artifact_id: str | None = None,
        sealed_at: datetime | None = None,
    ) -> RawEvidenceArtifactDescriptor:
        artifact_identity = artifact_id or f"artifact_{uuid4().hex}"
        sealed_timestamp = _aware_utc(sealed_at or datetime.now(UTC), field="sealed_at")
        if not _ARTIFACT_ID_RE.fullmatch(artifact_identity):
            raise ValueError("artifact_id is not an opaque raw evidence identity")
        if sealed_timestamp < created_at:
            raise ValueError("sealed_at must not precede created_at")
        if not _SHA256_RE.fullmatch(sha256) or size_bytes < 0:
            raise RawEvidenceIntegrityError("Raw evidence spool metadata is invalid.")
        self._assert_contained(temporary_path, require_exists=True)
        if temporary_path.stat().st_size != size_bytes or _sha256_file(temporary_path) != sha256:
            self._remove_staged_file(temporary_path)
            raise RawEvidenceIntegrityError(
                "Raw evidence spool bytes changed before publication."
            )
        objects_root, _, _ = self._ensure_roots()
        parent = objects_root / sha256[:2]
        parent.mkdir(parents=True, exist_ok=True)
        self.security.secure_directory(parent)
        self._assert_contained(parent, require_exists=True)
        storage_key = PurePosixPath(
            "objects", sha256[:2], f"{artifact_identity}-{sha256}.bin"
        ).as_posix()
        target = self._path_from_storage_key(storage_key, require_exists=False)
        if target.exists():
            self._remove_staged_file(temporary_path)
            raise RawEvidenceConflictError(
                "Raw evidence identity already has published bytes."
            )
        os.replace(temporary_path, target)
        try:
            self.security.secure_file(target)
            self._read_exact_file(
                target,
                expected_size=size_bytes,
                expected_sha256=sha256,
            )
            with self._sessions.begin() as session:
                run = session.execute(select(Run).where(Run.id == run_id)).scalar_one_or_none()
                if run is None or (run.project_id, run.site_id) != (project_id, site_id):
                    raise RawEvidenceNotFoundError(
                        "Owning run was unavailable while evidence was sealed."
                    )
                session.add(
                    RawEvidenceArtifact(
                        artifact_id=artifact_identity,
                        run_id=run_id,
                        project_id=project_id,
                        site_id=site_id,
                        artifact_type=artifact_type,
                        media_type=media_type,
                        storage_relpath=storage_key,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        capture_complete=capture_complete,
                        producer_executor_id=producer_executor_id,
                        created_at=created_at,
                        sealed_at=sealed_timestamp,
                    )
                )
        except Exception as error:
            target.unlink(missing_ok=True)
            if isinstance(error, RawEvidenceError):
                raise
            if isinstance(error, SqlIntegrityError):
                raise RawEvidenceConflictError(
                    "Raw evidence metadata conflicts with existing evidence."
                ) from error
            raise
        return RawEvidenceArtifactDescriptor(
            artifact_id=artifact_identity,
            run_id=run_id,
            project_id=project_id,
            site_id=site_id,
            artifact_type=artifact_type,
            media_type=media_type,
            sha256=sha256,
            size_bytes=size_bytes,
            capture_complete=capture_complete,
            producer_executor_id=producer_executor_id,
            created_at=created_at,
            sealed_at=sealed_timestamp,
        )

    def _ensure_roots(self) -> tuple[Path, Path, Path]:
        if self.security.is_reparse_point(self.root):
            raise RawEvidenceSecurityError(
                "Raw evidence storage root cannot be a reparse point."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self.security.secure_directory(self.root)
        self._assert_contained(self.root, require_exists=True, allow_root=True)
        objects = self.root / "objects"
        staging = self.root / "staging"
        private = self.root / "private"
        for path in (objects, staging, private):
            path.mkdir(parents=True, exist_ok=True)
            self.security.secure_directory(path)
            self._assert_contained(path, require_exists=True)
        return objects, staging, private

    def _assert_contained(
        self,
        path: Path,
        *,
        require_exists: bool,
        allow_root: bool = False,
    ) -> None:
        candidate = Path(path).absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise RawEvidenceSecurityError(
                "Raw evidence path escaped its runtime-owned root."
            ) from error
        if not allow_root and not relative.parts:
            raise RawEvidenceSecurityError("Raw evidence path cannot be the storage root.")
        paths = (
            self.root,
            *(
                self.root / Path(*relative.parts[:index])
                for index in range(1, len(relative.parts) + 1)
            ),
        )
        for current in paths:
            if self.security.is_reparse_point(current):
                raise RawEvidenceSecurityError(
                    "Raw evidence path contains a reparse point."
                )
        if require_exists and not candidate.exists():
            raise RawEvidenceIntegrityError("Raw evidence bytes are missing.")
        existing = candidate if candidate.exists() else candidate.parent
        try:
            canonical_root = self.root.resolve(strict=True)
            canonical_existing = existing.resolve(strict=True)
            canonical_existing.relative_to(canonical_root)
        except (FileNotFoundError, ValueError) as error:
            raise RawEvidenceSecurityError(
                "Raw evidence path failed canonical containment verification."
            ) from error
        if candidate.exists() and candidate.resolve(strict=True) != canonical_existing:
            raise RawEvidenceSecurityError(
                "Raw evidence path failed canonical identity verification."
            )

    def _path_from_storage_key(
        self,
        storage_key: str,
        *,
        require_exists: bool = True,
    ) -> Path:
        relative = _validated_internal_key(storage_key, prefix="objects")
        target = self.root.joinpath(*relative.parts)
        self._assert_contained(target, require_exists=require_exists)
        return target

    def _path_from_internal_key(self, storage_key: str) -> Path:
        relative = _validated_internal_key(storage_key, prefix=None)
        if relative.parts[0] not in {"objects", "staging"}:
            raise RawEvidenceSecurityError("Raw evidence reconciliation key is invalid.")
        target = self.root.joinpath(*relative.parts)
        self._assert_contained(target, require_exists=True)
        return target

    def _read_exact_file(
        self,
        target: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes:
        self._assert_contained(target, require_exists=True)
        if not target.is_file():
            raise RawEvidenceIntegrityError("Raw evidence bytes are unavailable.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
            try:
                if os.fstat(descriptor).st_size != expected_size:
                    raise RawEvidenceIntegrityError(
                        "Raw evidence size does not match its immutable record."
                    )
                chunks: list[bytes] = []
                remaining = expected_size + 1
                while remaining > 0:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(descriptor)
        except RawEvidenceError:
            raise
        except OSError as error:
            raise RawEvidenceIntegrityError("Raw evidence bytes could not be read.") from error
        payload = b"".join(chunks)
        if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise RawEvidenceIntegrityError(
                "Raw evidence digest does not match its immutable record."
            )
        self._assert_contained(target, require_exists=True)
        return payload

    def _load_row(self, *, run_id: str, artifact_id: str) -> RawEvidenceArtifact:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise RawEvidenceNotFoundError("Raw evidence artifact was not found.")
        with self._query_sessions() as session:
            row = session.execute(
                select(RawEvidenceArtifact)
                .join(
                    Run,
                    and_(
                        Run.id == RawEvidenceArtifact.run_id,
                        Run.project_id == RawEvidenceArtifact.project_id,
                        Run.site_id == RawEvidenceArtifact.site_id,
                    ),
                )
                .where(
                    RawEvidenceArtifact.artifact_id == artifact_id,
                    RawEvidenceArtifact.run_id == run_id,
                )
            ).scalar_one_or_none()
        if row is None:
            raise RawEvidenceNotFoundError("Raw evidence artifact was not found.")
        return row

    def _load_run_scope(self, run_id: str) -> tuple[str, str]:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 64:
            raise RawEvidenceNotFoundError("Owning run was not found.")
        with self._query_sessions() as session:
            row = session.execute(
                select(Run.project_id, Run.site_id).where(Run.id == run_id)
            ).one_or_none()
        if row is None:
            raise RawEvidenceNotFoundError("Owning run was not found.")
        return row.project_id, row.site_id

    @staticmethod
    def _validate_metadata(
        *,
        run_id: str,
        artifact_type: str,
        media_type: str,
        producer_executor_id: str,
        max_bytes: int,
    ) -> None:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 64:
            raise ValueError("run_id must contain 1 through 64 characters")
        if not _ARTIFACT_TYPE_RE.fullmatch(artifact_type):
            raise ValueError("artifact_type is invalid")
        if len(media_type) > 255 or not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise ValueError("media_type is invalid")
        if not _EXECUTOR_ID_RE.fullmatch(producer_executor_id):
            raise ValueError("producer_executor_id is invalid")
        if type(max_bytes) is not int or not 0 < max_bytes <= RAW_EVIDENCE_DEFAULT_MAX_BYTES:
            raise ValueError("max_bytes is outside the protected evidence ceiling")

    def _validate_backup_record(self, record: RawEvidenceBackupRecord) -> None:
        descriptor = record.descriptor
        if not isinstance(record.payload, bytes):
            raise TypeError("Raw evidence backup payload must be bytes.")
        if type(descriptor.capture_complete) is not bool:
            raise RawEvidenceIntegrityError(
                "Raw evidence backup completeness is invalid."
            )
        self._validate_metadata(
            run_id=descriptor.run_id,
            artifact_type=descriptor.artifact_type,
            media_type=descriptor.media_type,
            producer_executor_id=descriptor.producer_executor_id,
            max_bytes=max(1, descriptor.size_bytes),
        )
        if not _ARTIFACT_ID_RE.fullmatch(descriptor.artifact_id):
            raise RawEvidenceIntegrityError("Raw evidence backup identity is invalid.")
        if (
            len(record.payload) != descriptor.size_bytes
            or hashlib.sha256(record.payload).hexdigest() != descriptor.sha256
        ):
            raise RawEvidenceIntegrityError(
                "Raw evidence backup bytes do not match their record."
            )
        _aware_utc(descriptor.created_at, field="created_at")
        _aware_utc(descriptor.sealed_at, field="sealed_at")
        if descriptor.sealed_at < descriptor.created_at:
            raise RawEvidenceIntegrityError("Raw evidence backup timestamps are invalid.")

    def _remove_staged_file(self, path: Path) -> None:
        try:
            self._assert_contained(path, require_exists=True)
        except RawEvidenceIntegrityError:
            return
        path.unlink(missing_ok=True)

    def _stage_exact_bytes(self, payload: bytes) -> Path:
        _, staging_root, _ = self._ensure_roots()
        temporary_path = staging_root / f".spool-{uuid4().hex}.tmp"
        self._assert_contained(temporary_path, require_exists=False)
        descriptor = os.open(
            temporary_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(descriptor, "wb", buffering=0) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.security.secure_file(temporary_path)
            self._assert_contained(temporary_path, require_exists=True)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return temporary_path


class NmapRunPrivateArtifactsAdapter:
    """Run-bound adapter implementing NmapPrivateArtifacts without path egress."""

    def __init__(
        self,
        store: RawEvidenceArtifactStore,
        *,
        run_id: str,
        producer_executor_id: str,
    ) -> None:
        store._validate_metadata(
            run_id=run_id,
            artifact_type="nmap_xml",
            media_type="application/xml",
            producer_executor_id=producer_executor_id,
            max_bytes=1,
        )
        store._load_run_scope(run_id)
        self.store = store
        self.run_id = run_id
        self.producer_executor_id = producer_executor_id
        run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
        self._private_run_key = run_key
        self._private_run_root: Path | None = None
        self._private_directory: Path | None = None
        self._target_lists: set[Path] = set()

    def create_owner_only_target_list(self, payload: bytes) -> str:
        _validate_literal_ipv4_target_list(payload)
        private_directory = self._ensure_private_directory()
        target = private_directory / f"targets-{uuid4().hex}.txt"
        self.store._assert_contained(target, require_exists=False)
        descriptor = os.open(
            target,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(descriptor, "wb", buffering=0) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self.store.security.secure_file(target)
            self.store._assert_contained(target, require_exists=True)
            if target.read_bytes() != payload:
                raise RawEvidenceIntegrityError(
                    "Private Nmap target bytes changed before launch."
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        self._target_lists.add(target)
        return str(target)

    def create_owner_only_xml_spool(self, *, max_bytes: int) -> RawEvidenceSpool:
        return self.store.create_spool(
            run_id=self.run_id,
            artifact_type="nmap_xml",
            media_type="application/xml",
            producer_executor_id=self.producer_executor_id,
            max_bytes=max_bytes,
        )

    def create_owner_only_stderr_spool(self, *, max_bytes: int) -> RawEvidenceSpool:
        return self.store.create_spool(
            run_id=self.run_id,
            artifact_type="nmap_stderr",
            media_type="text/plain",
            producer_executor_id=self.producer_executor_id,
            max_bytes=max_bytes,
        )

    def private_temp_directory(self) -> str:
        private_directory = self._ensure_private_directory()
        self.store._assert_contained(private_directory, require_exists=True)
        return str(private_directory)

    def remove_target_list(self, path: str) -> None:
        target = Path(path)
        if target not in self._target_lists:
            raise RawEvidenceSecurityError("Private Nmap target path is not owned by this run.")
        self.store._assert_contained(target, require_exists=True)
        target.unlink()
        self._target_lists.remove(target)
        if not self._target_lists and self._private_directory is not None:
            self._private_directory.rmdir()
            self._private_directory = None
            try:
                if self._private_run_root is not None:
                    self._private_run_root.rmdir()
            except OSError:
                # Another attempt for this run may still own a sibling directory.
                pass
            self._private_run_root = None

    def _ensure_private_directory(self) -> Path:
        if self._private_directory is not None:
            return self._private_directory
        _, _, private_root = self.store._ensure_roots()
        private_run_root = private_root / self._private_run_key
        private_run_root.mkdir(parents=True, exist_ok=True)
        self.store.security.secure_directory(private_run_root)
        self.store._assert_contained(private_run_root, require_exists=True)
        private_directory = private_run_root / uuid4().hex
        private_directory.mkdir(exist_ok=False)
        self.store.security.secure_directory(private_directory)
        self.store._assert_contained(private_directory, require_exists=True)
        self._private_run_root = private_run_root
        self._private_directory = private_directory
        return private_directory


def _descriptor(row: RawEvidenceArtifact) -> RawEvidenceArtifactDescriptor:
    return RawEvidenceArtifactDescriptor(
        artifact_id=row.artifact_id,
        run_id=row.run_id,
        project_id=row.project_id,
        site_id=row.site_id,
        artifact_type=row.artifact_type,
        media_type=row.media_type,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        capture_complete=row.capture_complete,
        producer_executor_id=row.producer_executor_id,
        created_at=_aware_utc(row.created_at, field="created_at"),
        sealed_at=_aware_utc(row.sealed_at, field="sealed_at"),
    )


def _validated_internal_key(storage_key: str, *, prefix: str | None) -> PurePosixPath:
    if not isinstance(storage_key, str) or not storage_key or "\\" in storage_key:
        raise RawEvidenceSecurityError("Raw evidence storage key is invalid.")
    relative = PurePosixPath(storage_key)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RawEvidenceSecurityError("Raw evidence storage key is invalid.")
    if prefix is not None and (not relative.parts or relative.parts[0] != prefix):
        raise RawEvidenceSecurityError("Raw evidence storage key has an invalid class.")
    return relative


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_literal_ipv4_target_list(payload: bytes) -> None:
    if not isinstance(payload, bytes) or not payload or len(payload) > RAW_EVIDENCE_TARGET_LIST_MAX_BYTES:
        raise ValueError("Nmap target list is empty or exceeds its byte limit")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("Nmap target list must contain canonical literal IPv4 addresses") from error
    addresses = text.splitlines()
    if not addresses or len(addresses) > 4096 or not text.endswith("\n"):
        raise ValueError("Nmap target list has an invalid address count or terminator")
    canonical: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("Nmap target list contains a non-literal address") from error
        if address.version != 4 or str(address) != value:
            raise ValueError("Nmap target list must contain canonical literal IPv4 addresses")
        canonical.append(value)
    if len(set(canonical)) != len(canonical):
        raise ValueError("Nmap target list contains duplicate addresses")
