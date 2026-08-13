"""audited project and site grants for named users

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-10 22:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_scope_grants",
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column(
            "active_marker",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=True,
        ),
        sa.Column("granted_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.String(length=255), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name="ck_user_scope_grants_active_marker",
        ),
        sa.CheckConstraint(
            "(active_marker IS TRUE AND revoked_at IS NULL AND revoked_by IS NULL "
            "AND revoke_reason IS NULL) OR "
            "(active_marker IS NULL AND revoked_at IS NOT NULL "
            "AND revoked_by IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_user_scope_grants_revocation_state",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("grant_id"),
        sa.UniqueConstraint(
            "user_id",
            "project_id",
            "site_id",
            "active_marker",
            name="uq_user_scope_grants_one_active",
        ),
    )
    op.create_index(
        op.f("ix_user_scope_grants_user_id"),
        "user_scope_grants",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_scope_grants_project_id"),
        "user_scope_grants",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_scope_grants_site_id"),
        "user_scope_grants",
        ["site_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_scope_grants_effective",
        "user_scope_grants",
        ["user_id", "project_id", "site_id", "active_marker"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_user_scope_grants_effective", table_name="user_scope_grants")
    op.drop_index(op.f("ix_user_scope_grants_site_id"), table_name="user_scope_grants")
    op.drop_index(op.f("ix_user_scope_grants_project_id"), table_name="user_scope_grants")
    op.drop_index(op.f("ix_user_scope_grants_user_id"), table_name="user_scope_grants")
    op.drop_table("user_scope_grants")
