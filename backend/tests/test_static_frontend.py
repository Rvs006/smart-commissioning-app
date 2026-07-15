"""Static frontend serving: public/ files, SPA fallback, and the traversal guard.

Covers the v0.1.11 fix for the ELECTRACOM logo 404 in the portable exe. Vite
copies frontend/public/ to the dist ROOT, so /electracom-logo.png never matched
the /assets mount and the SPA fallback happily returned index.html for it.

HARNESS GOTCHA (do not "simplify" this to an env override): app.main binds
FRONTEND_DIST at import time (app/main.py), and alphabetically earlier test
modules (test_auth, ...) already import app.main during 'unittest discover'.
Setting SCT_FRONTEND_DIST via ApiTestCase.env is therefore silently ineffective
here. Instead we patch the app.main.FRONTEND_DIST module attribute -- which
spa_fallback reads per request -- and restore it in tearDownClass so the temp
path cannot leak into later test modules.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from harness import ApiTestCase

# Minimal but genuinely PNG-signatured bytes: the point of the test is the
# content-type/route, not image decoding.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"electracom-logo-fixture-bytes"
_INDEX_SENTINEL = "<!doctype html><title>sct-index-sentinel</title>"
_SECRET_SENTINEL = "outside-dist-secret"


class StaticFrontendTests(ApiTestCase):
    """spa_fallback serves real dist files, falls back to index for routes."""

    @classmethod
    def before_client(cls) -> None:
        cls._temp_dir = Path(tempfile.mkdtemp(prefix="sct-test-dist-"))
        dist = cls._temp_dir / "dist"
        (dist / "sub").mkdir(parents=True)
        (dist / "index.html").write_text(_INDEX_SENTINEL, encoding="utf-8")
        (dist / "electracom-logo.png").write_bytes(_PNG_BYTES)
        (dist / "sub" / "notes.txt").write_text("nested public file", encoding="utf-8")
        # OUTSIDE the dist root: nothing may ever serve this.
        (cls._temp_dir / "secret.txt").write_text(_SECRET_SENTINEL, encoding="utf-8")

        import app.main as main_module

        cls._main_module = main_module
        cls._original_dist = main_module.FRONTEND_DIST
        main_module.FRONTEND_DIST = dist
        cls._dist = dist

    @classmethod
    def tearDownClass(cls) -> None:
        cls._main_module.FRONTEND_DIST = cls._original_dist
        super().tearDownClass()
        shutil.rmtree(cls._temp_dir, ignore_errors=True)

    # -- real files under the dist root -----------------------------------

    def test_logo_served_with_png_content_type(self) -> None:
        # The actual reported bug: this returned index.html in the portable exe.
        response = self.client.get("/electracom-logo.png")
        self.assertEqual(response.status_code, 200, response.text)
        # Exact match: starlette appends a charset only for text/* media types.
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, _PNG_BYTES)

    def test_nested_public_file_served(self) -> None:
        # Proves the fix is generic to any current/future frontend public/ file,
        # not special-cased to the logo.
        response = self.client.get("/sub/notes.txt")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("text/plain"),
            response.headers.get("content-type"),
        )

    # -- SPA + API behaviour must be preserved -----------------------------

    def test_spa_route_falls_back_to_index(self) -> None:
        response = self.client.get("/reports")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("sct-index-sentinel", response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("text/html"),
            response.headers.get("content-type"),
        )

    def test_unknown_api_path_still_404(self) -> None:
        # Regression guard: the api/ gate must stay ahead of file resolution.
        response = self.client.get("/api/v1/route-that-does-not-exist")
        self.assertEqual(response.status_code, 404, response.text)

    # -- traversal ---------------------------------------------------------

    def test_traversal_never_escapes_dist(self) -> None:
        # '..%2f' rather than '/../': httpx/TestClient collapses literal dot
        # segments client-side, but '..%2f' is one opaque segment that survives
        # normalization and is percent-decoded to '../' in the ASGI path scope.
        response = self.client.get("/..%2fsecret.txt")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(_SECRET_SENTINEL, response.text)

    def test_resolver_guard_direct(self) -> None:
        # Deterministic: no HTTP client normalization in the way.
        from fastapi import HTTPException

        resolve = self._main_module._resolve_frontend_file

        # Only '../' forms are asserted as rejections: '..\\x' is a plain
        # filename on the ubuntu CI runner (None there, 404 only on Windows).
        with self.assertRaises(HTTPException) as caught:
            resolve("../secret.txt")
        self.assertEqual(caught.exception.status_code, 404)

        logo = resolve("electracom-logo.png")
        self.assertIsNotNone(logo)
        self.assertEqual(logo.name, "electracom-logo.png")
        self.assertTrue(logo.is_relative_to(self._dist.resolve()))

        # A real SPA route is not a file -> None -> index.html fallback.
        self.assertIsNone(resolve("reports"))

        nested = resolve("sub/notes.txt")
        self.assertIsNotNone(nested)
        self.assertEqual(nested.name, "notes.txt")


if __name__ == "__main__":
    unittest.main()
