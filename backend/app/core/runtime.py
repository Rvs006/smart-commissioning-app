import os
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = BACKEND_ROOT / "runtime"
CONFIGURATION_PATH = RUNTIME_ROOT / "configuration.json"
IMPORTS_ROOT = RUNTIME_ROOT / "imports"
IMPORT_FILES_ROOT = IMPORTS_ROOT / "files"
RUNS_ROOT = Path(os.getenv("SMART_COMMISSIONING_RUNS_ROOT", str(RUNTIME_ROOT / "runs"))).expanduser()
SECRETS_ROOT = Path(os.getenv("SMART_COMMISSIONING_SECRETS_ROOT", str(RUNTIME_ROOT / "secrets"))).expanduser()


def ensure_runtime_directories() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    IMPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    IMPORT_FILES_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    SECRETS_ROOT.mkdir(parents=True, exist_ok=True)
