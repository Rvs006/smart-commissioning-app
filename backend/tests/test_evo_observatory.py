import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.routes.evo import get_evo_overview


class EvoObservatoryTests(unittest.TestCase):
    def test_missing_snapshot_reports_pending_without_inventing_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch.dict(os.environ, {"EVO_OBSERVATORY_PATH": str(missing)}):
                result = get_evo_overview()

        self.assertEqual(result["workspace_status"], "not_initialized")
        self.assertEqual(result["protected_release"], "v0.1.40")
        self.assertIsNone(result["baseline"])
        self.assertEqual(result["experiments"], [])

    def test_snapshot_values_are_returned_with_protection_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observatory.json"
            path.write_text(
                json.dumps({"workspace_status": "ready", "baseline": {"id": "exp_0000"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"EVO_OBSERVATORY_PATH": str(path)}):
                result = get_evo_overview()

        self.assertEqual(result["workspace_status"], "ready")
        self.assertEqual(result["baseline"], {"id": "exp_0000"})
        self.assertEqual(result["protected_commit"], "b3b2f764b78449dae2f5232cd7aab1f2d47c30eb")


if __name__ == "__main__":
    unittest.main()
