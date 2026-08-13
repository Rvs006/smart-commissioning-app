"""ORM models for runs, issues, configuration snapshots, and imports.

The Run/RunIssue serialization (see db_run_store) mirrors the JSON file records
produced today by backend RunService / worker FileRunStore so API responses do
not change when the database becomes the source of truth.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smart_commissioning_core.db.base import Base, UTCDateTime, utcnow

RUN_DISCOVERY_OBSERVATION_PROTOCOL_MAX_LENGTH = 16
RUN_DISCOVERY_OBSERVATION_ENTITY_KIND_MAX_LENGTH = 32
RUN_DISCOVERY_OBSERVATION_ENTITY_KEY_MAX_LENGTH = 255
RUN_DISCOVERY_OBSERVATION_EVENT_KEY_MAX_LENGTH = 255
RUN_DISCOVERY_OBSERVATION_PHASE_MAX_LENGTH = 32
RUN_DISCOVERY_OBSERVATION_OUTCOME_MAX_LENGTH = 64
RUN_DISCOVERY_OBSERVATION_PAYLOAD_SCHEMA_VERSION_MAX_LENGTH = 32
RUN_DISCOVERY_OBSERVATION_PAYLOAD_MAX_BYTES = 65_536
# Portable SQLite/Postgres CHECK expressions count Unicode characters. The
# append boundary separately applies the byte ceiling above to canonical UTF-8.
RUN_DISCOVERY_OBSERVATION_PAYLOAD_MAX_CHARS = 65_536
RUN_DISCOVERY_OBSERVATION_STATE_MAX_ROWS = 50_000
RUN_DISCOVERY_OBSERVATION_STATE_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024

RUN_RETENTION_HOLD_ID_MAX_LENGTH = 64
RUN_RETENTION_HOLD_SCOPE_MAX_LENGTH = 255
RUN_RETENTION_HOLD_EVIDENCE_SET_ID_MAX_LENGTH = 128
RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH = 255
RUN_RETENTION_HOLD_REASON_MAX_LENGTH = 4_096

OBSERVATION_RETENTION_JOB_ID_MAX_LENGTH = 64
OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH = 255
OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH = 255
OBSERVATION_RETENTION_JOB_ERROR_CODE_MAX_LENGTH = 128

NMAP_AUTHORITY_ID_MAX_LENGTH = 64
NMAP_DEPLOYMENT_ID_MAX_LENGTH = 255
NMAP_AUDIT_ACTOR_MAX_LENGTH = 255
NMAP_AUDIT_REASON_MAX_LENGTH = 4_096
NMAP_POLICY_TEXT_MAX_LENGTH = 4_096
NMAP_POLICY_JSON_MAX_LENGTH = 32_768
NMAP_PROTECTED_PATH_MAX_LENGTH = 2_048

RAW_EVIDENCE_ARTIFACT_ID_MAX_LENGTH = 64
RAW_EVIDENCE_SCOPE_MAX_LENGTH = 255
RAW_EVIDENCE_TYPE_MAX_LENGTH = 64
RAW_EVIDENCE_MEDIA_TYPE_MAX_LENGTH = 255
RAW_EVIDENCE_STORAGE_KEY_MAX_LENGTH = 512
RAW_EVIDENCE_EXECUTOR_ID_MAX_LENGTH = 255
RAW_EVIDENCE_AUDIT_ID_MAX_LENGTH = 64


def _lowercase_hex_check(column_name: str, length: int) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = {length} AND "
        f"{column_name} = lower({column_name}) AND length({remainder}) = 0"
    )


def _lowercase_sha256_check(column_name: str) -> str:
    return _lowercase_hex_check(column_name, 64)


class User(Base):
    """A per-user identity for role-based access control.

    Each user authenticates with their own API key. Only a HASH of that key is
    stored (``api_key_hash`` — hashlib.sha256 hex digest); the raw key is shown
    exactly ONCE at creation time and never persisted. ``role`` is one of the
    ordered RBAC roles (smart_commissioning_core.rbac.Role) stored as its string
    value. This coexists with the legacy shared settings.api_key, which acts as a
    synthetic bootstrap-admin so an operator can create the first user.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32))
    # SHA-256 hex digest of the per-user key. Unique so a key resolves to exactly
    # one user; indexed because every authenticated request looks a user up by it.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Stamped on each successful authentication (best-effort; nullable until first use).
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Project(Base):
    """Minimal project row; auto-created on first reference (get-or-create)."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Site(Base):
    """Minimal site row; auto-created on first reference (get-or-create)."""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Run(Base):
    """A job run; mirrors the JSON file record shape used by the v1 API."""

    __tablename__ = "runs"

    # Existing run_YYYYMMDDHHMMSS_hex identifiers are kept as the primary key.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"))
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(128))
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cooperative-cancellation flag: set by request_cancel; engines poll it via
    # is_cancel_requested and stop early. Default False so existing rows / the
    # create path keep working unchanged.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Edge->hub sync columns (Phase: sync core). Both nullable and kept OUT of
    # the public _run_to_dict record (like cancel_requested) so the 13-key file
    # record contract is unchanged; exposed via DbRunStore/SyncRepository
    # accessors instead.
    #   edge_id    — the originating edge. NULL on a local edge run; stamped from
    #                the bundle manifest on hub ingest so the hub knows the source.
    #   synced_at  — when THIS instance last pushed the run (the edge watermark).
    #                NULL means "never synced from here / un-synced".
    edge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Lifecycle-v2 fencing. Nullable ownership timestamps keep legacy rows
    # readable; ``attempt`` and ``state_version`` have additive zero defaults.
    owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    issues: Mapped[list["RunIssue"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunIssue.position",
    )

    __table_args__ = (
        Index("ix_runs_project_site_created", "project_id", "site_id", "created_at"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_lease_expiry", "status", "lease_expires_at"),
    )


class RunDiscoveryObservation(Base):
    """One append-only provisional discovery event for a fenced run attempt."""

    __tablename__ = "run_discovery_observations"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_PROTOCOL_MAX_LENGTH), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_ENTITY_KIND_MAX_LENGTH), nullable=False
    )
    entity_key: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_ENTITY_KEY_MAX_LENGTH), nullable=False
    )
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_key: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_EVENT_KEY_MAX_LENGTH), nullable=False
    )
    phase: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_PHASE_MAX_LENGTH), nullable=False
    )
    outcome: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_OUTCOME_MAX_LENGTH), nullable=False
    )
    payload_schema_version: Mapped[str] = mapped_column(
        String(RUN_DISCOVERY_OBSERVATION_PAYLOAD_SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JSON(none_as_null=True), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "attempt > 0",
            name="ck_run_discovery_observations_attempt_positive",
        ),
        CheckConstraint(
            "entity_version > 0",
            name="ck_run_discovery_observations_entity_version_positive",
        ),
        CheckConstraint(
            "protocol IN ('ip', 'bacnet')",
            name="ck_run_discovery_observations_protocol",
        ),
        CheckConstraint(
            "entity_kind IN "
            "('lane', 'host', 'port', 'device', 'object', 'property', 'diagnostic')",
            name="ck_run_discovery_observations_entity_kind",
        ),
        CheckConstraint(
            "phase IN "
            "('planned', 'reachability', 'enrichment', 'comparison', 'finalize')",
            name="ck_run_discovery_observations_phase",
        ),
        CheckConstraint(
            "length(entity_key) BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_ENTITY_KEY_MAX_LENGTH}",
            name="ck_run_discovery_observations_entity_key_length",
        ),
        CheckConstraint(
            "length(event_key) BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_EVENT_KEY_MAX_LENGTH}",
            name="ck_run_discovery_observations_event_key_length",
        ),
        CheckConstraint(
            "length(outcome) BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_OUTCOME_MAX_LENGTH}",
            name="ck_run_discovery_observations_outcome_length",
        ),
        CheckConstraint(
            "length(payload_schema_version) BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_PAYLOAD_SCHEMA_VERSION_MAX_LENGTH}",
            name="ck_run_discovery_observations_payload_schema_version_length",
        ),
        CheckConstraint(
            "length(CAST(payload AS TEXT)) <= "
            f"{RUN_DISCOVERY_OBSERVATION_PAYLOAD_MAX_CHARS}",
            name="ck_run_discovery_observations_payload_length",
        ),
        CheckConstraint(
            _lowercase_sha256_check("payload_sha256"),
            name="ck_run_discovery_observations_payload_sha256",
        ),
        UniqueConstraint(
            "run_id",
            "attempt",
            "event_key",
            name="uq_run_discovery_observations_event",
        ),
        UniqueConstraint(
            "run_id",
            "attempt",
            "entity_kind",
            "entity_key",
            "entity_version",
            name="uq_run_discovery_observations_entity_version",
        ),
        Index(
            "ix_run_discovery_observations_page",
            "run_id",
            "attempt",
            "id",
        ),
        Index(
            "ix_run_discovery_observations_fold",
            "run_id",
            "attempt",
            "entity_kind",
            "entity_key",
            "entity_version",
            "id",
        ),
        Index(
            "ix_run_discovery_observations_retention",
            "created_at",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )


class RunDiscoveryObservationState(Base):
    """Lifecycle-owned bounds and commitment for one accepted attempt prefix."""

    __tablename__ = "run_discovery_observation_states"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canonical_payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    terminal_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_stream_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("attempt > 0", name="attempt_positive"),
        CheckConstraint(
            "observation_count BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_STATE_MAX_ROWS}",
            name="count_bounded",
        ),
        CheckConstraint(
            "canonical_payload_bytes BETWEEN 1 AND "
            f"{RUN_DISCOVERY_OBSERVATION_STATE_MAX_PAYLOAD_BYTES}",
            name="payload_bytes_bounded",
        ),
        CheckConstraint(
            "terminal_cursor > 0",
            name="terminal_cursor_positive",
        ),
        CheckConstraint(
            _lowercase_sha256_check("observation_stream_sha256"),
            name="stream_sha256",
        ),
    )


class RawEvidenceArtifact(Base):
    """Immutable run-owned metadata for exact protected evidence bytes.

    Filesystem locations are private implementation data. Callers and reports
    use ``artifact_id`` plus the immutable digest and never receive a path.
    """

    __tablename__ = "raw_evidence_artifacts"

    artifact_id: Mapped[str] = mapped_column(
        String(RAW_EVIDENCE_ARTIFACT_ID_MAX_LENGTH), primary_key=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(RAW_EVIDENCE_SCOPE_MAX_LENGTH), index=True
    )
    site_id: Mapped[str] = mapped_column(String(RAW_EVIDENCE_SCOPE_MAX_LENGTH), index=True)
    artifact_type: Mapped[str] = mapped_column(String(RAW_EVIDENCE_TYPE_MAX_LENGTH))
    media_type: Mapped[str] = mapped_column(String(RAW_EVIDENCE_MEDIA_TYPE_MAX_LENGTH))
    storage_relpath: Mapped[str] = mapped_column(
        String(RAW_EVIDENCE_STORAGE_KEY_MAX_LENGTH), unique=True
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    capture_complete: Mapped[bool] = mapped_column(Boolean)
    producer_executor_id: Mapped[str] = mapped_column(
        String(RAW_EVIDENCE_EXECUTOR_ID_MAX_LENGTH), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    sealed_at: Mapped[datetime] = mapped_column(UTCDateTime())

    __table_args__ = (
        CheckConstraint(
            "substr(artifact_id, 1, 9) = 'artifact_' AND "
            + _lowercase_hex_check("substr(artifact_id, 10)", 32),
            name="ck_raw_evidence_artifacts_opaque_id",
        ),
        CheckConstraint(
            f"length(project_id) BETWEEN 1 AND {RAW_EVIDENCE_SCOPE_MAX_LENGTH} AND "
            f"length(site_id) BETWEEN 1 AND {RAW_EVIDENCE_SCOPE_MAX_LENGTH}",
            name="ck_raw_evidence_artifacts_scope_length",
        ),
        CheckConstraint(
            f"length(artifact_type) BETWEEN 1 AND {RAW_EVIDENCE_TYPE_MAX_LENGTH} AND "
            f"length(media_type) BETWEEN 3 AND {RAW_EVIDENCE_MEDIA_TYPE_MAX_LENGTH}",
            name="ck_raw_evidence_artifacts_type_media_length",
        ),
        CheckConstraint(
            f"length(storage_relpath) BETWEEN 1 AND {RAW_EVIDENCE_STORAGE_KEY_MAX_LENGTH} AND "
            "storage_relpath LIKE 'objects/%' AND "
            "storage_relpath NOT LIKE '%..%'",
            name="ck_raw_evidence_artifacts_private_storage_key",
        ),
        CheckConstraint(
            _lowercase_sha256_check("sha256"),
            name="ck_raw_evidence_artifacts_sha256",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 0 AND 67108864",
            name="ck_raw_evidence_artifacts_size",
        ),
        CheckConstraint(
            "capture_complete IN (false, true)",
            name="ck_raw_evidence_artifacts_capture_complete",
        ),
        CheckConstraint(
            f"length(producer_executor_id) BETWEEN 1 AND {RAW_EVIDENCE_EXECUTOR_ID_MAX_LENGTH}",
            name="ck_raw_evidence_artifacts_executor_id",
        ),
        CheckConstraint(
            "sealed_at >= created_at",
            name="ck_raw_evidence_artifacts_timestamps",
        ),
        UniqueConstraint(
            "artifact_id",
            "run_id",
            "project_id",
            "site_id",
            name="uq_raw_evidence_artifacts_owner",
        ),
        Index(
            "ix_raw_evidence_artifacts_scope_run",
            "project_id",
            "site_id",
            "run_id",
            "sealed_at",
        ),
    )


class RawEvidenceDownloadAudit(Base):
    """Append-only audit of one verified project/site-scoped raw download."""

    __tablename__ = "raw_evidence_download_audits"

    audit_id: Mapped[str] = mapped_column(
        String(RAW_EVIDENCE_AUDIT_ID_MAX_LENGTH), primary_key=True
    )
    artifact_id: Mapped[str] = mapped_column(String(RAW_EVIDENCE_ARTIFACT_ID_MAX_LENGTH))
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(RAW_EVIDENCE_SCOPE_MAX_LENGTH), index=True)
    site_id: Mapped[str] = mapped_column(String(RAW_EVIDENCE_SCOPE_MAX_LENGTH), index=True)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    downloaded_by: Mapped[str] = mapped_column(String(RAW_EVIDENCE_EXECUTOR_ID_MAX_LENGTH))
    downloaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            f"length(audit_id) BETWEEN 1 AND {RAW_EVIDENCE_AUDIT_ID_MAX_LENGTH} AND "
            f"length(downloaded_by) BETWEEN 1 AND {RAW_EVIDENCE_EXECUTOR_ID_MAX_LENGTH}",
            name="ck_raw_evidence_download_audits_identifiers",
        ),
        CheckConstraint(
            _lowercase_sha256_check("artifact_sha256"),
            name="ck_raw_evidence_download_audits_sha256",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 0 AND 67108864",
            name="ck_raw_evidence_download_audits_size",
        ),
        Index(
            "ix_raw_evidence_download_audits_scope",
            "project_id",
            "site_id",
            "run_id",
            "downloaded_at",
        ),
    )


class RunRetentionHold(Base):
    """An audited legal or evidence hold that blocks run retention."""

    __tablename__ = "run_retention_holds"

    hold_id: Mapped[str] = mapped_column(
        String(RUN_RETENTION_HOLD_ID_MAX_LENGTH), primary_key=True
    )
    # Deliberately not an FK: a released hold is immutable audit history and
    # must survive deletion of the run it once protected.  The service resolves
    # the run and derives its scope before inserting an active hold.
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(RUN_RETENTION_HOLD_SCOPE_MAX_LENGTH), nullable=False
    )
    site_id: Mapped[str] = mapped_column(
        String(RUN_RETENTION_HOLD_SCOPE_MAX_LENGTH), nullable=False
    )
    hold_type: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_set_id: Mapped[str | None] = mapped_column(
        String(RUN_RETENTION_HOLD_EVIDENCE_SET_ID_MAX_LENGTH), nullable=True
    )
    active_marker: Mapped[bool | None] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=True
    )
    placed_by: Mapped[str] = mapped_column(
        String(RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    released_by: Mapped[str | None] = mapped_column(
        String(RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH), nullable=True
    )
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(hold_id) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_ID_MAX_LENGTH}",
            name="ck_run_retention_holds_hold_id_length",
        ),
        CheckConstraint(
            "length(project_id) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_SCOPE_MAX_LENGTH}",
            name="ck_run_retention_holds_project_id_length",
        ),
        CheckConstraint(
            "length(site_id) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_SCOPE_MAX_LENGTH}",
            name="ck_run_retention_holds_site_id_length",
        ),
        CheckConstraint(
            "hold_type IN ('legal', 'evidence')",
            name="ck_run_retention_holds_hold_type",
        ),
        CheckConstraint(
            "evidence_set_id IS NULL OR "
            "length(evidence_set_id) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_EVIDENCE_SET_ID_MAX_LENGTH}",
            name="ck_run_retention_holds_evidence_set_id_length",
        ),
        CheckConstraint(
            "(hold_type = 'evidence' AND evidence_set_id IS NOT NULL) OR "
            "(hold_type = 'legal' AND evidence_set_id IS NULL)",
            name="ck_run_retention_holds_evidence_set_for_type",
        ),
        CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name="ck_run_retention_holds_active_marker",
        ),
        CheckConstraint(
            "length(placed_by) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH}",
            name="ck_run_retention_holds_placed_by_length",
        ),
        CheckConstraint(
            "length(reason) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_REASON_MAX_LENGTH}",
            name="ck_run_retention_holds_reason_length",
        ),
        CheckConstraint(
            "released_by IS NULL OR length(released_by) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_ACTOR_MAX_LENGTH}",
            name="ck_run_retention_holds_released_by_length",
        ),
        CheckConstraint(
            "release_reason IS NULL OR length(release_reason) BETWEEN 1 AND "
            f"{RUN_RETENTION_HOLD_REASON_MAX_LENGTH}",
            name="ck_run_retention_holds_release_reason_length",
        ),
        CheckConstraint(
            "(active_marker IS TRUE AND released_by IS NULL "
            "AND release_reason IS NULL AND released_at IS NULL) OR "
            "(active_marker IS NULL AND released_by IS NOT NULL "
            "AND release_reason IS NOT NULL AND released_at IS NOT NULL)",
            name="ck_run_retention_holds_release_state",
        ),
        UniqueConstraint(
            "run_id",
            "hold_type",
            "active_marker",
            name="uq_run_retention_holds_one_active",
        ),
        Index(
            "ix_run_retention_holds_scope_active",
            "project_id",
            "site_id",
            "active_marker",
        ),
        Index(
            "ix_run_retention_holds_run_active",
            "run_id",
            "active_marker",
        ),
    )


class ObservationRetentionJob(Base):
    """A restart-safe, exact-scope provisional observation cleanup job."""

    __tablename__ = "observation_retention_jobs"

    job_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ID_MAX_LENGTH), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH),
        ForeignKey("projects.id"),
        nullable=False,
    )
    site_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH),
        ForeignKey("sites.id"),
        nullable=False,
    )
    keep_days: Mapped[int] = mapped_column(Integer, nullable=False)
    cutoff_sealed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    high_water_observation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_run_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_observation_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    next_cursor: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    batch_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    confirmed_by: Mapped[str | None] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deleted_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    batch_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ERROR_CODE_MAX_LENGTH), nullable=True
    )
    active_marker: Mapped[bool | None] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "length(job_id) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_ID_MAX_LENGTH}",
            name="ck_observation_retention_jobs_job_id_length",
        ),
        CheckConstraint(
            "length(project_id) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH}",
            name="ck_observation_retention_jobs_project_id_length",
        ),
        CheckConstraint(
            "length(site_id) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH}",
            name="ck_observation_retention_jobs_site_id_length",
        ),
        CheckConstraint(
            "keep_days BETWEEN 30 AND 3650",
            name="ck_observation_retention_jobs_keep_days",
        ),
        CheckConstraint(
            "high_water_observation_id >= 0",
            name="ck_observation_retention_jobs_high_water",
        ),
        CheckConstraint(
            "candidate_run_count >= 0",
            name="ck_observation_retention_jobs_candidate_run_count",
        ),
        CheckConstraint(
            "candidate_observation_count >= 0",
            name="ck_observation_retention_jobs_candidate_observation_count",
        ),
        CheckConstraint(
            _lowercase_sha256_check("candidate_manifest_sha256"),
            name="ck_observation_retention_jobs_candidate_manifest_sha256",
        ),
        CheckConstraint(
            "next_cursor >= 0",
            name="ck_observation_retention_jobs_next_cursor",
        ),
        CheckConstraint(
            "batch_limit BETWEEN 1 AND 1000",
            name="ck_observation_retention_jobs_batch_limit",
        ),
        CheckConstraint(
            "status IN ('preview', 'ready', 'running', 'complete', 'failed')",
            name="ck_observation_retention_jobs_status",
        ),
        CheckConstraint(
            "length(requested_by) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH}",
            name="ck_observation_retention_jobs_requested_by_length",
        ),
        CheckConstraint(
            "confirmed_by IS NULL OR length(confirmed_by) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH}",
            name="ck_observation_retention_jobs_confirmed_by_length",
        ),
        CheckConstraint(
            "(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_observation_retention_jobs_confirmation_state",
        ),
        CheckConstraint(
            "deleted_count >= 0",
            name="ck_observation_retention_jobs_deleted_count",
        ),
        CheckConstraint(
            "batch_count >= 0",
            name="ck_observation_retention_jobs_batch_count",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_ERROR_CODE_MAX_LENGTH}",
            name="ck_observation_retention_jobs_error_code_length",
        ),
        CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name="ck_observation_retention_jobs_active_marker",
        ),
        UniqueConstraint(
            "project_id",
            "site_id",
            "active_marker",
            name="uq_observation_retention_jobs_one_active_scope",
        ),
        Index(
            "ix_observation_retention_jobs_scope_active_status",
            "project_id",
            "site_id",
            "active_marker",
            "status",
        ),
    )


class ObservationRetentionCandidate(Base):
    """Frozen preview-time membership and sealed-run attestation."""

    __tablename__ = "observation_retention_candidates"

    job_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ID_MAX_LENGTH),
        ForeignKey("observation_retention_jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # No Run FK: candidate audit must survive later whole-run deletion.
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH), nullable=False
    )
    site_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    seal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    terminal_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation_stream_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deleted_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "length(run_id) BETWEEN 1 AND 64",
            name="ck_observation_retention_candidates_run_id_length",
        ),
        CheckConstraint(
            "length(project_id) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH}",
            name="ck_observation_retention_candidates_project_id_length",
        ),
        CheckConstraint(
            "length(site_id) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_SCOPE_MAX_LENGTH}",
            name="ck_observation_retention_candidates_site_id_length",
        ),
        CheckConstraint(
            "attempt > 0",
            name="ck_observation_retention_candidates_attempt_positive",
        ),
        CheckConstraint(
            "terminal_status IN ('succeeded', 'failed', 'cancelled')",
            name="ck_observation_retention_candidates_terminal_status",
        ),
        CheckConstraint(
            _lowercase_sha256_check("context_sha256"),
            name="ck_observation_retention_candidates_context_sha256",
        ),
        CheckConstraint(
            _lowercase_sha256_check("result_sha256"),
            name="ck_observation_retention_candidates_result_sha256",
        ),
        CheckConstraint(
            _lowercase_sha256_check("seal_sha256"),
            name="ck_observation_retention_candidates_seal_sha256",
        ),
        CheckConstraint(
            "terminal_cursor > 0",
            name="ck_observation_retention_candidates_terminal_cursor_positive",
        ),
        CheckConstraint(
            "observation_count > 0",
            name="ck_observation_retention_candidates_observation_count_positive",
        ),
        CheckConstraint(
            _lowercase_sha256_check("observation_stream_sha256"),
            name="ck_observation_retention_candidates_stream_sha256",
        ),
        CheckConstraint(
            "deleted_count BETWEEN 0 AND observation_count",
            name="ck_observation_retention_candidates_deleted_count",
        ),
        Index(
            "ix_observation_retention_candidates_job_cursor",
            "job_id",
            "terminal_cursor",
            "run_id",
        ),
        Index("ix_observation_retention_candidates_run", "run_id"),
    )


class ObservationRetentionBatch(Base):
    """Append-only audit row for one committed retention apply batch."""

    __tablename__ = "observation_retention_batches"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    job_id: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ID_MAX_LENGTH),
        ForeignKey("observation_retention_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(
        String(OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH), nullable=False
    )
    cursor_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cursor_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "batch_number > 0",
            name="ck_observation_retention_batches_batch_number_positive",
        ),
        CheckConstraint(
            "length(actor) BETWEEN 1 AND "
            f"{OBSERVATION_RETENTION_JOB_ACTOR_MAX_LENGTH}",
            name="ck_observation_retention_batches_actor_length",
        ),
        CheckConstraint(
            "cursor_before >= 0 AND cursor_after >= cursor_before",
            name="ck_observation_retention_batches_cursor_order",
        ),
        CheckConstraint(
            "attempted_count >= 0 AND attempted_count <= 1000",
            name="ck_observation_retention_batches_attempted_count",
        ),
        CheckConstraint(
            "deleted_count >= 0 AND deleted_count <= attempted_count",
            name="ck_observation_retention_batches_deleted_count",
        ),
        UniqueConstraint(
            "job_id",
            "batch_number",
            name="uq_observation_retention_batches_job_number",
        ),
        Index(
            "ix_observation_retention_batches_job_number",
            "job_id",
            "batch_number",
        ),
        {"sqlite_autoincrement": True},
    )


class RunIssue(Base):
    """One validation issue attached to a run; one column per ValidationIssueRecord field."""

    __tablename__ = "run_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)

    # ValidationIssueRecord fields (smart_commissioning_core.records) — keep in sync.
    issue_id: Mapped[str] = mapped_column(String(128))
    asset_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    point_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_evidence_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    run: Mapped[Run] = relationship(back_populates="issues")


class ConfigurationSnapshot(Base):
    """Versioned configuration payload per project+site; current = highest version.

    Secrets stay file-based — payloads only ever hold secret:// references.
    """

    __tablename__ = "configuration_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(255))
    site_id: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "site_id", "version"),
        Index("ix_configuration_snapshots_project_site", "project_id", "site_id"),
    )


class ImportRecord(Base):
    """An uploaded import batch; mirrors the imp_... summary/errors/accepted_rows files."""

    __tablename__ = "import_records"

    import_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    site_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    import_type: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_file_path: Mapped[str] = mapped_column(String(1024))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    accepted_rows: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        Index("ix_import_records_project_site", "project_id", "site_id"),
    )


class UdmiSchemaSet(Base):
    """An operator-uploaded non-published UDMI schema set (one row per label).

    ``version_label`` is stored in ``nonpub_version_key`` form (normalised,
    casefolded) so the register column, the upload form, and the run-time
    lookup can never drift apart on casing. ``files`` holds the complete
    ``{filename: schema}`` mapping; a re-upload under the same label replaces
    the row wholesale.
    """

    __tablename__ = "udmi_schema_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_label: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    files: Mapped[dict] = mapped_column(JSON, default=dict)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class DiscoveredDevice(Base):
    """A device observed by a discovery engine (IP / BACnet / MQTT).

    Generic on purpose: per-vendor or per-protocol fields go in the JSON
    ``attributes`` column so all three discovery engines reuse this table
    without schema churn. Rows are owned by a run and cascade-deleted with it.
    """

    __tablename__ = "discovered_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    site_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Free-form vendor/protocol-specific fields (MAC, hostname, ports, GUID...).
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (Index("ix_discovered_devices_run_position", "run_id", "position"),)


class DiscoveredPoint(Base):
    """A point observed by a discovery engine.

    ``device_ref`` is a soft reference (the engine's own device key, e.g. the
    device address) rather than an FK, so points can be ingested before/without
    a matching device row. Owned by a run; cascade-deleted with it.
    """

    __tablename__ = "discovered_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    device_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    point_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    point_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observed_value: Mapped[dict] = mapped_column(JSON, default=dict)
    units: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (Index("ix_discovered_points_run_position", "run_id", "position"),)


class DiscoveredTopic(Base):
    """An MQTT topic observed by the MQTT discovery engine.

    Owned by a run; cascade-deleted with it. ``last_payload`` keeps the most
    recent captured payload (JSON) and ``message_count`` the number seen.
    """

    __tablename__ = "discovered_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    topic: Mapped[str] = mapped_column(String(512))
    last_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (Index("ix_discovered_topics_run_position", "run_id", "position"),)


class RunExecutionContext(Base):
    """Insert-once canonical RunContextV1 captured before dispatch."""

    __tablename__ = "run_execution_contexts"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    context_json: Mapped[dict] = mapped_column(JSON)
    context_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RunDispatch(Base):
    """Transactional outbox row. Redis carries only this ID and the run ID."""

    __tablename__ = "run_dispatch_outbox"

    dispatch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), default="pending")
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ActiveProtocolSlot(Base):
    """One reserved queued/running run per canonical network collision key."""

    __tablename__ = "active_protocol_slots"

    protocol_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ScanAuthorization(Base):
    """One immutable, preview-bound, single-use active-scan approval."""

    __tablename__ = "scan_authorizations"

    authorization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    preview_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"), index=True)
    packet_plan_sha256: Mapped[str] = mapped_column(String(64), index=True)
    approved_by: Mapped[str] = mapped_column(String(255))
    ticket: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(Text)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime())
    not_after: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    use_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    consumed_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        CheckConstraint("max_uses = 1", name="ck_scan_authorizations_single_use"),
        CheckConstraint(
            "use_count >= 0 AND use_count <= max_uses",
            name="ck_scan_authorizations_use_count",
        ),
        CheckConstraint(
            "not_after > not_before",
            name="ck_scan_authorizations_window",
        ),
        Index(
            "ix_scan_authorizations_scope_window",
            "project_id",
            "site_id",
            "not_after",
        ),
    )


class NmapDeploymentPolicy(Base):
    """One immutable revision of deployment-wide Nmap authority.

    The current policy is the highest revision for ``deployment_id``. Creating
    a replacement inserts a new row and links it to the previous revision, so
    deployment and licence decisions remain auditable outside mutable job
    configuration snapshots.
    """

    __tablename__ = "nmap_deployment_policies"

    policy_id: Mapped[str] = mapped_column(String(NMAP_AUTHORITY_ID_MAX_LENGTH), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(
        String(NMAP_DEPLOYMENT_ID_MAX_LENGTH), index=True
    )
    network_executor_id: Mapped[str] = mapped_column(
        String(NMAP_DEPLOYMENT_ID_MAX_LENGTH), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    deployment_lane: Mapped[str] = mapped_column(String(32))
    provider_mode: Mapped[str] = mapped_column(String(32))
    organization_internal: Mapped[bool] = mapped_column(Boolean)
    deployment_owner: Mapped[str] = mapped_column(String(NMAP_AUDIT_ACTOR_MAX_LENGTH))
    operator_install_responsibility: Mapped[str] = mapped_column(Text)
    permitted_project_sites_json: Mapped[str] = mapped_column(Text)
    permitted_project_sites_sha256: Mapped[str] = mapped_column(String(64), index=True)
    update_owner: Mapped[str] = mapped_column(String(NMAP_AUDIT_ACTOR_MAX_LENGTH))
    reviewed_version_policy: Mapped[str] = mapped_column(Text)
    permitted_publishers_json: Mapped[str] = mapped_column(Text)
    permitted_versions_json: Mapped[str] = mapped_column(Text)
    permitted_signer_sha256_json: Mapped[str] = mapped_column(Text)
    permitted_executable_sha256_json: Mapped[str] = mapped_column(Text)
    permitted_data_manifest_sha256_json: Mapped[str] = mapped_column(Text)
    permitted_licence_sha256_json: Mapped[str] = mapped_column(Text)
    permitted_npsl_versions_json: Mapped[str] = mapped_column(Text)
    reviewed_scripts_json: Mapped[str] = mapped_column(Text)
    max_data_files: Mapped[int] = mapped_column(Integer)
    max_file_bytes: Mapped[int] = mapped_column(BigInteger)
    max_manifest_bytes: Mapped[int] = mapped_column(BigInteger)
    profile_policy_json: Mapped[str] = mapped_column(Text)
    profile_policy_sha256: Mapped[str] = mapped_column(String(64), index=True)
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    acknowledged_no_redistribution: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[str] = mapped_column(String(NMAP_AUDIT_ACTOR_MAX_LENGTH))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    supersedes_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "nmap_deployment_policies.policy_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_nmap_deployment_policies_revision"),
        CheckConstraint(
            "deployment_lane IN ('internal_same_organization', 'external_customer')",
            name="ck_nmap_deployment_policies_lane",
        ),
        CheckConstraint(
            "provider_mode IN ('disabled', 'operator_xml_import', "
            "'internal_operator_managed')",
            name="ck_nmap_deployment_policies_mode",
        ),
        CheckConstraint(
            "(deployment_lane = 'internal_same_organization' AND "
            "organization_internal IS TRUE) OR "
            "(deployment_lane = 'external_customer' AND "
            "organization_internal IS FALSE AND provider_mode = 'disabled')",
            name="ck_nmap_deployment_policies_lane_mode",
        ),
        CheckConstraint(
            "provider_mode = 'disabled' OR acknowledged_no_redistribution IS TRUE",
            name="ck_nmap_deployment_policies_acknowledgement",
        ),
        CheckConstraint(
            f"length(policy_id) BETWEEN 1 AND {NMAP_AUTHORITY_ID_MAX_LENGTH} AND "
            f"length(deployment_id) BETWEEN 1 AND {NMAP_DEPLOYMENT_ID_MAX_LENGTH} AND "
            f"length(network_executor_id) BETWEEN 1 AND {NMAP_DEPLOYMENT_ID_MAX_LENGTH} AND "
            f"length(deployment_owner) BETWEEN 1 AND {NMAP_AUDIT_ACTOR_MAX_LENGTH} AND "
            f"length(update_owner) BETWEEN 1 AND {NMAP_AUDIT_ACTOR_MAX_LENGTH} AND "
            f"length(created_by) BETWEEN 1 AND {NMAP_AUDIT_ACTOR_MAX_LENGTH}",
            name="ck_nmap_deployment_policies_identifiers",
        ),
        CheckConstraint(
            f"length(operator_install_responsibility) BETWEEN 1 AND {NMAP_POLICY_TEXT_MAX_LENGTH} "
            f"AND length(reviewed_version_policy) BETWEEN 1 AND {NMAP_POLICY_TEXT_MAX_LENGTH} "
            f"AND length(reason) BETWEEN 1 AND {NMAP_AUDIT_REASON_MAX_LENGTH}",
            name="ck_nmap_deployment_policies_text",
        ),
        CheckConstraint(
            f"length(permitted_project_sites_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_publishers_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_versions_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_signer_sha256_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_executable_sha256_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_data_manifest_sha256_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_licence_sha256_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(permitted_npsl_versions_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(reviewed_scripts_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH} "
            f"AND length(profile_policy_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH}",
            name="ck_nmap_deployment_policies_json_bounds",
        ),
        CheckConstraint(
            _lowercase_sha256_check("permitted_project_sites_sha256")
            + " AND "
            + _lowercase_sha256_check("profile_policy_sha256"),
            name="ck_nmap_deployment_policies_policy_sha256",
        ),
        CheckConstraint(
            "max_data_files BETWEEN 1 AND 65536 AND "
            "max_file_bytes BETWEEN 1024 AND 536870912 AND "
            "max_manifest_bytes BETWEEN 1024 AND 4294967296 AND "
            "max_manifest_bytes >= max_file_bytes",
            name="ck_nmap_deployment_policies_manifest_limits",
        ),
        CheckConstraint(
            "supersedes_policy_id IS NULL OR supersedes_policy_id <> policy_id",
            name="ck_nmap_deployment_policies_not_self_superseding",
        ),
        UniqueConstraint(
            "deployment_id",
            "network_executor_id",
            "revision",
            name="uq_nmap_deployment_policies_revision",
        ),
        UniqueConstraint(
            "supersedes_policy_id",
            name="uq_nmap_deployment_policies_linear_history",
        ),
        UniqueConstraint(
            "policy_id",
            "deployment_id",
            "network_executor_id",
            "revision",
            "deployment_lane",
            "provider_mode",
            "permitted_project_sites_sha256",
            name="uq_nmap_deployment_policies_confirmation_binding",
        ),
        Index(
            "ix_nmap_deployment_policies_current",
            "deployment_id",
            "network_executor_id",
            "revision",
        ),
    )


class NmapInstallationConfirmation(Base):
    """Append-only administrator confirmation of one exact trusted install."""

    __tablename__ = "nmap_installation_confirmations"

    confirmation_id: Mapped[str] = mapped_column(
        String(NMAP_AUTHORITY_ID_MAX_LENGTH), primary_key=True
    )
    policy_id: Mapped[str] = mapped_column(String(NMAP_AUTHORITY_ID_MAX_LENGTH))
    deployment_id: Mapped[str] = mapped_column(
        String(NMAP_DEPLOYMENT_ID_MAX_LENGTH), index=True
    )
    network_executor_id: Mapped[str] = mapped_column(
        String(NMAP_DEPLOYMENT_ID_MAX_LENGTH), index=True
    )
    policy_revision: Mapped[int] = mapped_column(Integer)
    deployment_lane: Mapped[str] = mapped_column(String(32))
    provider_mode: Mapped[str] = mapped_column(String(32))
    permitted_project_sites_json: Mapped[str] = mapped_column(Text)
    permitted_project_sites_sha256: Mapped[str] = mapped_column(String(64), index=True)
    detection_candidate_json: Mapped[str] = mapped_column(Text)
    detection_candidate_sha256: Mapped[str] = mapped_column(String(64), index=True)
    machine_executor_identity: Mapped[str] = mapped_column(
        String(NMAP_DEPLOYMENT_ID_MAX_LENGTH), index=True
    )
    publisher: Mapped[str] = mapped_column(String(512))
    version: Mapped[str] = mapped_column(String(128))
    # Protected persistence only. Public capability responses expose neither
    # these paths nor the ACL/reparse evidence used to trust them.
    install_root: Mapped[str] = mapped_column(Text)
    executable_path: Mapped[str] = mapped_column(Text)
    data_directory: Mapped[str] = mapped_column(Text)
    executable_sha256: Mapped[str] = mapped_column(String(64), index=True)
    data_manifest_sha256: Mapped[str] = mapped_column(String(64), index=True)
    data_file_count: Mapped[int] = mapped_column(Integer)
    data_total_bytes: Mapped[int] = mapped_column(BigInteger)
    licence_relative_path: Mapped[str] = mapped_column(String(255))
    licence_sha256: Mapped[str] = mapped_column(String(64), index=True)
    npsl_version: Mapped[str] = mapped_column(String(32))
    reviewed_scripts_json: Mapped[str] = mapped_column(Text)
    signer_sha256: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64), index=True)
    npcap_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    npcap_state: Mapped[str] = mapped_column(String(32))
    raw_capable: Mapped[bool] = mapped_column(Boolean)
    confirmed_by: Mapped[str] = mapped_column(String(NMAP_AUDIT_ACTOR_MAX_LENGTH))
    reason: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "policy_id",
                "deployment_id",
                "network_executor_id",
                "policy_revision",
                "deployment_lane",
                "provider_mode",
                "permitted_project_sites_sha256",
            ],
            [
                "nmap_deployment_policies.policy_id",
                "nmap_deployment_policies.deployment_id",
                "nmap_deployment_policies.network_executor_id",
                "nmap_deployment_policies.revision",
                "nmap_deployment_policies.deployment_lane",
                "nmap_deployment_policies.provider_mode",
                "nmap_deployment_policies.permitted_project_sites_sha256",
            ],
            ondelete="RESTRICT",
            name="fk_nmap_confirmations_exact_policy",
        ),
        CheckConstraint(
            "policy_revision > 0",
            name="ck_nmap_installation_confirmations_policy_revision",
        ),
        CheckConstraint(
            "deployment_lane = 'internal_same_organization' AND "
            "provider_mode = 'internal_operator_managed'",
            name="ck_nmap_installation_confirmations_internal_only",
        ),
        CheckConstraint(
            f"length(confirmation_id) BETWEEN 1 AND {NMAP_AUTHORITY_ID_MAX_LENGTH} AND "
            f"length(policy_id) BETWEEN 1 AND {NMAP_AUTHORITY_ID_MAX_LENGTH} AND "
            f"length(deployment_id) BETWEEN 1 AND {NMAP_DEPLOYMENT_ID_MAX_LENGTH} AND "
            f"length(network_executor_id) BETWEEN 1 AND {NMAP_DEPLOYMENT_ID_MAX_LENGTH} AND "
            f"length(machine_executor_identity) BETWEEN 1 AND {NMAP_DEPLOYMENT_ID_MAX_LENGTH} AND "
            f"length(confirmed_by) BETWEEN 1 AND {NMAP_AUDIT_ACTOR_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_identifiers",
        ),
        CheckConstraint(
            "length(machine_executor_identity) = 49 AND "
            "substr(machine_executor_identity, 1, 13) = 'machine-guid:' AND "
            "machine_executor_identity = lower(machine_executor_identity)",
            name="ck_nmap_installation_confirmations_machine_identity",
        ),
        CheckConstraint(
            f"length(permitted_project_sites_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_scope_json",
        ),
        CheckConstraint(
            f"length(detection_candidate_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_candidate_json",
        ),
        CheckConstraint(
            f"length(reviewed_scripts_json) BETWEEN 2 AND {NMAP_POLICY_JSON_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_scripts_json",
        ),
        CheckConstraint(
            "length(publisher) BETWEEN 1 AND 512 AND length(version) BETWEEN 1 AND 128 "
            "AND length(npsl_version) BETWEEN 1 AND 32",
            name="ck_nmap_installation_confirmations_identity_text",
        ),
        CheckConstraint(
            f"length(install_root) BETWEEN 3 AND {NMAP_PROTECTED_PATH_MAX_LENGTH} AND "
            f"length(executable_path) BETWEEN 3 AND {NMAP_PROTECTED_PATH_MAX_LENGTH} AND "
            f"length(data_directory) BETWEEN 3 AND {NMAP_PROTECTED_PATH_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_path_bounds",
        ),
        CheckConstraint(
            "data_file_count BETWEEN 1 AND 65536",
            name="ck_nmap_installation_confirmations_file_count",
        ),
        CheckConstraint(
            "data_total_bytes BETWEEN 1 AND 4294967296",
            name="ck_nmap_installation_confirmations_data_bytes",
        ),
        CheckConstraint(
            "length(licence_relative_path) BETWEEN 1 AND 255 "
            "AND licence_relative_path NOT IN ('.', '..')",
            name="ck_nmap_installation_confirmations_licence_path",
        ),
        CheckConstraint(
            "npcap_state IN ('not_checked', 'missing', 'admin_only', "
            "'connect_only', 'raw_capable')",
            name="ck_nmap_installation_confirmations_npcap_state",
        ),
        CheckConstraint(
            "(raw_capable IS TRUE AND npcap_state = 'raw_capable') OR "
            "(raw_capable IS FALSE AND npcap_state <> 'raw_capable')",
            name="ck_nmap_installation_confirmations_raw_capability",
        ),
        CheckConstraint(
            f"length(reason) BETWEEN 1 AND {NMAP_AUDIT_REASON_MAX_LENGTH}",
            name="ck_nmap_installation_confirmations_reason",
        ),
        CheckConstraint(
            "npcap_version IS NULL OR length(npcap_version) BETWEEN 1 AND 128",
            name="ck_nmap_installation_confirmations_npcap_version",
        ),
        CheckConstraint(
            " AND ".join(
                _lowercase_sha256_check(column_name)
                for column_name in (
                    "executable_sha256",
                    "data_manifest_sha256",
                    "licence_sha256",
                    "signer_sha256",
                    "permitted_project_sites_sha256",
                    "detection_candidate_sha256",
                    "fingerprint_sha256",
                )
            ),
            name="ck_nmap_installation_confirmations_sha256",
        ),
        Index(
            "ix_nmap_installation_confirmations_policy_time",
            "policy_id",
            "confirmed_at",
        ),
        Index(
            "ix_nmap_installation_confirmations_deployment_time",
            "deployment_id",
            "confirmed_at",
        ),
    )


class UserScopeGrant(Base):
    """Audited project/site access granted to one named user.

    Global role and resource scope are deliberately separate: ``User.role``
    controls what an operator may do, while an active row here controls where a
    non-admin named user may do it. ``active_marker`` is ``True`` for a current
    grant and becomes ``NULL`` on revocation. Including it in the unique key
    gives SQLite and PostgreSQL one active grant per user/scope while retaining
    any number of immutable revoked-history rows.
    """

    __tablename__ = "user_scope_grants"

    grant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id"), index=True)
    active_marker: Mapped[bool | None] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=True
    )
    granted_by: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name="ck_user_scope_grants_active_marker",
        ),
        CheckConstraint(
            "(active_marker IS TRUE AND revoked_at IS NULL AND revoked_by IS NULL "
            "AND revoke_reason IS NULL) OR "
            "(active_marker IS NULL AND revoked_at IS NOT NULL "
            "AND revoked_by IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_user_scope_grants_revocation_state",
        ),
        UniqueConstraint(
            "user_id",
            "project_id",
            "site_id",
            "active_marker",
            name="uq_user_scope_grants_one_active",
        ),
        Index(
            "ix_user_scope_grants_effective",
            "user_id",
            "project_id",
            "site_id",
            "active_marker",
        ),
    )

class RunLink(Base):
    """Typed relation between immutable runs, beginning with preview -> live."""

    __tablename__ = "run_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    child_run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    relation: Mapped[str] = mapped_column(String(32))
    authorization_id: Mapped[str | None] = mapped_column(
        ForeignKey("scan_authorizations.authorization_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "relation IN ('preview', 'retry', 'property_expansion', 'soak_iteration')",
            name="ck_run_links_relation",
        ),
        UniqueConstraint(
            "parent_run_id",
            "child_run_id",
            "relation",
            name="uq_run_links_pair_relation",
        ),
        UniqueConstraint(
            "child_run_id",
            "relation",
            name="uq_run_links_child_relation",
        ),
        CheckConstraint("parent_run_id <> child_run_id", name="ck_run_links_distinct"),
        Index("ix_run_links_parent_relation", "parent_run_id", "relation"),
    )


class RunResult(Base):
    """Insert-once canonical terminal result payload."""

    __tablename__ = "run_results"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    terminal_status: Mapped[str] = mapped_column(String(32))
    terminal_stage: Mapped[str] = mapped_column(String(128))
    summary: Mapped[dict] = mapped_column(JSON)
    result_payload: Mapped[dict] = mapped_column(JSON)
    result_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RunSeal(Base):
    """Evidence seal binding a result digest to the exact execution context."""

    __tablename__ = "run_seals"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    terminal_status: Mapped[str] = mapped_column(String(32))
    context_sha256: Mapped[str] = mapped_column(String(64), index=True)
    result_sha256: Mapped[str] = mapped_column(String(64), index=True)
    sealed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ReportEvidenceContract(Base):
    """Immutable provenance dispatch for report evidence verification."""

    __tablename__ = "report_evidence_contracts"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    site_id: Mapped[str] = mapped_column(String(255), nullable=False)
    classified_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "contract_version IN ('sealed_v1', 'legacy_pre_lifecycle')",
            name="ck_report_evidence_contracts_version",
        ),
        Index(
            "ix_report_evidence_contracts_scope",
            "project_id",
            "site_id",
            "run_id",
        ),
    )


class RunLifecycleConflict(Base):
    """Observable audit row for stale, duplicate-conflicting, or terminal writes."""

    __tablename__ = "run_lifecycle_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    operation: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(128))
    owner_token_fingerprint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempted_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempted_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SyncCredential(Base):
    """A dedicated edge-to-hub machine credential; plaintext keys are never stored."""

    __tablename__ = "sync_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    edge_id: Mapped[str] = mapped_column(String(255), index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    signing_key_fingerprint: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SyncCredentialScope(Base):
    """One exact project/site pair an edge credential may submit."""

    __tablename__ = "sync_credential_scopes"

    credential_id: Mapped[str] = mapped_column(
        ForeignKey("sync_credentials.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(255), primary_key=True)


class SyncArtifact(Base):
    """Hub-owned location for exact edge report bytes and their signed manifest."""

    __tablename__ = "sync_artifacts"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    storage_relpath: Mapped[str] = mapped_column(String(1024))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(255))
    renderer_version: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(255))
    signing_key_id: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SyncReceipt(Base):
    """Persistent per-item v2 ingest outcome; no credential or tenant secrets."""

    __tablename__ = "sync_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    credential_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bundle_id: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    receipt_class: Mapped[str] = mapped_column(String(64))
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    item_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "credential_id",
            "bundle_id",
            "item_id",
            "receipt_class",
            name="uq_sync_receipt_retry",
        ),
    )


class SyncDeliveryState(Base):
    """Edge-side v2 acknowledgement state; the source of truth for its watermark."""

    __tablename__ = "sync_delivery_state"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    protocol_version: Mapped[str] = mapped_column(String(16), default="2.0")
    item_sha256: Mapped[str] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_receipt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_receipt_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
