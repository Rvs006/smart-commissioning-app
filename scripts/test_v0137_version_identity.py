#!/usr/bin/env python3
"""Compatibility check for the current v0.1.38 version identity."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V0137VersionIdentityTests(unittest.TestCase):
    def test_runtime_and_package_sources_are_exactly_v0138(self) -> None:
        for relative, variable in (
            ("core/smart_commissioning_core/__init__.py", "__version__"),
            ("backend/app/main.py", "APP_VERSION"),
            ("backend/app/services/run_context_builder.py", "_APP_VERSION"),
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertEqual(
                re.findall(rf"(?m)^{re.escape(variable)}\s*=\s*[\"']([^\"']+)[\"']\s*$", source),
                ["0.1.38"],
                relative,
            )
        renderer_source = (ROOT / "backend/app/services/report_artifacts.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            re.findall(
                r"(?m)^REPORT_RENDERER_VERSION\s*=\s*[\"']([^\"']+)[\"']$",
                renderer_source,
            ),
            ["0.1.38"],
        )
        for relative in ("core/pyproject.toml", "backend/pyproject.toml", "worker/pyproject.toml"):
            package = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(package["project"]["version"], "0.1.38", relative)
        for relative in ("frontend/package.json", "frontend/package-lock.json"):
            payload = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], "0.1.38", relative)
            if relative.endswith("package-lock.json"):
                self.assertEqual(payload["packages"][""]["version"], "0.1.38", relative)

    def test_handoff_files_are_byte_identical_and_current(self) -> None:
        agents = (ROOT / "AGENTS.md").read_bytes()
        claude = (ROOT / "CLAUDE.md").read_bytes()
        self.assertEqual(agents, claude)
        text = agents.decode("utf-8")
        self.assertIn("v0.1.38 release line", text)
        self.assertIn("Field acceptance remains open", text)
        self.assertNotIn("Before a v0.1.36 release", text)


if __name__ == "__main__":
    unittest.main()
