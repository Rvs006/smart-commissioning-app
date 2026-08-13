#!/usr/bin/env python3
"""Verify release-facing version sources are v0.1.41."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V0141VersionIdentityTests(unittest.TestCase):
    def test_runtime_package_and_renderer_versions_are_v0141(self) -> None:
        for relative, variable in (
            ("core/smart_commissioning_core/__init__.py", "__version__"),
            ("backend/app/main.py", "APP_VERSION"),
            ("backend/app/services/run_context_builder.py", "_APP_VERSION"),
            ("backend/app/services/report_artifacts.py", "REPORT_RENDERER_VERSION"),
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(
                re.findall(rf"(?m)^{re.escape(variable)}\s*=\s*[\"']([^\"']+)[\"']$", source),
                ["0.1.41"],
                relative,
            )
        for relative in ("core/pyproject.toml", "backend/pyproject.toml", "worker/pyproject.toml"):
            package = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(package["project"]["version"], "0.1.41", relative)

        frontend = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(frontend["version"], "0.1.41")
        self.assertEqual(lock["version"], "0.1.41")
        self.assertEqual(lock["packages"][""]["version"], "0.1.41")
        for relative in ("AGENTS.md", "CLAUDE.md", "CHANGELOG.md"):
            self.assertIn("v0.1.41", (ROOT / relative).read_text(encoding="utf-8"), relative)
        for relative in ("AGENTS.md", "CLAUDE.md"):
            lineage = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("v0.1.40 base commit `085e79b876998a426ac7078b63a2abec36f900a6`", lineage, relative)
            self.assertIn("latest public release is v0.1.41", lineage, relative)
            self.assertIn("there is no later candidate", lineage, relative)
            self.assertNotIn("prepared from the verified v0.1.38 release line", lineage, relative)
            self.assertNotIn("latest public release remains v0.1.38", lineage, relative)


if __name__ == "__main__":
    unittest.main()
