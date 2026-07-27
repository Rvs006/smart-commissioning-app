#!/usr/bin/env python3
"""Tests for hosted frontend render and console inspection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_hosted_frontend import inspect_interaction_report, inspect_rendered_pages


class HostedFrontendChecks(unittest.TestCase):
    expected_version = "v0.1.28"

    def test_accepts_populated_root_and_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = Path(temporary) / "home.html"
            page.write_text(
                '<html><body><div id="root"><h1>Smart Commissioning</h1>'
                '<button type="button">Open</button></div></body></html>',
                encoding="utf-8",
            )
            self.assertEqual(inspect_rendered_pages([page], "DevTools listening"), [])

    def test_rejects_empty_root_and_console_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            page = Path(temporary) / "home.html"
            page.write_text('<div id="root"></div>', encoding="utf-8")
            failures = inspect_rendered_pages(
                [page], "CONSOLE ERROR: Uncaught TypeError: fixture"
            )
            self.assertTrue(any("root stayed empty" in value for value in failures))
            self.assertTrue(any("browser console" in value for value in failures))

    def test_accepts_real_control_interaction_report(self) -> None:
        report = {
            "schema_version": 2,
            "app_version": self.expected_version,
            "routes": [
                {
                    "route": route,
                    "control": "Review Comments",
                    "version_label": self.expected_version,
                    "version_visible": True,
                    "root_populated": True,
                    "opened": True,
                    "closed": True,
                }
                for route in ("/", "/configuration", "/run-history")
            ],
            "browser_errors": [],
        }
        self.assertEqual(inspect_interaction_report(report, self.expected_version), [])

    def test_rejects_unproven_control_interaction(self) -> None:
        report = {
            "schema_version": 2,
            "app_version": self.expected_version,
            "routes": [
                {
                    "route": "/",
                    "control": "Review Comments",
                    "version_label": self.expected_version,
                    "version_visible": True,
                    "root_populated": True,
                    "opened": False,
                    "closed": True,
                }
            ],
            "browser_errors": ["console.error call"],
        }
        failures = inspect_interaction_report(report, self.expected_version)
        self.assertTrue(any("did not prove opened" in value for value in failures))
        self.assertTrue(any("exact hosted routes" in value for value in failures))
        self.assertTrue(any("runtime errors" in value for value in failures))

    def test_rejects_wrong_visible_version_label(self) -> None:
        report = {
            "schema_version": 2,
            "app_version": "dev",
            "routes": [
                {
                    "route": route,
                    "control": "Review Comments",
                    "version_label": "dev",
                    "version_visible": False,
                    "root_populated": True,
                    "opened": True,
                    "closed": True,
                }
                for route in ("/", "/configuration", "/run-history")
            ],
            "browser_errors": [],
        }
        failures = inspect_interaction_report(report, self.expected_version)
        self.assertTrue(any("wrong application version" in value for value in failures))
        self.assertEqual(
            sum("wrong visible version label" in value for value in failures),
            3,
        )
        self.assertEqual(
            sum("did not prove version visibility" in value for value in failures),
            3,
        )


if __name__ == "__main__":
    unittest.main()
