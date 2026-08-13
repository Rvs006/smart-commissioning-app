"""bind report rows to an explicit evidence contract

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-11 09:00:00.000000
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_F6_MIGRATION = "v0.1.26"


def upgrade() -> None:
    op.create_table(
        "report_evidence_contracts",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=255), nullable=False),
        sa.Column("site_id", sa.String(length=255), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version IN ('sealed_v1', 'legacy_pre_lifecycle')",
            name="ck_report_evidence_contracts_version",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_report_evidence_contracts_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_report_evidence_contracts")),
    )
    op.create_index(
        "ix_report_evidence_contracts_scope",
        "report_evidence_contracts",
        ["project_id", "site_id", "run_id"],
        unique=False,
    )
    _backfill_report_contracts(op.get_bind())


def _backfill_report_contracts(connection) -> None:  # noqa: ANN001
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=connection)
    contracts = sa.Table("report_evidence_contracts", metadata, autoload_with=connection)
    sync_artifacts = (
        sa.Table("sync_artifacts", metadata, autoload_with=connection)
        if sa.inspect(connection).has_table("sync_artifacts")
        else None
    )
    synchronized_run_ids = (
        set(connection.execute(sa.select(sync_artifacts.c.run_id)).scalars())
        if sync_artifacts is not None
        else set()
    )
    now = datetime.now(UTC)
    rows = connection.execute(
        sa.select(
            runs.c.id,
            runs.c.project_id,
            runs.c.site_id,
            runs.c.parameters,
            runs.c.result_summary,
        ).where(runs.c.job_type == "report_generation")
    ).mappings()
    for row in rows:
        parameters = row["parameters"] if isinstance(row["parameters"], Mapping) else {}
        summary = row["result_summary"] if isinstance(row["result_summary"], Mapping) else {}
        legacy_marker = summary.get("legacy_report_integrity")
        if (
            isinstance(legacy_marker, Mapping)
            and legacy_marker.get("migration") == _F6_MIGRATION
            and legacy_marker.get("silently_resigned") is False
        ):
            contract_version = "legacy_pre_lifecycle"
        elif (
            "report_snapshot_v2" in parameters
            or "report_snapshot_sha256" in parameters
            or "artifact_manifest" in summary
            or row["id"] in synchronized_run_ids
        ):
            contract_version = "sealed_v1"
        else:
            # Ambiguous rows deliberately remain unclassified and fail closed.
            continue
        connection.execute(
            contracts.insert().values(
                run_id=row["id"],
                contract_version=contract_version,
                project_id=row["project_id"],
                site_id=row["site_id"],
                classified_at=now,
            )
        )


def downgrade() -> None:
    op.drop_table("report_evidence_contracts")
