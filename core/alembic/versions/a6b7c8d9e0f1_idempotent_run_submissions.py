"""add atomic idempotency keys for run submissions

Revision ID: a6b7c8d9e0f1
Revises: f5d6e7f8a9b0
Create Date: 2026-08-15 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: str | None = "f5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lowercase_sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND "
        f"{column_name} = lower({column_name}) AND length({remainder}) = 0"
    )


def upgrade() -> None:
    op.create_table(
        "run_idempotency_keys",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("requesting_principal", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(requesting_principal) BETWEEN 1 AND 255",
            name="ck_run_idempotency_keys_principal_length",
        ),
        sa.CheckConstraint(
            "length(operation) BETWEEN 1 AND 64",
            name="ck_run_idempotency_keys_operation_length",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="ck_run_idempotency_keys_key_length",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("request_sha256"),
            name="ck_run_idempotency_keys_request_sha256",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "requesting_principal",
            "project_id",
            "site_id",
            "operation",
            "idempotency_key",
            name="uq_run_idempotency_keys_scope_key",
        ),
    )
    op.create_index(
        "ix_run_idempotency_keys_scope_lookup",
        "run_idempotency_keys",
        ["requesting_principal", "project_id", "site_id", "operation", "idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_run_idempotency_keys_scope_lookup", table_name="run_idempotency_keys")
    op.drop_table("run_idempotency_keys")
