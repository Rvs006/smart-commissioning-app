"""scan authorizations and run links

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-10 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_authorizations",
        sa.Column("authorization_id", sa.String(length=64), nullable=False),
        sa.Column("preview_run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("packet_plan_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("ticket", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), server_default="1", nullable=False),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_run_id", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_uses = 1", name="ck_scan_authorizations_single_use"),
        sa.CheckConstraint(
            "use_count >= 0 AND use_count <= max_uses",
            name="ck_scan_authorizations_use_count",
        ),
        sa.CheckConstraint(
            "not_after > not_before",
            name="ck_scan_authorizations_window",
        ),
        sa.ForeignKeyConstraint(["consumed_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["preview_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("authorization_id"),
        sa.UniqueConstraint("consumed_run_id"),
    )
    op.create_index(
        op.f("ix_scan_authorizations_preview_run_id"),
        "scan_authorizations",
        ["preview_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scan_authorizations_project_id"),
        "scan_authorizations",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scan_authorizations_site_id"),
        "scan_authorizations",
        ["site_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scan_authorizations_packet_plan_sha256"),
        "scan_authorizations",
        ["packet_plan_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scan_authorizations_not_after"),
        "scan_authorizations",
        ["not_after"],
        unique=False,
    )
    op.create_index(
        "ix_scan_authorizations_scope_window",
        "scan_authorizations",
        ["project_id", "site_id", "not_after"],
        unique=False,
    )

    op.create_table(
        "run_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_run_id", sa.String(length=64), nullable=False),
        sa.Column("child_run_id", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("authorization_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relation IN ('preview', 'retry', 'property_expansion', 'soak_iteration')",
            name="ck_run_links_relation",
        ),
        sa.CheckConstraint("parent_run_id <> child_run_id", name="ck_run_links_distinct"),
        sa.ForeignKeyConstraint(["child_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["scan_authorizations.authorization_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "child_run_id",
            "relation",
            name="uq_run_links_child_relation",
        ),
        sa.UniqueConstraint(
            "parent_run_id",
            "child_run_id",
            "relation",
            name="uq_run_links_pair_relation",
        ),
    )
    op.create_index(
        op.f("ix_run_links_parent_run_id"),
        "run_links",
        ["parent_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_run_links_child_run_id"),
        "run_links",
        ["child_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_run_links_parent_relation",
        "run_links",
        ["parent_run_id", "relation"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_links_parent_relation", table_name="run_links")
    op.drop_index(op.f("ix_run_links_child_run_id"), table_name="run_links")
    op.drop_index(op.f("ix_run_links_parent_run_id"), table_name="run_links")
    op.drop_table("run_links")
    op.drop_index("ix_scan_authorizations_scope_window", table_name="scan_authorizations")
    op.drop_index(op.f("ix_scan_authorizations_not_after"), table_name="scan_authorizations")
    op.drop_index(
        op.f("ix_scan_authorizations_packet_plan_sha256"),
        table_name="scan_authorizations",
    )
    op.drop_index(op.f("ix_scan_authorizations_site_id"), table_name="scan_authorizations")
    op.drop_index(
        op.f("ix_scan_authorizations_project_id"),
        table_name="scan_authorizations",
    )
    op.drop_index(
        op.f("ix_scan_authorizations_preview_run_id"),
        table_name="scan_authorizations",
    )
    op.drop_table("scan_authorizations")
