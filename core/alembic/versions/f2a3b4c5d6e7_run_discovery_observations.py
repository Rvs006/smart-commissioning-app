"""add provisional discovery observations and retention controls

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-11 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PAYLOAD_MAX_CHARS = 65_536
_STATE_MAX_ROWS = 50_000
_STATE_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


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
        "run_discovery_observations",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("entity_kind", sa.String(length=32), nullable=False),
        sa.Column("entity_key", sa.String(length=255), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("payload_schema_version", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt > 0",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_attempt_positive"
            ),
        ),
        sa.CheckConstraint(
            "entity_version > 0",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_entity_version_positive"
            ),
        ),
        sa.CheckConstraint(
            "protocol IN ('ip', 'bacnet')",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_protocol"
            ),
        ),
        sa.CheckConstraint(
            "entity_kind IN "
            "('lane', 'host', 'port', 'device', 'object', 'property', 'diagnostic')",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_entity_kind"
            ),
        ),
        sa.CheckConstraint(
            "phase IN "
            "('planned', 'reachability', 'enrichment', 'comparison', 'finalize')",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_phase"
            ),
        ),
        sa.CheckConstraint(
            "length(entity_key) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_entity_key_length"
            ),
        ),
        sa.CheckConstraint(
            "length(event_key) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_event_key_length"
            ),
        ),
        sa.CheckConstraint(
            "length(outcome) BETWEEN 1 AND 64",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_outcome_length"
            ),
        ),
        sa.CheckConstraint(
            "length(payload_schema_version) BETWEEN 1 AND 32",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_payload_schema_version_length"
            ),
        ),
        sa.CheckConstraint(
            f"length(CAST(payload AS TEXT)) <= {_PAYLOAD_MAX_CHARS}",
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_payload_length"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("payload_sha256"),
            name=op.f(
                "ck_run_discovery_observations_ck_run_discovery_observations_payload_sha256"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_discovery_observations_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_discovery_observations")),
        sa.UniqueConstraint(
            "run_id",
            "attempt",
            "event_key",
            name="uq_run_discovery_observations_event",
        ),
        sa.UniqueConstraint(
            "run_id",
            "attempt",
            "entity_kind",
            "entity_key",
            "entity_version",
            name="uq_run_discovery_observations_entity_version",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_run_discovery_observations_page",
        "run_discovery_observations",
        ["run_id", "attempt", "id"],
        unique=False,
    )
    op.create_index(
        "ix_run_discovery_observations_fold",
        "run_discovery_observations",
        [
            "run_id",
            "attempt",
            "entity_kind",
            "entity_key",
            "entity_version",
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_run_discovery_observations_retention",
        "run_discovery_observations",
        ["created_at", "id"],
        unique=False,
    )

    op.create_table(
        "run_discovery_observation_states",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("observation_count", sa.BigInteger(), nullable=False),
        sa.Column("canonical_payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("terminal_cursor", sa.BigInteger(), nullable=False),
        sa.Column("observation_stream_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "attempt > 0",
            name=op.f("ck_run_discovery_observation_states_attempt_positive"),
        ),
        sa.CheckConstraint(
            f"observation_count BETWEEN 1 AND {_STATE_MAX_ROWS}",
            name=op.f("ck_run_discovery_observation_states_count_bounded"),
        ),
        sa.CheckConstraint(
            "canonical_payload_bytes BETWEEN 1 AND "
            f"{_STATE_MAX_PAYLOAD_BYTES}",
            name=op.f(
                "ck_run_discovery_observation_states_payload_bytes_bounded"
            ),
        ),
        sa.CheckConstraint(
            "terminal_cursor > 0",
            name=op.f(
                "ck_run_discovery_observation_states_terminal_cursor_positive"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("observation_stream_sha256"),
            name=op.f("ck_run_discovery_observation_states_stream_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_discovery_observation_states_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "attempt",
            name=op.f("pk_run_discovery_observation_states"),
        ),
    )

    op.create_table(
        "run_retention_holds",
        sa.Column("hold_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("hold_type", sa.String(length=16), nullable=False),
        sa.Column("evidence_set_id", sa.String(length=128), nullable=True),
        sa.Column(
            "active_marker",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=True,
        ),
        sa.Column("placed_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.String(length=255), nullable=True),
        sa.Column("release_reason", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(hold_id) BETWEEN 1 AND 64",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_hold_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(project_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_project_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(site_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_site_id_length"
            ),
        ),
        sa.CheckConstraint(
            "hold_type IN ('legal', 'evidence')",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_hold_type"
            ),
        ),
        sa.CheckConstraint(
            "evidence_set_id IS NULL OR "
            "length(evidence_set_id) BETWEEN 1 AND 128",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_evidence_set_id_length"
            ),
        ),
        sa.CheckConstraint(
            "(hold_type = 'evidence' AND evidence_set_id IS NOT NULL) OR "
            "(hold_type = 'legal' AND evidence_set_id IS NULL)",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_evidence_set_for_type"
            ),
        ),
        sa.CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_active_marker"
            ),
        ),
        sa.CheckConstraint(
            "length(placed_by) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_placed_by_length"
            ),
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 4096",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_reason_length"
            ),
        ),
        sa.CheckConstraint(
            "released_by IS NULL OR length(released_by) BETWEEN 1 AND 255",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_released_by_length"
            ),
        ),
        sa.CheckConstraint(
            "release_reason IS NULL OR "
            "length(release_reason) BETWEEN 1 AND 4096",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_release_reason_length"
            ),
        ),
        sa.CheckConstraint(
            "(active_marker IS TRUE AND released_by IS NULL "
            "AND release_reason IS NULL AND released_at IS NULL) OR "
            "(active_marker IS NULL AND released_by IS NOT NULL "
            "AND release_reason IS NOT NULL AND released_at IS NOT NULL)",
            name=op.f(
                "ck_run_retention_holds_ck_run_retention_holds_release_state"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "hold_id", name=op.f("pk_run_retention_holds")
        ),
        sa.UniqueConstraint(
            "run_id",
            "hold_type",
            "active_marker",
            name="uq_run_retention_holds_one_active",
        ),
    )
    op.create_index(
        "ix_run_retention_holds_scope_active",
        "run_retention_holds",
        ["project_id", "site_id", "active_marker"],
        unique=False,
    )
    op.create_index(
        "ix_run_retention_holds_run_active",
        "run_retention_holds",
        ["run_id", "active_marker"],
        unique=False,
    )

    op.create_table(
        "observation_retention_jobs",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("keep_days", sa.Integer(), nullable=False),
        sa.Column("cutoff_sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("high_water_observation_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_run_count", sa.BigInteger(), nullable=False),
        sa.Column("candidate_observation_count", sa.BigInteger(), nullable=False),
        sa.Column("candidate_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "next_cursor",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("batch_limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deleted_count",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "batch_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "active_marker",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(job_id) BETWEEN 1 AND 64",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_job_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(project_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_project_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(site_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_site_id_length"
            ),
        ),
        sa.CheckConstraint(
            "keep_days BETWEEN 30 AND 3650",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_keep_days"
            ),
        ),
        sa.CheckConstraint(
            "high_water_observation_id >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_high_water"
            ),
        ),
        sa.CheckConstraint(
            "candidate_run_count >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_candidate_run_count"
            ),
        ),
        sa.CheckConstraint(
            "candidate_observation_count >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_candidate_observation_count"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("candidate_manifest_sha256"),
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_candidate_manifest_sha256"
            ),
        ),
        sa.CheckConstraint(
            "next_cursor >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_next_cursor"
            ),
        ),
        sa.CheckConstraint(
            "batch_limit BETWEEN 1 AND 1000",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_batch_limit"
            ),
        ),
        sa.CheckConstraint(
            "status IN ('preview', 'ready', 'running', 'complete', 'failed')",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_status"
            ),
        ),
        sa.CheckConstraint(
            "length(requested_by) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_requested_by_length"
            ),
        ),
        sa.CheckConstraint(
            "confirmed_by IS NULL OR length(confirmed_by) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_confirmed_by_length"
            ),
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_confirmation_state"
            ),
        ),
        sa.CheckConstraint(
            "deleted_count >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_deleted_count"
            ),
        ),
        sa.CheckConstraint(
            "batch_count >= 0",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_batch_count"
            ),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 128",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_error_code_length"
            ),
        ),
        sa.CheckConstraint(
            "active_marker IS NULL OR active_marker IS TRUE",
            name=op.f(
                "ck_observation_retention_jobs_ck_observation_retention_jobs_active_marker"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_observation_retention_jobs_project_id_projects"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_observation_retention_jobs_site_id_sites"),
        ),
        sa.PrimaryKeyConstraint(
            "job_id", name=op.f("pk_observation_retention_jobs")
        ),
        sa.UniqueConstraint(
            "project_id",
            "site_id",
            "active_marker",
            name="uq_observation_retention_jobs_one_active_scope",
        ),
    )
    op.create_index(
        "ix_observation_retention_jobs_scope_active_status",
        "observation_retention_jobs",
        ["project_id", "site_id", "active_marker", "status"],
        unique=False,
    )

    op.create_table(
        "observation_retention_candidates",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("terminal_status", sa.String(length=32), nullable=False),
        sa.Column("context_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_sha256", sa.String(length=64), nullable=False),
        sa.Column("seal_sha256", sa.String(length=64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_cursor", sa.BigInteger(), nullable=False),
        sa.Column("observation_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "observation_stream_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(run_id) BETWEEN 1 AND 64",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_run_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(project_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_project_id_length"
            ),
        ),
        sa.CheckConstraint(
            "length(site_id) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_site_id_length"
            ),
        ),
        sa.CheckConstraint(
            "attempt > 0",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_attempt_positive"
            ),
        ),
        sa.CheckConstraint(
            "terminal_status IN ('succeeded', 'failed', 'cancelled')",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_terminal_status"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("context_sha256"),
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_context_sha256"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("result_sha256"),
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_result_sha256"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("seal_sha256"),
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_seal_sha256"
            ),
        ),
        sa.CheckConstraint(
            "terminal_cursor > 0",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_terminal_cursor_positive"
            ),
        ),
        sa.CheckConstraint(
            "observation_count > 0",
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_observation_count_positive"
            ),
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("observation_stream_sha256"),
            name=op.f(
                "ck_observation_retention_candidates_ck_observation_retention_candidates_stream_sha256"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["observation_retention_jobs.job_id"],
            name=op.f(
                "fk_observation_retention_candidates_job_id_observation_retention_jobs"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "run_id",
            name=op.f("pk_observation_retention_candidates"),
        ),
    )
    op.create_index(
        "ix_observation_retention_candidates_job_cursor",
        "observation_retention_candidates",
        ["job_id", "terminal_cursor", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_observation_retention_candidates_run",
        "observation_retention_candidates",
        ["run_id"],
        unique=False,
    )

    op.create_table(
        "observation_retention_batches",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("cursor_before", sa.BigInteger(), nullable=False),
        sa.Column("cursor_after", sa.BigInteger(), nullable=False),
        sa.Column("attempted_count", sa.Integer(), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "batch_number > 0",
            name=op.f(
                "ck_observation_retention_batches_ck_observation_retention_batches_batch_number_positive"
            ),
        ),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 255",
            name=op.f(
                "ck_observation_retention_batches_ck_observation_retention_batches_actor_length"
            ),
        ),
        sa.CheckConstraint(
            "cursor_before >= 0 AND cursor_after >= cursor_before",
            name=op.f(
                "ck_observation_retention_batches_ck_observation_retention_batches_cursor_order"
            ),
        ),
        sa.CheckConstraint(
            "attempted_count >= 0 AND attempted_count <= 1000",
            name=op.f(
                "ck_observation_retention_batches_ck_observation_retention_batches_attempted_count"
            ),
        ),
        sa.CheckConstraint(
            "deleted_count >= 0 AND deleted_count <= attempted_count",
            name=op.f(
                "ck_observation_retention_batches_ck_observation_retention_batches_deleted_count"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["observation_retention_jobs.job_id"],
            name=op.f(
                "fk_observation_retention_batches_job_id_observation_retention_jobs"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_observation_retention_batches"),
        ),
        sa.UniqueConstraint(
            "job_id",
            "batch_number",
            name="uq_observation_retention_batches_job_number",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_observation_retention_batches_job_number",
        "observation_retention_batches",
        ["job_id", "batch_number"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Close every insert/delete race before proving that no evidence or
        # retention audit history would be discarded by the downgrade.
        bind.execute(
            sa.text(
                "LOCK TABLE run_discovery_observations, run_retention_holds, "
                "run_discovery_observation_states, "
                "observation_retention_jobs, observation_retention_candidates, "
                "observation_retention_batches, runs IN ACCESS EXCLUSIVE MODE"
            )
        )

    first_row = bind.scalar(
        sa.text("SELECT 1 FROM run_discovery_observations LIMIT 1")
    )
    if first_row is not None:
        raise RuntimeError(
            "Downgrade requires the run discovery observation table is empty; "
            "provisional evidence is never deleted automatically."
        )

    state_row = bind.scalar(
        sa.text("SELECT 1 FROM run_discovery_observation_states LIMIT 1")
    )
    if state_row is not None:
        raise RuntimeError(
            "Downgrade requires the run discovery observation state table is empty; "
            "retained evidence commitments are never deleted automatically."
        )

    hold_row = bind.scalar(sa.text("SELECT 1 FROM run_retention_holds LIMIT 1"))
    if hold_row is not None:
        raise RuntimeError(
            "Downgrade requires the run retention hold audit table is empty."
        )

    candidate_row = bind.scalar(
        sa.text("SELECT 1 FROM observation_retention_candidates LIMIT 1")
    )
    if candidate_row is not None:
        raise RuntimeError(
            "Downgrade requires the observation retention candidate audit table is empty."
        )

    batch_row = bind.scalar(
        sa.text("SELECT 1 FROM observation_retention_batches LIMIT 1")
    )
    if batch_row is not None:
        raise RuntimeError(
            "Downgrade requires the observation retention batch audit table is empty."
        )

    retention_job_row = bind.scalar(
        sa.text("SELECT 1 FROM observation_retention_jobs LIMIT 1")
    )
    if retention_job_row is not None:
        raise RuntimeError(
            "Downgrade requires the observation retention job audit table is empty."
        )

    active_discovery_run = bind.scalar(
        sa.text(
            "SELECT 1 FROM runs "
            "WHERE job_type IN ('ip_discovery', 'bacnet_discovery') "
            "AND status NOT IN ('succeeded', 'failed', 'cancelled') LIMIT 1"
        )
    )
    if active_discovery_run is not None:
        raise RuntimeError(
            "Downgrade requires all active IP/BACnet discovery runs are terminal."
        )

    op.drop_table("observation_retention_batches")
    op.drop_table("observation_retention_candidates")
    op.drop_table("observation_retention_jobs")
    op.drop_table("run_retention_holds")
    op.drop_table("run_discovery_observation_states")
    op.drop_table("run_discovery_observations")
