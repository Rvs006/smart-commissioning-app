"""udmi_schema_sets table for operator-uploaded nonpub schema sets

Revision ID: e5f6a7b8c9d0
Revises: d1f2a3b4c5d6
Create Date: 2026-07-14 12:00:00.000000

Adds the ``udmi_schema_sets`` table backing non-published UDMI schema set
support (smart_commissioning_core.udmi_schema, field ask 2026-07-14): some
projects deliberately do not conform to any published UDMI version, so an
engineer uploads the project's Draft 7 schema set (state.json / metadata.json /
events_pointset.json plus their $ref closure) under a ``nonpub.*`` version
label. Run creation embeds the stored sets into run parameters, so the queued
worker validates from the shared database with no filesystem coupling.

Columns:
  * id            — autoincrement integer PK.
  * version_label — the nonpub label in nonpub_version_key form (unique,
                    indexed; one row per label — a re-upload replaces the row).
  * files         — JSON ``{filename: schema}`` mapping (the complete set).
  * uploaded_at   — UTC upload time (refreshed on replace).
  * uploaded_by   — username of the uploading principal; nullable.

UTCDateTime is emitted as plain DateTime(timezone=True) at the DDL level
(matching the prior migrations; the application type decorator only matters in
Python). create_table is portable across SQLite and Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "udmi_schema_sets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_label", sa.String(length=255), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_udmi_schema_sets")),
    )
    op.create_index(
        op.f("ix_udmi_schema_sets_version_label"),
        "udmi_schema_sets",
        ["version_label"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_udmi_schema_sets_version_label"), table_name="udmi_schema_sets")
    op.drop_table("udmi_schema_sets")
