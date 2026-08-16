#!/usr/bin/env python3
"""Check rendered hosted SPA routes, controls, and browser-console output."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

CONSOLE_FAILURE = re.compile(
    r"(?:CONSOLE[^\n]*(?:ERROR|SEVERE)|Uncaught (?:TypeError|ReferenceError|Error))",
    re.IGNORECASE,
)


def inspect_rendered_pages(pages: list[Path], browser_log: str) -> list[str]:
    failures: list[str] = []
    if CONSOLE_FAILURE.search(browser_log):
        failures.append("browser console contains an uncaught or severe application error")
    for path in pages:
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r'<div\s+id=["\']root["\']\s*>\s*</div>', text, re.IGNORECASE):
            failures.append(f"{path.name}: React root stayed empty")
        if "id=\"root\"" not in text and "id='root'" not in text:
            failures.append(f"{path.name}: rendered document lacks the React root")
        if "<button" not in text.casefold():
            failures.append(f"{path.name}: rendered document exposes no control")
        if "Smart Commissioning" not in text:
            failures.append(f"{path.name}: application identity is missing")
        if path.name == "brief.html" and "How a protected discovery run works" not in text:
            failures.append("brief.html: protected discovery guidance is missing")
        if path.name == "learning.html" and "Operator guides for protected scans and evidence" not in text:
            failures.append("learning.html: operator guidance is missing")
    return failures


def inspect_interaction_report(report: Any, expected_version: str) -> list[str]:
    failures: list[str] = []
    if not isinstance(report, dict) or report.get("schema_version") != 2:
        return ["browser interaction report has the wrong schema"]
    if report.get("app_version") != expected_version:
        failures.append("browser interaction report has the wrong application version")
    routes = report.get("routes")
    if not isinstance(routes, list) or not routes:
        failures.append("browser interaction report contains no routes")
    else:
        paths: set[str] = set()
        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                failures.append(f"browser interaction route {index} is not an object")
                continue
            path = route.get("route")
            if isinstance(path, str):
                paths.add(path)
            expected_control = {
                "/#/brief": "Brief tabs",
                "/#/learning": "Learning setup and roles",
            }.get(path, "Review Comments")
            if route.get("control") != expected_control:
                failures.append(f"browser interaction route {index} used the wrong control")
            for claim in ("root_populated",):
                if route.get(claim) is not True:
                    failures.append(f"browser interaction route {index} did not prove {claim}")
            if path in {"/#/brief", "/#/learning"}:
                if route.get("release_version") != expected_version:
                    failures.append(
                        f"browser interaction route {index} has the wrong release version"
                    )
                viewports = route.get("viewports")
                if not isinstance(viewports, list) or {
                    item.get("name") for item in viewports if isinstance(item, dict)
                } != {"desktop", "mobile"}:
                    failures.append(
                        f"browser interaction route {index} lacks desktop and mobile evidence"
                    )
                else:
                    for viewport in viewports:
                        if viewport.get("horizontal_overflow") is not False:
                            failures.append(
                                f"browser interaction route {index} has horizontal overflow"
                            )
                        if viewport.get("clipped_content") is not False:
                            failures.append(
                                f"browser interaction route {index} has clipped content"
                            )
                        if viewport.get("clipping_negative_fixture") is not True:
                            failures.append(
                                f"browser interaction route {index} did not prove clipping detection"
                            )
                        expected_controls = {
                            "/#/brief": {
                                "Basics",
                                "Key Features",
                                "Section Reference",
                                "Guided Tour",
                                "Theme",
                                "Commissioning Engineer",
                                "BMS Designer",
                                "Project Manager",
                                "Integration Engineer",
                            },
                            "/#/learning": {
                                "Theme",
                                "Commissioning Engineer",
                                "BMS Designer",
                                "Project Manager",
                                "Integration Engineer",
                            },
                        }[path]
                        clicked_controls = viewport.get("clicked_controls")
                        if (
                            not isinstance(clicked_controls, list)
                            or len(clicked_controls) != len(expected_controls)
                            or set(clicked_controls) != expected_controls
                        ):
                            failures.append(
                                f"browser interaction route {index} lacks the complete guidance control set"
                            )
            else:
                if route.get("version_label") != expected_version:
                    failures.append(
                        f"browser interaction route {index} has the wrong visible version label"
                    )
                if route.get("version_visible") is not True:
                    failures.append(
                        f"browser interaction route {index} did not prove version visibility"
                    )
                for claim in ("opened", "closed"):
                    if route.get(claim) is not True:
                        failures.append(f"browser interaction route {index} did not prove {claim}")
        expected = {"/", "/configuration", "/run-history", "/#/brief", "/#/learning"}
        if paths != expected:
            failures.append("browser interaction report does not cover the exact hosted routes")
    if report.get("browser_errors") != []:
        failures.append("browser interaction report contains console or runtime errors")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=Path, action="append", required=True)
    parser.add_argument("--browser-log", type=Path, required=True)
    parser.add_argument("--interaction-report", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    failures = inspect_rendered_pages(
        args.page,
        args.browser_log.read_text(encoding="utf-8", errors="replace"),
    )
    try:
        report = json.loads(args.interaction_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        failures.append(f"browser interaction report is unreadable: {type(error).__name__}")
    else:
        failures.extend(inspect_interaction_report(report, args.expected_version))
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"hosted frontend runtime: OK ({len(args.page)} routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
