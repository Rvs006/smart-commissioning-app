#!/usr/bin/env python3
"""Verify release-facing version sources and runtime boundary are v0.1.46."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V0146VersionIdentityTests(unittest.TestCase):
    def test_runtime_package_frontend_and_documentation_identity_are_v0146(self) -> None:
        source = (ROOT / "core/smart_commissioning_core/__init__.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']$', source), ["0.1.46"])
        for relative in ("core/pyproject.toml", "backend/pyproject.toml", "worker/pyproject.toml"):
            package = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(package["project"]["version"], "0.1.46", relative)
        frontend = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(frontend["version"], "0.1.46")
        self.assertEqual(lock["version"], "0.1.46")
        self.assertEqual(lock["packages"][""]["version"], "0.1.46")
        for relative in ("AGENTS.md", "CLAUDE.md"):
            lineage = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("v0.1.46 candidate", lineage, relative)
            self.assertIn("latest public release is v0.1.44", lineage, relative)
        self.assertIn("[0.1.46]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_every_runtime_surface_uses_the_validated_identity_resolver(self) -> None:
        required_resolvers = {
            "backend/app/main.py": "validate_runtime_application_version",
            "backend/app/api/routes/health.py": "effective_application_version",
            "backend/app/services/report_artifacts.py": "effective_application_version",
            "backend/app/services/run_context_builder.py": "effective_application_version",
        }
        for relative, resolver in required_resolvers.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(resolver, source, relative)

    def test_portable_builder_uses_the_canonical_package_version_without_git_describe(self) -> None:
        source = (ROOT / "packaging/windows_portable/build.ps1").read_text(encoding="utf-8")

        self.assertIn('$BuildVersion = "v$PackagedVersion"', source)
        self.assertIn("must match the packaged application version", source)
        self.assertNotIn("describe --tags", source)

    def test_docker_defaults_use_the_canonical_release_stamp(self) -> None:
        for relative in ("backend/Dockerfile", "worker/Dockerfile", "frontend/Dockerfile"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("ARG APP_VERSION=v0.1.46", source, relative)
        for relative in ("infra/docker-compose.yml", "infra/docker-compose.sync-acceptance.yml"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('APP_VERSION: "${APP_VERSION:-v0.1.46}"', source, relative)


if __name__ == "__main__":
    unittest.main()
