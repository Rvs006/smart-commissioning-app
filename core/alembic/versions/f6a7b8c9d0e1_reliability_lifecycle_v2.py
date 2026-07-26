"""reliability lifecycle v2

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-26 12:00:00.000000

Adds frozen execution contexts, transactional dispatch identities, fenced
ownership, protocol reservations, insert-once results/seals, and persistent
conflict accounting. Every change is additive and portable across SQLite and
Postgres. Legacy runs remain readable with zero attempts and NULL v2 fields.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _unicode_safe_text(value) -> str:  # noqa: ANN001
    return _LONE_SURROGATE_RE.sub(lambda match: f"\\u{ord(match.group(0)):04X}", str(value))


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_token", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("attempt", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("result_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("state_version", sa.Integer(), server_default="0", nullable=False))
        batch_op.create_index("ix_runs_lease_expiry", ["status", "lease_expires_at"], unique=False)

    op.create_table(
        "run_execution_contexts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_execution_contexts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_execution_contexts")),
    )
    with op.batch_alter_table("run_execution_contexts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_run_execution_contexts_context_sha256"),
            ["context_sha256"],
            unique=False,
        )

    op.create_table(
        "run_dispatch_outbox",
        sa.Column("dispatch_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_dispatch_outbox_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("dispatch_id", name=op.f("pk_run_dispatch_outbox")),
    )
    with op.batch_alter_table("run_dispatch_outbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_dispatch_outbox_run_id"), ["run_id"], unique=True)

    op.create_table(
        "active_protocol_slots",
        sa.Column("protocol_key", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("owner_token", sa.String(length=128), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_active_protocol_slots_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("protocol_key", name=op.f("pk_active_protocol_slots")),
    )
    with op.batch_alter_table("active_protocol_slots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_active_protocol_slots_run_id"), ["run_id"], unique=True)

    op.create_table(
        "run_results",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("terminal_stage", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_results_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_results")),
    )
    with op.batch_alter_table("run_results", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_results_result_sha256"), ["result_sha256"], unique=False)

    op.create_table(
        "run_seals",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_seals_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_seals")),
    )
    with op.batch_alter_table("run_seals", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_seals_context_sha256"), ["context_sha256"], unique=False)
        batch_op.create_index(batch_op.f("ix_run_seals_result_sha256"), ["result_sha256"], unique=False)

    op.create_table(
        "run_lifecycle_conflicts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("owner_token_fingerprint", sa.String(length=16), nullable=True),
        sa.Column("attempted_status", sa.String(length=32), nullable=True),
        sa.Column("attempted_sha256", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_lifecycle_conflicts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_lifecycle_conflicts")),
    )
    with op.batch_alter_table("run_lifecycle_conflicts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_lifecycle_conflicts_run_id"), ["run_id"], unique=False)

    _backfill_terminal_results(op.get_bind())


def _json_safe(value):  # noqa: ANN001, ANN202 - Alembic data migration helper
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return f"{value} (non-standard JSON number)"
    if isinstance(value, dict):
        return {_unicode_safe_text(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, str):
        return _unicode_safe_text(value)
    return value


def _canonical_sha256(value) -> str:  # noqa: ANN001
    payload = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_report_integrity(summary: dict) -> dict:
    """Classify retained report integrity without generating new evidence."""
    integrity = summary.get("integrity")
    if integrity is None:
        classification = "missing"
    elif not isinstance(integrity, dict):
        classification = "conflicting"
    else:
        artifact_hash = integrity.get("hash")
        structurally_complete = (
            isinstance(artifact_hash, str)
            and len(artifact_hash) == 64
            and all(character in "0123456789abcdef" for character in artifact_hash)
            and isinstance(integrity.get("signature"), str)
            and bool(integrity.get("signature"))
            and isinstance(integrity.get("public_key_pem"), str)
            and bool(integrity.get("public_key_pem"))
        )
        classification = "present_unverified" if structurally_complete else "conflicting"
    return {
        "classification": classification,
        "migration": "v0.1.26",
        "silently_resigned": False,
    }


def _backfill_terminal_results(connection) -> None:  # noqa: ANN001
    """Idempotently seal every legacy terminal run from its retained rows."""
    runs = sa.table(
        "runs",
        sa.column("id"),
        sa.column("project_id"),
        sa.column("site_id"),
        sa.column("job_type"),
        sa.column("status"),
        sa.column("stage"),
        sa.column("parameters", sa.JSON()),
        sa.column("result_summary", sa.JSON()),
        sa.column("execution_mode"),
        sa.column("error_message"),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("terminal_at", sa.DateTime(timezone=True)),
        sa.column("result_sha256"),
    )
    results = sa.table(
        "run_results",
        sa.column("run_id"),
        sa.column("schema_version"),
        sa.column("terminal_status"),
        sa.column("terminal_stage"),
        sa.column("summary", sa.JSON()),
        sa.column("result_payload", sa.JSON()),
        sa.column("result_sha256"),
        sa.column("created_at"),
    )
    seals = sa.table(
        "run_seals",
        sa.column("run_id"),
        sa.column("terminal_status"),
        sa.column("context_sha256"),
        sa.column("result_sha256"),
        sa.column("sealed_at"),
    )
    existing = set(connection.execute(sa.select(results.c.run_id)).scalars())
    terminal_rows = connection.execute(
        sa.select(runs).where(runs.c.status.in_(("succeeded", "failed", "cancelled")))
    ).mappings()
    child_specs = (
        ("run_issues", "issues", ("id", "run_id", "position")),
        ("discovered_devices", "devices", ("id", "run_id", "position", "created_at")),
        ("discovered_points", "points", ("id", "run_id", "position", "created_at")),
        ("discovered_topics", "topics", ("id", "run_id", "position", "created_at")),
    )
    metadata = sa.MetaData()
    child_tables = {
        name: sa.Table(name, metadata, autoload_with=connection) for name, _payload_key, _excluded in child_specs
    }
    for run in terminal_rows:
        run_id = run["id"]
        if run_id in existing:
            continue
        summary = dict(run["result_summary"] or {})
        if run["job_type"] == "report_generation":
            summary["legacy_report_integrity"] = _legacy_report_integrity(summary)
            connection.execute(runs.update().where(runs.c.id == run_id).values(result_summary=summary))
        payload = {
            "schema_version": "1.0",
            "status": run["status"],
            "stage": run["stage"],
            "summary": summary,
            "issues": [],
            "devices": [],
            "points": [],
            "topics": [],
            "error_message": run["error_message"],
        }
        for table_name, payload_key, excluded in child_specs:
            table = child_tables[table_name]
            rows = connection.execute(
                sa.select(table).where(table.c.run_id == run_id).order_by(table.c.position, table.c.id)
            ).mappings()
            payload[payload_key] = [
                _json_safe({key: value for key, value in row.items() if key not in excluded}) for row in rows
            ]
        result_sha256 = _canonical_sha256(payload)
        legacy_context = {
            "schema_version": "legacy-0",
            "run_id": run_id,
            "project_id": run["project_id"],
            "site_id": run["site_id"],
            "job_type": run["job_type"],
            "parameters": _json_safe(run["parameters"] or {}),
            "execution_mode": run["execution_mode"],
        }
        context_sha256 = _canonical_sha256(legacy_context)
        terminal_at = run["updated_at"] or run["created_at"] or datetime.now(UTC)
        connection.execute(
            results.insert().values(
                run_id=run_id,
                schema_version="1.0",
                terminal_status=run["status"],
                terminal_stage=run["stage"],
                summary=summary,
                result_payload=payload,
                result_sha256=result_sha256,
                created_at=terminal_at,
            )
        )
        connection.execute(
            seals.insert().values(
                run_id=run_id,
                terminal_status=run["status"],
                context_sha256=context_sha256,
                result_sha256=result_sha256,
                sealed_at=terminal_at,
            )
        )
        connection.execute(
            runs.update()
            .where(runs.c.id == run_id)
            .values(
                terminal_at=terminal_at,
                result_sha256=result_sha256,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("run_lifecycle_conflicts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_lifecycle_conflicts_run_id"))
    op.drop_table("run_lifecycle_conflicts")

    with op.batch_alter_table("run_seals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_seals_result_sha256"))
        batch_op.drop_index(batch_op.f("ix_run_seals_context_sha256"))
    op.drop_table("run_seals")

    with op.batch_alter_table("run_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_results_result_sha256"))
    op.drop_table("run_results")

    with op.batch_alter_table("active_protocol_slots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_active_protocol_slots_run_id"))
    op.drop_table("active_protocol_slots")

    with op.batch_alter_table("run_dispatch_outbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_dispatch_outbox_run_id"))
    op.drop_table("run_dispatch_outbox")

    with op.batch_alter_table("run_execution_contexts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_execution_contexts_context_sha256"))
    op.drop_table("run_execution_contexts")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_lease_expiry")
        batch_op.drop_column("state_version")
        batch_op.drop_column("result_sha256")
        batch_op.drop_column("terminal_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("attempt")
        batch_op.drop_column("owner_token")
