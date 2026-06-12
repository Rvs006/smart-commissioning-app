"""Edge->hub synchronization: build + ingest signed, immutable run bundles.

This is the transport-agnostic core of the local-first / central-hub
architecture. On-site *edge* instances produce immutable, signed run+evidence
records; a central *hub* aggregates them across projects/sites. The edge builds
a ``.scbundle`` (a zip) of terminal runs and signs the manifest with its edge
signing key; the hub verifies trust + signature + per-run hashes and ingests
each run IMMUTABLY (insert-or-skip; never overwrite).

No HTTP, no network, no Postgres here — :func:`build_sync_bundle` returns bytes
and :func:`ingest_sync_bundle` consumes bytes. A caller may write the bytes to a
``.scbundle`` file (offline transfer) or POST them over HTTP; the core does not
care. The whole round-trip is provable in-process across two SQLite engines.

Bundle layout (zip)::

    manifest.json          # metadata + detached Ed25519 signature
    runs/<run_id>.json     # canonical JSON of one run's full content

The manifest pins, per run, the SHA-256 of its canonical content member, plus
the edge id and public key, and a detached signature over the canonical manifest
body. Determinism: given identical inputs the produced bytes are reproducible
(stable key order, fixed zip member timestamps, canonical JSON).

Honesty / infra boundary:
  * Round-trip is exercised in-process across two temp SQLite DBs and via a
    FastAPI TestClient that just shuttles the bytes. See core/tests/test_sync.py.
  * A real remote hub (HTTP transport, a Postgres-backed hub) is NOT implemented
    here — only the byte producer/consumer is. Those paths are live_untested.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from sqlalchemy.engine import Engine

from smart_commissioning_core import __version__ as core_version
from smart_commissioning_core.db.repositories import (
    SyncRepository,
)
from smart_commissioning_core.integrity import (
    SigningKey,
    cryptography_available,
    public_key_fingerprint,
    sha256_bytes,
    verify_bytes,
)
from smart_commissioning_core.sync_identity import EdgeIdentity

__all__ = [
    "BUNDLE_FORMAT_VERSION",
    "SCHEMA_VERSION",
    "IngestSummary",
    "SyncError",
    "build_run_content",
    "build_sync_bundle",
    "ingest_sync_bundle",
    "read_manifest",
]

# Top-level on-disk format version of the .scbundle zip layout.
BUNDLE_FORMAT_VERSION = 1
# Schema version of the per-run content payload + manifest content map.
SCHEMA_VERSION = 1

_MANIFEST_MEMBER = "manifest.json"
_RUNS_PREFIX = "runs/"
# Fixed zip member timestamp so identical inputs yield reproducible bytes.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# Manifest fields populated *by* signing; excluded from the signed body so the
# canonical bytes are identical at sign time and verify time (mirrors
# backup_service._SIGNATURE_FIELDS).
_SIGNATURE_FIELDS = ("signature",)


class SyncError(RuntimeError):
    """Bundle build/ingest failure (missing run, unsupported state, parse error)."""


# ---------------------------------------------------------------------------
# Canonical run content
# ---------------------------------------------------------------------------


def build_run_content(export: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical, stable-ordered content dict for one run.

    ``export`` is a ``SyncRepository.get_run_for_export`` payload. The returned
    dict is what gets canonical-JSON-encoded into a ``runs/<run_id>.json`` member
    and hashed; its key order and contents are stable so the hash is reproducible
    and comparable across edge/hub.

    IMPORTANT: ``edge_id`` is deliberately NOT part of the hashed content. It is
    provenance metadata (NULL on the edge, stamped on the hub at ingest), so
    including it would make the hub's recomputed hash differ from the edge's and
    break idempotency. The edge_id travels in the manifest instead; the immutable
    *content* is identical on both sides.

    Discovery rows are normalized to their content fields only: the per-DB
    bookkeeping columns (``id``, ``run_id``, ``created_at``) are stripped because
    they are server-assigned and differ between the edge and the hub — keeping
    them would break the content hash equality that idempotency/immutability
    depend on.
    """
    run = export["run"]
    return {
        "schema_version": SCHEMA_VERSION,
        "run": run,  # the 13-key file record (incl. result_summary['integrity'])
        "issues": list(export.get("issues") or []),
        "devices": [_normalize_discovery_row(d) for d in (export.get("devices") or [])],
        "points": [_normalize_discovery_row(p) for p in (export.get("points") or [])],
        "topics": [_normalize_discovery_row(t) for t in (export.get("topics") or [])],
    }


# DB-assigned bookkeeping fields excluded from a discovery row's hashed content.
_DISCOVERY_VOLATILE_FIELDS = ("id", "run_id", "created_at")


def _normalize_discovery_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop server-assigned bookkeeping fields so edge/hub content hashes match."""
    return {key: value for key, value in row.items() if key not in _DISCOVERY_VOLATILE_FIELDS}


def _canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, compact separators)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_manifest_body(manifest: dict[str, Any]) -> bytes:
    """Deterministic JSON of the manifest body the signature covers."""
    body = {key: value for key, value in manifest.items() if key not in _SIGNATURE_FIELDS}
    return _canonical_json(body)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_sync_bundle(
    engine: Engine,
    *,
    run_ids: list[str] | None = None,
    since_watermark: bool | None = None,
    signing_key: SigningKey,
    edge_identity: EdgeIdentity,
    created_at: datetime,
    include_reports: bool = False,
) -> bytes:
    """Build a signed ``.scbundle`` of TERMINAL runs and return its zip bytes.

    Selection (exactly one source of run ids):
      * ``run_ids`` — an explicit list. Each must exist AND be terminal; an
        in-flight or missing run raises :class:`SyncError` (in-flight runs are
        NEVER bundled).
      * else ``since_watermark=True`` (or ``run_ids=None``) — every terminal run
        with ``synced_at IS NULL`` (the un-synced watermark set), oldest-first.

    For each run the full content is gathered: the 13-key run record (incl.
    ``result_summary['integrity']``), issues, and discovery devices/points/
    topics. Each is encoded as canonical JSON (stable key order) so the per-run
    SHA-256 is reproducible.

    The manifest records ``schema_version``, the edge id + public key PEM +
    fingerprint (from ``edge_identity``), the caller-supplied ``created_at``, the
    ordered ``run_ids``, a per-run content sha256 map, and the bundle format
    version. The manifest body is signed (detached Ed25519) with ``signing_key``
    and the base64 signature embedded.

    Pure/deterministic given its inputs (no clock, no network). ``include_reports``
    is accepted for forward-compatibility: signed report artifacts already live
    inside ``result_summary['integrity']`` of the run record and travel with it,
    so no separate member is emitted today (flag is recorded in the manifest).
    """
    repository = SyncRepository(engine)

    selected = _select_run_ids(repository, run_ids=run_ids, since_watermark=since_watermark)

    members: list[tuple[str, bytes]] = []
    content_hashes: dict[str, str] = {}
    for run_id in selected:
        export = repository.get_run_for_export(run_id)
        if export is None:  # pragma: no cover - guarded by _select_run_ids
            raise SyncError(f"Run not found while building bundle: {run_id}")
        content_bytes = _canonical_json(build_run_content(export))
        member_name = f"{_RUNS_PREFIX}{run_id}.json"
        members.append((member_name, content_bytes))
        content_hashes[run_id] = sha256_bytes(content_bytes)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bundle_format_version": BUNDLE_FORMAT_VERSION,
        "core_version": core_version,
        "edge_id": edge_identity.edge_id,
        "edge_public_key_pem": edge_identity.public_key_pem,
        "edge_public_key_fingerprint": edge_identity.public_key_fingerprint,
        "created_at": created_at.isoformat(),
        "run_ids": list(selected),
        "include_reports": bool(include_reports),
        "content": {run_id: content_hashes[run_id] for run_id in selected},
        "signature_algorithm": "ed25519",
        "signature": None,
    }

    signature = signing_key.sign(_canonical_manifest_body(manifest))
    manifest["signature"] = base64.b64encode(signature).decode("ascii")

    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    return _write_zip([*members, (_MANIFEST_MEMBER, manifest_bytes)])


def _select_run_ids(
    repository: SyncRepository,
    *,
    run_ids: list[str] | None,
    since_watermark: bool | None,
) -> list[str]:
    """Resolve and validate the ordered run ids to bundle.

    Explicit ``run_ids`` are validated to exist and be terminal (in-flight or
    missing -> SyncError). Otherwise the un-synced terminal watermark set is
    used. Duplicate ids in an explicit list are de-duplicated preserving order.
    """
    if run_ids is not None:
        seen: set[str] = set()
        ordered: list[str] = []
        for run_id in run_ids:
            if run_id in seen:
                continue
            seen.add(run_id)
            export = repository.get_run_for_export(run_id)
            if export is None:
                raise SyncError(f"Cannot bundle missing run: {run_id}")
            status = export["run"].get("status")
            if status not in _terminal_statuses():
                raise SyncError(
                    f"Refusing to bundle non-terminal run {run_id!r} (status={status!r}); "
                    "only terminal runs sync."
                )
            ordered.append(run_id)
        return ordered
    return repository.list_unsynced_terminal_runs()


def _terminal_statuses() -> tuple[str, ...]:
    from smart_commissioning_core.db.repositories import TERMINAL_RUN_STATUSES

    return TERMINAL_RUN_STATUSES


def _write_zip(members: list[tuple[str, bytes]]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, payload in sorted(members, key=lambda item: item[0]):
            info = ZipInfo(filename=name, date_time=_ZIP_EPOCH)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@dataclass
class IngestSummary:
    """Outcome of ingesting one bundle into a hub.

    Trust failures (untrusted edge, key mismatch, bad signature) are all-or-
    nothing for the whole bundle: ``accepted`` is False and the per-run counters
    stay zero (nothing is written). Per-run outcomes (when the bundle is trusted)
    populate the counters and the id lists.
    """

    accepted: bool = False
    # All-or-nothing bundle-level rejection reason (None when accepted).
    rejected_reason: str | None = None
    edge_id: str | None = None

    inserted: int = 0
    skipped_identical: int = 0
    rejected_immutable: int = 0
    # Per-run hash mismatch (member bytes don't match the manifest). When this
    # fires the whole bundle is rejected (a tampered bundle writes nothing).
    rejected_bad_hash: int = 0
    # Bundle-level trust counters (0/1 — a bundle has a single edge identity).
    rejected_untrusted: int = 0
    rejected_bad_signature: int = 0

    inserted_run_ids: list[str] = field(default_factory=list)
    skipped_run_ids: list[str] = field(default_factory=list)
    rejected_immutable_run_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe summary (for an API response / logging)."""
        return {
            "accepted": self.accepted,
            "rejected_reason": self.rejected_reason,
            "edge_id": self.edge_id,
            "inserted": self.inserted,
            "skipped_identical": self.skipped_identical,
            "rejected_immutable": self.rejected_immutable,
            "rejected_bad_hash": self.rejected_bad_hash,
            "rejected_untrusted": self.rejected_untrusted,
            "rejected_bad_signature": self.rejected_bad_signature,
            "inserted_run_ids": list(self.inserted_run_ids),
            "skipped_run_ids": list(self.skipped_run_ids),
            "rejected_immutable_run_ids": list(self.rejected_immutable_run_ids),
        }


def read_manifest(bundle_bytes: bytes) -> dict[str, Any]:
    """Parse and return the manifest dict from a bundle (no verification)."""
    with ZipFile(BytesIO(bundle_bytes)) as archive:
        if _MANIFEST_MEMBER not in set(archive.namelist()):
            raise SyncError("Bundle is missing manifest.json.")
        return json.loads(archive.read(_MANIFEST_MEMBER).decode("utf-8"))


def ingest_sync_bundle(
    engine: Engine,
    bundle_bytes: bytes,
    *,
    trusted_edges: dict[str, str],
    now: datetime,
) -> IngestSummary:
    """Verify trust + signature + hashes, then immutably ingest a bundle.

    ``trusted_edges`` maps ``edge_id -> expected public key fingerprint OR PEM``.
    A bundle is accepted only when its edge is known and its embedded public key
    matches the trusted material for that edge.

    FAIL CLOSED, in order. Steps (a)-(d) are all-or-nothing for the bundle —
    nothing is written if any fails:

      (a) parse the manifest;
      (b) reject if ``edge_id`` not in ``trusted_edges`` OR the manifest's
          embedded public key fingerprint != the trusted fingerprint
          (untrusted / forged key) -> ``rejected_untrusted``;
      (c) verify the manifest's detached signature against the (now-trusted)
          embedded public key -> ``rejected_bad_signature`` on failure;
      (d) verify each run member's sha256 matches the manifest -> reject the
          whole bundle (``rejected_bad_hash``) on any mismatch.

    Step (e) is PER-RUN immutable upsert (only reached when a-d pass):
      * run id absent on the hub -> INSERT (run + issues + discovery), stamping
        ``edge_id`` from the manifest -> ``inserted``;
      * run id present AND content hash identical -> SKIP (idempotent no-op) ->
        ``skipped_identical``;
      * run id present AND content differs -> REJECT that run as an immutability
        violation (do NOT overwrite) -> ``rejected_immutable``.
    """
    summary = IngestSummary()

    # (a) parse
    try:
        manifest = read_manifest(bundle_bytes)
    except (SyncError, ValueError, KeyError) as exc:
        summary.rejected_reason = f"unparseable_bundle: {exc}"
        return summary

    edge_id = manifest.get("edge_id")
    summary.edge_id = edge_id
    embedded_pem = manifest.get("edge_public_key_pem")
    embedded_fingerprint = manifest.get("edge_public_key_fingerprint")

    # (b) trust: known edge + key matches the pinned trust material.
    if not _edge_is_trusted(edge_id, embedded_pem, embedded_fingerprint, trusted_edges):
        summary.rejected_untrusted = 1
        summary.rejected_reason = "untrusted_edge_or_key"
        return summary

    if not cryptography_available():  # pragma: no cover - crypto present in tests
        summary.rejected_reason = "cryptography_unavailable"
        return summary

    # (c) signature over the canonical manifest body, using the now-trusted key.
    signature_b64 = manifest.get("signature")
    if not signature_b64 or not embedded_pem:
        summary.rejected_bad_signature = 1
        summary.rejected_reason = "missing_signature"
        return summary
    try:
        signature = base64.b64decode(signature_b64)
    except (ValueError, TypeError):
        summary.rejected_bad_signature = 1
        summary.rejected_reason = "bad_signature_encoding"
        return summary
    if not verify_bytes(_canonical_manifest_body(manifest), signature, embedded_pem):
        summary.rejected_bad_signature = 1
        summary.rejected_reason = "bad_signature"
        return summary

    # (d) per-run member hashes must match the manifest content map BEFORE any
    # write. A single mismatch rejects the whole bundle (tampered -> nothing).
    declared = manifest.get("content")
    run_ids = manifest.get("run_ids")
    if not isinstance(declared, dict) or not isinstance(run_ids, list):
        summary.rejected_reason = "malformed_manifest"
        return summary

    contents: dict[str, bytes] = {}
    with ZipFile(BytesIO(bundle_bytes)) as archive:
        names = set(archive.namelist())
        for run_id in run_ids:
            member = f"{_RUNS_PREFIX}{run_id}.json"
            expected_hash = declared.get(run_id)
            if member not in names or not expected_hash:
                summary.rejected_bad_hash = 1
                summary.rejected_reason = f"missing_member: {run_id}"
                return summary
            raw = archive.read(member)
            if sha256_bytes(raw) != expected_hash:
                summary.rejected_bad_hash = 1
                summary.rejected_reason = f"member_hash_mismatch: {run_id}"
                return summary
            contents[run_id] = raw

    # (e) per-run immutable upsert. Bundle is trusted + intact past this point.
    summary.accepted = True
    repository = SyncRepository(engine)
    for run_id in run_ids:
        content = json.loads(contents[run_id].decode("utf-8"))
        _ingest_one_run(repository, run_id, content, edge_id, summary)
    return summary


def _ingest_one_run(
    repository: SyncRepository,
    run_id: str,
    content: dict[str, Any],
    edge_id: str | None,
    summary: IngestSummary,
) -> None:
    """Insert / skip / reject one run per the immutability rule."""
    existing = repository.get_run_for_export(run_id)
    if existing is not None:
        existing_hash = sha256_bytes(_canonical_json(build_run_content(existing)))
        incoming_hash = sha256_bytes(_canonical_json(content))
        if existing_hash == incoming_hash:
            summary.skipped_identical += 1
            summary.skipped_run_ids.append(run_id)
        else:
            # Same run id but different content: immutability violation. Never
            # overwrite the hub copy.
            summary.rejected_immutable += 1
            summary.rejected_immutable_run_ids.append(run_id)
        return

    repository.insert_run_record(
        run=content["run"],
        issues=list(content.get("issues") or []),
        devices=list(content.get("devices") or []),
        points=list(content.get("points") or []),
        topics=list(content.get("topics") or []),
        edge_id=edge_id,
    )
    summary.inserted += 1
    summary.inserted_run_ids.append(run_id)


def _edge_is_trusted(
    edge_id: object,
    embedded_pem: object,
    embedded_fingerprint: object,
    trusted_edges: dict[str, str],
) -> bool:
    """True iff ``edge_id`` is known and its key matches the pinned trust value.

    The trust value may be a fingerprint (16 hex chars) or a full PEM. The
    embedded key is reduced to its authoritative fingerprint from the PEM itself
    (never trusting the manifest's self-reported fingerprint string) so a forged
    ``edge_public_key_fingerprint`` cannot mask a swapped key.
    """
    if not isinstance(edge_id, str) or edge_id not in trusted_edges:
        return False
    expected = trusted_edges[edge_id]
    if not embedded_pem or not isinstance(embedded_pem, str):
        return False

    actual_fingerprint = _fingerprint_from_pem(embedded_pem)
    if actual_fingerprint is None:
        return False

    expected_fingerprint = _coerce_fingerprint(expected)
    if expected_fingerprint is None:
        return False
    return actual_fingerprint == expected_fingerprint


def _coerce_fingerprint(trust_value: str) -> str | None:
    """Reduce a trust value (fingerprint or PEM) to a 16-char fingerprint."""
    value = trust_value.strip()
    if "BEGIN PUBLIC KEY" in value:
        return _fingerprint_from_pem(value)
    return value or None


def _fingerprint_from_pem(pem: str) -> str | None:
    """Authoritative fingerprint derived from a PEM's raw public key bytes."""
    if not cryptography_available():  # pragma: no cover - crypto present in tests
        return None
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
    except (ValueError, TypeError):
        return None
