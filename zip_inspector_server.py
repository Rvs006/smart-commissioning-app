from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "zip_inspector.html"
MAX_PREVIEW_BYTES = 48_000
MAX_TEXT_CHARS = 8_000
MAX_LISTED_ENTRIES = 2_000


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".py",
    ".java",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".env",
    ".gitignore",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
DATA_EXTENSIONS = {".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".sql", ".db", ".sqlite", ".sqlite3"}
CODE_EXTENSIONS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".java",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".html",
    ".css",
    ".scss",
}

LANGUAGE_HINTS = {
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".py": "python",
    ".json": "json",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".xml": "xml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sql": "sql",
    ".csv": "csv",
    ".txt": "text",
}

IGNORABLE_SEGMENTS = {
    "__macosx",
    ".ds_store",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".git",
    ".idea",
    ".vscode",
    "coverage",
    "__pycache__",
}

STACK_RULES = [
    ("React", {"package.json", "vite.config.ts", "vite.config.js", "next.config.js"}, {".jsx", ".tsx"}),
    ("Python", {"requirements.txt", "pyproject.toml", "setup.py", "poetry.lock"}, {".py"}),
    ("Node.js", {"package.json", "pnpm-lock.yaml", "package-lock.json", "yarn.lock"}, {".js", ".mjs", ".cjs"}),
    ("Tailwind CSS", {"tailwind.config.js", "tailwind.config.ts", "postcss.config.js"}, set()),
    ("Docker", {"docker-compose.yml", "docker-compose.yaml", "dockerfile"}, set()),
    ("Java", {"pom.xml", "build.gradle"}, {".java"}),
    ("C#", {".sln", ".csproj"}, {".cs"}),
]


def format_bytes(num: int) -> str:
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    for unit in units:
        if size < step or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= step
    return f"{num} B"


def safe_entry_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def entry_extension(name: str) -> str:
    return Path(name).suffix.lower()


def classify_entry(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = entry_extension(name)
    lower = name.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in CODE_EXTENSIONS:
        return "code"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    if ext in DATA_EXTENSIONS:
        return "data"
    if ext in {".zip", ".rar", ".7z", ".gz", ".tar"}:
        return "archive"
    if "readme" in lower or ext in {".md", ".txt"}:
        return "document"
    return "other"


def is_probably_text(name: str, raw: bytes) -> bool:
    ext = entry_extension(name)
    if ext in TEXT_EXTENSIONS:
        return True
    if b"\x00" in raw:
        return False
    sample = raw[:1024]
    text_chars = sum(1 for byte in sample if 9 <= byte <= 13 or 32 <= byte <= 126)
    return text_chars / max(1, len(sample)) > 0.85


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_workspace_zips() -> list[str]:
    return sorted(str(path) for path in ROOT.rglob("*.zip"))


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, str] = {}

    def put(self, path: str) -> str:
        session_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._sessions[session_id] = path
        return session_id

    def get(self, session_id: str) -> str | None:
        with self._lock:
            return self._sessions.get(session_id)


SESSIONS = SessionStore()


def detect_stacks(names: set[str], extensions: Counter[str]) -> list[str]:
    stacks: list[str] = []
    lower_names = {name.lower() for name in names}
    basenames = {Path(name).name.lower() for name in names}
    for label, markers, ext_markers in STACK_RULES:
        matched = any(marker.lower() in basenames for marker in markers)
        matched = matched or any(extensions.get(ext, 0) > 0 for ext in ext_markers)
        if matched:
            stacks.append(label)
    return stacks


def guess_archive_shape(entries: list[dict[str, Any]], names: set[str], stacks: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    ext_counts = Counter(entry["ext"] for entry in entries if entry["ext"])
    has_docs = any("readme" in name.lower() or name.lower().endswith(".pdf") for name in names)
    has_many_images = sum(1 for entry in entries if entry["kind"] == "image") >= 5
    has_code = sum(ext_counts[ext] for ext in CODE_EXTENSIONS) >= 5
    has_data = sum(1 for entry in entries if entry["kind"] == "data") >= 3

    if "React" in stacks and has_code:
        reasons.append("contains React-style source files and frontend project markers")
        return "frontend prototype or product bundle", reasons
    if "Python" in stacks and has_code:
        reasons.append("contains Python project markers and executable source")
        return "backend or automation project", reasons
    if has_docs and not has_code:
        reasons.append("mostly documents/specifications rather than runnable code")
        return "documentation bundle", reasons
    if has_data and not has_code:
        reasons.append("mostly structured data files such as CSV, JSON, or SQL")
        return "dataset or register package", reasons
    if has_many_images and not has_code:
        reasons.append("mostly visual/image assets")
        return "design or asset pack", reasons
    if has_code and has_data:
        reasons.append("contains both source code and supporting data/config")
        return "mixed application bundle", reasons
    reasons.append("no strong framework signal; shape inferred from filenames and file types")
    return "general archive", reasons


def build_borrow_recommendations(entries: list[dict[str, Any]], names: set[str], stacks: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    lower_names = {name.lower() for name in names}
    basenames = {Path(name).name.lower() for name in names}
    recs: list[dict[str, str]] = []
    avoids: list[dict[str, str]] = []

    if any(entry["kind"] == "code" for entry in entries):
        recs.append(
            {
                "title": "Reuse interaction patterns, not literal screens",
                "why": "The archive appears to include application code or UI source.",
                "useFor": "Borrow page flow, table-inspector layouts, filters, tabs, and status chips for the commissioning UI.",
            }
        )
    if "package.json" in basenames or "vite.config.ts" in basenames or "vite.config.js" in basenames:
        recs.append(
            {
                "title": "Borrow project structure and build conventions",
                "why": "It looks like a modern frontend app bundle.",
                "useFor": "Lift folder organization, state boundaries, and component grouping, but keep Smart Commissioning domain models separate.",
            }
        )
    if any(entry["kind"] == "data" for entry in entries):
        recs.append(
            {
                "title": "Reuse sample schemas and register formats",
                "why": "The archive includes structured data files.",
                "useFor": "Map CSV/JSON fields into import templates for IP registers, BACnet registers, MQTT registers, and validation files.",
            }
        )
    if any("readme" in name.lower() or name.lower().endswith(".pdf") for name in names):
        recs.append(
            {
                "title": "Borrow terminology and workflow language",
                "why": "Documentation often captures the domain language better than the code.",
                "useFor": "Use headings, help text, and process steps that align with engineering workflows rather than generic dashboard copy.",
            }
        )
    if any(ext in {".csv", ".xlsx"} for ext in (entry["ext"] for entry in entries)):
        recs.append(
            {
                "title": "Lift import/export column definitions",
                "why": "Spreadsheet-oriented archives usually encode the operational model.",
                "useFor": "Standardize import validation, required columns, and report exports around those real project structures.",
            }
        )
    if not recs:
        recs.append(
            {
                "title": "Use the archive as reference material, not as architecture",
                "why": "There are no strong code or schema signals yet.",
                "useFor": "Borrow naming, examples, and edge cases after a deeper review of the individual files.",
            }
        )

    if any(any(segment in name for segment in {"node_modules", "dist", "build"}) for name in lower_names):
        avoids.append(
            {
                "title": "Do not copy compiled output",
                "why": "Build artifacts hide the original structure and create maintenance debt.",
            }
        )
    avoids.append(
        {
            "title": "Do not borrow branding or one-off project content wholesale",
            "why": "Visual assets, client names, and environment-specific values should be treated as examples, not product defaults.",
        }
    )
    if any("package-lock.json" in name or "pnpm-lock.yaml" in name or "yarn.lock" in name for name in lower_names):
        avoids.append(
            {
                "title": "Do not infer architecture from lockfiles alone",
                "why": "Dependency locks tell you what was installed, not what is important to keep.",
            }
        )
    return recs, avoids


def analyze_zip(path: str) -> dict[str, Any]:
    zip_path = Path(path)
    if not zip_path.exists():
        raise FileNotFoundError(f"Archive not found: {path}")
    if zip_path.suffix.lower() != ".zip":
        raise ValueError("Only .zip files are supported by this inspector.")

    entries: list[dict[str, Any]] = []
    names: set[str] = set()
    ext_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    top_level_counts: Counter[str] = Counter()
    top_level_sizes: Counter[str] = Counter()
    preview_candidates: list[str] = []
    ignored = 0

    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        total_compressed = sum(info.compress_size for info in infos)
        total_uncompressed = sum(info.file_size for info in infos)

        for info in infos:
            name = safe_entry_name(info.filename)
            if not name:
                continue
            parts = [part for part in name.split("/") if part]
            if any(part.lower() in IGNORABLE_SEGMENTS for part in parts):
                ignored += 1
                continue
            is_dir = info.is_dir()
            top_level = parts[0] if parts else "(root)"
            ext = entry_extension(name)
            kind = classify_entry(name, is_dir)
            names.add(name)
            ext_counts[ext or "(none)"] += 1
            kind_counts[kind] += 1
            top_level_counts[top_level] += 1
            top_level_sizes[top_level] += info.file_size

            entry = {
                "path": name,
                "size": info.file_size,
                "sizeLabel": format_bytes(info.file_size),
                "compressedSize": info.compress_size,
                "compressedSizeLabel": format_bytes(info.compress_size),
                "ext": ext,
                "kind": kind,
                "directory": is_dir,
                "previewable": (not is_dir) and (ext in TEXT_EXTENSIONS or ext in IMAGE_EXTENSIONS),
                "topLevel": top_level,
            }
            entries.append(entry)

            if not is_dir and len(preview_candidates) < 12 and entry["previewable"]:
                preview_candidates.append(name)

        listed_entries = sorted(entries, key=lambda entry: (entry["directory"], entry["path"].lower()))
        stacks = detect_stacks(names, ext_counts)
        shape, reasons = guess_archive_shape(listed_entries, names, stacks)
        recs, avoids = build_borrow_recommendations(listed_entries, names, stacks)

        interesting = [
            entry["path"]
            for entry in listed_entries
            if (
                re.search(r"readme|spec|guide|schema|sample|mock|wireframe|api|config|package|requirements|points|register", entry["path"], re.IGNORECASE)
                or entry["kind"] in {"code", "data", "document"}
            )
        ][:24]

        return {
            "archiveName": zip_path.name,
            "archivePath": str(zip_path),
            "workspaceZipFiles": find_workspace_zips(),
            "sessionEntriesAvailable": min(len(listed_entries), MAX_LISTED_ENTRIES),
            "summary": {
                "entries": len(listed_entries),
                "ignoredEntries": ignored,
                "directories": sum(1 for entry in listed_entries if entry["directory"]),
                "files": sum(1 for entry in listed_entries if not entry["directory"]),
                "compressedSize": total_compressed,
                "compressedSizeLabel": format_bytes(total_compressed),
                "uncompressedSize": total_uncompressed,
                "uncompressedSizeLabel": format_bytes(total_uncompressed),
            },
            "classification": {
                "shape": shape,
                "reasons": reasons,
                "stacks": stacks,
            },
            "counts": {
                "byKind": [{"label": key, "count": count} for key, count in kind_counts.most_common()],
                "byExtension": [{"label": key, "count": count} for key, count in ext_counts.most_common(18)],
                "topLevel": [
                    {
                        "label": key,
                        "count": count,
                        "sizeLabel": format_bytes(top_level_sizes[key]),
                    }
                    for key, count in top_level_counts.most_common(16)
                ],
            },
            "entries": listed_entries[:MAX_LISTED_ENTRIES],
            "interestingPaths": interesting,
            "previewCandidates": preview_candidates,
            "borrow": recs,
            "avoid": avoids,
        }


def preview_entry(path: str, entry_name: str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        normalized = safe_entry_name(entry_name)
        try:
            raw = archive.read(normalized)
        except KeyError as exc:
            raise FileNotFoundError(f"Entry not found: {entry_name}") from exc

        ext = entry_extension(normalized)
        if ext in IMAGE_EXTENSIONS:
            mime = mimetypes.guess_type(normalized)[0] or "application/octet-stream"
            data_url = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
            return {
                "mode": "image",
                "path": normalized,
                "mimeType": mime,
                "sizeLabel": format_bytes(len(raw)),
                "dataUrl": data_url,
            }

        if is_probably_text(normalized, raw):
            text = decode_text(raw[:MAX_PREVIEW_BYTES])
            truncated = len(raw) > MAX_PREVIEW_BYTES or len(text) > MAX_TEXT_CHARS
            text = text[:MAX_TEXT_CHARS]
            return {
                "mode": "text",
                "path": normalized,
                "language": LANGUAGE_HINTS.get(ext, "text"),
                "sizeLabel": format_bytes(len(raw)),
                "truncated": truncated,
                "content": text,
            }

        return {
            "mode": "binary",
            "path": normalized,
            "sizeLabel": format_bytes(len(raw)),
            "mimeType": mimetypes.guess_type(normalized)[0] or "application/octet-stream",
        }


class ZipInspectorHandler(BaseHTTPRequestHandler):
    server_version = "ZipInspector/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_text(HTML_FILE.read_text(encoding="utf-8"), content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/api/workspace":
            self._send_json({"workspaceZipFiles": find_workspace_zips()})
            return
        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/analyze-path":
                payload = self._read_json()
                source_path = str(payload.get("path", "")).strip()
                if not source_path:
                    raise ValueError("A zip path is required.")
                result = analyze_zip(source_path)
                session_id = SESSIONS.put(source_path)
                result["sessionId"] = session_id
                self._send_json(result)
                return

            if parsed.path == "/api/analyze-upload":
                filename = self.headers.get("X-Filename", "uploaded.zip")
                length = int(self.headers.get("Content-Length", "0"))
                if not filename.lower().endswith(".zip"):
                    raise ValueError("Upload must be a .zip file.")
                raw = self.rfile.read(length)
                temp_dir = Path(tempfile.mkdtemp(prefix="zip-inspector-"))
                archive_path = temp_dir / Path(filename).name
                archive_path.write_bytes(raw)
                result = analyze_zip(str(archive_path))
                session_id = SESSIONS.put(str(archive_path))
                result["sessionId"] = session_id
                self._send_json(result)
                return

            if parsed.path == "/api/preview":
                payload = self._read_json()
                session_id = str(payload.get("sessionId", "")).strip()
                entry_name = str(payload.get("entryPath", "")).strip()
                archive_path = SESSIONS.get(session_id)
                if not archive_path:
                    raise FileNotFoundError("Preview session not found. Re-run analysis.")
                if not entry_name:
                    raise ValueError("Entry path is required.")
                self._send_json(preview_entry(archive_path, entry_name))
                return

            self._send_json({"error": "Not found"}, status=404)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": f"Unexpected server error: {exc}"}, status=500)


def main() -> None:
    port = int(os.environ.get("ZIP_INSPECTOR_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), ZipInspectorHandler)
    print(f"ZIP Inspector running on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
