import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def get_runs_root() -> Path:
    configured = os.getenv("SMART_COMMISSIONING_RUNS_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "backend" / "runtime" / "runs"


class FileRunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_runs_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def update_status(
        self,
        run_id: str,
        *,
        status: str,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, object]:
        def mutate(run: dict[str, object]) -> None:
            run["status"] = status
            if stage is not None:
                run["stage"] = stage
            if progress_percent is not None:
                run["progress_percent"] = max(0, min(100, progress_percent))
            run["error_message"] = error_message

        return self._update(run_id, mutate)

    def replace_result(
        self,
        run_id: str,
        *,
        result_summary: dict[str, object],
        issues: list[dict[str, object]],
    ) -> dict[str, object]:
        def mutate(run: dict[str, object]) -> None:
            run["result_summary"] = result_summary
            run["issues"] = issues

        return self._update(run_id, mutate)

    def update_summary(self, run_id: str, result_summary: dict[str, object]) -> dict[str, object]:
        def mutate(run: dict[str, object]) -> None:
            current = run.get("result_summary")
            if not isinstance(current, dict):
                current = {}
            run["result_summary"] = {**current, **result_summary}

        return self._update(run_id, mutate)

    def _update(self, run_id: str, mutate: Callable[[dict[str, object]], None]) -> dict[str, object]:
        path = self._path(run_id)
        run = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(run, dict):
            raise ValueError(f"Run file {path} must contain a JSON object.")
        mutate(run)
        run["updated_at"] = datetime.now(timezone.utc).isoformat()

        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
        temp_path.replace(path)
        return run

    def _path(self, run_id: str) -> Path:
        if "/" in run_id or "\\" in run_id or ".." in run_id:
            raise FileNotFoundError(run_id)
        return self.root / f"{run_id}.json"
