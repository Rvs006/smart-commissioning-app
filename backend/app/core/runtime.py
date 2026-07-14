import os
import stat
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
# Overridable so the portable launcher can anchor ALL backend-derived state
# (default sqlite path, imports, edge identity, secrets default) to one
# machine-stable folder that survives per-release exe folders. Unset (dev,
# hosted compose) keeps the historical backend-local layout.
RUNTIME_ROOT = Path(
    os.getenv("SMART_COMMISSIONING_RUNTIME_ROOT", str(BACKEND_ROOT / "runtime"))
).expanduser()
IMPORTS_ROOT = RUNTIME_ROOT / "imports"
IMPORT_FILES_ROOT = IMPORTS_ROOT / "files"
SECRETS_ROOT = Path(os.getenv("SMART_COMMISSIONING_SECRETS_ROOT", str(RUNTIME_ROOT / "secrets"))).expanduser()


def ensure_runtime_directories() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    IMPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    IMPORT_FILES_ROOT.mkdir(parents=True, exist_ok=True)
    SECRETS_ROOT.mkdir(parents=True, exist_ok=True)
    # SECRETS_ROOT holds the Fernet key (.secret_store_key) and every encrypted
    # cert/key. The per-file 0600 is only meaningful if the directory itself is
    # owner-only, so lock it down here (the directory ACL was previously left at
    # the process umask — a multi-user at-rest gap).
    _restrict_directory(SECRETS_ROOT)


def _restrict_directory(path: Path) -> None:
    """Best-effort restrict a sensitive directory to the owner only.

    POSIX (the hosted multi-user Docker target): chmod 0700 — effective. Windows
    (the single-user portable target): chmod is a no-op, so reset the ACL to the
    current user via icacls. All failures are non-fatal.
    """
    try:
        os.chmod(path, stat.S_IRWXU)  # 0o700
    except OSError:
        pass
    if sys.platform == "win32":
        user = os.environ.get("USERNAME")
        if user:
            try:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass
