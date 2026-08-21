"""Vendor-time rewrite so the standalone scanner UIs can run inside an SCT iframe.

The vendored apps call their API and load their assets with ABSOLUTE paths
(``fetch('/api/scan')``, ``<script src="/app.js">``). Served from an SCT sub-path
(``/api/v1/scanners/ip/raw/``) inside an iframe, those resolve to the SCT origin
root instead of the proxy. This rewrites them to RELATIVE paths (``api/scan``,
``app.js``) so they resolve against the panel URL, and injects the (inert until
the write-guard milestone) ``sct-bridge.js`` tag.

Deterministic and idempotent: re-running after a fresh re-import is a no-op once
already-relative. Pinned by ``test_scanner_raw_embed_contract.py`` so a future
upstream drop that reintroduces absolute paths reddens CI naming this step.

Run from anywhere:  python scripts/rewrite_vendored_scanner_embed.py

public/ is the committed source of truth. All three dist/bundle.js are gitignored
and rebuilt from public/ by each app's build-bundle.js during the portable build,
so no manual rebundle is needed for the repo or CI (a fresh checkout has no
bundle, and the supervisor falls back to server.js, which serves public/). Rebuild
a bundle locally only if you want to smoke-test the packaged entry:
  node scanners/vendor/network-ip-scanner/build-bundle.js
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "scanners" / "vendor"
APPS = ("network-ip-scanner", "bacnet-scanner", "mqtt-discovery")

BRIDGE_TAG = '<script src="sct-bridge.js" defer></script>'

# (find, replace) applied to every js/html asset. Quote-anchored so we only touch
# real URL literals, never a stray "/api/" inside prose.
_API_REPLACEMENTS = (
    ("'/api/", "'api/"),
    ('"/api/', '"api/'),
    ("`/api/", "`api/"),
)
# HTML-only: the two absolute asset refs in each index.html.
_ASSET_REPLACEMENTS = (
    ('"/app.js"', '"app.js"'),
    ('"/styles.css"', '"styles.css"'),
)


def _rewrite_text(text: str, *, is_html: bool) -> str:
    for find, repl in _API_REPLACEMENTS:
        text = text.replace(find, repl)
    if is_html:
        for find, repl in _ASSET_REPLACEMENTS:
            text = text.replace(find, repl)
        if BRIDGE_TAG not in text and "</body>" in text:
            text = text.replace("</body>", f"  {BRIDGE_TAG}\n</body>", 1)
    return text


def rewrite_file(path: Path) -> bool:
    """Rewrite one asset in place. Returns True if bytes changed."""
    original = path.read_text(encoding="utf-8")
    updated = _rewrite_text(original, is_html=path.suffix == ".html")
    if updated != original:
        path.write_text(updated, encoding="utf-8")
    return updated != original


def main() -> int:
    changed = 0
    for app in APPS:
        public = VENDOR / app / "public"
        for name in ("app.js", "index.html"):
            target = public / name
            if not target.is_file():
                print(f"skip (missing): {target}")
                continue
            if rewrite_file(target):
                changed += 1
                print(f"rewrote: {target.relative_to(ROOT)}")
    print(f"done: {changed} file(s) changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
