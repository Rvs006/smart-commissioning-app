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
