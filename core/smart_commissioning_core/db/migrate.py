"""Programmatic Alembic migrations (backend startup calls upgrade_to_head).

Locating the Alembic environment
--------------------------------
``alembic.ini`` and the ``alembic/`` script tree live at the *core* project
root (next to ``pyproject.toml``), following the conventional Alembic layout
that the ``alembic`` CLI and ``alembic/env.py`` expect. They are NOT inside the
``smart_commissioning_core`` package, so they cannot be reached with
``importlib.resources`` on the package.

To make migrations work for BOTH layouts we search a list of candidate roots:

* Editable / source-tree install: ``Path(__file__).resolve().parents[2]`` is
  the core project root, where ``alembic.ini`` + ``alembic/`` sit directly.
* Non-editable wheel install: ``pyproject.toml`` ships these via
  ``[tool.setuptools.data-files]`` into
  ``<sys.prefix>/share/smart_commissioning_core/``. We probe ``sys.prefix`` and
  ``site.PREFIXES`` (covers virtualenvs and ``--user`` installs).

The first candidate that actually contains ``alembic.ini`` wins, so the
existing editable-install path keeps working unchanged.
"""

from __future__ import annotations

import site
import sys
from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config

# Editable / source-tree root: core/ (contains alembic.ini and alembic/).
_SOURCE_TREE_ROOT = Path(__file__).resolve().parents[2]

# Wheel data-files install location, relative to an install prefix
# (see [tool.setuptools.data-files] in core/pyproject.toml).
_DATA_FILES_SUBDIR = Path("share") / "smart_commissioning_core"


def _candidate_roots() -> list[Path]:
    """Ordered roots that may hold ``alembic.ini`` + ``alembic/``.

    Source tree first (keeps editable installs working), then the wheel
    data-files locations under the active install prefixes.
    """
    roots: list[Path] = [_SOURCE_TREE_ROOT]
    prefixes = [sys.prefix, getattr(sys, "base_prefix", sys.prefix)]
    prefixes += list(getattr(site, "PREFIXES", []))
    for prefix in prefixes:
        if prefix:
            roots.append(Path(prefix) / _DATA_FILES_SUBDIR)
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(root)
    return unique


def _resolve_alembic_paths() -> tuple[Path, Path]:
    """Return ``(alembic_ini, script_location)`` for the active install.

    Raises ``FileNotFoundError`` if no candidate root carries ``alembic.ini``,
    which is a clear packaging error rather than a silent mis-migration.
    """
    candidates = _candidate_roots()
    for root in candidates:
        ini = root / "alembic.ini"
        if ini.is_file():
            return ini, root / "alembic"
    searched = "\n  ".join(str(root) for root in candidates)
    raise FileNotFoundError(
        "Could not locate alembic.ini for smart_commissioning_core migrations. "
        "Searched:\n  " + searched
    )


# Resolve once at import; exposed for callers/tests that introspect paths.
ALEMBIC_INI_PATH, ALEMBIC_SCRIPT_PATH = _resolve_alembic_paths()


def build_alembic_config(url: str | None = None) -> Config:
    """Build an Alembic Config pointing at the core migration scripts.

    The URL is passed as ``-x db_url=...`` so it takes the same top precedence
    as on the alembic CLI (see core/alembic/env.py).
    """
    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    if url is not None:
        config.cmd_opts = Namespace(x=[f"db_url={url}"])
    return config


def upgrade_to_head(url: str) -> None:
    """Create/upgrade the schema at ``url`` to the latest revision.

    Idempotent: running against an already-migrated database is a no-op.
    """
    command.upgrade(build_alembic_config(url), "head")
