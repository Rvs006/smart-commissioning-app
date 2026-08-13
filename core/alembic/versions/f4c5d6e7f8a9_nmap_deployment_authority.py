"""add deployment-scoped Nmap policy and installation confirmations

Revision ID: f4c5d6e7f8a9
Revises: f3b4c5d6e7f8
Create Date: 2026-08-12 14:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c5d6e7f8a9"
down_revision: str | None = "f3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lowercase_sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column_name}) = 64 AND {column_name} = lower({column_name}) AND length({remainder}) = 0"


def _create_immutable_history_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("nmap_deployment_policies", "nmap_installation_confirmations")
    if dialect == "sqlite":
        for table in tables:
            for operation in ("UPDATE", "DELETE"):
                trigger = f"trg_{table}_no_{operation.lower()}"
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER {trigger} BEFORE {operation} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, "
                        f"'{table} is immutable history'); END"
                    )
                )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                "CREATE FUNCTION reject_nmap_authority_history_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'Nmap authority history is immutable'; END; $$"
            )
        )
        for table in tables:
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table}_immutable "
                    f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                    "EXECUTE FUNCTION reject_nmap_authority_history_mutation()"
                )
            )


def _drop_immutable_history_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("nmap_deployment_policies", "nmap_installation_confirmations")
    if dialect == "sqlite":
        for table in tables:
            for operation in ("update", "delete"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_no_{operation}"))
    elif dialect == "postgresql":
        for table in tables:
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS reject_nmap_authority_history_mutation()"))


def upgrade() -> None:
    op.create_table(
        "nmap_deployment_policies",
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=255), nullable=False),
        sa.Column("network_executor_id", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deployment_lane", sa.String(length=32), nullable=False),
        sa.Column("provider_mode", sa.String(length=32), nullable=False),
        sa.Column("organization_internal", sa.Boolean(), nullable=False),
        sa.Column("deployment_owner", sa.String(length=255), nullable=False),
        sa.Column("operator_install_responsibility", sa.Text(), nullable=False),
        sa.Column("permitted_project_sites_json", sa.Text(), nullable=False),
        sa.Column("permitted_project_sites_sha256", sa.String(length=64), nullable=False),
        sa.Column("update_owner", sa.String(length=255), nullable=False),
        sa.Column("reviewed_version_policy", sa.Text(), nullable=False),
        sa.Column("permitted_publishers_json", sa.Text(), nullable=False),
        sa.Column("permitted_versions_json", sa.Text(), nullable=False),
        sa.Column("permitted_signer_sha256_json", sa.Text(), nullable=False),
        sa.Column("permitted_executable_sha256_json", sa.Text(), nullable=False),
        sa.Column("permitted_data_manifest_sha256_json", sa.Text(), nullable=False),
        sa.Column("permitted_licence_sha256_json", sa.Text(), nullable=False),
        sa.Column("permitted_npsl_versions_json", sa.Text(), nullable=False),
        sa.Column("reviewed_scripts_json", sa.Text(), nullable=False),
        sa.Column("max_data_files", sa.Integer(), nullable=False),
        sa.Column("max_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_manifest_bytes", sa.BigInteger(), nullable=False),
        sa.Column("profile_policy_json", sa.Text(), nullable=False),
        sa.Column("profile_policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_no_redistribution", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_policy_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_nmap_deployment_policies_revision"),
        sa.CheckConstraint(
            "deployment_lane IN ('internal_same_organization', 'external_customer')",
            name="ck_nmap_deployment_policies_lane",
        ),
        sa.CheckConstraint(
            "provider_mode IN ('disabled', 'operator_xml_import', 'internal_operator_managed')",
            name="ck_nmap_deployment_policies_mode",
        ),
        sa.CheckConstraint(
            "(deployment_lane = 'internal_same_organization' AND "
            "organization_internal IS TRUE) OR "
            "(deployment_lane = 'external_customer' AND "
            "organization_internal IS FALSE AND provider_mode = 'disabled')",
            name="ck_nmap_deployment_policies_lane_mode",
        ),
        sa.CheckConstraint(
            "provider_mode = 'disabled' OR acknowledged_no_redistribution IS TRUE",
            name="ck_nmap_deployment_policies_acknowledgement",
        ),
        sa.CheckConstraint(
            "length(policy_id) BETWEEN 1 AND 64 AND "
            "length(deployment_id) BETWEEN 1 AND 255 AND "
            "length(network_executor_id) BETWEEN 1 AND 255 AND "
            "length(deployment_owner) BETWEEN 1 AND 255 AND "
            "length(update_owner) BETWEEN 1 AND 255 AND "
            "length(created_by) BETWEEN 1 AND 255",
            name="ck_nmap_deployment_policies_identifiers",
        ),
        sa.CheckConstraint(
            "length(operator_install_responsibility) BETWEEN 1 AND 4096 AND "
            "length(reviewed_version_policy) BETWEEN 1 AND 4096 AND "
            "length(reason) BETWEEN 1 AND 4096",
            name="ck_nmap_deployment_policies_text",
        ),
        sa.CheckConstraint(
            "length(permitted_project_sites_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_publishers_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_versions_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_signer_sha256_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_executable_sha256_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_data_manifest_sha256_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_licence_sha256_json) BETWEEN 2 AND 32768 AND "
            "length(permitted_npsl_versions_json) BETWEEN 2 AND 32768 AND "
            "length(reviewed_scripts_json) BETWEEN 2 AND 32768 AND "
            "length(profile_policy_json) BETWEEN 2 AND 32768",
            name="ck_nmap_deployment_policies_json_bounds",
        ),
        sa.CheckConstraint(
            _lowercase_sha256_check("permitted_project_sites_sha256")
            + " AND "
            + _lowercase_sha256_check("profile_policy_sha256"),
            name="ck_nmap_deployment_policies_policy_sha256",
        ),
        sa.CheckConstraint(
            "max_data_files BETWEEN 1 AND 65536 AND "
            "max_file_bytes BETWEEN 1024 AND 536870912 AND "
            "max_manifest_bytes BETWEEN 1024 AND 4294967296 AND "
            "max_manifest_bytes >= max_file_bytes",
            name="ck_nmap_deployment_policies_manifest_limits",
        ),
        sa.CheckConstraint(
            "supersedes_policy_id IS NULL OR supersedes_policy_id <> policy_id",
            name="ck_nmap_deployment_policies_not_self_superseding",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_policy_id"],
            ["nmap_deployment_policies.policy_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("policy_id"),
        sa.UniqueConstraint(
            "deployment_id",
            "network_executor_id",
            "revision",
            name="uq_nmap_deployment_policies_revision",
        ),
        sa.UniqueConstraint(
            "supersedes_policy_id",
            name="uq_nmap_deployment_policies_linear_history",
        ),
        sa.UniqueConstraint(
            "policy_id",
            "deployment_id",
            "network_executor_id",
            "revision",
            "deployment_lane",
            "provider_mode",
            "permitted_project_sites_sha256",
            name="uq_nmap_deployment_policies_confirmation_binding",
        ),
    )
    op.create_index(
        op.f("ix_nmap_deployment_policies_deployment_id"),
        "nmap_deployment_policies",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_nmap_deployment_policies_network_executor_id"),
        "nmap_deployment_policies",
        ["network_executor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_nmap_deployment_policies_permitted_project_sites_sha256"),
        "nmap_deployment_policies",
        ["permitted_project_sites_sha256"],
        unique=False,
    )
    op.create_index(
        op.f("ix_nmap_deployment_policies_profile_policy_sha256"),
        "nmap_deployment_policies",
        ["profile_policy_sha256"],
        unique=False,
    )
    op.create_index(
        "ix_nmap_deployment_policies_current",
        "nmap_deployment_policies",
        ["deployment_id", "network_executor_id", "revision"],
        unique=False,
    )

    op.create_table(
        "nmap_installation_confirmations",
        sa.Column("confirmation_id", sa.String(length=64), nullable=False),
        sa.Column("policy_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=255), nullable=False),
        sa.Column("network_executor_id", sa.String(length=255), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("deployment_lane", sa.String(length=32), nullable=False),
        sa.Column("provider_mode", sa.String(length=32), nullable=False),
        sa.Column("permitted_project_sites_json", sa.Text(), nullable=False),
        sa.Column("permitted_project_sites_sha256", sa.String(length=64), nullable=False),
        sa.Column("detection_candidate_json", sa.Text(), nullable=False),
        sa.Column("detection_candidate_sha256", sa.String(length=64), nullable=False),
        sa.Column("machine_executor_identity", sa.String(length=255), nullable=False),
        sa.Column("publisher", sa.String(length=512), nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("install_root", sa.Text(), nullable=False),
        sa.Column("executable_path", sa.Text(), nullable=False),
        sa.Column("data_directory", sa.Text(), nullable=False),
        sa.Column("executable_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_file_count", sa.Integer(), nullable=False),
        sa.Column("data_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("licence_relative_path", sa.String(length=255), nullable=False),
        sa.Column("licence_sha256", sa.String(length=64), nullable=False),
        sa.Column("npsl_version", sa.String(length=32), nullable=False),
        sa.Column("reviewed_scripts_json", sa.Text(), nullable=False),
        sa.Column("signer_sha256", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("npcap_version", sa.String(length=128), nullable=True),
        sa.Column("npcap_state", sa.String(length=32), nullable=False),
        sa.Column("raw_capable", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "policy_revision > 0",
            name="ck_nmap_installation_confirmations_policy_revision",
        ),
        sa.CheckConstraint(
            "deployment_lane = 'internal_same_organization' AND provider_mode = 'internal_operator_managed'",
            name="ck_nmap_installation_confirmations_internal_only",
        ),
        sa.CheckConstraint(
            "length(confirmation_id) BETWEEN 1 AND 64 AND "
            "length(policy_id) BETWEEN 1 AND 64 AND "
            "length(deployment_id) BETWEEN 1 AND 255 AND "
            "length(network_executor_id) BETWEEN 1 AND 255 AND "
            "length(machine_executor_identity) BETWEEN 1 AND 255 AND "
            "length(confirmed_by) BETWEEN 1 AND 255",
            name="ck_nmap_installation_confirmations_identifiers",
        ),
        sa.CheckConstraint(
            "length(machine_executor_identity) = 49 AND "
            "substr(machine_executor_identity, 1, 13) = 'machine-guid:' AND "
            "machine_executor_identity = lower(machine_executor_identity)",
            name="ck_nmap_installation_confirmations_machine_identity",
        ),
        sa.CheckConstraint(
            "length(permitted_project_sites_json) BETWEEN 2 AND 32768",
            name="ck_nmap_installation_confirmations_scope_json",
        ),
        sa.CheckConstraint(
            "length(detection_candidate_json) BETWEEN 2 AND 32768",
            name="ck_nmap_installation_confirmations_candidate_json",
        ),
        sa.CheckConstraint(
            "length(reviewed_scripts_json) BETWEEN 2 AND 32768",
            name="ck_nmap_installation_confirmations_scripts_json",
        ),
        sa.CheckConstraint(
            "length(publisher) BETWEEN 1 AND 512 AND "
            "length(version) BETWEEN 1 AND 128 AND "
            "length(npsl_version) BETWEEN 1 AND 32",
            name="ck_nmap_installation_confirmations_identity_text",
        ),
        sa.CheckConstraint(
            "length(install_root) BETWEEN 3 AND 2048 AND "
            "length(executable_path) BETWEEN 3 AND 2048 AND "
            "length(data_directory) BETWEEN 3 AND 2048",
            name="ck_nmap_installation_confirmations_path_bounds",
        ),
        sa.CheckConstraint(
            "data_file_count BETWEEN 1 AND 65536",
            name="ck_nmap_installation_confirmations_file_count",
        ),
        sa.CheckConstraint(
            "data_total_bytes BETWEEN 1 AND 4294967296",
            name="ck_nmap_installation_confirmations_data_bytes",
        ),
        sa.CheckConstraint(
            "length(licence_relative_path) BETWEEN 1 AND 255 AND licence_relative_path NOT IN ('.', '..')",
            name="ck_nmap_installation_confirmations_licence_path",
        ),
        sa.CheckConstraint(
            "npcap_state IN ('not_checked', 'missing', 'admin_only', 'connect_only', 'raw_capable')",
            name="ck_nmap_installation_confirmations_npcap_state",
        ),
        sa.CheckConstraint(
            "(raw_capable IS TRUE AND npcap_state = 'raw_capable') OR "
            "(raw_capable IS FALSE AND npcap_state <> 'raw_capable')",
            name="ck_nmap_installation_confirmations_raw_capability",
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 4096",
            name="ck_nmap_installation_confirmations_reason",
        ),
        sa.CheckConstraint(
            "npcap_version IS NULL OR length(npcap_version) BETWEEN 1 AND 128",
            name="ck_nmap_installation_confirmations_npcap_version",
        ),
        sa.CheckConstraint(
            " AND ".join(
                _lowercase_sha256_check(column_name)
                for column_name in (
                    "executable_sha256",
                    "data_manifest_sha256",
                    "licence_sha256",
                    "signer_sha256",
                    "permitted_project_sites_sha256",
                    "detection_candidate_sha256",
                    "fingerprint_sha256",
                )
            ),
            name="ck_nmap_installation_confirmations_sha256",
        ),
        sa.ForeignKeyConstraint(
            [
                "policy_id",
                "deployment_id",
                "network_executor_id",
                "policy_revision",
                "deployment_lane",
                "provider_mode",
                "permitted_project_sites_sha256",
            ],
            [
                "nmap_deployment_policies.policy_id",
                "nmap_deployment_policies.deployment_id",
                "nmap_deployment_policies.network_executor_id",
                "nmap_deployment_policies.revision",
                "nmap_deployment_policies.deployment_lane",
                "nmap_deployment_policies.provider_mode",
                "nmap_deployment_policies.permitted_project_sites_sha256",
            ],
            ondelete="RESTRICT",
            name="fk_nmap_confirmations_exact_policy",
        ),
        sa.PrimaryKeyConstraint("confirmation_id"),
    )
    for column_name in (
        "deployment_id",
        "network_executor_id",
        "permitted_project_sites_sha256",
        "detection_candidate_sha256",
        "machine_executor_identity",
        "executable_sha256",
        "data_manifest_sha256",
        "licence_sha256",
        "signer_sha256",
        "fingerprint_sha256",
    ):
        op.create_index(
            op.f(f"ix_nmap_installation_confirmations_{column_name}"),
            "nmap_installation_confirmations",
            [column_name],
            unique=False,
        )
    op.create_index(
        "ix_nmap_installation_confirmations_policy_time",
        "nmap_installation_confirmations",
        ["policy_id", "confirmed_at"],
        unique=False,
    )
    op.create_index(
        "ix_nmap_installation_confirmations_deployment_time",
        "nmap_installation_confirmations",
        ["deployment_id", "confirmed_at"],
        unique=False,
    )
    _create_immutable_history_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("LOCK TABLE nmap_installation_confirmations, nmap_deployment_policies IN ACCESS EXCLUSIVE MODE")
        )
    authority_exists = bind.scalar(
        sa.text(
            "SELECT 1 WHERE "
            "EXISTS (SELECT 1 FROM nmap_installation_confirmations) OR "
            "EXISTS (SELECT 1 FROM nmap_deployment_policies)"
        )
    )
    if authority_exists is not None:
        raise RuntimeError("Downgrade refuses to delete nonempty Nmap authority history.")

    _drop_immutable_history_guards()
    op.drop_index(
        "ix_nmap_installation_confirmations_deployment_time",
        table_name="nmap_installation_confirmations",
    )
    op.drop_index(
        "ix_nmap_installation_confirmations_policy_time",
        table_name="nmap_installation_confirmations",
    )
    for column_name in reversed(
        (
            "deployment_id",
            "network_executor_id",
            "permitted_project_sites_sha256",
            "detection_candidate_sha256",
            "machine_executor_identity",
            "executable_sha256",
            "data_manifest_sha256",
            "licence_sha256",
            "signer_sha256",
            "fingerprint_sha256",
        )
    ):
        op.drop_index(
            op.f(f"ix_nmap_installation_confirmations_{column_name}"),
            table_name="nmap_installation_confirmations",
        )
    op.drop_table("nmap_installation_confirmations")
    op.drop_index(
        "ix_nmap_deployment_policies_current",
        table_name="nmap_deployment_policies",
    )
    op.drop_index(
        op.f("ix_nmap_deployment_policies_profile_policy_sha256"),
        table_name="nmap_deployment_policies",
    )
    op.drop_index(
        op.f("ix_nmap_deployment_policies_permitted_project_sites_sha256"),
        table_name="nmap_deployment_policies",
    )
    op.drop_index(
        op.f("ix_nmap_deployment_policies_network_executor_id"),
        table_name="nmap_deployment_policies",
    )
    op.drop_index(
        op.f("ix_nmap_deployment_policies_deployment_id"),
        table_name="nmap_deployment_policies",
    )
    op.drop_table("nmap_deployment_policies")
