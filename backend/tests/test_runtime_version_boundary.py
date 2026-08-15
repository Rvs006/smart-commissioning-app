"""Regression coverage for release identity shared by API, reports, and evidence."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from smart_commissioning_core import __version__

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_VERSION_ENV = "SMART_COMMISSIONING_APP_VERSION"


class RuntimeVersionBoundaryTests(unittest.TestCase):
    def test_report_renderer_refuses_a_runtime_stamp_from_another_release(self) -> None:
        from app.services.report_artifacts import effective_report_renderer_version

        with mock.patch.dict(os.environ, {_RUNTIME_VERSION_ENV: "9.9.9"}):
            with self.assertRaisesRegex(RuntimeError, "must match the packaged application version"):
                effective_report_renderer_version()

    def test_backend_refuses_a_runtime_stamp_from_another_release_before_startup(self) -> None:
        environment = dict(os.environ)
        environment[_RUNTIME_VERSION_ENV] = "9.9.9"
        source_paths = [
            str(_REPOSITORY_ROOT / "backend"),
            str(_REPOSITORY_ROOT / "core"),
            environment.get("PYTHONPATH", ""),
        ]
        environment["PYTHONPATH"] = os.pathsep.join(path for path in source_paths if path)

        result = subprocess.run(
            [sys.executable, "-c", "from app.main import app"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match the packaged application version", result.stderr)

    def test_backend_accepts_the_canonical_portable_stamp_at_startup(self) -> None:
        environment = dict(os.environ)
        environment[_RUNTIME_VERSION_ENV] = f"v{__version__}"
        source_paths = [
            str(_REPOSITORY_ROOT / "backend"),
            str(_REPOSITORY_ROOT / "core"),
            environment.get("PYTHONPATH", ""),
        ]
        environment["PYTHONPATH"] = os.pathsep.join(path for path in source_paths if path)

        result = subprocess.run(
            [sys.executable, "-c", "from app.main import app"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lifespan_rejects_an_environment_change_after_import(self) -> None:
        from app.main import app, lifespan

        async def start_lifespan() -> None:
            async with lifespan(app):
                pass

        with mock.patch.dict(os.environ, {_RUNTIME_VERSION_ENV: "9.9.9"}):
            with self.assertRaisesRegex(RuntimeError, "must match the packaged application version"):
                asyncio.run(start_lifespan())

    def test_development_stamp_is_rejected_before_it_can_misidentify_docker_output(self) -> None:
        from app.versioning import effective_application_version

        with mock.patch.dict(os.environ, {_RUNTIME_VERSION_ENV: "dev"}):
            with self.assertRaisesRegex(RuntimeError, "must match the packaged application version"):
                effective_application_version()

    def test_matching_portable_stamp_is_retained_for_report_provenance(self) -> None:
        from app.services.report_artifacts import effective_report_renderer_version

        with mock.patch.dict(os.environ, {_RUNTIME_VERSION_ENV: f"v{__version__}"}):
            self.assertEqual(effective_report_renderer_version(), f"v{__version__}")

    def test_run_context_refuses_a_mismatched_stamp_before_reading_configuration(self) -> None:
        from app.services.run_context_builder import build_run_context

        with mock.patch.dict(os.environ, {_RUNTIME_VERSION_ENV: "9.9.9"}):
            with self.assertRaisesRegex(RuntimeError, "must match the packaged application version"):
                build_run_context(
                    engine=None,
                    project_id="version-boundary-project",
                    site_id="version-boundary-site",
                    job_type="ip_discovery",
                    parameters={},
                    requesting_principal="version-boundary-test",
                )


if __name__ == "__main__":
    unittest.main()
