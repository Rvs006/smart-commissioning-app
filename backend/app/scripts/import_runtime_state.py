"""One-shot migration of file-based runtime state into the database.

Reads the legacy JSON artifacts written by the previous file-backed services:

- runtime/runs/*.json                      -> runs + run_issues tables
- runtime/configuration.json               -> configuration_snapshots table
- runtime/imports/*.summary|errors|accepted_rows.json -> import_records table

Usage (from backend/):

    python -m app.scripts.import_runtime_state

Idempotent: rows whose primary keys already exist are skipped, and the
configuration is only seeded when the target project/site has no snapshot yet.
Uploaded import files and secret material stay on disk untouched.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from smart_commissioning_core.db.db_run_store import get_or_create_project_and_site
from smart_commissioning_core.db.engine import create_engine_from_url, session_factory
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.models import Run, RunIssue
from smart_commissioning_core.db.repositories import ConfigurationRepository, ImportRepository
from smart_commissioning_core.records import ValidationIssueRecord

from app.core.config import get_settings
from app.core.runtime import (
    CONFIGURATION_PATH,
    IMPORT_FILES_ROOT,
    IMPORTS_ROOT,
    RUNS_ROOT,
    ensure_runtime_directories,
)
from app.services.configuration_service import DEFAULT_PROJECT_ID, DEFAULT_SITE_ID


def _parse_timestamp(value: object, fallback: datetime | None = None) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback or datetime.now(UTC)


def _load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def import_runs(engine, runs_root: Path) -> tuple[int, int]:
    """Insert legacy run records; returns (migrated, skipped)."""
    migrated = 0
    skipped = 0
    sessions = session_factory(engine)
    for path in sorted(runs_root.glob("*.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
            skipped += 1
            continue
        run_id = payload["run_id"]
        try:
            migrated, skipped = _import_single_run(sessions, run_id, payload, migrated, skipped)
        except (ValueError, TypeError):
            skipped += 1
    return migrated, skipped


def _import_single_run(sessions, run_id: str, payload: dict, migrated: int, skipped: int) -> tuple[int, int]:
    with sessions.begin() as session:
        if session.get(Run, run_id) is not None:
            return migrated, skipped + 1
        project_id = str(payload.get("project_id") or DEFAULT_PROJECT_ID)
        site_id = str(payload.get("site_id") or DEFAULT_SITE_ID)
        get_or_create_project_and_site(session, project_id, site_id)
        result_summary = payload.get("result_summary")
        result_summary = result_summary if isinstance(result_summary, dict) else {}
        created_at = _parse_timestamp(payload.get("created_at"))
        execution_mode = result_summary.get("execution_mode")
        session.add(
            Run(
                id=run_id,
                project_id=project_id,
                site_id=site_id,
                job_type=str(payload.get("job_type") or "udmi_validation"),
                status=str(payload.get("status") or "queued"),
                stage=str(payload.get("stage") or "awaiting_worker"),
                progress_percent=int(payload.get("progress_percent") or 0),
                parameters=payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {},
                result_summary=result_summary,
                execution_mode=execution_mode if isinstance(execution_mode, str) else None,
                error_message=payload.get("error_message") if isinstance(payload.get("error_message"), str) else None,
                created_at=created_at,
                updated_at=_parse_timestamp(payload.get("updated_at"), fallback=created_at),
            )
        )
        issues = payload.get("issues")
        for position, issue in enumerate(issues if isinstance(issues, list) else []):
            record = ValidationIssueRecord.model_validate(issue)
            session.add(RunIssue(run_id=run_id, position=position, **record.model_dump()))
    return migrated + 1, skipped


def import_configuration(engine, configuration_path: Path, project_id: str, site_id: str) -> int:
    """Seed the configuration from configuration.json; returns snapshots created."""
    payload = _load_json(configuration_path)
    if not isinstance(payload, dict):
        return 0
    repository = ConfigurationRepository(engine)
    if repository.get_current(project_id, site_id) is not None:
        return 0
    repository.save(project_id, site_id, payload)
    return 1


def import_imports(engine, imports_root: Path) -> tuple[int, int]:
    """Insert legacy import batches; returns (migrated, skipped)."""
    migrated = 0
    skipped = 0
    repository = ImportRepository(engine)
    for summary_path in sorted(imports_root.glob("*.summary.json")):
        summary = _load_json(summary_path)
        if not isinstance(summary, dict) or not isinstance(summary.get("import_id"), str):
            skipped += 1
            continue
        import_id = summary["import_id"]
        try:
            repository.get(import_id)
            skipped += 1
            continue
        except FileNotFoundError:
            pass

        errors_payload = _load_json(imports_root / f"{import_id}.errors.json")
        errors = errors_payload.get("errors") if isinstance(errors_payload, dict) else []
        accepted_rows = _load_json(imports_root / f"{import_id}.accepted_rows.json")
        stored_file_name = str(summary.get("stored_file_name") or "")
        repository.create(
            import_id=import_id,
            import_type=str(summary.get("import_type") or ""),
            project_id=summary.get("project_id") if isinstance(summary.get("project_id"), str) else None,
            site_id=summary.get("site_id") if isinstance(summary.get("site_id"), str) else None,
            original_filename=str(summary.get("file_name") or stored_file_name),
            stored_file_path=str(IMPORT_FILES_ROOT / stored_file_name) if stored_file_name else "",
            summary=summary,
            accepted_rows=accepted_rows if isinstance(accepted_rows, list) else [],
            errors=errors if isinstance(errors, list) else [],
            created_at=_parse_timestamp(summary.get("created_at")),
        )
        migrated += 1
    return migrated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID, help="Project for the configuration snapshot.")
    parser.add_argument("--site-id", default=DEFAULT_SITE_ID, help="Site for the configuration snapshot.")
    arguments = parser.parse_args()

    settings = get_settings()
    ensure_runtime_directories()
    upgrade_to_head(settings.database_url)
    engine = create_engine_from_url(settings.database_url)
    try:
        runs_migrated, runs_skipped = import_runs(engine, RUNS_ROOT)
        configs_migrated = import_configuration(
            engine, CONFIGURATION_PATH, arguments.project_id, arguments.site_id
        )
        imports_migrated, imports_skipped = import_imports(engine, IMPORTS_ROOT)
    finally:
        engine.dispose()

    print(f"Database: {settings.database_url}")
    print(f"Runs: {runs_migrated} migrated, {runs_skipped} skipped (already present or unreadable)")
    print(f"Configurations: {configs_migrated} migrated")
    print(f"Imports: {imports_migrated} migrated, {imports_skipped} skipped (already present or unreadable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
