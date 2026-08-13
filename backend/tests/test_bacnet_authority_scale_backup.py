"""Scale evidence for immutable BACnet point authorities and backup restore."""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from app.services.backup_service import (
    BackupSources,
    RestoreTarget,
    create_backup_bundle,
    encrypt_backup_bundle,
    restore_backup_bundle,
)
from app.services.discovery_contract_service import (
    SCAN_CONTRACT_MAX_BYTES,
    resolve_bacnet_discovery_parameters,
)
from app.services.run_context_builder import build_run_context
from smart_commissioning_core.db.engine import (
    create_engine_from_url,
    default_sqlite_url,
)
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.integrity import SigningKey
from smart_commissioning_core.run_context import canonical_json_bytes, canonical_sha256

_POINT_COUNT = 25_000
_PROJECT_ID = "scale-project"
_SITE_ID = "scale-site"
_IMPORT_ID = "bacnet-points-25000"
_TIMING_LIMITS_SECONDS = {
    "seed": 60.0,
    "authority": 30.0,
    "context_lookup": 30.0,
    "direct_lookup": 15.0,
    "encrypted_backup_restore": 120.0,
    "restored_context_lookup": 30.0,
    "total": 240.0,
}


def _recipient_rsa_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    return (
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _make_engine(runtime_root: Path):
    database_url = default_sqlite_url(runtime_root)
    upgrade_to_head(database_url)
    return create_engine_from_url(database_url), database_url


class BacnetAuthorityScaleBackupTests(unittest.TestCase):
    def test_25000_point_authority_context_lookup_and_encrypted_restore(self) -> None:
        total_started = time.perf_counter()
        with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as cleanup:
            root = Path(temporary_directory)
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            engine, database_url = _make_engine(runtime_root)
            cleanup.callback(engine.dispose)

            imports_files_root = runtime_root / "imports" / "files"
            imports_files_root.mkdir(parents=True)
            source_bytes = b"25,000-point deterministic BACnet scale fixture\n"
            source_path = imports_files_root / "bacnet_points.csv"
            source_path.write_bytes(source_bytes)

            rows = [
                {
                    "Asset ID": "ahu-1",
                    "Device instance": "2001",
                    "BACnet network": "20",
                    "Internetwork ID": "plant-a",
                    "Object type": "analogInput",
                    "Object instance": str(index),
                    "Expected point name": f"point-{index}",
                }
                for index in range(_POINT_COUNT)
            ]
            accepted_rows_sha256 = canonical_sha256(rows)
            repository = ImportRepository(engine)

            seed_started = time.perf_counter()
            repository.create(
                import_id=_IMPORT_ID,
                import_type="bacnet_points",
                project_id=_PROJECT_ID,
                site_id=_SITE_ID,
                original_filename=source_path.name,
                stored_file_path=str(source_path),
                summary={
                    "import_id": _IMPORT_ID,
                    "import_type": "bacnet_points",
                    "accepted_rows": _POINT_COUNT,
                    "rejected_rows": 0,
                    "file_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "accepted_rows_sha256": accepted_rows_sha256,
                    "authority_schema_version": "1.0",
                },
                accepted_rows=rows,
                errors=[],
                created_at=datetime(2026, 8, 10, tzinfo=UTC),
            )
            seed_seconds = time.perf_counter() - seed_started

            authority_started = time.perf_counter()
            parameters = resolve_bacnet_discovery_parameters(
                {
                    "dry_run": True,
                    "internetwork_id": "plant-a",
                    "bacnet_points_import_id": _IMPORT_ID,
                    "source_ip": "192.0.2.10",
                    "local_address": "192.0.2.10/24",
                    "source_interface_identity_v1": {
                        "schema_version": "1.0",
                        "selection": "explicit",
                        "executor_scope": "test-network-executor",
                        "interface_id": "test-if:1",
                        "interface_name": "Test field NIC",
                        "source_ip": "192.0.2.10",
                        "prefix_length": 24,
                        "local_address": "192.0.2.10/24",
                        "default_route_metric": None,
                    },
                },
                project_id=_PROJECT_ID,
                site_id=_SITE_ID,
                import_repository=repository,
            )
            authority_seconds = time.perf_counter() - authority_started
            contract = parameters["scan_contract_v1"]
            authority = contract["bacnet"]["authorities"]["points"]
            contract_bytes = len(canonical_json_bytes(contract))

            secrets_root = runtime_root / "secrets"
            context_started = time.perf_counter()
            with (
                mock.patch(
                    "app.services.configuration_service.SECRETS_ROOT",
                    secrets_root,
                ),
                mock.patch(
                    "app.services.configuration_service.ensure_runtime_directories",
                    return_value=None,
                ),
            ):
                context = build_run_context(
                    engine=engine,
                    project_id=_PROJECT_ID,
                    site_id=_SITE_ID,
                    job_type="bacnet_discovery",
                    parameters=parameters,
                    requesting_principal="scale-benchmark",
                )
            context_lookup_seconds = time.perf_counter() - context_started
            context_bytes = len(canonical_json_bytes(context.model_dump(mode="json")))
            RunLifecycleRepository(engine).create_run_with_context(
                job_type="bacnet_discovery",
                context=context,
                run_id="run_bacnet_scale_backup",
                dispatch_id="dispatch_bacnet_scale_backup",
                now=datetime(2026, 8, 10, tzinfo=UTC),
            )

            direct_lookup_started = time.perf_counter()
            stored_rows = repository.get_accepted_rows(_IMPORT_ID)
            stored_rows_digest = canonical_sha256(stored_rows)
            direct_lookup_seconds = time.perf_counter() - direct_lookup_started

            public_pem, private_pem = _recipient_rsa_keypair()
            signing_key = SigningKey.generate()
            restored_root = root / "restored"
            restore_target = RestoreTarget(
                database_path=restored_root / "smart_commissioning.db",
                secrets_root=restored_root / "secrets",
                imports_files_root=restored_root / "imports" / "files",
            )
            backup_started = time.perf_counter()
            signed_bundle = create_backup_bundle(
                BackupSources(
                    database_url=database_url,
                    secrets_root=secrets_root,
                    imports_files_root=imports_files_root,
                ),
                created_at=datetime(2026, 8, 10, tzinfo=UTC),
                signing_key=signing_key,
            )
            encrypted_bundle = encrypt_backup_bundle(signed_bundle, public_pem)
            restore_backup_bundle(
                encrypted_bundle,
                restore_target,
                recipient_private_key_pem=private_pem,
            )
            encrypted_backup_restore_seconds = time.perf_counter() - backup_started

            restored_url = f"sqlite:///{restore_target.database_path.as_posix()}"
            restored_engine = create_engine_from_url(restored_url)
            cleanup.callback(restored_engine.dispose)
            restored_repository = ImportRepository(restored_engine)
            restored_rows = restored_repository.get_accepted_rows(_IMPORT_ID)
            restored_rows_digest = canonical_sha256(restored_rows)

            restored_context_started = time.perf_counter()
            with (
                mock.patch(
                    "app.services.configuration_service.SECRETS_ROOT",
                    restore_target.secrets_root,
                ),
                mock.patch(
                    "app.services.configuration_service.ensure_runtime_directories",
                    return_value=None,
                ),
            ):
                restored_context = build_run_context(
                    engine=restored_engine,
                    project_id=_PROJECT_ID,
                    site_id=_SITE_ID,
                    job_type="bacnet_discovery",
                    parameters=parameters,
                    requesting_principal="scale-benchmark",
                )
            restored_context_lookup_seconds = time.perf_counter() - restored_context_started
            total_seconds = time.perf_counter() - total_started

            metrics = {
                "point_count": _POINT_COUNT,
                "contract_bytes": contract_bytes,
                "context_bytes": context_bytes,
                "database_bytes": restore_target.database_path.stat().st_size,
                "signed_bundle_bytes": len(signed_bundle),
                "encrypted_bundle_bytes": len(encrypted_bundle),
                "seed_seconds": round(seed_seconds, 3),
                "authority_seconds": round(authority_seconds, 3),
                "context_lookup_seconds": round(context_lookup_seconds, 3),
                "direct_lookup_seconds": round(direct_lookup_seconds, 3),
                "encrypted_backup_restore_seconds": round(encrypted_backup_restore_seconds, 3),
                "restored_context_lookup_seconds": round(restored_context_lookup_seconds, 3),
                "total_seconds": round(total_seconds, 3),
            }
            evidence = f"25,000-point authority benchmark: {metrics}"

            with self.subTest(metrics=metrics):
                self.assertEqual(authority["accepted_count"], _POINT_COUNT, evidence)
                self.assertEqual(
                    authority["accepted_rows_sha256"],
                    accepted_rows_sha256,
                    evidence,
                )
                self.assertNotIn("accepted_rows", authority, evidence)
                self.assertLess(contract_bytes, SCAN_CONTRACT_MAX_BYTES, evidence)
                self.assertLess(context_bytes, SCAN_CONTRACT_MAX_BYTES, evidence)
                self.assertEqual(len(stored_rows), _POINT_COUNT, evidence)
                self.assertEqual(stored_rows_digest, accepted_rows_sha256, evidence)
                self.assertEqual(len(restored_rows), _POINT_COUNT, evidence)
                self.assertEqual(restored_rows_digest, accepted_rows_sha256, evidence)
                self.assertEqual(
                    (restore_target.imports_files_root / source_path.name).read_bytes(),
                    source_bytes,
                    evidence,
                )
                self.assertEqual(context.imports, restored_context.imports, evidence)
                self.assertTrue(
                    any(
                        binding.resource_id == _IMPORT_ID and binding.sha256 == accepted_rows_sha256
                        for binding in context.imports
                    ),
                    evidence,
                )

                timings = {
                    "seed": seed_seconds,
                    "authority": authority_seconds,
                    "context_lookup": context_lookup_seconds,
                    "direct_lookup": direct_lookup_seconds,
                    "encrypted_backup_restore": encrypted_backup_restore_seconds,
                    "restored_context_lookup": restored_context_lookup_seconds,
                    "total": total_seconds,
                }
                for phase, elapsed in timings.items():
                    self.assertLess(
                        elapsed,
                        _TIMING_LIMITS_SECONDS[phase],
                        evidence,
                    )


if __name__ == "__main__":
    unittest.main()
