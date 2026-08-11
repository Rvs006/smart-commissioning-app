"""Worker scan-authority rows remain bound to the sealed preview digest."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKER_ROOT = Path(__file__).resolve().parents[1]
for path in (_REPOSITORY_ROOT / "core", _WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import tasks  # noqa: E402
from smart_commissioning_core.execution_context import (  # noqa: E402
    ExecutionContextIntegrityError,
)
from smart_commissioning_core.run_context import (  # noqa: E402
    RunContextV1,
    canonical_sha256,
)


def _scan_context(rows: list[dict[str, object]]) -> RunContextV1:
    digest = canonical_sha256(rows)
    return RunContextV1.model_validate(
        {
            "project_id": "project-authority",
            "site_id": "site-authority",
            "configuration_snapshot": {},
            "configuration_version": 1,
            "registers": [],
            "imports": [{"resource_id": "imp-authority", "sha256": digest}],
            "schema_versions": {},
            "engine_parameters": {
                "scan_contract_v1": {
                    "ip": {
                        "authority": {
                            "import_id": "imp-authority",
                            "accepted_rows_sha256": digest,
                            "accepted_count": len(rows),
                        }
                    }
                }
            },
            "connection_settings": {},
            "secret_references": {},
            "requesting_principal": "engineer-7",
            "application_version": "0.1.41",
        }
    )


class WorkerImportIntegrityTests(unittest.TestCase):
    def test_bound_rows_are_rehashed_immediately_before_engine_use(self) -> None:
        rows = [{"ip_address": "192.0.2.10", "asset_id": "ahu-1"}]
        context = _scan_context(rows)

        with mock.patch.object(
            tasks.import_repository,
            "get_accepted_rows",
            return_value=rows,
        ):
            self.assertEqual(tasks._import_loader(context)("imp-authority"), rows)

    def test_tampered_bound_rows_fail_closed_before_engine_use(self) -> None:
        context = _scan_context(
            [{"ip_address": "192.0.2.10", "asset_id": "ahu-1"}]
        )
        tampered = [{"ip_address": "192.0.2.11", "asset_id": "ahu-1"}]

        with (
            mock.patch.object(
                tasks.import_repository,
                "get_accepted_rows",
                return_value=tampered,
            ),
            self.assertRaises(ExecutionContextIntegrityError),
        ):
            tasks._import_loader(context)("imp-authority")


if __name__ == "__main__":
    unittest.main()
