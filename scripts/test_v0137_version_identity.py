#!/usr/bin/env python3
"""Verify all user-visible and artifact version sources are v0.1.37."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V0137VersionIdentityTests(unittest.TestCase):
    def test_runtime_and_package_sources_are_exactly_v0137(self) -> None:
        for relative, variable in (
            ("core/smart_commissioning_core/__init__.py", "__version__"),
            ("backend/app/main.py", "APP_VERSION"),
            ("backend/app/services/run_context_builder.py", "_APP_VERSION"),
            ("backend/app/services/report_artifacts.py", "REPORT_RENDERER_VERSION"),
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(
                re.findall(rf"(?m)^{re.escape(variable)}\s*=\s*[\"']([^\"']+)[\"']\s*$", source),
                ["0.1.37"],
                relative,
            )
        for relative in ("core/pyproject.toml", "backend/pyproject.toml", "worker/pyproject.toml"):
            package = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(package["project"]["version"], "0.1.37", relative)
        for relative in ("frontend/package.json", "frontend/package-lock.json"):
            payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], "0.1.37", relative)
            if relative.endswith("package-lock.json"):
                self.assertEqual(payload["packages"][""]["version"], "0.1.37", relative)

    def test_handoff_files_are_byte_identical_and_current(self) -> None:
        agents = (ROOT / "AGENTS.md").read_bytes()
        claude = (ROOT / "CLAUDE.md").read_bytes()
        self.assertEqual(agents, claude)
        text = agents.decode("utf-8")
        self.assertIn("v0.1.37 is the current release candidate", text)
        self.assertIn("field acceptance is still open", text)
        self.assertNotIn("Before a v0.1.36 release", text)


if __name__ == "__main__":
    unittest.main()
