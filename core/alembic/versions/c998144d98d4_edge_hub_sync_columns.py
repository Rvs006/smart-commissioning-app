"""edge hub sync columns

Revision ID: c998144d98d4
Revises: c4a7ced176a9
Create Date: 2026-06-12 16:10:00.000000

Adds the edge->hub synchronization columns to the runs table for the local-first
+ central-hub architecture (smart_commissioning_core.sync):

  * runs.edge_id   — the originating edge id. NULL for a local edge run; stamped
                     from the bundle manifest when the hub ingests a run, so the
                     hub knows which edge produced each record. Nullable, no
                     server default (existing rows stay NULL = local).
  * runs.synced_at — UTC timestamp of when THIS instance last pushed the run.
                     Used as the edge watermark (NULL = un-synced). Nullable.

Both columns are nullable, so the NOT NULL backfill problem the cancel_requested
migration faced does not apply here. UTCDateTime is emitted as plain
DateTime(timezone=True) at the DDL level (matching the prior migrations; the
application type decorator only matters in Python). batch_alter_table keeps the
ALTER working on SQLite as well as Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c998144d98d4"
down_revision: str | None = "c4a7ced176a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("edge_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("synced_at")
        batch_op.drop_column("edge_id")
