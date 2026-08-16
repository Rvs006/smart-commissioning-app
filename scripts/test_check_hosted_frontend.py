#!/usr/bin/env python3
"""Tests for hosted frontend render and console inspection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from check_hosted_frontend import inspect_interaction_report, inspect_rendered_pages

BRIEF_CONTROLS = [
    "Basics",
    "Key Features",
    "Section Reference",
    "Guided Tour",
    "Theme",
    "Commissioning Engineer",
    "BMS Designer",
    "Project Manager",
    "Integration Engineer",
]
LEARNING_CONTROLS = [
    "Theme",
    "Commissioning Engineer",
    "BMS Designer",
    "Project Manager",
    "Integration Engineer",
]


def guidance_viewports(controls: list[str]) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "horizontal_overflow": False,
            "clipped_content": False,
            "clipping_negative_fixture": True,
            "clicked_controls": list(controls),
        }
        for name in ("desktop", "mobile")
    ]


def guidance_routes(version: str) -> list[dict[str, object]]:
    return [
        {
            "route": "/#/brief",
            "control": "Brief tabs",
            "release_version": version,
            "root_populated": True,
            "viewports": guidance_viewports(BRIEF_CONTROLS),
        },
        {
            "route": "/#/learning",
            "control": "Learning setup and roles",
            "release_version": version,
            "root_populated": True,
            "viewports": guidance_viewports(LEARNING_CONTROLS),
        },
    ]


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
            ]
            + guidance_routes(self.expected_version),
            "browser_errors": [],
        }
        self.assertEqual(inspect_interaction_report(report, self.expected_version), [])

    def test_rejects_guidance_overflow_or_missing_mobile_evidence(self) -> None:
        routes = guidance_routes(self.expected_version)
        routes[0]["viewports"] = [
            {
                "name": "desktop",
                "horizontal_overflow": False,
                "clipped_content": False,
                "clipping_negative_fixture": True,
                "clicked_controls": BRIEF_CONTROLS,
            }
        ]
        routes[1]["viewports"] = [
            {
                "name": "desktop",
                "horizontal_overflow": False,
                "clipped_content": False,
                "clipping_negative_fixture": False,
                "clicked_controls": LEARNING_CONTROLS,
            },
            {
                "name": "mobile",
                "horizontal_overflow": True,
                "clipped_content": True,
                "clipping_negative_fixture": True,
                "clicked_controls": LEARNING_CONTROLS,
            },
        ]
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
            ]
            + routes,
            "browser_errors": [],
        }
        failures = inspect_interaction_report(report, self.expected_version)
        self.assertTrue(any("desktop and mobile evidence" in value for value in failures))
        self.assertTrue(any("horizontal overflow" in value for value in failures))
        self.assertTrue(any("clipped content" in value for value in failures))
        self.assertTrue(any("did not prove clipping detection" in value for value in failures))

    def test_rejects_clipped_guidance_without_page_overflow(self) -> None:
        routes = guidance_routes(self.expected_version)
        routes[0]["viewports"][1] = {
            "name": "mobile",
            "horizontal_overflow": False,
            "clipped_content": True,
            "clipping_negative_fixture": True,
            "clicked_controls": BRIEF_CONTROLS,
        }
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
            ]
            + routes,
            "browser_errors": [],
        }
        failures = inspect_interaction_report(report, self.expected_version)
        self.assertFalse(any("horizontal overflow" in value for value in failures))
        self.assertTrue(any("clipped content" in value for value in failures))

    def test_rejects_incomplete_guidance_control_evidence(self) -> None:
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
            ]
            + guidance_routes(self.expected_version),
            "browser_errors": [],
        }
        report["routes"][3]["viewports"][0]["clicked_controls"].remove("Theme")
        failures = inspect_interaction_report(report, self.expected_version)
        self.assertTrue(any("complete guidance control set" in value for value in failures))

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
            ]
            + guidance_routes("dev"),
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
