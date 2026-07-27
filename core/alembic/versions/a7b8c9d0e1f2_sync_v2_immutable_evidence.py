"""sync v2 immutable evidence

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-27 04:00:00.000000

Adds dedicated hashed machine credentials and project/site scopes, exact hub
artifact locations, per-item receipts, and edge acknowledgement state.  The
migration is additive; v1 run watermarks and sealed evidence are untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("edge_id", sa.String(length=255), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("signing_key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_credentials")),
    )
    with op.batch_alter_table("sync_credentials", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_credentials_edge_id"), ["edge_id"])
        batch_op.create_index(
            batch_op.f("ix_sync_credentials_api_key_hash"), ["api_key_hash"], unique=True
        )

    op.create_table(
        "sync_credential_scopes",
        sa.Column("credential_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["sync_credentials.id"],
            name=op.f("fk_sync_credential_scopes_credential_id_sync_credentials"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "credential_id", "project_id", "site_id", name=op.f("pk_sync_credential_scopes")
        ),
    )

    op.create_table(
        "sync_artifacts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_relpath", sa.String(length=1024), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("renderer_version", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=255), nullable=False),
        sa.Column("signing_key_id", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_sync_artifacts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_sync_artifacts")),
    )
    with op.batch_alter_table("sync_artifacts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_artifacts_artifact_sha256"), ["artifact_sha256"]
        )

    op.create_table(
        "sync_receipts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("receipt_class", sa.String(length=64), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("item_sha256", sa.String(length=64), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["sync_credentials.id"],
            name=op.f("fk_sync_receipts_credential_id_sync_credentials"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_receipts")),
        sa.UniqueConstraint(
            "credential_id",
            "bundle_id",
            "item_id",
            "receipt_class",
            name="uq_sync_receipt_retry",
        ),
    )
    with op.batch_alter_table("sync_receipts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_receipts_bundle_id"), ["bundle_id"])
        batch_op.create_index(batch_op.f("ix_sync_receipts_credential_id"), ["credential_id"])
        batch_op.create_index(batch_op.f("ix_sync_receipts_run_id"), ["run_id"])

    op.create_table(
        "sync_delivery_state",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.String(length=16), nullable=False),
        sa.Column("item_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_receipt_id", sa.String(length=64), nullable=True),
        sa.Column("last_receipt_class", sa.String(length=64), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_sync_delivery_state_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_sync_delivery_state")),
    )


def downgrade() -> None:
    op.drop_table("sync_delivery_state")
    with op.batch_alter_table("sync_receipts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_receipts_run_id"))
        batch_op.drop_index(batch_op.f("ix_sync_receipts_credential_id"))
        batch_op.drop_index(batch_op.f("ix_sync_receipts_bundle_id"))
    op.drop_table("sync_receipts")
    with op.batch_alter_table("sync_artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_artifacts_artifact_sha256"))
    op.drop_table("sync_artifacts")
    op.drop_table("sync_credential_scopes")
    with op.batch_alter_table("sync_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_credentials_api_key_hash"))
        batch_op.drop_index(batch_op.f("ix_sync_credentials_edge_id"))
    op.drop_table("sync_credentials")
