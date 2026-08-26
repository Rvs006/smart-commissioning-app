"""Embed-rewrite contract: the vendored scanner UIs must use RELATIVE paths so
they run inside an SCT iframe.

If a future re-import forgets `scripts/rewrite_vendored_scanner_embed.py`, this
fails naming the seam - the REIMPORT.md philosophy. The committed source of truth
is public/ (dist/bundle.js is gitignored and rebuilt from public/ by the portable
build), so asserting on public/ covers both server.js and packaged runs.
stdlib unittest, reads files from disk, no app import.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[2] / "scanners" / "vendor"
_ABSOLUTE_API = ("'/api/", '"/api/', "`/api/")
_ABSOLUTE_ASSETS = ('"/app.js"', '"/styles.css"')


def _assert_relative(test: unittest.TestCase, label: str, text: str, *, is_html: bool) -> None:
    for token in _ABSOLUTE_API:
        test.assertNotIn(token, text, f"{label}: absolute API path {token!r} not rewritten")
    if is_html:
        for token in _ABSOLUTE_ASSETS:
            test.assertNotIn(token, text, f"{label}: absolute asset {token!r} not rewritten")
        test.assertIn("sct-bridge.js", text, f"{label}: bridge tag missing")
        test.assertIn("sct-theme.css", text, f"{label}: theme tag missing")


class EmbedRewriteContractTest(unittest.TestCase):
    def test_loose_public_files_are_rewritten(self) -> None:
        for app in ("network-ip-scanner", "bacnet-scanner", "mqtt-discovery"):
            public = _VENDOR / app / "public"
            _assert_relative(self, f"{app}/app.js", (public / "app.js").read_text("utf-8"), is_html=False)
            _assert_relative(self, f"{app}/index.html", (public / "index.html").read_text("utf-8"), is_html=True)


if __name__ == "__main__":
    unittest.main()
