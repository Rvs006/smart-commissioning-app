from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.migrate import build_alembic_config, upgrade_to_head
from smart_commissioning_core.db.models import (
    NmapDeploymentPolicy,
    NmapInstallationConfirmation,
)
from smart_commissioning_core.run_context import canonical_json_bytes, canonical_sha256
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_SCOPES = [{"project_id": "project-a", "site_id": "site-a"}]
_SCOPES_JSON = '[{"project_id":"project-a","site_id":"site-a"}]'
_SCOPES_SHA256 = canonical_sha256(_SCOPES)
_CANDIDATE = {
    "schema_version": "1.0",
    "registry_view": "64",
    "registry_key": "Nmap",
    "display_name": "Nmap 7.95",
    "registry_publisher": "Insecure.Com LLC",
    "version": "7.95",
    "install_root": r"C:\\Program Files (x86)\\Nmap",
    "executable_path": r"C:\\Program Files (x86)\\Nmap\\nmap.exe",
    "data_directory": r"C:\\Program Files (x86)\\Nmap",
}
_CANDIDATE_JSON = canonical_json_bytes(_CANDIDATE).decode("utf-8")


def _policy_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "policy_id": "policy-1",
        "deployment_id": "deployment-1",
        "network_executor_id": "inline:deployment-1",
        "revision": 1,
        "deployment_lane": "internal_same_organization",
        "provider_mode": "internal_operator_managed",
        "organization_internal": True,
        "deployment_owner": "deployment-owner",
        "operator_install_responsibility": "Internal IT installs the official package.",
        "permitted_project_sites_json": _SCOPES_JSON,
        "permitted_project_sites_sha256": _SCOPES_SHA256,
        "update_owner": "update-owner",
        "reviewed_version_policy": "Nmap 7.95 only",
        "permitted_publishers_json": '["Insecure.Com LLC"]',
        "permitted_versions_json": '["7.95"]',
        "permitted_signer_sha256_json": f'["{_SHA_B}"]',
        "permitted_executable_sha256_json": f'["{_SHA_A}"]',
        "permitted_data_manifest_sha256_json": f'["{_SHA_B}"]',
        "permitted_licence_sha256_json": f'["{_SHA_A}"]',
        "permitted_npsl_versions_json": '["1.0"]',
        "reviewed_scripts_json": "[]",
        "max_data_files": 8192,
        "max_file_bytes": 64 * 1024 * 1024,
        "max_manifest_bytes": 512 * 1024 * 1024,
        "profile_policy_json": '["tcp_connect_inventory"]',
        "profile_policy_sha256": _SHA_B,
        "reviewed_at": _NOW,
        "acknowledged_no_redistribution": True,
        "created_by": "admin-user",
        "reason": "Initial internal provider approval.",
        "created_at": _NOW,
        "supersedes_policy_id": None,
    }
    values.update(overrides)
    return values


def _confirmation_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "confirmation_id": "confirmation-1",
        "policy_id": "policy-1",
        "deployment_id": "deployment-1",
        "network_executor_id": "inline:deployment-1",
        "policy_revision": 1,
        "deployment_lane": "internal_same_organization",
        "provider_mode": "internal_operator_managed",
        "permitted_project_sites_json": _SCOPES_JSON,
        "permitted_project_sites_sha256": _SCOPES_SHA256,
        "detection_candidate_json": _CANDIDATE_JSON,
        "detection_candidate_sha256": canonical_sha256(_CANDIDATE),
        "machine_executor_identity": "machine-guid:12345678-1234-1234-1234-123456789abc",
        "publisher": "Insecure.Com LLC",
        "version": "7.95",
        "install_root": r"C:\\Program Files (x86)\\Nmap",
        "executable_path": r"C:\\Program Files (x86)\\Nmap\\nmap.exe",
        "data_directory": r"C:\\Program Files (x86)\\Nmap",
        "executable_sha256": _SHA_A,
        "data_manifest_sha256": _SHA_B,
        "data_file_count": 48,
        "data_total_bytes": 1048576,
        "licence_relative_path": "LICENSE",
        "licence_sha256": _SHA_A,
        "npsl_version": "1.0",
        "reviewed_scripts_json": "[]",
        "signer_sha256": _SHA_B,
        "fingerprint_sha256": _SHA_B,
        "npcap_version": "1.79",
        "npcap_state": "raw_capable",
        "raw_capable": True,
        "confirmed_by": "admin-user",
        "reason": "Verified during the maintenance window.",
        "confirmed_at": _NOW,
    }
    values.update(overrides)
    return values


class NmapCapabilitySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        # Alembic's deliberately failed downgrade may leave a short-lived
        # SQLite handle on Windows; cleanup is best-effort after engine.dispose.
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._temp.cleanup)
        database_path = Path(self._temp.name) / "nmap-capability.db"
        self.url = f"sqlite:///{database_path.as_posix()}"
        upgrade_to_head(self.url)
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

    def test_migration_matches_models_and_creates_authority_tables(self) -> None:
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("nmap_deployment_policies", tables)
        self.assertIn("nmap_installation_confirmations", tables)
        with self.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_external_lane_cannot_enable_execution_or_xml_import(self) -> None:
        invalid_modes = ("internal_operator_managed", "operator_xml_import")
        for index, mode in enumerate(invalid_modes, start=1):
            with self.subTest(mode=mode), self.assertRaises(IntegrityError):
                with Session(self.engine) as session, session.begin():
                    session.add(
                        NmapDeploymentPolicy(
                            **_policy_values(
                                policy_id=f"external-{index}",
                                deployment_lane="external_customer",
                                provider_mode=mode,
                                organization_internal=False,
                            )
                        )
                    )

    def test_policy_revision_and_all_digest_fields_are_constrained(self) -> None:
        invalid = (
            {"revision": 0},
            {"profile_policy_sha256": "A" * 64},
            {"profile_policy_sha256": "a" * 63},
        )
        for index, overrides in enumerate(invalid, start=1):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError):
                with Session(self.engine) as session, session.begin():
                    session.add(NmapDeploymentPolicy(**_policy_values(policy_id=f"invalid-{index}", **overrides)))

    def test_confirmation_binds_policy_lane_actor_time_and_exact_fingerprint(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(NmapDeploymentPolicy(**_policy_values()))
            session.add(NmapInstallationConfirmation(**_confirmation_values()))

        with Session(self.engine) as session:
            stored = session.scalar(
                select(NmapInstallationConfirmation).where(
                    NmapInstallationConfirmation.confirmation_id == "confirmation-1"
                )
            )
            assert stored is not None
            self.assertEqual(stored.policy_id, "policy-1")
            self.assertEqual(stored.deployment_lane, "internal_same_organization")
            self.assertEqual(stored.confirmed_by, "admin-user")
            self.assertEqual(stored.confirmed_at, _NOW)
            self.assertEqual(stored.fingerprint_sha256, _SHA_B)

    def test_confirmation_requires_canonical_machine_executor_identity(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(NmapDeploymentPolicy(**_policy_values()))

        invalid_identities = (
            "executor-a",
            "machine-guid:AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "machine-guid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa",
        )
        for index, identity in enumerate(invalid_identities, start=1):
            with self.subTest(identity=identity), self.assertRaises(IntegrityError):
                with Session(self.engine) as session, session.begin():
                    session.add(
                        NmapInstallationConfirmation(
                            **_confirmation_values(
                                confirmation_id=f"invalid-machine-{index}",
                                machine_executor_identity=identity,
                            )
                        )
                    )

    def test_authority_rows_reject_update_and_delete(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(NmapDeploymentPolicy(**_policy_values()))
            session.add(NmapInstallationConfirmation(**_confirmation_values()))

        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(
                    update(NmapInstallationConfirmation)
                    .where(NmapInstallationConfirmation.confirmation_id == "confirmation-1")
                    .values(reason="rewritten")
                )
        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM nmap_installation_confirmations WHERE confirmation_id = 'confirmation-1'")
                )
        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(
                    update(NmapDeploymentPolicy)
                    .where(NmapDeploymentPolicy.policy_id == "policy-1")
                    .values(reason="rewritten")
                )
        with Session(self.engine) as session, session.begin():
            session.add(
                NmapDeploymentPolicy(
                    **_policy_values(
                        policy_id="policy-delete-guard",
                        deployment_id="deployment-delete-guard",
                        network_executor_id="inline:deployment-delete-guard",
                    )
                )
            )
        with self.assertRaises(DatabaseError):
            with self.engine.begin() as connection:
                connection.execute(text("DELETE FROM nmap_deployment_policies WHERE policy_id = 'policy-delete-guard'"))

    def test_downgrade_refuses_to_delete_nonempty_authority_history(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(NmapDeploymentPolicy(**_policy_values()))

        with self.assertRaisesRegex(RuntimeError, "Nmap authority"):
            command.downgrade(build_alembic_config(self.url), "f3b4c5d6e7f8")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(text("SELECT version_num FROM alembic_version")),
                "f4c5d6e7f8a9",
            )


if __name__ == "__main__":
    unittest.main()
