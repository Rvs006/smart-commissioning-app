"""users table for per-user identity + RBAC

Revision ID: d1f2a3b4c5d6
Revises: c998144d98d4
Create Date: 2026-06-12 17:00:00.000000

Adds the ``users`` table backing per-user identity and role-based access control
(smart_commissioning_core.rbac). Each user authenticates with their own API key;
only the SHA-256 HASH of that key is stored (``api_key_hash``), never the
plaintext. This is additive only — the legacy shared ``settings.api_key`` and
the loopback ``local`` mode keep working unchanged (they resolve to a synthetic
admin principal), so existing auth is not broken.

Columns:
  * id            — uuid string PK.
  * username      — unique login name (indexed, unique).
  * role          — one of viewer|reviewer|engineer|admin (Role.value).
  * api_key_hash  — sha256 hex digest of the per-user key (unique, indexed; the
                    auth hot path looks a user up by it).
  * is_active     — soft-disable flag; NOT NULL, server default TRUE.
  * created_at    — UTC creation time.
  * last_used_at  — UTC of last successful auth; nullable (NULL until first use).

UTCDateTime is emitted as plain DateTime(timezone=True) at the DDL level
(matching the prior migrations; the application type decorator only matters in
Python). create_table is portable across SQLite and Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f2a3b4c5d6"
down_revision: str | None = "c998144d98d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        op.f("ix_users_username"), "users", ["username"], unique=True
    )
    op.create_index(
        op.f("ix_users_api_key_hash"), "users", ["api_key_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_api_key_hash"), table_name="users")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
