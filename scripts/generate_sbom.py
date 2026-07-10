#!/usr/bin/env python3
"""Generate a Software Bill of Materials (SBOM) + license inventory.

This script walks the *declared* runtime dependencies of the three Smart
Commissioning Python distributions (``core``, ``backend``, ``worker``) plus
their installed transitive dependencies, reads each package's license metadata
via the standard library (``importlib.metadata``), and emits two artefacts:

  * a CycloneDX-flavoured JSON SBOM (``--json``), and
  * a human-readable markdown inventory (``--markdown``, defaults to
    ``docs/SBOM.md``).

It deliberately uses ONLY the standard library so it runs in CI without
pinning an external SBOM tool. ``pip-licenses`` / ``cyclonedx-py`` are richer
but add a version-pinning burden; see ``docs/SBOM.md`` for the documented
invocation of those tools as an alternative.

Honesty notes:

  * Only *installed* distributions report a real version + license. A
    dependency that is declared but not installed (for example the optional
    ``bacpypes3`` BACnet extra, which is intentionally absent here) is listed
    with ``installed: false`` and no fabricated version.
  * The license string is whatever the package metadata declares
    (``License-Expression``, the ``License`` field, or a ``License ::``
    trove classifier). No license is inferred or guessed.

Exit codes:

  * 0  — SBOM written; every resolved license is on the allowlist.
  * 0  — with ``--check`` omitted (default), disallowed licenses are reported
         but do not fail the run.
  * 2  — with ``--check``, at least one installed dependency carries a license
         that is not on the allowlist (or is unknown).
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The three first-party distributions whose dependency trees we inventory.
# name -> path to the project directory containing pyproject.toml.
FIRST_PARTY = {
    "smart-commissioning-core": REPO_ROOT / "core",
    "smart-commissioning-api": REPO_ROOT / "backend",
    "smart-commissioning-worker": REPO_ROOT / "worker",
}

# Licenses permitted for runtime dependencies. Permissive + weak-copyleft
# (LGPL/MPL, which we use only as separately-installed libraries) are allowed;
# strong copyleft (GPL/AGPL) is intentionally absent so the --check gate flags
# it. Matching is case-insensitive substring against the resolved license text.
DEFAULT_ALLOWLIST = (
    "MIT",
    "BSD",
    "Apache",
    "Apache-2.0",
    "Apache Software License",
    "ISC",
    "Python Software Foundation",
    "PSF",
    "MPL",
    "Mozilla Public License",
    # Weak copyleft — acceptable as dynamically-linked libraries; flagged in
    # docs/SBOM.md so the obligation (offer the library source) is explicit.
    "LGPL",
    "Lesser General Public License",
)

# Short purpose blurbs for the first-party declared dependencies, surfaced in
# the markdown so a reader sees *why* each top-level dep is present.
PURPOSE = {
    "fastapi": "HTTP API framework (routing, validation, OpenAPI).",
    "uvicorn": "ASGI server that runs the FastAPI app.",
    "starlette": "ASGI toolkit underlying FastAPI (middleware, routing).",
    "sqlalchemy": "ORM / database access layer for runs, imports, configuration.",
    "alembic": "Database schema migrations (applied on API startup).",
    "jsonschema": "Offline validation against the vendored canonical UDMI schemas.",
    "referencing": "Offline JSON-Schema $ref registry for the vendored UDMI closure.",
    "dramatiq": "Background job queue (worker actors over Redis).",
    "redis": "Redis client for the Dramatiq broker + readiness ping.",
    "pydantic": "Typed request/response and domain models.",
    "pydantic-settings": "Environment-variable settings loading (Settings).",
    "pydantic-core": "Compiled validation core for Pydantic v2.",
    "cryptography": "Fernet encryption of secret material at rest.",
    "openpyxl": "XLSX import parsing and report generation.",
    "prometheus-client": "Metrics exposition for the /metrics endpoint.",
    "psycopg": "PostgreSQL driver (hosted profile DATABASE_URL).",
    "psycopg-binary": "Prebuilt PostgreSQL libpq bindings for psycopg.",
    "httpx": "HTTP client used by the API/tests.",
    "python-multipart": "multipart/form-data parsing for file uploads.",
    "bacpypes3": "OPTIONAL: real BACnet/IP transport (UNVALIDATED extra).",
}


@dataclass
class Component:
    name: str
    installed: bool
    version: str | None
    license: str | None
    classifiers: list[str] = field(default_factory=list)
    required_by: set[str] = field(default_factory=set)
    purpose: str | None = None

    def license_display(self) -> str:
        return self.license or "UNKNOWN"


def _normalize(name: str) -> str:
    """PEP 503 normalized distribution name (lowercase, runs of -/_/. -> -)."""
    out = []
    prev_dash = False
    for ch in name.lower():
        if ch in "-_.":
            if not prev_dash:
                out.append("-")
            prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out).strip("-")


def _read_license(md: importlib_metadata.PackageMetadata) -> tuple[str | None, list[str]]:
    """Resolve the license string + license classifiers from package metadata.

    Preference order: SPDX ``License-Expression`` (PEP 639) -> the free-form
    ``License`` field -> ``License ::`` trove classifiers. Returns the resolved
    string (or None) and the list of license classifiers for transparency.
    """
    classifiers = [c for c in (md.get_all("Classifier") or []) if c.startswith("License")]
    expr = md.get("License-Expression")
    if expr and expr.strip():
        return expr.strip(), classifiers
    raw = md.get("License")
    if raw and raw.strip() and "\n" not in raw.strip():
        # Some packages dump the entire license TEXT into this field; keep only
        # short, single-line declarations.
        line = raw.strip()
        if len(line) <= 80:
            return line, classifiers
    if classifiers:
        # Derive a compact name from the trove classifier, e.g.
        # "License :: OSI Approved :: MIT License" -> "MIT License".
        tail = classifiers[0].split("::")[-1].strip()
        return tail, classifiers
    return None, classifiers


def _declared_requirements(project_dir: Path) -> list[str]:
    """Return the distribution names declared under [project].dependencies.

    Parses pyproject.toml with tomllib (stdlib, 3.11+). Extras / optional
    dependency groups are NOT walked here (the optional bacnet extra is handled
    separately) so the core inventory reflects the default install.
    """
    import tomllib

    data = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps: list[str] = list(project.get("dependencies", []))
    names: list[str] = []
    for spec in deps:
        names.append(_requirement_name(spec))
    return names


def _optional_requirements(project_dir: Path) -> dict[str, list[str]]:
    import tomllib

    data = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups = project.get("optional-dependencies", {})
    return {group: [_requirement_name(s) for s in specs] for group, specs in groups.items()}


def _requirement_name(spec: str) -> str:
    """Extract the bare distribution name from a PEP 508 requirement string."""
    name = spec.strip()
    for sep in (";", "[", "(", "=", "<", ">", "!", "~", " "):
        idx = name.find(sep)
        if idx != -1:
            name = name[:idx]
    return name.strip()


def _collect_installed_index() -> dict[str, importlib_metadata.Distribution]:
    index: dict[str, importlib_metadata.Distribution] = {}
    for dist in importlib_metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            index[_normalize(name)] = dist
    return index


def _component_for(
    norm_name: str,
    index: dict[str, importlib_metadata.Distribution],
) -> Component:
    dist = index.get(norm_name)
    if dist is None:
        return Component(name=norm_name, installed=False, version=None, license=None)
    md = dist.metadata
    license_str, classifiers = _read_license(md)
    return Component(
        name=md["Name"] or norm_name,
        installed=True,
        version=dist.version,
        license=license_str,
        classifiers=classifiers,
    )


def _walk_dependencies(
    seeds: dict[str, set[str]],
    index: dict[str, importlib_metadata.Distribution],
) -> dict[str, Component]:
    """BFS over installed metadata Requires-Dist to gather transitive deps.

    ``seeds`` maps a normalized dependency name -> the set of first-party
    projects that declared it directly. Returns name -> Component, with
    ``required_by`` accumulated across the walk.
    """
    components: dict[str, Component] = {}
    queue: list[str] = []

    for norm_name, requirers in seeds.items():
        comp = _component_for(norm_name, index)
        comp.required_by |= requirers
        comp.purpose = PURPOSE.get(norm_name)
        components[norm_name] = comp
        queue.append(norm_name)

    while queue:
        current = queue.pop(0)
        dist = index.get(current)
        if dist is None:
            continue
        for req in dist.requires or []:
            # Skip extras-gated requirements (only needed when an extra is
            # requested); we inventory the default install.
            if "extra ==" in req:
                continue
            dep_norm = _normalize(_requirement_name(req))
            if not dep_norm:
                continue
            if dep_norm not in components:
                comp = _component_for(dep_norm, index)
                comp.required_by.add(current)
                comp.purpose = PURPOSE.get(dep_norm)
                components[dep_norm] = comp
                queue.append(dep_norm)
            else:
                components[dep_norm].required_by.add(current)
    return components


def _license_allowed(license_str: str | None, allowlist: tuple[str, ...]) -> bool:
    if not license_str:
        return False
    haystack = license_str.lower()
    return any(allowed.lower() in haystack for allowed in allowlist)


def build_sbom() -> dict[str, object]:
    index = _collect_installed_index()

    # Direct declared deps across the three projects.
    seeds: dict[str, set[str]] = {}
    optional: dict[str, set[str]] = {}
    for project_name, project_dir in FIRST_PARTY.items():
        for dep in _declared_requirements(project_dir):
            seeds.setdefault(_normalize(dep), set()).add(project_name)
        for group, deps in _optional_requirements(project_dir).items():
            for dep in deps:
                optional.setdefault(_normalize(dep), set()).add(f"{project_name}[{group}]")

    components = _walk_dependencies(seeds, index)

    # Include optional/extra deps as components too, but clearly marked.
    for opt_norm, requirers in optional.items():
        comp = components.get(opt_norm)
        if comp is None:
            comp = _component_for(opt_norm, index)
            comp.purpose = PURPOSE.get(opt_norm)
            components[opt_norm] = comp
        comp.required_by |= requirers

    direct_names = set(seeds) | set(optional)

    component_list = []
    for norm_name in sorted(components):
        comp = components[norm_name]
        component_list.append(
            {
                "name": comp.name,
                "normalized_name": norm_name,
                "installed": comp.installed,
                "version": comp.version,
                "license": comp.license_display(),
                "license_classifiers": comp.classifiers,
                "direct": norm_name in direct_names,
                "required_by": sorted(comp.required_by),
                "purpose": comp.purpose,
            }
        )

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": "scripts/generate_sbom.py (importlib.metadata, stdlib)",
            "component": {"type": "application", "name": "smart-commissioning-app"},
        },
        "components": component_list,
    }


def render_markdown(sbom: dict[str, object], allowlist: tuple[str, ...]) -> str:
    components = sbom["components"]
    direct = [c for c in components if c["direct"]]
    transitive = [c for c in components if not c["direct"]]

    lines: list[str] = []
    lines.append("<!-- GENERATED by scripts/generate_sbom.py - do not edit the tables by hand. -->")
    lines.append("")
    lines.append("## Generated inventory")
    lines.append("")
    lines.append(f"Generated: {sbom['metadata']['timestamp']}")
    lines.append("")
    lines.append("### Direct (declared) dependencies")
    lines.append("")
    lines.append("| Package | Version | License | Purpose | Required by |")
    lines.append("| --- | --- | --- | --- | --- |")
    for c in direct:
        version = c["version"] or "(not installed)"
        purpose = c["purpose"] or ""
        req = ", ".join(c["required_by"])
        lines.append(f"| {c['name']} | {version} | {c['license']} | {purpose} | {req} |")
    lines.append("")
    lines.append("### Transitive dependencies")
    lines.append("")
    lines.append("| Package | Version | License | Required by |")
    lines.append("| --- | --- | --- | --- |")
    for c in transitive:
        version = c["version"] or "(not installed)"
        req = ", ".join(c["required_by"])
        lines.append(f"| {c['name']} | {version} | {c['license']} | {req} |")
    lines.append("")

    flagged = [
        c
        for c in components
        if c["installed"] and not _license_allowed(c["license"], allowlist)
    ]
    lines.append("### License check")
    lines.append("")
    lines.append(f"Allowlist: {', '.join(allowlist)}")
    lines.append("")
    if flagged:
        lines.append("Packages NOT on the allowlist (review required):")
        lines.append("")
        for c in flagged:
            lines.append(f"- {c['name']} {c['version']}: {c['license']}")
    else:
        lines.append("All installed dependencies resolve to an allowlisted license.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="Write the CycloneDX JSON SBOM here.")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Write the human-readable markdown table block here (e.g. docs/SBOM.generated.md).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 2 if any installed dependency has a non-allowlisted/unknown license.",
    )
    args = parser.parse_args(argv)

    allowlist = DEFAULT_ALLOWLIST

    sbom = build_sbom()
    markdown = render_markdown(sbom, allowlist)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown + "\n", encoding="utf-8")
    if not args.json and not args.markdown:
        print(markdown)

    flagged = [
        c
        for c in sbom["components"]
        if c["installed"] and not _license_allowed(c["license"], allowlist)
    ]
    if flagged:
        names = ", ".join(f"{c['name']} ({c['license']})" for c in flagged)
        print(f"\n[license-check] {len(flagged)} dependency(ies) not on the allowlist: {names}", file=sys.stderr)
        if args.check:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
