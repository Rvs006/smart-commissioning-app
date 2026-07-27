"""Strict Sync v2 wire bundle for immutable terminal evidence and report bytes."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Literal
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo, is_zipfile

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import Engine

from smart_commissioning_core.db.repositories import TERMINAL_RUN_STATUSES, SyncRepository
from smart_commissioning_core.db.sync_v2_repository import SyncV2Repository
from smart_commissioning_core.integrity import (
    SigningKey,
    public_key_fingerprint,
    sha256_bytes,
    verify_bytes,
)
from smart_commissioning_core.run_context import RunContextV1, canonical_sha256
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from smart_commissioning_core.sync_identity import EdgeIdentity

PROTOCOL_VERSION = "2.0"
BUNDLE_MEDIA_TYPE = "application/vnd.smart-commissioning.sync-v2+zip"
RECEIPT_CLASSES = frozenset(
    {
        "accepted",
        "byte_identical",
        "conflict",
        "unauthorized",
        "malformed",
        "manifest_signature_failed",
        "artifact_hash_failed",
        "artifact_size_failed",
        "missing_artifact",
        "partial_bundle",
    }
)
ACKNOWLEDGED_RECEIPT_CLASSES = frozenset({"accepted", "byte_identical"})
RETRYABLE_RECEIPT_CLASSES = frozenset(
    {
        "malformed",
        "manifest_signature_failed",
        "artifact_hash_failed",
        "artifact_size_failed",
        "missing_artifact",
        "partial_bundle",
    }
)

_MANIFEST_MEMBER = "manifest.json"
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "mqtt_password",
        "broker_password",
        "key_password",
        "key_passphrase",
        "passphrase",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
        "api_token",
        "api_key",
        "api_secret",
        "client_secret",
        "access_key",
        "secret_access_key",
        "private_key",
        "private_key_pem",
        "signing_private_key",
        "client_key",
        "client_key_pem",
        "tls_key",
        "secret_key",
        "client_certificate",
        "client_cert",
        "ca_certificate",
        "ca_cert",
        "certificate",
        "credentials",
        "secret",
        "owner_token",
        "authorization",
    }
)
_SENSITIVE_COMPACT_KEYS = frozenset(key.replace("_", "") for key in _SENSITIVE_KEYS)
_PRIVATE_KEY_PEM_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----",
    flags=re.IGNORECASE,
)
_CERTIFICATE_PEM_RE = re.compile(
    r"-----BEGIN [^-\r\n]*CERTIFICATE[^-\r\n]*-----",
    flags=re.IGNORECASE,
)
_SAFE_SENSITIVE_KEY_SUFFIXES = frozenset(
    {
        "fingerprint",
        "hash",
        "id",
        "ids",
        "ref",
        "reference",
        "references",
        "refs",
        "sha256",
        "version",
        "versions",
    }
)
_MAX_ARTIFACT_ARCHIVE_MEMBERS = 4096
_MAX_ARTIFACT_ARCHIVE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
_MAX_ARTIFACT_ARCHIVE_DEPTH = 3
_REPORT_PARAMETER_KEYS = (
    "output_format",
    "report_type",
    "source_run_ids",
    "report_title_custom",
    "report_title",
    "report_generated_at",
    "renderer_version",
    "report_snapshot_v2",
    "report_snapshot_sha256",
)
_REPORT_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "site_id",
        "report_type",
        "output_format",
        "source_run_ids",
        "source_result_hashes",
        "selected_assets",
        "filters",
        "configuration_provenance",
        "schema_versions",
        "renderer_version",
        "displayed_counts",
        "report_metadata",
        "udmi_report_snapshot",
        "source_run_snapshots",
        "source_run_seals",
        "source_discovery_snapshots",
        "udmi_scope",
    }
)
_REPORT_METADATA_KEYS = frozenset(
    {
        "output_format",
        "report_type",
        "report_title_custom",
        "report_title",
        "report_generated_at",
        "renderer_version",
    }
)


class SyncV2Error(RuntimeError):
    """A v2 bundle could not be built or authenticated."""


class SyncV2Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    item_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    item_member: str = Field(min_length=1, max_length=255)
    item_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_member: str | None = Field(default=None, max_length=255)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_size: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _artifact_fields_are_coherent(self) -> SyncV2Descriptor:
        present = (
            self.artifact_member is not None,
            self.artifact_sha256 is not None,
            self.artifact_size is not None,
        )
        if any(present) and not all(present):
            raise ValueError("artifact descriptor fields must be all present or all absent")
        if self.item_member != f"items/{self.item_id}.json":
            raise ValueError("item member does not match item id")
        if self.artifact_member is not None and self.artifact_member != (
            f"artifacts/sha256/{self.artifact_sha256}"
        ):
            raise ValueError("artifact member does not match artifact digest")
        return self


class SyncV2Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    protocol: Literal["smart-commissioning-sync"]
    protocol_version: Literal["2.0"]
    bundle_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    edge_id: str = Field(min_length=1, max_length=255)
    created_at: str
    items: tuple[SyncV2Descriptor, ...]
    signature_algorithm: Literal["ed25519"]
    signing_key_id: str = Field(min_length=1, max_length=64)
    public_key_pem: str
    signed_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str


class SyncV2Run(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    run_id: str = Field(min_length=1, max_length=64)
    job_type: str = Field(min_length=1, max_length=64)
    status: Literal["succeeded", "failed", "cancelled"]
    stage: str = Field(min_length=1, max_length=128)
    progress_percent: StrictInt = Field(ge=0, le=100)
    created_at: str
    updated_at: str
    project_id: str = Field(min_length=1, max_length=255)
    site_id: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any]
    result_summary: dict[str, Any]
    issues: tuple[dict[str, Any], ...]
    error_message: str | None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        return _validated_timestamp(value)


class SyncV2Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["1.0"]
    terminal_status: Literal["succeeded", "failed", "cancelled"]
    terminal_stage: str = Field(min_length=1, max_length=128)
    summary: dict[str, Any]
    result_payload: dict[str, Any]
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        return _validated_timestamp(value)


class SyncV2Seal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    terminal_status: Literal["succeeded", "failed", "cancelled"]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_at: str

    @field_validator("sealed_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        return _validated_timestamp(value)


class SyncV2ExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["1.0"]
    context_json: dict[str, Any]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str

    @field_validator("created_at")
    @classmethod
    def _aware_timestamp(cls, value: str) -> str:
        return _validated_timestamp(value)


class SyncV2Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal["2.0"]
    run: SyncV2Run
    result: SyncV2Result
    seal: SyncV2Seal
    execution_context: SyncV2ExecutionContext | None = None
    report_snapshot: dict[str, Any] | None = None
    artifact_manifest: dict[str, Any] | None = None


class SyncV2Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    receipt_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=64)
    class_: str = Field(alias="class")
    acknowledged: StrictBool
    retryable: StrictBool

    @model_validator(mode="after")
    def _valid_class(self) -> SyncV2Receipt:
        if self.class_ not in RECEIPT_CLASSES:
            raise ValueError("unsupported receipt class")
        if self.acknowledged != (self.class_ in ACKNOWLEDGED_RECEIPT_CLASSES):
            raise ValueError("receipt acknowledgement does not match its class")
        if self.retryable != (self.class_ in RETRYABLE_RECEIPT_CLASSES):
            raise ValueError("receipt retryability does not match its class")
        return self


@dataclass(frozen=True)
class OpenedSyncV2Bundle:
    manifest: SyncV2Manifest
    members: dict[str, bytes]


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def strict_json_loads(value: str) -> Any:
    """Parse one wire JSON document without ambiguous or non-standard values."""

    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, child in pairs:
            if key in parsed:
                raise ValueError("JSON object contains a duplicate member name")
            parsed[key] = child
        return parsed

    def reject_non_finite(token: str) -> None:
        raise ValueError(f"JSON contains non-standard numeric token {token}")

    return json.loads(
        value,
        object_pairs_hook=object_without_duplicates,
        parse_constant=reject_non_finite,
    )


def _validated_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp is not valid ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _validate_wire_parameters(
    run: dict[str, Any],
    *,
    context_hash: str,
    report_snapshot: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
) -> None:
    parameters = run["parameters"]
    is_report = run["job_type"] == "report_generation"
    if not is_report:
        if parameters != {"context_sha256": context_hash}:
            raise SyncV2Error("malformed_item")
        return

    if set(parameters) != set(_REPORT_PARAMETER_KEYS):
        raise SyncV2Error("malformed_item")
    if not isinstance(report_snapshot, dict) or not isinstance(artifact_manifest, dict):
        raise SyncV2Error("malformed_item")
    if parameters["report_snapshot_v2"] != report_snapshot:
        raise SyncV2Error("malformed_item")
    if parameters["report_snapshot_sha256"] != context_hash:
        raise SyncV2Error("malformed_item")
    if set(report_snapshot) != _REPORT_SNAPSHOT_KEYS:
        raise SyncV2Error("malformed_item")
    if (
        report_snapshot.get("schema_version") != "2.0"
        or report_snapshot.get("project_id") != run["project_id"]
        or report_snapshot.get("site_id") != run["site_id"]
        or report_snapshot.get("report_type") != parameters["report_type"]
        or report_snapshot.get("output_format") != parameters["output_format"]
        or report_snapshot.get("source_run_ids") != parameters["source_run_ids"]
        or report_snapshot.get("renderer_version") != parameters["renderer_version"]
        or artifact_manifest.get("renderer_version") != parameters["renderer_version"]
    ):
        raise SyncV2Error("malformed_item")
    metadata = report_snapshot.get("report_metadata")
    if not isinstance(metadata, dict) or set(metadata) != _REPORT_METADATA_KEYS:
        raise SyncV2Error("malformed_item")
    if any(metadata[key] != parameters[key] for key in _REPORT_METADATA_KEYS):
        raise SyncV2Error("malformed_item")


def build_sync_v2_bundle(
    engine: Engine,
    *,
    signing_key: SigningKey,
    edge_identity: EdgeIdentity,
    created_at: datetime,
    artifact_loader: Callable[[dict[str, Any]], bytes],
    run_ids: list[str] | None = None,
) -> bytes:
    """Build deterministic v2 bytes from sealed terminal runs only."""

    repository = SyncRepository(engine)
    selected = run_ids if run_ids is not None else SyncV2Repository(engine).list_pending_run_ids()
    members: list[tuple[str, bytes]] = []
    descriptors: list[dict[str, object]] = []
    seen: set[str] = set()
    for run_id in selected:
        if run_id in seen:
            continue
        seen.add(run_id)
        export = repository.get_run_for_export(run_id)
        if export is None:
            raise SyncV2Error(f"Cannot bundle missing run: {run_id}")
        if export["run"].get("status") not in TERMINAL_RUN_STATUSES:
            raise SyncV2Error(f"Cannot bundle non-terminal run: {run_id}")
        item, artifact = _build_item(export, artifact_loader=artifact_loader)
        item_bytes = canonical_json_bytes(item)
        result_sha256 = str(item["seal"]["result_sha256"])
        item_id = sha256_bytes(f"{run_id}\0{result_sha256}".encode())[:32]
        item_member = f"items/{item_id}.json"
        descriptor: dict[str, object] = {
            "item_id": item_id,
            "run_id": run_id,
            "project_id": item["run"]["project_id"],
            "site_id": item["run"]["site_id"],
            "item_member": item_member,
            "item_sha256": sha256_bytes(item_bytes),
            "result_sha256": result_sha256,
            "artifact_member": None,
            "artifact_sha256": None,
            "artifact_size": None,
        }
        members.append((item_member, item_bytes))
        if artifact is not None:
            artifact_sha256 = sha256_bytes(artifact)
            artifact_member = f"artifacts/sha256/{artifact_sha256}"
            descriptor.update(
                {
                    "artifact_member": artifact_member,
                    "artifact_sha256": artifact_sha256,
                    "artifact_size": len(artifact),
                }
            )
            members.append((artifact_member, artifact))
        descriptors.append(descriptor)

    if not descriptors:
        raise SyncV2Error("Cannot build an empty Sync v2 bundle.")

    manifest: dict[str, object] = {
        "protocol": "smart-commissioning-sync",
        "protocol_version": PROTOCOL_VERSION,
        "bundle_id": "0" * 64,
        "edge_id": edge_identity.edge_id,
        "created_at": created_at.isoformat(),
        "items": descriptors,
        "signature_algorithm": "ed25519",
        "signing_key_id": signing_key.public_key_fingerprint(),
        "public_key_pem": signing_key.public_key_pem(),
        "signed_manifest_sha256": "0" * 64,
        "signature": "",
    }
    manifest["bundle_id"] = _bundle_id(manifest)
    signed_body = _signed_manifest_body(manifest)
    manifest["signed_manifest_sha256"] = sha256_bytes(signed_body)
    manifest["signature"] = base64.b64encode(signing_key.sign(signed_body)).decode("ascii")
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    return _write_zip([*members, (_MANIFEST_MEMBER, manifest_bytes)])


def open_sync_v2_bundle(
    bundle: bytes,
    *,
    expected_edge_id: str,
    expected_signing_key_fingerprint: str,
    max_items: int = 500,
    max_uncompressed_bytes: int = 200 * 1024 * 1024,
) -> OpenedSyncV2Bundle:
    """Authenticate the outer bundle without collapsing per-item failures."""

    try:
        with ZipFile(BytesIO(bundle)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > 1 + (2 * max_items):
                raise SyncV2Error("Bundle member count exceeds the configured limit.")
            if len(names) != len(set(names)):
                raise SyncV2Error("Bundle contains duplicate members.")
            if sum(info.file_size for info in infos) > max_uncompressed_bytes:
                raise SyncV2Error("Bundle expands beyond the configured limit.")
            if any(info.flag_bits & 0x1 for info in infos):
                raise SyncV2Error("Encrypted ZIP members are not supported.")
            for name in names:
                _validate_member_name(name)
            if _MANIFEST_MEMBER not in names:
                raise SyncV2Error("Bundle is missing manifest.json.")
            raw_manifest = archive.read(_MANIFEST_MEMBER)
            members = {name: archive.read(name) for name in names if name != _MANIFEST_MEMBER}
    except (BadZipFile, KeyError, OSError, ValueError) as error:
        raise SyncV2Error("Bundle is not a readable Sync v2 archive.") from error
    try:
        payload = strict_json_loads(raw_manifest.decode("utf-8"))
        manifest = SyncV2Manifest.model_validate(payload)
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise SyncV2Error("Bundle manifest is malformed.") from error
    if len(manifest.items) > max_items:
        raise SyncV2Error("Bundle item count exceeds the configured limit.")
    if not manifest.items:
        raise SyncV2Error("Bundle must contain at least one item.")
    if len({item.item_id for item in manifest.items}) != len(manifest.items):
        raise SyncV2Error("Bundle contains duplicate item IDs.")
    if len({item.run_id for item in manifest.items}) != len(manifest.items):
        raise SyncV2Error("Bundle contains duplicate run IDs.")
    try:
        created_at = datetime.fromisoformat(manifest.created_at)
    except ValueError as error:
        raise SyncV2Error("Bundle creation time is invalid.") from error
    if created_at.tzinfo is None:
        raise SyncV2Error("Bundle creation time must include a timezone.")
    if manifest.edge_id != expected_edge_id:
        raise SyncV2Error("Bundle identity does not match the authenticated credential.")
    actual_fingerprint = _fingerprint_from_pem(manifest.public_key_pem)
    if (
        actual_fingerprint != manifest.signing_key_id
        or actual_fingerprint != expected_signing_key_fingerprint
    ):
        raise SyncV2Error("Bundle signing key does not match the authenticated credential.")
    raw = manifest.model_dump(mode="json")
    if _bundle_id(raw) != manifest.bundle_id:
        raise SyncV2Error("Bundle ID does not match its canonical manifest.")
    signed_body = _signed_manifest_body(raw)
    if sha256_bytes(signed_body) != manifest.signed_manifest_sha256:
        raise SyncV2Error("Bundle signed-manifest digest is invalid.")
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
    except ValueError as error:
        raise SyncV2Error("Bundle signature encoding is invalid.") from error
    if not verify_bytes(signed_body, signature, manifest.public_key_pem):
        raise SyncV2Error("Bundle signature is invalid.")
    declared = {
        descriptor.item_member for descriptor in manifest.items
    } | {
        descriptor.artifact_member
        for descriptor in manifest.items
        if descriptor.artifact_member is not None
    }
    extras = set(members) - declared
    if extras:
        raise SyncV2Error("Bundle contains undeclared members.")
    return OpenedSyncV2Bundle(manifest=manifest, members=members)


def validate_sync_v2_item(raw: bytes, descriptor: SyncV2Descriptor) -> dict[str, Any]:
    """Validate canonical terminal/result/context coherence for one item."""

    if sha256_bytes(raw) != descriptor.item_sha256:
        raise SyncV2Error("item_hash_mismatch")
    try:
        parsed = strict_json_loads(raw.decode("utf-8"))
        item = SyncV2Item.model_validate(parsed)
    except (UnicodeDecodeError, ValueError, ValidationError) as error:
        raise SyncV2Error("malformed_item") from error
    run = item.run.model_dump(mode="json")
    result = item.result.model_dump(mode="json")
    seal = item.seal.model_dump(mode="json")
    if run.get("run_id") != descriptor.run_id:
        raise SyncV2Error("malformed_item")
    if run.get("project_id") != descriptor.project_id or run.get("site_id") != descriptor.site_id:
        raise SyncV2Error("malformed_item")
    try:
        terminal = TerminalResultV1.model_validate(result["result_payload"])
    except ValidationError as error:
        raise SyncV2Error("malformed_item") from error
    result_sha256 = terminal.sha256()
    expected_item_id = sha256_bytes(
        f"{descriptor.run_id}\0{result_sha256}".encode()
    )[:32]
    if descriptor.item_id != expected_item_id:
        raise SyncV2Error("malformed_item")
    if result_sha256 != descriptor.result_sha256:
        raise SyncV2Error("malformed_item")
    if result["result_sha256"] != result_sha256:
        raise SyncV2Error("malformed_item")
    if result["terminal_status"] != terminal.status:
        raise SyncV2Error("malformed_item")
    if result["terminal_stage"] != terminal.stage:
        raise SyncV2Error("malformed_item")
    if result["summary"] != terminal.summary:
        raise SyncV2Error("malformed_item")
    if seal["result_sha256"] != result_sha256:
        raise SyncV2Error("malformed_item")
    if seal["terminal_status"] != terminal.status:
        raise SyncV2Error("malformed_item")
    if run.get("status") != terminal.status or run.get("stage") != terminal.stage:
        raise SyncV2Error("malformed_item")
    if run.get("result_summary") != terminal.summary:
        raise SyncV2Error("malformed_item")
    if run.get("issues") != list(terminal.issues):
        raise SyncV2Error("malformed_item")
    if run.get("error_message") != terminal.error_message:
        raise SyncV2Error("malformed_item")
    if run.get("progress_percent") != 100:
        raise SyncV2Error("malformed_item")
    terminal_at = datetime.fromisoformat(seal["sealed_at"])
    if (
        datetime.fromisoformat(result["created_at"]) != terminal_at
        or datetime.fromisoformat(run["updated_at"]) != terminal_at
        or datetime.fromisoformat(run["created_at"]) > terminal_at
    ):
        raise SyncV2Error("malformed_item")
    context_hash: str
    is_report = run.get("job_type") == "report_generation"
    context_payload = (
        item.execution_context.model_dump(mode="json")
        if item.execution_context is not None
        else None
    )
    if not is_report and context_payload is not None:
        try:
            context = RunContextV1.model_validate(context_payload["context_json"])
        except ValidationError as error:
            raise SyncV2Error("malformed_item") from error
        if context.project_id != run["project_id"] or context.site_id != run["site_id"]:
            raise SyncV2Error("malformed_item")
        context_hash = context.sha256()
        if context_hash != context_payload["context_sha256"]:
            raise SyncV2Error("malformed_item")
        if item.report_snapshot is not None or item.artifact_manifest is not None:
            raise SyncV2Error("malformed_item")
    elif is_report and item.report_snapshot is not None:
        if item.execution_context is not None or item.artifact_manifest is None:
            raise SyncV2Error("malformed_item")
        context_hash = canonical_sha256(item.report_snapshot)
        if terminal.summary.get("artifact_manifest") != item.artifact_manifest:
            raise SyncV2Error("malformed_item")
        if item.artifact_manifest.get("report_id") != descriptor.run_id:
            raise SyncV2Error("malformed_item")
        if item.artifact_manifest.get("snapshot_sha256") != context_hash:
            raise SyncV2Error("malformed_item")
    else:
        raise SyncV2Error("malformed_item")
    if seal["context_sha256"] != context_hash:
        raise SyncV2Error("malformed_item")
    _validate_wire_parameters(
        run,
        context_hash=context_hash,
        report_snapshot=item.report_snapshot,
        artifact_manifest=item.artifact_manifest,
    )
    _assert_no_secret_material(item.model_dump(mode="json"))
    return item.model_dump(mode="json")


def receipt_dict(
    *,
    receipt_id: str,
    descriptor: SyncV2Descriptor,
    receipt_class: str,
) -> dict[str, object]:
    acknowledged = receipt_class in ACKNOWLEDGED_RECEIPT_CLASSES
    return {
        "receipt_id": receipt_id,
        "item_id": descriptor.item_id,
        "run_id": descriptor.run_id,
        "class": receipt_class,
        "acknowledged": acknowledged,
        "retryable": receipt_class in RETRYABLE_RECEIPT_CLASSES,
    }


def _build_item(
    export: dict[str, Any],
    *,
    artifact_loader: Callable[[dict[str, Any]], bytes],
) -> tuple[dict[str, Any], bytes | None]:
    run = dict(export["run"])
    result = export.get("result")
    seal = export.get("seal")
    if not isinstance(result, dict) or not isinstance(seal, dict):
        raise SyncV2Error(f"Run {run.get('run_id')} is terminal but unsealed.")
    terminal = TerminalResultV1.model_validate(result.get("result_payload"))
    if terminal.sha256() != seal.get("result_sha256"):
        raise SyncV2Error(f"Run {run.get('run_id')} has an incoherent terminal seal.")
    parameters = dict(run.get("parameters") or {})
    is_report = run.get("job_type") == "report_generation"
    safe_parameters = (
        {key: parameters[key] for key in _REPORT_PARAMETER_KEYS if key in parameters}
        if is_report
        else {"context_sha256": seal.get("context_sha256")}
    )
    safe_run = {
        key: value
        for key, value in run.items()
        if key not in {"parameters", "issues"}
    }
    safe_run["parameters"] = safe_parameters
    safe_run["issues"] = list(terminal.issues)
    context = export.get("context")
    report_snapshot = parameters.get("report_snapshot_v2") if is_report else None
    artifact_manifest = terminal.summary.get("artifact_manifest") if is_report else None
    artifact: bytes | None = None
    if is_report:
        if not isinstance(report_snapshot, dict):
            raise SyncV2Error(f"Report run {run.get('run_id')} has no complete frozen snapshot.")
        if not isinstance(artifact_manifest, dict):
            raise SyncV2Error(f"Report run {run.get('run_id')} has no signed artifact manifest.")
        artifact = artifact_loader(artifact_manifest)
        if not artifact:
            raise SyncV2Error(f"Report run {run.get('run_id')} has no artifact bytes.")
    elif not isinstance(context, dict):
        raise SyncV2Error(f"Run {run.get('run_id')} has no frozen execution context.")
    item = {
        "schema_version": PROTOCOL_VERSION,
        "run": safe_run,
        "result": result,
        "seal": seal,
        "execution_context": context if not is_report else None,
        "report_snapshot": report_snapshot,
        "artifact_manifest": artifact_manifest,
    }
    _assert_no_secret_material(item)
    if artifact is not None:
        _assert_no_forbidden_artifact_material(artifact)
    return item, artifact


def _assert_no_secret_material(value: Any, *, key: str | None = None) -> None:
    raw_key = (key or "").strip()
    acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw_key)
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", acronym_split)
    normalized_key = re.sub(r"[^a-z0-9]+", "_", camel_split.casefold()).strip("_")
    compact_key = normalized_key.replace("_", "")
    suffix = normalized_key.rsplit("_", 1)[-1]
    safe_metadata_key = (
        normalized_key not in _SENSITIVE_KEYS
        and suffix in _SAFE_SENSITIVE_KEY_SUFFIXES
    )
    padded_key = f"_{normalized_key}_"
    sensitive_key = not safe_metadata_key and (
        compact_key in _SENSITIVE_COMPACT_KEYS
        or any(f"_{candidate}_" in padded_key for candidate in _SENSITIVE_KEYS)
    )
    if sensitive_key and value not in (None, "", "********"):
        if not _is_secret_reference(value):
            raise SyncV2Error("Sync evidence contains forbidden secret material.")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_no_secret_material(child, key=str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_secret_material(child)
    elif isinstance(value, str):
        if _PRIVATE_KEY_PEM_RE.search(value) or _CERTIFICATE_PEM_RE.search(value):
            raise SyncV2Error("Sync evidence contains forbidden key or certificate material.")


def _is_secret_reference(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("secret://")
    if not isinstance(value, Mapping) or set(value) != {"reference", "version"}:
        return False
    reference = value.get("reference")
    version = value.get("version")
    return (
        isinstance(reference, str)
        and reference.startswith("secret://")
        and isinstance(version, str)
        and bool(version.strip())
    )


def _assert_no_forbidden_artifact_material(
    artifact: bytes,
    *,
    max_archive_members: int = _MAX_ARTIFACT_ARCHIVE_MEMBERS,
    max_archive_uncompressed_bytes: int = _MAX_ARTIFACT_ARCHIVE_UNCOMPRESSED_BYTES,
    max_archive_depth: int = _MAX_ARTIFACT_ARCHIVE_DEPTH,
) -> None:
    """Reject PEM material in raw or compressed report content under hard bounds."""

    if (
        max_archive_members < 1
        or max_archive_uncompressed_bytes < 1
        or max_archive_depth < 1
    ):
        raise ValueError("Artifact archive scan limits must be positive.")
    if len(artifact) > max_archive_uncompressed_bytes:
        raise SyncV2Error("Report artifact exceeds the secret-scan byte limit.")

    scanned_members = 0
    expanded_bytes = 0

    def scan(payload: bytes, *, depth: int) -> None:
        nonlocal expanded_bytes, scanned_members
        text = payload.decode("latin-1")
        if _PRIVATE_KEY_PEM_RE.search(text) or _CERTIFICATE_PEM_RE.search(text):
            raise SyncV2Error(
                "Report artifact contains forbidden private-key or certificate material."
            )
        if not is_zipfile(BytesIO(payload)):
            return
        if depth >= max_archive_depth:
            raise SyncV2Error("Report artifact archive nesting exceeds the scan limit.")
        try:
            with ZipFile(BytesIO(payload)) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise SyncV2Error("Report artifact archive contains duplicate members.")
                for info in infos:
                    scanned_members += 1
                    if scanned_members > max_archive_members:
                        raise SyncV2Error(
                            "Report artifact archive member count exceeds the scan limit."
                        )
                    _validate_artifact_member_name(info.filename)
                    if info.flag_bits & 0x1:
                        raise SyncV2Error(
                            "Encrypted report artifact members are not supported."
                        )
                    expanded_bytes += info.file_size
                    if expanded_bytes > max_archive_uncompressed_bytes:
                        raise SyncV2Error(
                            "Report artifact archive expands beyond the scan limit."
                        )
                    if info.is_dir():
                        continue
                    child = archive.read(info)
                    if len(child) != info.file_size:
                        raise SyncV2Error("Report artifact member size is inconsistent.")
                    scan(child, depth=depth + 1)
        except SyncV2Error:
            raise
        except (BadZipFile, KeyError, NotImplementedError, OSError, RuntimeError, ValueError) as error:
            raise SyncV2Error("Report artifact archive could not be scanned safely.") from error

    scan(artifact, depth=0)


def _validate_artifact_member_name(name: str) -> None:
    trimmed = name.rstrip("/")
    path = PurePosixPath(trimmed)
    if (
        not trimmed
        or "\\" in name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise SyncV2Error("Report artifact archive contains an unsafe member path.")


def _signed_manifest_body(manifest: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(
        {
            key: value
            for key, value in manifest.items()
            if key not in {"signature", "signed_manifest_sha256"}
        }
    )


def _bundle_id(manifest: Mapping[str, object]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                key: value
                for key, value in manifest.items()
                if key not in {"bundle_id", "signature", "signed_manifest_sha256"}
            }
        )
    )


def _fingerprint_from_pem(pem: str) -> str | None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        loaded = serialization.load_pem_public_key(pem.encode("ascii"))
        if not isinstance(loaded, Ed25519PublicKey):
            return None
        raw = loaded.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return public_key_fingerprint(raw)
    except (TypeError, ValueError, UnicodeEncodeError):
        return None


def _validate_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise SyncV2Error("Bundle contains an unsafe member path.")
    if name != _MANIFEST_MEMBER and not (
        name.startswith("items/") or name.startswith("artifacts/sha256/")
    ):
        raise SyncV2Error("Bundle contains an unsupported member path.")


def _write_zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, payload in sorted(members, key=lambda item: item[0]):
            info = ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()
