"""add protected run-owned raw evidence artifacts

Revision ID: f5d6e7f8a9b0
Revises: f4c5d6e7f8a9
Create Date: 2026-08-12 15:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5d6e7f8a9b0"
down_revision: str | None = "f4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lowercase_hex_check(expression: str, length: int) -> str:
    remainder = expression
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({expression}) = {length} AND "
        f"{expression} = lower({expression}) AND length({remainder}) = 0"
    )


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_raw_evidence_artifacts_scope_owner "
                "BEFORE INSERT ON raw_evidence_artifacts "
                "WHEN NOT EXISTS (SELECT 1 FROM runs WHERE id = NEW.run_id "
                "AND project_id = NEW.project_id AND site_id = NEW.site_id) "
                "BEGIN SELECT RAISE(ABORT, "
                "'raw evidence scope must match owning run'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_raw_evidence_artifacts_no_update "
                "BEFORE UPDATE ON raw_evidence_artifacts "
                "BEGIN SELECT RAISE(ABORT, "
                "'raw evidence artifact metadata is immutable'); END"
            )
        )
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    "CREATE TRIGGER trg_raw_evidence_download_audits_no_"
                    f"{operation.lower()} BEFORE {operation} "
                    "ON raw_evidence_download_audits "
                    "BEGIN SELECT RAISE(ABORT, "
                    "'raw evidence download audit is immutable'); END"
                )
            )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                "CREATE FUNCTION validate_raw_evidence_artifact_scope() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM runs WHERE id = NEW.run_id "
                "AND project_id = NEW.project_id AND site_id = NEW.site_id) THEN "
                "RAISE EXCEPTION 'raw evidence scope must match owning run'; "
                "END IF; RETURN NEW; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_raw_evidence_artifacts_scope_owner "
                "BEFORE INSERT ON raw_evidence_artifacts FOR EACH ROW "
                "EXECUTE FUNCTION validate_raw_evidence_artifact_scope()"
            )
        )
        op.execute(
            sa.text(
                "CREATE FUNCTION reject_raw_evidence_artifact_update() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'raw evidence artifact metadata is immutable'; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_raw_evidence_artifacts_no_update "
                "BEFORE UPDATE ON raw_evidence_artifacts FOR EACH ROW "
                "EXECUTE FUNCTION reject_raw_evidence_artifact_update()"
            )
        )
        op.execute(
            sa.text(
                "CREATE FUNCTION reject_raw_evidence_download_audit_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'raw evidence download audit is immutable'; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_raw_evidence_download_audits_immutable "
                "BEFORE UPDATE OR DELETE ON raw_evidence_download_audits FOR EACH ROW "
                "EXECUTE FUNCTION reject_raw_evidence_download_audit_mutation()"
            )
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_raw_evidence_artifacts_scope_owner"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_raw_evidence_artifacts_no_update"))
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_raw_evidence_download_audits_no_update")
        )
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_raw_evidence_download_audits_no_delete")
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_raw_evidence_artifacts_scope_owner "
                "ON raw_evidence_artifacts"
            )
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_raw_evidence_artifacts_no_update "
                "ON raw_evidence_artifacts"
            )
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_raw_evidence_download_audits_immutable "
                "ON raw_evidence_download_audits"
            )
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS reject_raw_evidence_artifact_update()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS validate_raw_evidence_artifact_scope()"))
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS reject_raw_evidence_download_audit_mutation()")
        )


def upgrade() -> None:
    op.create_table(
        "raw_evidence_artifacts",
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("storage_relpath", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("capture_complete", sa.Boolean(), nullable=False),
        sa.Column("producer_executor_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "substr(artifact_id, 1, 9) = 'artifact_' AND "
            + _lowercase_hex_check("substr(artifact_id, 10)", 32),
            name="ck_raw_evidence_artifacts_opaque_id",
        ),
        sa.CheckConstraint(
            "length(project_id) BETWEEN 1 AND 255 AND "
            "length(site_id) BETWEEN 1 AND 255",
            name="ck_raw_evidence_artifacts_scope_length",
        ),
        sa.CheckConstraint(
            "length(artifact_type) BETWEEN 1 AND 64 AND "
            "length(media_type) BETWEEN 3 AND 255",
            name="ck_raw_evidence_artifacts_type_media_length",
        ),
        sa.CheckConstraint(
            "length(storage_relpath) BETWEEN 1 AND 512 AND "
            "storage_relpath LIKE 'objects/%' AND "
            "storage_relpath NOT LIKE '%..%'",
            name="ck_raw_evidence_artifacts_private_storage_key",
        ),
        sa.CheckConstraint(
            _lowercase_hex_check("sha256", 64),
            name="ck_raw_evidence_artifacts_sha256",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 0 AND 67108864",
            name="ck_raw_evidence_artifacts_size",
        ),
        sa.CheckConstraint(
            "capture_complete IN (false, true)",
            name="ck_raw_evidence_artifacts_capture_complete",
        ),
        sa.CheckConstraint(
            "length(producer_executor_id) BETWEEN 1 AND 255",
            name="ck_raw_evidence_artifacts_executor_id",
        ),
        sa.CheckConstraint(
            "sealed_at >= created_at",
            name="ck_raw_evidence_artifacts_timestamps",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("storage_relpath"),
        sa.UniqueConstraint(
            "artifact_id",
            "run_id",
            "project_id",
            "site_id",
            name="uq_raw_evidence_artifacts_owner",
        ),
    )
    op.create_index(
        op.f("ix_raw_evidence_artifacts_run_id"),
        "raw_evidence_artifacts",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_artifacts_project_id"),
        "raw_evidence_artifacts",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_artifacts_site_id"),
        "raw_evidence_artifacts",
        ["site_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_artifacts_sha256"),
        "raw_evidence_artifacts",
        ["sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_artifacts_producer_executor_id"),
        "raw_evidence_artifacts",
        ["producer_executor_id"],
        unique=False,
    )
    op.create_index(
        "ix_raw_evidence_artifacts_scope_run",
        "raw_evidence_artifacts",
        ["project_id", "site_id", "run_id", "sealed_at"],
        unique=False,
    )

    op.create_table(
        "raw_evidence_download_audits",
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("downloaded_by", sa.String(length=255), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(audit_id) BETWEEN 1 AND 64 AND "
            "length(downloaded_by) BETWEEN 1 AND 255",
            name="ck_raw_evidence_download_audits_identifiers",
        ),
        sa.CheckConstraint(
            _lowercase_hex_check("artifact_sha256", 64),
            name="ck_raw_evidence_download_audits_sha256",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 0 AND 67108864",
            name="ck_raw_evidence_download_audits_size",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        op.f("ix_raw_evidence_download_audits_run_id"),
        "raw_evidence_download_audits",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_download_audits_project_id"),
        "raw_evidence_download_audits",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_raw_evidence_download_audits_site_id"),
        "raw_evidence_download_audits",
        ["site_id"],
        unique=False,
    )
    op.create_index(
        "ix_raw_evidence_download_audits_scope",
        "raw_evidence_download_audits",
        ["project_id", "site_id", "run_id", "downloaded_at"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE raw_evidence_artifacts, raw_evidence_download_audits "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    artifact_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM raw_evidence_artifacts")
    )
    audit_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM raw_evidence_download_audits")
    )
    if artifact_count or audit_count:
        raise RuntimeError(
            "Raw evidence artifacts and download audits must be empty before downgrade."
        )
    _drop_immutability_guards()
    op.drop_index(
        "ix_raw_evidence_download_audits_scope",
        table_name="raw_evidence_download_audits",
    )
    op.drop_index(
        op.f("ix_raw_evidence_download_audits_site_id"),
        table_name="raw_evidence_download_audits",
    )
    op.drop_index(
        op.f("ix_raw_evidence_download_audits_project_id"),
        table_name="raw_evidence_download_audits",
    )
    op.drop_index(
        op.f("ix_raw_evidence_download_audits_run_id"),
        table_name="raw_evidence_download_audits",
    )
    op.drop_table("raw_evidence_download_audits")
    op.drop_index(
        "ix_raw_evidence_artifacts_scope_run",
        table_name="raw_evidence_artifacts",
    )
    op.drop_index(
        op.f("ix_raw_evidence_artifacts_producer_executor_id"),
        table_name="raw_evidence_artifacts",
    )
    op.drop_index(
        op.f("ix_raw_evidence_artifacts_sha256"),
        table_name="raw_evidence_artifacts",
    )
    op.drop_index(
        op.f("ix_raw_evidence_artifacts_site_id"),
        table_name="raw_evidence_artifacts",
    )
    op.drop_index(
        op.f("ix_raw_evidence_artifacts_project_id"),
        table_name="raw_evidence_artifacts",
    )
    op.drop_index(
        op.f("ix_raw_evidence_artifacts_run_id"),
        table_name="raw_evidence_artifacts",
    )
    op.drop_table("raw_evidence_artifacts")
