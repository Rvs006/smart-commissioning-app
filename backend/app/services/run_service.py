import logging
from datetime import UTC, datetime, timedelta

from smart_commissioning_core.db.db_run_store import (
    WORKER_STALE_OBSERVED_AT_KEY,
    DbRunStore,
)
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import Run, RunResult, RunSeal
from smart_commissioning_core.db.repositories import DiscoveryRepository
from smart_commissioning_core.db.run_lifecycle import RunLifecycleRepository
from smart_commissioning_core.integrity import sha256_bytes
from smart_commissioning_core.owned_run_store import OwnedRunStore
from smart_commissioning_core.run_lifecycle import TerminalResultV1
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import edge_identity, get_settings
from app.core.db import get_engine
from app.core.runtime import ensure_runtime_directories
from app.schemas.jobs import (
    JobCreateRequest,
    JobStatus,
    JobSummary,
    JobType,
    ReportRequest,
    ReportSummary,
    RunRecord,
    ValidationIssueRecord,
)
from app.services.report_artifacts import (
    REPORT_RENDERER_VERSION,
    REPORT_SNAPSHOT_SCHEMA_VERSION,
    canonical_json_bytes,
    snapshot_sha256,
    verify_signed_manifest,
)
from app.services.report_naming import build_report_file_name
from app.services.run_context_builder import build_run_context
from app.services.udmi_report_model import (
    build_udmi_report_model,
    normalise_udmi_report_scope,
)

logger = logging.getLogger(__name__)

DISCOVERY_JOB_TYPES: set[JobType] = {"ip_discovery", "bacnet_discovery", "mqtt_discovery"}
VALIDATION_JOB_TYPES: set[JobType] = {
    "udmi_validation",
    "mqtt_config_publish",
    "bacnet_validation",
    "mapping_validation",
}
REPORT_JOB_TYPES: set[JobType] = {"report_generation"}
_DEFAULT_REPORT_TITLE = "Smart Commissioning Report"
_DEFAULT_UDMI_REPORT_TITLE = "UDMI Validation Report"
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# Operator-facing message stamped on a run the startup sweep found fossilized at
# "running" (see RunService.sweep_interrupted_runs). Credential-free and generic
# by design (this text can reach the UI).
INTERRUPTED_RUN_MESSAGE = (
    "This run was interrupted by an application restart before it could finish, "
    "so no results were saved. Please run it again."
)

WORKER_INTERRUPTED_RUN_MESSAGE = (
    "The background worker stopped updating this run before it could finish, "
    "so the result is incomplete. Please run it again."
)

# A running actor refreshes Run.updated_at every 30 seconds from worker/app/tasks.py.
# Four missed beats is long enough to ride out a brief database stall without
# leaving a hard-killed worker's row alive forever. Queued messages get a much
# larger allowance because a healthy worker pool may legitimately be busy.
_RUNNING_WORKER_STALE_AFTER = timedelta(minutes=2)
_QUEUED_WORKER_STALE_AFTER = timedelta(hours=1)
# An expired timestamp is only a suspicion after a database outage: the API and
# a still-live worker can reconnect at the same instant, and the API may win the
# first row lock before the worker writes its next beat. Require a full
# two-minute confirmation window with no worker write before making it terminal.
_WORKER_STALE_CONFIRM_AFTER = timedelta(minutes=2)


def _was_queued_to_worker(result_summary: dict[str, object]) -> bool:
    """Return True if the run was handed to the background (Dramatiq) worker queue.

    The queue-dispatch path (app.services.run_dispatch.dispatch_run) is the ONLY
    writer of ``queue_name`` / ``actor_name`` into a run's result_summary, so
    their presence is the precise "went to the worker" marker. dispatch_run writes
    them BEFORE handing the run to the queue (and clears them back to None if it
    falls back to inline), so a worker that has already flipped the run to
    'running' always carries the markers — closing the window in which the sweep
    would otherwise false-fail a live worker run whose markers had not been
    written yet. The ``queued`` / ``worker_required`` flags are deliberately NOT
    used here: the run store stamps them on EVERY freshly created run (inline runs
    included), so they cannot tell an inline run apart from a queued one.
    """
    return bool(result_summary.get("queue_name") or result_summary.get("actor_name"))


def _worker_run_is_stale(
    *,
    status: str,
    updated_at: datetime,
    now: datetime | None = None,
) -> bool:
    """Return whether a worker-bound active run has missed its liveness window."""
    if status not in {"queued", "running"}:
        return False
    observed = updated_at if updated_at.tzinfo is not None else updated_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    threshold = (
        _QUEUED_WORKER_STALE_AFTER if status == "queued" else _RUNNING_WORKER_STALE_AFTER
    )
    return current - observed.astimezone(UTC) > threshold


def _stale_observed_at(result_summary: dict[str, object]) -> datetime | None:
    """Parse the internal first-stale observation, tolerating old/bad rows."""
    value = result_summary.get(WORKER_STALE_OBSERVED_AT_KEY)
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return observed.astimezone(UTC)


class RunService:
    """Thin wrapper over the shared database-backed run store.

    The public API mirrors the previous file-backed implementation exactly:
    method names, RunRecord/JobSummary return models, and FileNotFoundError
    for missing run ids (routes translate that into 404 responses).
    """

    def __init__(self, engine: Engine | None = None) -> None:
        ensure_runtime_directories()
        self._engine = engine if engine is not None else get_engine()
        self._store = DbRunStore(self._engine)
        self._lifecycle = RunLifecycleRepository(self._engine)

    def create_job_run(
        self,
        request: JobCreateRequest,
        *,
        expected_job_type: JobType,
        requesting_principal: str = "system",
    ) -> RunRecord:
        if request.job_type != expected_job_type:
            raise ValueError(f"Endpoint expects job_type '{expected_job_type}'.")

        context = build_run_context(
            engine=self._engine,
            project_id=request.project_id,
            site_id=request.site_id,
            job_type=expected_job_type,
            parameters=dict(request.parameters),
            requesting_principal=requesting_principal,
        )
        envelope = self._lifecycle.create_run_with_context(
            job_type=expected_job_type,
            context=context,
            execution_mode=get_settings().job_execution_mode,
            edge_id=edge_identity().edge_id,
        )
        logger.info("run created run_id=%s job_type=%s", envelope.run_id, expected_job_type)
        return self.get_run(envelope.run_id)

    def get_dispatch_for_run(self, run_id: str):
        return self._lifecycle.get_dispatch_for_run(run_id)

    def get_execution_context(self, run_id: str):
        return self._lifecycle.get_context(run_id)

    def claim_owned_run(
        self, run_id: str, *, lease_seconds: int = 60
    ) -> OwnedRunStore | None:
        dispatch = self._lifecycle.get_dispatch_for_run(run_id)
        lease = self._lifecycle.claim_run(
            run_id,
            dispatch.dispatch_id,
            lease_seconds=lease_seconds,
        )
        return OwnedRunStore(self._lifecycle, lease) if lease is not None else None

    def mark_dispatch_published(self, dispatch_id: str) -> bool:
        return self._lifecycle.mark_dispatch_published(dispatch_id)

    def list_pending_dispatches(self):
        return self._lifecycle.list_pending_dispatches()

    def recover_expired_leases(self) -> list[str]:
        return self._lifecycle.recover_expired_leases()

    def create_report_run(self, request: ReportRequest) -> tuple[RunRecord, ReportSummary]:
        # A report must never silently change scope because a source id is bad or
        # belongs to another project/site. Empty source lists remain valid for a
        # metadata-only report.
        sources: list[RunRecord] = []
        source_seals: dict[str, dict[str, object]] = {}
        source_context_provenance: dict[str, dict[str, object]] = {}
        for source_run_id in request.source_run_ids:
            try:
                source = self.get_run(source_run_id)
            except FileNotFoundError as error:
                raise ValueError(f"Source run '{source_run_id}' was not found.") from error
            if source.project_id != request.project_id or source.site_id != request.site_id:
                raise ValueError(
                    f"Source run '{source_run_id}' does not belong to project "
                    f"'{request.project_id}' and site '{request.site_id}'."
                )
            if source.status not in _TERMINAL_RUN_STATUSES:
                raise ValueError(
                    f"Source run '{source_run_id}' is not terminal "
                    f"(status '{source.status}'). Wait for it to succeed, fail, or be cancelled."
                )
            # Lifecycle-v2 runs are reportable only after the authoritative
            # finalizer has committed a terminal result and immutable seal.
            # Pre-v0.1.26 rows have no execution-context row and remain readable
            # for backward-compatible historical reporting.
            try:
                context = self._lifecycle.get_context(source_run_id)
            except FileNotFoundError:
                context = None
            if context is not None:
                try:
                    seal = self._lifecycle.get_seal(source_run_id)
                except FileNotFoundError as error:
                    raise ValueError(
                        f"Source run '{source_run_id}' is terminal but unsealed. "
                        "Recover or rerun it before generating evidence."
                    ) from error
                if seal.terminal_status != source.status:
                    raise ValueError(
                        f"Source run '{source_run_id}' has inconsistent terminal metadata."
                    )
                source_seals[source_run_id] = seal.model_dump(mode="json")
                captured = context.context
                source_context_provenance[source_run_id] = {
                    "context_sha256": context.context_sha256,
                    "configuration_version": captured.configuration_version,
                    "configuration_sha256": sha256_bytes(
                        canonical_json_bytes(captured.configuration_snapshot)
                    ),
                    "schema_versions": dict(captured.schema_versions),
                    "credential_version_ids": sorted(
                        f"{location}:{reference.version}"
                        for location, reference in captured.secret_references.items()
                    ),
                    "application_version": captured.application_version,
                }
            if request.report_type == "udmi_validation":
                if source.job_type != "udmi_validation":
                    raise ValueError(
                        f"Source run '{source_run_id}' must be a UDMI validation run; "
                        f"found job type '{source.job_type}'."
                    )
            sources.append(source)

        custom_report_title = request.report_title is not None
        parameters: dict[str, object] = {
            "output_format": request.output_format,
            "report_type": request.report_type,
            "source_run_ids": request.source_run_ids,
            # Older report runs have no marker. The summary projection treats
            # that absence as legacy naming so an upgrade never renames an
            # existing downloadable artifact.
            "report_title_custom": custom_report_title,
            "report_title": request.report_title
            or (
                _DEFAULT_UDMI_REPORT_TITLE
                if request.report_type == "udmi_validation"
                else _DEFAULT_REPORT_TITLE
            ),
            "report_generated_at": datetime.now(UTC).isoformat(),
            "renderer_version": REPORT_RENDERER_VERSION,
        }
        snapshot_sources: list[RunRecord] = []
        seen_source_ids: set[str] = set()
        for source in sources:
            if source.run_id in seen_source_ids:
                continue
            seen_source_ids.add(source.run_id)
            snapshot_sources.append(source)
        parameters["source_run_ids"] = [source.run_id for source in snapshot_sources]
        if request.udmi_scope is not None:
            snapshot_source_objects: list[object] = list(snapshot_sources)
            parameters["udmi_scope"] = normalise_udmi_report_scope(
                request.udmi_scope.model_dump(mode="json"),
                snapshot_source_objects,
            )
        if request.report_type == "udmi_validation":
            snapshot_source_objects = list(snapshot_sources)
            report_snapshot = build_udmi_report_model(
                snapshot_source_objects,
                parameters.get("udmi_scope"),
            )
            parameters["udmi_report_snapshot"] = report_snapshot
        source_snapshots = [
            self._safe_report_value(source.model_dump(mode="json"))
            for source in snapshot_sources
        ]
        parameters["source_run_snapshots"] = source_snapshots
        parameters["source_run_seals"] = {
            source.run_id: source_seals[source.run_id]
            for source in snapshot_sources
            if source.run_id in source_seals
        }
        discovery_snapshots = self._snapshot_discovery_rows(snapshot_sources)
        parameters["source_discovery_snapshots"] = discovery_snapshots
        raw_scope = parameters.get("udmi_scope")
        raw_selected_payloads = (
            raw_scope.get("selected_payloads", []) if isinstance(raw_scope, dict) else []
        )
        selected_payloads = (
            raw_selected_payloads if isinstance(raw_selected_payloads, list) else []
        )
        selected_assets = sorted(
            {
                str(row.get("asset_id"))
                for row in selected_payloads
                if isinstance(row, dict) and row.get("asset_id")
            },
            key=str.casefold,
        )
        report_snapshot_v2 = {
            "schema_version": REPORT_SNAPSHOT_SCHEMA_VERSION,
            "project_id": request.project_id,
            "site_id": request.site_id,
            "report_type": request.report_type,
            "output_format": request.output_format,
            "source_run_ids": [source.run_id for source in snapshot_sources],
            "source_result_hashes": {
                source.run_id: (
                    source_seals[source.run_id]["result_sha256"]
                    if source.run_id in source_seals
                    else sha256_bytes(canonical_json_bytes(snapshot))
                )
                for source, snapshot in zip(snapshot_sources, source_snapshots, strict=True)
            },
            "selected_assets": selected_assets,
            "filters": parameters.get("udmi_scope") or {},
            "configuration_provenance": {
                source.run_id: source_context_provenance[source.run_id]
                for source in snapshot_sources
                if source.run_id in source_context_provenance
            },
            "schema_versions": {
                source_id: provenance["schema_versions"]
                for source_id, provenance in source_context_provenance.items()
            },
            "renderer_version": REPORT_RENDERER_VERSION,
            "displayed_counts": self._displayed_counts(
                snapshot_sources,
                discovery_snapshots=discovery_snapshots,
                udmi_snapshot=parameters.get("udmi_report_snapshot"),
            ),
        }
        report_snapshot_v2.update(
            {
                "report_metadata": {
                    "output_format": request.output_format,
                    "report_type": request.report_type,
                    "report_title_custom": custom_report_title,
                    "report_title": parameters["report_title"],
                    "report_generated_at": parameters["report_generated_at"],
                    "renderer_version": REPORT_RENDERER_VERSION,
                },
                "udmi_report_snapshot": parameters.get("udmi_report_snapshot"),
                "source_run_snapshots": source_snapshots,
                "source_run_seals": parameters["source_run_seals"],
                "source_discovery_snapshots": discovery_snapshots,
                "udmi_scope": parameters.get("udmi_scope"),
            }
        )
        parameters["report_snapshot_v2"] = report_snapshot_v2
        parameters["report_snapshot_sha256"] = snapshot_sha256(report_snapshot_v2)

        report_title = str(parameters["report_title"])
        run = self._create_run(
            project_id=request.project_id,
            site_id=request.site_id,
            job_type="report_generation",
            parameters=parameters,
        )
        # Pin the rendered timestamp at creation. New reports therefore build
        # entirely from their own stored record and downloads never need to
        # mutate this provenance field.
        # Reports are NOT processed by a worker actor: the artifact is built
        # on-demand from the stored run record at GET /reports/{id}/download. A
        # report run therefore has nothing to wait for — it is ready the moment
        # it is created. Without this the run sat at the default "queued" status
        # forever and the UI (which only offers a download for "succeeded"
        # reports) could never export it. Mark it terminal-succeeded immediately.
        report = ReportSummary(
            report_id=run.run_id,
            report_type=request.report_type,
            output_format=request.output_format,
            status=run.status,
            file_name=build_report_file_name(
                request.report_type,
                run.run_id,
                request.output_format,
                report_title=report_title,
                custom_title=custom_report_title,
            ),
            # Same projection the list/get path builds in reports.py; `run` here is
            # the RunRecord returned by update_run_status, so created_at is the
            # stored value and the POST response cannot disagree with the later
            # GET of the same report.
            created_at=run.created_at,
            source_run_ids=list(request.source_run_ids),
            report_title=report_title,
        )
        return run, report

    def _snapshot_discovery_rows(self, sources: list[RunRecord]) -> dict[str, object]:
        repository = DiscoveryRepository(self._engine)
        snapshots: dict[str, object] = {}
        for source in sources:
            if source.job_type not in DISCOVERY_JOB_TYPES:
                continue
            topics: list[dict[str, object]] = []
            for topic in repository.list_topics(source.run_id):
                safe_topic = self._safe_report_value(dict(topic))
                if isinstance(safe_topic, dict):
                    topics.append(safe_topic)
            snapshots[source.run_id] = {
                "devices": self._safe_report_value(repository.list_devices(source.run_id)),
                "points": self._safe_report_value(repository.list_points(source.run_id)),
                "topics": topics,
            }
        return snapshots

    @staticmethod
    def _safe_report_value(value: object) -> object:
        """Copy renderer input while dropping secrets and unsafe raw payloads."""
        excluded = {
            "password",
            "broker_password",
            "mqtt_password",
            "key_password",
            "token",
            "access_token",
            "api_token",
            "api_key",
            "private_key",
            "client_certificate",
            "ca_certificate",
            "secret",
            "payload",
            "last_payload",
            "raw_payload",
            "payload_bytes",
            "raw_message",
        }
        if isinstance(value, dict):
            return {
                str(key): RunService._safe_report_value(child)
                for key, child in value.items()
                if str(key).strip().casefold().replace("-", "_") not in excluded
            }
        if isinstance(value, list):
            return [RunService._safe_report_value(child) for child in value]
        if isinstance(value, tuple):
            return [RunService._safe_report_value(child) for child in value]
        return value

    @staticmethod
    def _displayed_counts(
        sources: list[RunRecord],
        *,
        discovery_snapshots: dict[str, object],
        udmi_snapshot: object,
    ) -> dict[str, object]:
        counts: dict[str, object] = {
            "source_runs": len(sources),
            "issues": sum(len(source.issues) for source in sources),
        }
        for key in ("devices", "points", "topics"):
            counts[key] = sum(
                len(snapshot.get(key, []))
                for snapshot in discovery_snapshots.values()
                if isinstance(snapshot, dict) and isinstance(snapshot.get(key), list)
            )
        if isinstance(udmi_snapshot, dict):
            for key in (
                "asset_metrics",
                "payload_metrics",
                "fault_metrics",
                "issue_metrics",
            ):
                value = udmi_snapshot.get(key)
                if isinstance(value, dict):
                    counts[key] = RunService._safe_report_value(value)
        return counts

    def get_run(self, run_id: str) -> RunRecord:
        # Read paths are observational only. Lease expiry and stale-worker
        # recovery belong to the periodic recovery task, never to GET or SSE.
        return RunRecord.model_validate(self._store.get_run(run_id))

    def delete_report_runs(
        self,
        report_ids: list[str],
    ) -> list[tuple[str, dict[str, object] | None]]:
        """Delete validated generated-report rows in one transaction.

        Every requested id is resolved and checked before the first mutation.
        Source runs therefore cannot be removed through the report API, and a
        mixed valid/invalid batch leaves all reports intact. The returned signed
        manifests let the route remove report-owned local bytes after commit;
        shared content-addressed sync bytes are deliberately outside this method.
        """

        ordered_ids = list(dict.fromkeys(report_id.strip() for report_id in report_ids))
        with session_factory(self._engine).begin() as session:
            rows = session.execute(
                select(Run).where(Run.id.in_(ordered_ids)).with_for_update()
            ).scalars().all()
            rows_by_id = {row.id: row for row in rows}
            for report_id in ordered_ids:
                row = rows_by_id.get(report_id)
                if row is None or row.job_type != "report_generation":
                    raise FileNotFoundError(report_id)

            deleted: list[tuple[str, dict[str, object] | None]] = []
            for report_id in ordered_ids:
                row = rows_by_id[report_id]
                summary = row.result_summary if isinstance(row.result_summary, dict) else {}
                raw_manifest = summary.get("artifact_manifest")
                manifest = dict(raw_manifest) if isinstance(raw_manifest, dict) else None
                deleted.append((report_id, manifest))
                session.delete(row)
        logger.info("report runs deleted count=%s", len(deleted))
        return deleted

    def list_runs(
        self,
        *,
        job_types: set[JobType] | None = None,
        project_id: str | None = None,
        site_id: str | None = None,
        edge_id: str | None = None,
        status: JobStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobSummary]:
        """Return run summaries newest-first, including each run's edge_id.

        The core DbRunStore.list_runs intentionally strips edge_id (and the other
        sync columns) from its public record shape, so this query goes straight to
        the Run table on the same engine to surface edge attribution and to filter
        by edge_id / status without touching core. The result is the same
        ordering (created_at desc, id desc) the store uses, mapped to JobSummary
        (which now carries the additive edge_id field).
        """
        statement = select(
            Run.id,
            Run.job_type,
            Run.status,
            Run.stage,
            Run.progress_percent,
            Run.created_at,
            Run.updated_at,
            Run.edge_id,
        ).order_by(Run.created_at.desc(), Run.id.desc())
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        if site_id is not None:
            statement = statement.where(Run.site_id == site_id)
        if job_types is not None:
            statement = statement.where(Run.job_type.in_(sorted(job_types)))
        if edge_id is not None:
            statement = statement.where(Run.edge_id == edge_id)
        if status is not None:
            statement = statement.where(Run.status == status)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)

        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [
            JobSummary(
                run_id=row.id,
                job_type=row.job_type,
                status=row.status,
                stage=row.stage,
                progress_percent=row.progress_percent,
                created_at=row.created_at,
                updated_at=row.updated_at,
                edge_id=row.edge_id,
            )
            for row in rows
        ]

    def list_report_records(self, *, limit: int = 100, offset: int = 0) -> list[RunRecord]:
        """Return report runs with their persisted metadata in one query.

        The Reports list needs the stored parameters to form names and source
        run links.  Fetching summaries and then calling ``get_run`` for every
        row turns a simple page load into an N+1 query pattern. The explicit
        page bounds keep a large historical archive from becoming an unbounded
        response or DOM render.
        """
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        statement = (
            select(Run)
            .where(Run.job_type == "report_generation")
            .order_by(Run.created_at.desc(), Run.id.desc())
            .offset(safe_offset)
            .limit(safe_limit)
        )
        with session_factory(self._engine)() as session:
            rows = session.scalars(statement).all()
            return [
                RunRecord.model_validate(
                    {
                        "run_id": row.id,
                        "project_id": row.project_id,
                        "site_id": row.site_id,
                        "job_type": row.job_type,
                        "status": row.status,
                        "stage": row.stage,
                        "progress_percent": row.progress_percent,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                        "edge_id": row.edge_id,
                        "parameters": row.parameters if isinstance(row.parameters, dict) else {},
                        "result_summary": {},
                        "issues": [],
                        "error_message": row.error_message,
                    }
                )
                for row in rows
            ]

    def count_report_records(self) -> int:
        statement = select(func.count()).select_from(Run).where(Run.job_type == "report_generation")
        with session_factory(self._engine)() as session:
            return int(session.scalar(statement) or 0)

    def runtime_ready(self) -> tuple[bool, str]:
        try:
            ensure_runtime_directories()
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except (OSError, SQLAlchemyError) as error:
            return False, str(error)
        return True, "run store database is reachable"

    def update_run_status(
        self,
        run_id: str,
        *,
        status: JobStatus,
        stage: str | None = None,
        progress_percent: int | None = None,
        error_message: str | None = None,
    ) -> RunRecord:
        record = self._store.update_run_status(
            run_id,
            status=status,
            stage=stage,
            progress_percent=progress_percent,
            error_message=error_message,
        )
        # Run-lifecycle breadcrumb: every status transition written through the
        # service (running -> succeeded/failed/cancelled, and the startup sweep)
        # so a log bundle tells the session story.
        logger.info("run status run_id=%s status=%s stage=%s", run_id, status, stage)
        return RunRecord.model_validate(record)

    def sweep_interrupted_runs(self) -> list[str]:
        """Mark runs fossilized at "running"/"queued" by a restart as failed.

        A run stuck at "running" — or at the default "queued" status — after an
        application restart (or a crash/500 mid-persist) will never reach a
        terminal status on its own: the process that owned it is gone. Flip such
        runs to "failed" with :data:`INTERRUPTED_RUN_MESSAGE` so the operator sees
        an actionable result instead of a run that spins forever.

        "queued" is swept too because a backgrounded inline run (ITEM-4) is
        committed at "queued" and only flips to "running" once its daemon thread
        starts; a portable-exe process exit in that window strands it at "queued",
        which the crash guard in :mod:`app.services.run_dispatch` cannot catch
        (process death, not an exception). Without this the module head's Execute
        stayed disabled across restarts (the run rehydrates as a live monitor that
        can never terminate).

        Worker-bound rows enter a two-minute confirmation window after their
        liveness timestamp expires: one hour while queued, or two minutes while a
        running actor should be writing 30-second heartbeats. A live actor clears
        the suspicion marker with its next write; only continued silence becomes
        terminal. Inline runs carry no worker markers and are reclaimed immediately
        at startup. Returns the ids swept (may be empty).
        """
        statement = select(Run.id, Run.result_summary).where(Run.status.in_(("running", "queued")))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
        swept: list[str] = []
        for run_id, result_summary in rows:
            summary = result_summary if isinstance(result_summary, dict) else {}
            if _was_queued_to_worker(summary):
                if self._recover_stale_worker_run(run_id):
                    swept.append(run_id)
                continue
            self.update_run_status(
                run_id,
                status="failed",
                stage="interrupted_by_restart",
                progress_percent=100,
                error_message=INTERRUPTED_RUN_MESSAGE,
            )
            swept.append(run_id)
        return swept

    def _recover_stale_worker_run(self, run_id: str) -> bool:
        """Confirm, then atomically fail, a worker whose heartbeat stays stale.

        The first stale observation is deliberately non-terminal. This closes
        the race after a database outage where the API reconnects milliseconds
        before the live worker's next heartbeat. DbRunStore clears the marker on
        every accepted lifecycle, result-summary, or issue write.
        """
        statement = (
            select(Run.status, Run.result_summary, Run.updated_at)
            .where(Run.id == run_id)
            .with_for_update()
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).one_or_none()
            if row is None:
                return False
            summary = row.result_summary if isinstance(row.result_summary, dict) else {}
            if not _was_queued_to_worker(summary) or row.status not in {"queued", "running"}:
                return False
            now = datetime.now(UTC)
            first_observed_at = _stale_observed_at(summary)
            heartbeat_at = (
                row.updated_at
                if row.updated_at.tzinfo is not None
                else row.updated_at.replace(tzinfo=UTC)
            ).astimezone(UTC)
            if first_observed_at is not None and heartbeat_at > first_observed_at:
                # A result/issue write can advance updated_at even if the
                # dedicated heartbeat write failed. That is still proof the live
                # actor reached the database after suspicion was recorded.
                refreshed_summary = dict(summary)
                refreshed_summary.pop(WORKER_STALE_OBSERVED_AT_KEY, None)
                connection.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.status == row.status)
                    .values(result_summary=refreshed_summary)
                )
                return False
            if not _worker_run_is_stale(
                status=row.status,
                updated_at=row.updated_at,
                now=now,
            ):
                return False
            if first_observed_at is None:
                # Do not update Run.updated_at here: that column remains the real
                # worker heartbeat. The JSON marker starts a separate, bounded
                # confirmation window and is removed by the next live beat.
                connection.execute(
                    update(Run)
                    .where(Run.id == run_id, Run.status == row.status)
                    .values(
                        result_summary={
                            **summary,
                            WORKER_STALE_OBSERVED_AT_KEY: now.isoformat(),
                        }
                    )
                )
                logger.warning(
                    "worker heartbeat stale; awaiting confirmation for run_id=%s",
                    run_id,
                )
                return False
            if now - first_observed_at <= _WORKER_STALE_CONFIRM_AFTER:
                return False
            terminal_summary = dict(summary)
            terminal_summary.pop(WORKER_STALE_OBSERVED_AT_KEY, None)
            result = connection.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == row.status)
                .values(
                    status="failed",
                    stage="worker_heartbeat_expired",
                    progress_percent=100,
                    error_message=WORKER_INTERRUPTED_RUN_MESSAGE,
                    cancel_requested=True,
                    result_summary=terminal_summary,
                    updated_at=now,
                )
            )
        recovered = bool(result.rowcount)
        if recovered:
            logger.warning("worker heartbeat expired for run_id=%s", run_id)
        return recovered

    def update_result_summary(
        self,
        run_id: str,
        result_summary: dict[str, object],
        *,
        merge: bool = True,
    ) -> RunRecord:
        record = self._store.update_result_summary(run_id, result_summary, merge=merge)
        return RunRecord.model_validate(record)

    def complete_report_run(self, run_id: str, manifest: dict[str, object]) -> RunRecord:
        """Publish one immutable artifact manifest and terminal status atomically."""

        if not verify_signed_manifest(manifest):
            raise ValueError("Report artifact manifest is not a supported valid signature.")
        if manifest.get("report_id") != run_id:
            raise ValueError("Report artifact manifest identifies a different run.")
        now = datetime.now(UTC)
        factory = session_factory(self._engine)
        with factory.begin() as session:
            row = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if row is None:
                raise FileNotFoundError(run_id)
            if row.job_type != "report_generation":
                raise ValueError(f"Run '{run_id}' is not a report-generation run.")
            current = row.result_summary if isinstance(row.result_summary, dict) else {}
            existing = current.get("artifact_manifest")
            if existing is not None:
                if existing != manifest or row.status != "succeeded":
                    raise RuntimeError("Report artifact finalization conflicts with stored state.")
                result_row = session.get(RunResult, run_id)
                seal_row = session.get(RunSeal, run_id)
                if (
                    result_row is None
                    or seal_row is None
                    or result_row.result_sha256 != row.result_sha256
                    or seal_row.result_sha256 != row.result_sha256
                ):
                    raise RuntimeError("Report artifact finalization is not fully sealed.")
            else:
                if row.status not in {"queued", "running"}:
                    raise RuntimeError(f"Report run '{run_id}' is already terminal ({row.status}).")
                report_snapshot = row.parameters.get("report_snapshot_v2")
                expected_snapshot_hash = row.parameters.get("report_snapshot_sha256")
                if not isinstance(report_snapshot, dict) or not isinstance(
                    expected_snapshot_hash, str
                ):
                    raise RuntimeError("Report run has no complete frozen snapshot.")
                if snapshot_sha256(report_snapshot) != expected_snapshot_hash:
                    raise RuntimeError("Report run snapshot digest is inconsistent.")
                if manifest.get("snapshot_sha256") != expected_snapshot_hash:
                    raise RuntimeError("Artifact manifest does not bind the frozen snapshot.")
                artifact_hash = manifest.get("artifact_sha256")
                next_summary = {
                    **current,
                    "artifact_manifest": dict(manifest),
                    "integrity": {
                        "algorithm": "sha256",
                        "hash": artifact_hash,
                        "signature_algorithm": manifest.get("signature_algorithm"),
                        "signature": manifest.get("signature"),
                        "public_key_pem": manifest.get("public_key_pem"),
                        "public_key_fingerprint": manifest.get("signing_key_id"),
                        "signed_at": manifest.get("signed_at"),
                    },
                }
                terminal = TerminalResultV1(
                    status="succeeded",
                    stage="report_ready",
                    summary=next_summary,
                )
                result_sha256 = terminal.sha256()
                row.result_summary = next_summary
                row.status = terminal.status
                row.stage = terminal.stage
                row.progress_percent = 100
                row.updated_at = now
                row.terminal_at = now
                row.result_sha256 = result_sha256
                session.add(
                    RunResult(
                        run_id=run_id,
                        schema_version=terminal.schema_version,
                        terminal_status=terminal.status,
                        terminal_stage=terminal.stage,
                        summary=next_summary,
                        result_payload=terminal.model_dump(mode="json"),
                        result_sha256=result_sha256,
                        created_at=now,
                    )
                )
                session.add(
                    RunSeal(
                        run_id=run_id,
                        terminal_status=terminal.status,
                        context_sha256=expected_snapshot_hash,
                        result_sha256=result_sha256,
                        sealed_at=now,
                    )
                )
        return self.get_run(run_id)

    def replace_issues(
        self,
        run_id: str,
        issues: list[ValidationIssueRecord | dict[str, object]],
    ) -> RunRecord:
        return RunRecord.model_validate(self._store.replace_issues(run_id, issues))

    # -- cooperative cancellation (CancellableRunStore protocol) --------------

    def request_cancel(self, run_id: str) -> RunRecord:
        """Flag the run as cancellation-requested; returns the updated run.

        Cooperative: running engines poll :meth:`is_cancel_requested` and stop
        early, flipping the terminal status to ``cancelled``. Raises
        FileNotFoundError for a missing run (the route maps that to 404).
        """
        try:
            self._lifecycle.get_context(run_id)
        except FileNotFoundError:
            return RunRecord.model_validate(self._store.request_cancel(run_id))
        self._lifecycle.request_cancel(run_id)
        return self.get_run(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        """Return True if cancellation has been requested for the run.

        Exposed so the engine framework (and inline dispatch) can build a
        cancellation checker from this RunService directly. Never raises for a
        missing run (a vanished run cannot be cancelled).
        """
        return self._store.is_cancel_requested(run_id)

    @property
    def engine(self) -> Engine:
        """The shared SQLAlchemy engine backing this service.

        Exposed so route dispatch can build a DiscoveryRepository / loaders on
        the SAME engine/database the run store uses.
        """
        return self._engine

    def _create_run(
        self,
        *,
        project_id: str,
        site_id: str,
        job_type: JobType,
        parameters: dict[str, object],
    ) -> RunRecord:
        record = self._store.create_run(
            project_id=project_id,
            site_id=site_id,
            job_type=job_type,
            parameters=parameters,
        )
        # Run attribution: stamp the local edge_id so a run's origin is recorded
        # before it ever syncs. DbRunStore's public record projection omits this
        # column, so carry the same resolved value in the immediate RunRecord.
        # Report materialization uses it to bind the signed artifact manifest to
        # the edge identity that will later sign the outer Sync v2 bundle.
        local_edge_id = self._stamp_local_edge_id(str(record["run_id"]))
        # Run-lifecycle breadcrumb (run created) — see update_run_status.
        logger.info("run created run_id=%s job_type=%s", record["run_id"], job_type)
        return RunRecord.model_validate({**record, "edge_id": local_edge_id})

    def _stamp_local_edge_id(self, run_id: str) -> str | None:
        """Record the originating (local) edge id on a freshly created run.

        Best-effort and non-fatal: edge_id is provenance metadata, not part of
        the run contract. If identity resolution fails (e.g. crypto unavailable
        still yields an id, but an I/O error could occur), run creation must not
        break — the run simply stays unattributed (edge_id NULL) and can still
        be processed locally.
        """
        try:
            local_edge_id = edge_identity().edge_id
        except Exception:  # pragma: no cover - identity I/O is best effort
            return None
        with self._engine.begin() as connection:
            connection.execute(
                update(Run).where(Run.id == run_id).values(edge_id=local_edge_id)
            )
        return local_edge_id
