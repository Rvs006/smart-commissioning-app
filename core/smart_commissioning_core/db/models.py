"""ORM models for runs, issues, configuration snapshots, and imports.

The Run/RunIssue serialization (see db_run_store) mirrors the JSON file records
produced today by backend RunService / worker FileRunStore so API responses do
not change when the database becomes the source of truth.
"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from smart_commissioning_core.db.base import Base, UTCDateTime, utcnow


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
