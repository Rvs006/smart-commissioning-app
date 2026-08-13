"""Backup and restore for the edge (SQLite) deployment.

A backup is a single zip *bundle* containing:

  * ``db/smart_commissioning.db`` — a CONSISTENT copy of the SQLite database,
    taken via SQLite's online backup API (never a naive mid-write file copy).
  * ``secrets/*`` — encrypted PEMs and the ``.secret_store_key``.
  * ``imports/files/*`` — uploaded import source files referenced by import
    records.
  * ``artifacts/*`` — immutable, content-addressed report artifacts.
  * ``report-signing/*`` — the API-only evidence-signing key, so restored
    artifacts remain verifiable.
  * ``manifest.json`` — bundle metadata: versions, a caller-supplied
    ``created_at`` timestamp, the SHA-256 of every member, and a detached
    Ed25519 signature over the canonical manifest body (via integrity.py).

Backup and restore both rehash every import selected by a frozen scan context.
Restore runs that semantic check after manifest verification and before writing
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
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from smart_commissioning_core import __version__ as core_version
from smart_commissioning_core.execution_context import (
    ExecutionContextIntegrityError,
    scan_authority_bindings,
    verify_bound_import_rows,
)
from smart_commissioning_core.integrity import (
    SigningKey,
    cryptography_available,
    sha256_bytes,
    verify_bytes,
)
from smart_commissioning_core.run_context import RunContextV1
from smart_commissioning_core.sealed_run_integrity import (
    SealedRunIntegrityError,
    verify_legacy_report_evidence_run,
    verify_report_evidence_run,
    verify_sealed_run,
)
from sqlalchemy.engine import make_url

# Members inside the bundle.
_DB_MEMBER = "db/smart_commissioning.db"
_MANIFEST_MEMBER = "manifest.json"
_SECRETS_PREFIX = "secrets/"
_IMPORTS_PREFIX = "imports/files/"
_ARTIFACTS_PREFIX = "artifacts/"
_REPORT_SIGNING_PREFIX = "report-signing/"

_BUNDLE_FORMAT_VERSION = 1
_ENVELOPE_FORMAT_VERSION = "1.0"
_ENVELOPE_ALGORITHM = "RSA-OAEP-SHA256+AES-256-GCM"
# Fixed zip member timestamp so a given input yields reproducible bytes.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_MISSING = object()
_PRE_REPORT_CONTRACT_REVISIONS = frozenset(
    {
        "c9663f90f68a",
        "c4a7ced176a9",
        "c998144d98d4",
        "d1f2a3b4c5d6",
        "e5f6a7b8c9d0",
        "f6a7b8c9d0e1",
        "a7b8c9d0e1f2",
        "b8c9d0e1f2a3",
        "c9d0e1f2a3b4",
        "d0e1f2a3b4c5",
    }
)


class BackupError(RuntimeError):
    """Backup/restore failure (unsupported backend, verification, overwrite)."""


@dataclass(frozen=True)
class BackupSources:
    """Filesystem inputs for a backup bundle (all optional except the DB URL)."""

    database_url: str
    secrets_root: Path | None = None
    imports_files_root: Path | None = None
    report_artifacts_root: Path | None = None
    report_signing_root: Path | None = None


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


def _verify_database_integrity(database_bytes: bytes) -> None:
    """Verify frozen authorities and sealed evidence in one SQLite snapshot."""

    with tempfile.TemporaryDirectory(prefix="smart-commissioning-backup-check-") as verification_directory:
        verification_path = Path(verification_directory) / "snapshot.db"
        verification_path.write_bytes(database_bytes)
        connection = sqlite3.connect(str(verification_path))
        connection.row_factory = sqlite3.Row
        try:
            schema_objects = {
                str(row["name"]): str(row["type"])
                for row in connection.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')")
            }
            _verify_scan_authorities(connection, schema_objects)
            _verify_sealed_runs(connection, schema_objects)
        except BackupError:
            raise
        except sqlite3.Error as error:
            raise BackupError(f"Backup database could not be checked for evidence integrity: {error}") from error
        finally:
            connection.close()


def _verify_scan_authorities(
    connection: sqlite3.Connection,
    schema_objects: Mapping[str, str],
) -> None:
    context_object = schema_objects.get("run_execution_contexts")
    if context_object is None:
        return
    if context_object != "table":
        raise BackupError("Backup database run_execution_contexts object is not a table.")
    context_rows = list(connection.execute("SELECT run_id, context_json, context_sha256 FROM run_execution_contexts"))
    cached_import_rows: dict[str, list[object]] = {}
    for row in context_rows:
        run_id = str(row["run_id"])
        try:
            context = RunContextV1.model_validate(_decode_json(row["context_json"]))
            if context.sha256() != row["context_sha256"]:
                raise ExecutionContextIntegrityError(
                    "stored execution context digest does not match its canonical context"
                )
            contract = context.engine_parameters.get("scan_contract_v1", _MISSING)
            if contract is _MISSING:
                continue
            if not isinstance(contract, Mapping):
                raise ExecutionContextIntegrityError("scan contract metadata is malformed")
            for import_id in scan_authority_bindings(context):
                rows = cached_import_rows.get(import_id)
                if rows is None:
                    if schema_objects.get("import_records") != "table":
                        raise ExecutionContextIntegrityError(f"scan authority import '{import_id}' is missing")
                    record = connection.execute(
                        "SELECT accepted_rows FROM import_records WHERE import_id = ?",
                        (import_id,),
                    ).fetchone()
                    if record is None:
                        raise ExecutionContextIntegrityError(f"scan authority import '{import_id}' is missing")
                    parsed_rows = _decode_json(record["accepted_rows"])
                    if not isinstance(parsed_rows, list):
                        raise ExecutionContextIntegrityError(f"scan authority import '{import_id}' rows are malformed")
                    rows = parsed_rows
                    cached_import_rows[import_id] = rows
                verify_bound_import_rows(context, import_id, rows)
        except (
            ExecutionContextIntegrityError,
            TypeError,
            ValueError,
        ) as error:
            raise BackupError(f"Backup scan authority verification failed for run '{run_id}': {error}") from error


def _verify_sealed_runs(
    connection: sqlite3.Connection,
    schema_objects: Mapping[str, str],
) -> None:
    contract_object = schema_objects.get("report_evidence_contracts")
    declared_revision = (
        connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        if schema_objects.get("alembic_version") == "table"
        else None
    )
    if (
        contract_object is None
        and declared_revision is not None
        and declared_revision["version_num"] not in _PRE_REPORT_CONTRACT_REVISIONS
    ):
        raise BackupError("Backup database current schema is missing report evidence contracts.")
    if contract_object is not None and contract_object != "table":
        raise BackupError("Backup database report evidence contract schema is invalid.")
    has_contract_table = contract_object == "table"
    lifecycle_tables = {
        "run_execution_contexts",
        "run_results",
        "run_seals",
    }
    present_lifecycle_tables = lifecycle_tables.intersection(schema_objects)
    if not present_lifecycle_tables:
        if has_contract_table:
            raise BackupError("Backup database has report contracts without a lifecycle schema.")
        return
    if present_lifecycle_tables != lifecycle_tables:
        missing = ", ".join(sorted(lifecycle_tables - present_lifecycle_tables))
        raise BackupError(f"Backup database has a partially present modern lifecycle schema; missing: {missing}.")
    required_tables = {
        "runs",
        "run_issues",
        "discovered_devices",
        "discovered_points",
        "discovered_topics",
        *lifecycle_tables,
    }
    invalid_objects = sorted(name for name in required_tables if schema_objects.get(name) != "table")
    if invalid_objects:
        raise BackupError("Backup database sealed-run schema is incomplete: " + ", ".join(invalid_objects))

    candidates = list(
        connection.execute(
            """
            SELECT *
            FROM runs
            WHERE status IN ('succeeded', 'failed', 'cancelled')
               OR id IN (SELECT run_id FROM run_results)
               OR id IN (SELECT run_id FROM run_seals)
            ORDER BY id
            """
        )
    )
    for run_row in candidates:
        run = dict(run_row)
        run_id = str(run["id"])
        run["result_summary"] = _decode_json(run.get("result_summary"))
        run["parameters"] = _decode_json(run.get("parameters"))
        run["modern_lifecycle_component_present"] = _has_modern_lifecycle_component(
            connection,
            schema_objects,
            run_id,
        )
        sync_artifact = (
            connection.execute(
                "SELECT manifest_json FROM sync_artifacts WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            if schema_objects.get("sync_artifacts") == "table"
            else None
        )
        run["sync_artifact_present"] = sync_artifact is not None
        run["sync_artifact_manifest"] = (
            _decode_json(sync_artifact["manifest_json"])
            if sync_artifact is not None
            else None
        )
        context = _one_row(
            connection,
            "SELECT * FROM run_execution_contexts WHERE run_id = ?",
            run_id,
        )
        if context is not None:
            context["context_json"] = _decode_json(context.get("context_json"))
        result = _one_row(
            connection,
            "SELECT * FROM run_results WHERE run_id = ?",
            run_id,
        )
        if result is not None:
            result["summary"] = _decode_json(result.get("summary"))
            result["result_payload"] = _decode_json(result.get("result_payload"))
        seal = _one_row(
            connection,
            "SELECT * FROM run_seals WHERE run_id = ?",
            run_id,
        )
        projections = {
            "issues": _projection_rows(connection, "run_issues", run_id),
            "devices": _projection_rows(connection, "discovered_devices", run_id),
            "points": _projection_rows(connection, "discovered_points", run_id),
            "topics": _projection_rows(connection, "discovered_topics", run_id),
        }
        try:
            parameters = run.get("parameters")
            if run.get("job_type") == "report_generation" and has_contract_table:
                contract = connection.execute(
                    "SELECT contract_version FROM report_evidence_contracts WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                contract_version = contract["contract_version"] if contract is not None else None
                if contract_version == "sealed_v1":
                    verifier = verify_report_evidence_run
                elif contract_version == "legacy_pre_lifecycle":
                    verifier = verify_legacy_report_evidence_run
                else:
                    raise SealedRunIntegrityError(
                        "lifecycle",
                        "report has no recognized evidence contract",
                    )
            elif run.get("job_type") == "report_generation":
                summary = run.get("result_summary")
                marker = summary.get("legacy_report_integrity") if isinstance(summary, Mapping) else None
                if (
                    isinstance(marker, Mapping)
                    and marker.get("migration") == "v0.1.26"
                    and marker.get("silently_resigned") is False
                ):
                    verifier = verify_legacy_report_evidence_run
                else:
                    has_modern_report_shape = any(row is not None for row in (context, result, seal)) or (
                        isinstance(parameters, Mapping)
                        and bool({"report_snapshot_v2", "report_snapshot_sha256"}.intersection(parameters))
                    )
                    verifier = verify_report_evidence_run if has_modern_report_shape else verify_sealed_run
            else:
                verifier = verify_sealed_run
            verifier(
                run_id=run_id,
                run=run,
                context=context,
                result=result,
                seal=seal,
                **projections,
            )
        except (SealedRunIntegrityError, TypeError, ValueError) as error:
            raise BackupError(f"Backup sealed run verification failed for run '{run_id}': {error}") from error


def _has_modern_lifecycle_component(
    connection: sqlite3.Connection,
    schema_objects: Mapping[str, str],
    run_id: str,
) -> bool:
    """Return whether a lifecycle-only outbox row proves modern provenance."""

    if schema_objects.get("run_dispatch_outbox") != "table":
        return False
    return (
        connection.execute(
            "SELECT 1 FROM run_dispatch_outbox WHERE run_id = ? LIMIT 1",
            (run_id,),
        ).fetchone()
        is not None
    )


def _projection_rows(
    connection: sqlite3.Connection,
    table: str,
    run_id: str,
) -> list[dict[str, object]]:
    rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE run_id = ? ORDER BY position, id",  # noqa: S608 - fixed table allow-list
            (run_id,),
        )
    ]
    json_fields = {
        "run_issues": (),
        "discovered_devices": ("attributes",),
        "discovered_points": ("observed_value", "attributes"),
        "discovered_topics": ("last_payload", "attributes"),
    }[table]
    for row in rows:
        for field in json_fields:
            row[field] = _decode_json(row.get(field))
    return rows


def _one_row(
    connection: sqlite3.Connection,
    query: str,
    run_id: str,
) -> dict[str, object] | None:
    row = connection.execute(query, (run_id,)).fetchone()
    return dict(row) if row is not None else None


def _decode_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


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
    database_bytes = _consistent_sqlite_bytes(_sqlite_path(sources.database_url))
    _verify_database_integrity(database_bytes)
    members: list[tuple[str, bytes]] = [(_DB_MEMBER, database_bytes)]
    members.extend(_iter_dir_members(sources.secrets_root, _SECRETS_PREFIX))
    members.extend(_iter_dir_members(sources.imports_files_root, _IMPORTS_PREFIX))
    members.extend(_iter_dir_members(sources.report_artifacts_root, _ARTIFACTS_PREFIX))
    members.extend(_iter_dir_members(sources.report_signing_root, _REPORT_SIGNING_PREFIX))

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


def encrypt_backup_bundle(bundle_bytes: bytes, recipient_public_key_pem: str | bytes) -> bytes:
    """Encrypt one signed backup ZIP for an explicit RSA recipient.

    The random AES key protects the complete signed bundle. RSA-OAEP wraps only
    that key, so backup size is independent of the recipient key size. Restore
    authenticates and decrypts this envelope before verifying the inner manifest
    signature and member hashes.
    """

    if not cryptography_available():
        raise BackupError("Recipient-encrypted backup requires cryptography.")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        pem = (
            recipient_public_key_pem.encode("utf-8")
            if isinstance(recipient_public_key_pem, str)
            else recipient_public_key_pem
        )
        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
            raise BackupError("Backup recipient must use an RSA public key of at least 2048 bits.")
        content_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        wrapped_key = public_key.encrypt(
            content_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        public_der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        header: dict[str, object] = {
            "envelope_format_version": _ENVELOPE_FORMAT_VERSION,
            "algorithm": _ENVELOPE_ALGORITHM,
            "recipient_public_key_sha256": sha256_bytes(public_der),
            "signed_bundle_sha256": sha256_bytes(bundle_bytes),
            "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
        }
        associated_data = _canonical_envelope_header(header)
        ciphertext = AESGCM(content_key).encrypt(nonce, bundle_bytes, associated_data)
        envelope = {
            **header,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except BackupError:
        raise
    except (TypeError, ValueError) as error:
        raise BackupError("Backup recipient public key is invalid.") from error


def decrypt_backup_bundle(
    envelope_bytes: bytes,
    recipient_private_key_pem: str | bytes,
    *,
    password: bytes | None = None,
) -> bytes:
    """Authenticate and decrypt an encrypted backup envelope."""

    if not cryptography_available():
        raise BackupError("Encrypted backup restore requires cryptography.")
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        envelope = json.loads(envelope_bytes.decode("utf-8"))
        if not isinstance(envelope, dict):
            raise BackupError("Encrypted backup envelope is malformed.")
        header = {
            key: envelope.get(key)
            for key in (
                "envelope_format_version",
                "algorithm",
                "recipient_public_key_sha256",
                "signed_bundle_sha256",
                "wrapped_key",
                "nonce",
            )
        }
        if header["envelope_format_version"] != _ENVELOPE_FORMAT_VERSION or header["algorithm"] != _ENVELOPE_ALGORITHM:
            raise BackupError("Encrypted backup envelope version or algorithm is unsupported.")
        pem = (
            recipient_private_key_pem.encode("utf-8")
            if isinstance(recipient_private_key_pem, str)
            else recipient_private_key_pem
        )
        private_key = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise BackupError("Backup recipient private key must be RSA.")
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if sha256_bytes(public_der) != header["recipient_public_key_sha256"]:
            raise BackupError("Backup envelope belongs to a different recipient key.")
        content_key = private_key.decrypt(
            base64.b64decode(str(header["wrapped_key"]), validate=True),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        nonce = base64.b64decode(str(header["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(envelope.get("ciphertext")), validate=True)
        bundle = AESGCM(content_key).decrypt(
            nonce,
            ciphertext,
            _canonical_envelope_header(header),
        )
        if sha256_bytes(bundle) != header["signed_bundle_sha256"]:
            raise BackupError("Decrypted backup bundle digest does not match its envelope.")
        return bundle
    except BackupError:
        raise
    except InvalidTag as error:
        raise BackupError("Encrypted backup authentication failed.") from error
    except Exception as error:
        raise BackupError("Encrypted backup could not be decrypted with this recipient key.") from error


def _canonical_envelope_header(header: dict[str, object]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    report_artifacts_root: Path | None = None
    report_signing_root: Path | None = None


def _is_supported_restore_member(name: str) -> bool:
    if name == _DB_MEMBER:
        return True
    return any(
        name.startswith(prefix) and len(name) > len(prefix)
        for prefix in (
            _SECRETS_PREFIX,
            _IMPORTS_PREFIX,
            _ARTIFACTS_PREFIX,
            _REPORT_SIGNING_PREFIX,
        )
    )


def _validate_restore_member_path(name: str) -> None:
    """Reject non-canonical or rooted archive paths on every host OS."""

    candidates = [name]
    for prefix in (
        _SECRETS_PREFIX,
        _IMPORTS_PREFIX,
        _ARTIFACTS_PREFIX,
        _REPORT_SIGNING_PREFIX,
    ):
        if name.startswith(prefix):
            candidates.append(name[len(prefix) :])
            break
    for candidate in candidates:
        portable = candidate.replace("\\", "/")
        if (
            not candidate
            or candidate.startswith(("/", "\\"))
            or PurePosixPath(portable).is_absolute()
            or bool(PureWindowsPath(candidate).drive)
            or any(component in {".", ".."} for component in portable.split("/"))
        ):
            raise BackupError(f"Bundle member path is invalid: {name}")


def _canonical_restore_member_identity(name: str) -> str:
    """Return a portable case-insensitive identity for one supported member."""

    if name == _DB_MEMBER:
        return "database"
    for prefix in (
        _SECRETS_PREFIX,
        _IMPORTS_PREFIX,
        _ARTIFACTS_PREFIX,
        _REPORT_SIGNING_PREFIX,
    ):
        if name.startswith(prefix):
            relative = name[len(prefix) :].replace("\\", "/")
            components = (component.casefold() for component in relative.split("/") if component)
            return prefix.casefold() + "/".join(components)
    raise BackupError(f"Bundle declares unsupported restore member: {name}")


def verify_bundle(bundle_bytes: bytes, *, allow_unsigned: bool = False) -> dict[str, object]:
    """Verify the manifest signature and every member hash; return the manifest.

    Raises :class:`BackupError` on any mismatch. ``allow_unsigned`` permits a
    bundle whose manifest carries no signature (e.g. produced without crypto).
    """
    with ZipFile(_readonly_buffer(bundle_bytes)) as archive:
        member_names = [info.filename for info in archive.infolist()]
        duplicates = sorted(name for name, count in Counter(member_names).items() if count > 1)
        if duplicates:
            raise BackupError("Bundle contains duplicate member names: " + ", ".join(duplicates))
        archive_names = set(member_names)
        if _MANIFEST_MEMBER not in archive_names:
            raise BackupError("Bundle is missing manifest.json.")
        manifest = json.loads(archive.read(_MANIFEST_MEMBER).decode("utf-8"))
        if not isinstance(manifest, dict):
            raise BackupError("Bundle manifest is malformed.")
        bundle_format_version = manifest.get("bundle_format_version")
        if type(bundle_format_version) is not int or bundle_format_version != _BUNDLE_FORMAT_VERSION:
            raise BackupError(f"Bundle manifest version is unsupported; expected integer {_BUNDLE_FORMAT_VERSION}.")

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
        if _MANIFEST_MEMBER in declared:
            raise BackupError("Manifest must not declare itself as a payload member.")
        if any(not isinstance(name, str) for name in declared):
            raise BackupError("Manifest member names are malformed.")
        for name in declared:
            _validate_restore_member_path(name)
        if _DB_MEMBER not in declared:
            raise BackupError("Bundle manifest does not declare its SQLite database member.")
        unsupported = sorted(name for name in declared if not _is_supported_restore_member(name))
        if unsupported:
            raise BackupError("Bundle declares unsupported restore members: " + ", ".join(unsupported))
        canonical_members: dict[str, str] = {}
        for name in declared:
            identity = _canonical_restore_member_identity(name)
            existing = canonical_members.get(identity)
            if existing is not None:
                raise BackupError(f"Bundle members resolve to one canonical restore destination: {existing}, {name}")
            canonical_members[identity] = name
        expected_archive_names = set(declared) | {_MANIFEST_MEMBER}
        undeclared = sorted(archive_names - expected_archive_names)
        missing = sorted(expected_archive_names - archive_names)
        if undeclared:
            raise BackupError("Bundle contains undeclared archive members: " + ", ".join(undeclared))
        if missing:
            raise BackupError("Bundle is missing declared members: " + ", ".join(missing))
        for name, expected_hash in declared.items():
            if sha256_bytes(archive.read(name)) != expected_hash:
                raise BackupError(f"Member hash mismatch (tampered): {name}")
    return manifest


def _verified_database_member(bundle_bytes: bytes, manifest: Mapping[str, object]) -> bytes:
    declared = manifest.get("members")
    if not isinstance(declared, Mapping) or _DB_MEMBER not in declared:
        raise BackupError("Bundle manifest does not declare its SQLite database member.")
    with ZipFile(_readonly_buffer(bundle_bytes)) as archive:
        try:
            return archive.read(_DB_MEMBER)
        except KeyError as error:
            raise BackupError("Bundle is missing its SQLite database member.") from error


def restore_backup_bundle(
    bundle_bytes: bytes,
    target: RestoreTarget,
    *,
    force: bool = False,
    allow_unsigned: bool = False,
    recipient_private_key_pem: str | bytes | None = None,
    recipient_private_key_password: bytes | None = None,
) -> dict[str, object]:
    """Verify then restore a bundle into ``target``; return the manifest.

    Refuses to overwrite an existing populated database file unless
    ``force=True``. Verification (signature + member hashes) runs first, so a
    tampered bundle never writes a single byte.
    """
    if _looks_like_encrypted_envelope(bundle_bytes):
        if recipient_private_key_pem is None:
            raise BackupError("Encrypted backup requires the recipient private key.")
        bundle_bytes = decrypt_backup_bundle(
            bundle_bytes,
            recipient_private_key_pem,
            password=recipient_private_key_password,
        )
    manifest = verify_bundle(bundle_bytes, allow_unsigned=allow_unsigned)
    _verify_database_integrity(_verified_database_member(bundle_bytes, manifest))

    if target.database_path.exists() and target.database_path.stat().st_size > 0 and not force:
        raise BackupError(f"Refusing to overwrite existing database at {target.database_path}; pass force=True.")

    # Resolve every declared destination and read every payload before creating
    # restore roots. One escaping or unsupported member leaves no target state.
    with ZipFile(_readonly_buffer(bundle_bytes)) as archive:
        planned: list[tuple[Path, bytes]] = []
        canonical_destinations: dict[str, str] = {}
        declared = manifest["members"]
        assert isinstance(declared, dict)
        for name in sorted(declared):
            destination = _member_destination(name, target)
            if destination is None:
                raise BackupError(f"Bundle declares unsupported restore member: {name}")
            canonical_destination = str(destination.resolve()).replace("\\", "/").casefold()
            existing = canonical_destinations.get(canonical_destination)
            if existing is not None:
                raise BackupError(f"Bundle members resolve to one canonical restore destination: {existing}, {name}")
            canonical_destinations[canonical_destination] = name
            planned.append((destination, archive.read(name)))

    restore_roots = (
        target.database_path.parent,
        target.secrets_root,
        target.imports_files_root,
        _report_artifacts_target(target),
        _report_signing_target(target),
    )
    for root in restore_roots:
        root.mkdir(parents=True, exist_ok=True)
        _restrict_restore_directory(root)

    for destination, payload in planned:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_owner_only(destination, payload)
    return manifest


def _looks_like_encrypted_envelope(payload: bytes) -> bool:
    stripped = payload.lstrip()
    return stripped.startswith(b"{") and b'"envelope_format_version"' in stripped


def _write_owner_only(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - directory ACL is authoritative on Windows
        pass


def _restrict_restore_directory(path: Path) -> None:
    # Reuse the runtime's POSIX mode and Windows icacls implementation so a
    # restored secret or raw artifact does not inherit a broad destination ACL.
    from app.core.runtime import _restrict_directory

    _restrict_directory(path)


def _ensure_within(destination: Path, root: Path, name: str) -> Path:
    """Resolve ``destination`` and assert it stays within ``root``.

    Defense-in-depth against zip path traversal: a crafted member name (e.g.
    ``secrets/../../etc/passwd``) must never resolve to a path outside its
    intended restore root, even when the manifest is unsigned. Raises
    :class:`BackupError` (so nothing is written) when it escapes.
    """
    resolved_root = root.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination != resolved_root and not resolved_destination.is_relative_to(resolved_root):
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
        destination = target.secrets_root / name[len(_SECRETS_PREFIX) :]
        return _ensure_within(destination, target.secrets_root, name)
    if name.startswith(_IMPORTS_PREFIX):
        destination = target.imports_files_root / name[len(_IMPORTS_PREFIX) :]
        return _ensure_within(destination, target.imports_files_root, name)
    if name.startswith(_ARTIFACTS_PREFIX):
        root = _report_artifacts_target(target)
        destination = root / name[len(_ARTIFACTS_PREFIX) :]
        return _ensure_within(destination, root, name)
    if name.startswith(_REPORT_SIGNING_PREFIX):
        root = _report_signing_target(target)
        destination = root / name[len(_REPORT_SIGNING_PREFIX) :]
        return _ensure_within(destination, root, name)
    return None


def _report_artifacts_target(target: RestoreTarget) -> Path:
    """Return the explicit artifact root or its legacy-call-site default."""
    return target.report_artifacts_root or target.database_path.parent / "artifacts"


def _report_signing_target(target: RestoreTarget) -> Path:
    """Return the explicit signing root or its legacy-call-site default."""
    return target.report_signing_root or target.database_path.parent / "report-signing"


def _readonly_buffer(data: bytes):  # noqa: ANN202 - tiny BytesIO factory
    from io import BytesIO

    return BytesIO(data)
