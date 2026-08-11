"""add per-run observation retention deletion attestations

Revision ID: f3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-11 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE observation_retention_jobs, "
                "observation_retention_batches IN ACCESS EXCLUSIVE MODE"
            )
        )
    historical_deletion = bind.scalar(
        sa.text(
            "SELECT 1 WHERE "
            "EXISTS (SELECT 1 FROM observation_retention_jobs "
            "WHERE deleted_count > 0) OR "
            "EXISTS (SELECT 1 FROM observation_retention_batches "
            "WHERE deleted_count > 0)"
        )
    )
    if historical_deletion is not None:
        raise RuntimeError(
            "Upgrade cannot continue because historical observation retention "
            "deletions cannot be reconstructed per run."
        )

    with op.batch_alter_table("observation_retention_candidates") as batch_op:
        batch_op.add_column(
            sa.Column(
                "deleted_count",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_observation_retention_candidates_deleted_count",
            "deleted_count BETWEEN 0 AND observation_count",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "LOCK TABLE observation_retention_candidates "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    attested_deletion = bind.scalar(
        sa.text(
            "SELECT 1 FROM observation_retention_candidates "
            "WHERE deleted_count > 0 LIMIT 1"
        )
    )
    if attested_deletion is not None:
        raise RuntimeError(
            "Downgrade requires every observation retention candidate deletion "
            "attestation to be zero."
        )

    with op.batch_alter_table("observation_retention_candidates") as batch_op:
        batch_op.drop_constraint(
            "ck_observation_retention_candidates_deleted_count",
            type_="check",
        )
        batch_op.drop_column("deleted_count")
