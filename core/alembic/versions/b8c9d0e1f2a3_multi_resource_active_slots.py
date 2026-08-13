"""allow multiple active protocol slots per run

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-10 21:15:00.000000

Keeps the resource key as the collision authority while changing the run lookup
index from unique to non-unique. Existing one-key reservations remain valid.
Rollback requires all executors to be stopped and this reservation table to be
empty; the downgrade refuses to mutate a live slot table.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("active_protocol_slots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_active_protocol_slots_run_id"))
        batch_op.create_index(
            batch_op.f("ix_active_protocol_slots_run_id"),
            ["run_id"],
            unique=False,
        )


def downgrade() -> None:
    active_count = int(
        op.get_bind().scalar(text("SELECT COUNT(*) FROM active_protocol_slots")) or 0
    )
    if active_count:
        raise RuntimeError(
            "Cannot downgrade multi-resource slots while reservations exist; "
            "stop executors and drain active protocol slots before retrying."
        )
    with op.batch_alter_table("active_protocol_slots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_active_protocol_slots_run_id"))
        batch_op.create_index(
            batch_op.f("ix_active_protocol_slots_run_id"),
            ["run_id"],
            unique=True,
        )
