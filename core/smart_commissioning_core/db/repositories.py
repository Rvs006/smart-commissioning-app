"""Configuration and import repositories backed by the database.

Configuration payloads are stored as opaque JSON snapshots versioned per
project+site (current = highest version). Secret material is NOT stored here —
payloads only carry secret:// references; the secret files stay on disk.

Import records mirror the imp_... summary/errors/accepted_rows JSON files
written today by backend ImportService.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from smart_commissioning_core.db.db_run_store import (
    get_or_create_project_and_site,
)
from smart_commissioning_core.db.engine import session_factory
from smart_commissioning_core.db.models import (
    ConfigurationSnapshot,
    DiscoveredDevice,
    DiscoveredPoint,
    DiscoveredTopic,
    ImportRecord,
    Run,
    RunIssue,
)

# Terminal run statuses eligible for sync. In-flight runs (queued/running/...)
# are NEVER bundled: only finished, immutable records leave the edge.
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")


class ConfigurationRepository:
    """Versioned configuration snapshots per project+site."""

    def __init__(self, engine: Engine) -> None:
        self._session_factory = session_factory(engine)

    def get_current(self, project_id: str, site_id: str) -> dict[str, object] | None:
        """Return the highest-version payload for the project+site, or None."""
        statement = (
            select(ConfigurationSnapshot)
            .where(
                ConfigurationSnapshot.project_id == project_id,
                ConfigurationSnapshot.site_id == site_id,
            )
            .order_by(ConfigurationSnapshot.version.desc())
            .limit(1)
        )
        with self._session_factory() as session:
            snapshot = session.scalars(statement).one_or_none()
            if snapshot is None:
                return None
            return dict(snapshot.payload)

    def save(self, project_id: str, site_id: str, payload: dict[str, object]) -> int:
        """Persist a new snapshot version in one transaction and return it.

        The version is monotonic per project+site; the unique constraint on
        (project_id, site_id, version) guards concurrent writers. On Postgres
        two concurrent savers can compute the same version — the loser's
        IntegrityError is retried once against the fresh max.
        """
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    current_version = session.scalar(
                        select(func.max(ConfigurationSnapshot.version)).where(
                            ConfigurationSnapshot.project_id == project_id,
                            ConfigurationSnapshot.site_id == site_id,
                        )
                    )
                    new_version = (current_version or 0) + 1
                    session.add(
                        ConfigurationSnapshot(
                            project_id=project_id,
                            site_id=site_id,
                            version=new_version,
                            payload=dict(payload),
                            created_at=datetime.now(UTC),
                        )
                    )
                    return new_version
            except IntegrityError:
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")


def _import_to_dict(record: ImportRecord) -> dict[str, object]:
    return {
        "import_id": record.import_id,
        "import_type": record.import_type,
        "project_id": record.project_id,
        "site_id": record.site_id,
        "original_filename": record.original_filename,
        "stored_file_path": record.stored_file_path,
        "summary": dict(record.summary or {}),
        "accepted_rows": list(record.accepted_rows or []),
        "errors": list(record.errors or []),
        "created_at": record.created_at.isoformat(),
    }


class ImportRepository:
    """Import batches mirroring the ImportService summary/errors/accepted_rows shapes.

    Missing import ids raise FileNotFoundError(import_id), matching the
    file-based behaviour the API routes already handle.
    """

    def __init__(self, engine: Engine) -> None:
        self._session_factory = session_factory(engine)

    def create(
        self,
        *,
        import_id: str,
        import_type: str,
        original_filename: str,
        stored_file_path: str,
        summary: dict[str, object],
        accepted_rows: list[dict[str, object]] | None = None,
        errors: list[dict[str, object]] | None = None,
        project_id: str | None = None,
        site_id: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, object]:
        record = ImportRecord(
            import_id=import_id,
            import_type=import_type,
            project_id=project_id,
            site_id=site_id,
            original_filename=original_filename,
            stored_file_path=stored_file_path,
            summary=dict(summary),
            accepted_rows=list(accepted_rows or []),
            errors=list(errors or []),
            created_at=created_at or datetime.now(UTC),
        )
        with self._session_factory.begin() as session:
            session.add(record)
            session.flush()
            return _import_to_dict(record)

    def get(self, import_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            return _import_to_dict(self._load(session, import_id))

    def get_summary(self, import_id: str) -> dict[str, object]:
        """Return the stored ImportBatchSummary-shaped payload."""
        with self._session_factory() as session:
            return dict(self._load(session, import_id).summary or {})

    def get_errors(self, import_id: str) -> dict[str, object]:
        """Return an ImportErrorReport-shaped payload: {import_id, errors}."""
        with self._session_factory() as session:
            record = self._load(session, import_id)
            return {"import_id": record.import_id, "errors": list(record.errors or [])}

    def get_accepted_rows(self, import_id: str) -> list[dict[str, object]]:
        with self._session_factory() as session:
            return list(self._load(session, import_id).accepted_rows or [])

    def list(
        self,
        project_id: str | None = None,
        site_id: str | None = None,
        import_type: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        statement = select(ImportRecord).order_by(
            ImportRecord.created_at.desc(), ImportRecord.import_id.desc()
        )
        if project_id is not None:
            statement = statement.where(ImportRecord.project_id == project_id)
        if site_id is not None:
            statement = statement.where(ImportRecord.site_id == site_id)
        if import_type is not None:
            statement = statement.where(ImportRecord.import_type == import_type)
        if offset:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            records = session.scalars(statement).all()
            return [_import_to_dict(record) for record in records]

    def _load(self, session: Session, import_id: str) -> ImportRecord:
        record = session.get(ImportRecord, import_id)
        if record is None:
            raise FileNotFoundError(import_id)
        return record


def _device_to_dict(row: DiscoveredDevice) -> dict[str, object]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "position": row.position,
        "project_id": row.project_id,
        "site_id": row.site_id,
        "address": row.address,
        "device_type": row.device_type,
        "name": row.name,
        "vendor": row.vendor,
        "model": row.model,
        "attributes": dict(row.attributes or {}),
        "created_at": row.created_at.isoformat(),
    }


def _point_to_dict(row: DiscoveredPoint) -> dict[str, object]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "position": row.position,
        "device_ref": row.device_ref,
        "point_id": row.point_id,
        "point_name": row.point_name,
        "observed_value": dict(row.observed_value or {}),
        "units": row.units,
        "attributes": dict(row.attributes or {}),
        "created_at": row.created_at.isoformat(),
    }


def _topic_to_dict(row: DiscoveredTopic) -> dict[str, object]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "position": row.position,
        "topic": row.topic,
        "last_payload": dict(row.last_payload or {}),
        "message_count": row.message_count,
        "attributes": dict(row.attributes or {}),
        "created_at": row.created_at.isoformat(),
    }


class DiscoveryRepository:
    """Persist + read discovery results (devices, points, MQTT topics) per run.

    Generic over the three discovery engines: per-vendor / per-protocol fields
    live in each row's JSON ``attributes`` column. All ``replace_*`` methods are
    idempotent re-writes for a run (delete existing rows for the run, then
    insert the supplied rows in caller order), executed in a SINGLE transaction
    so a run never sees a half-written result set. Rows cascade-delete with the
    owning run (FK ``ondelete="CASCADE"``).

    Method contract (consumed verbatim by the wiring agent / API):

        replace_devices(run_id: str, devices: list[dict]) -> int
        replace_points(run_id: str, points: list[dict]) -> int
        replace_topics(run_id: str, topics: list[dict]) -> int
            Replace all rows of that kind for the run; return the count written.
            Each input dict may carry any of the model's named columns plus an
            ``attributes`` dict; unknown keys are NOT silently dropped into the
            row — pass engine-specific data under ``attributes``.

        list_devices(run_id) -> list[dict]
        list_points(run_id) -> list[dict]
        list_topics(run_id) -> list[dict]
            Return rows for the run in stored ``position`` order as plain dicts.

        count_devices(run_id) -> int
        count_points(run_id) -> int
        count_topics(run_id) -> int
            Return the number of rows of that kind for the run.
    """

    # Named (non-attributes) columns accepted on each row dict.
    _DEVICE_COLUMNS = (
        "project_id",
        "site_id",
        "address",
        "device_type",
        "name",
        "vendor",
        "model",
    )
    _POINT_COLUMNS = ("device_ref", "point_id", "point_name", "observed_value", "units")
    _TOPIC_COLUMNS = ("topic", "last_payload", "message_count")

    def __init__(self, engine: Engine) -> None:
        self._session_factory = session_factory(engine)

    # -- devices --------------------------------------------------------------

    def replace_devices(
        self, run_id: str, devices: list[dict[str, object]]
    ) -> int:
        with self._session_factory.begin() as session:
            session.execute(
                delete(DiscoveredDevice).where(DiscoveredDevice.run_id == run_id)
            )
            session.flush()
            for position, payload in enumerate(devices):
                session.add(self._device_row(run_id, position, payload))
            session.flush()
        return len(devices)

    def list_devices(self, run_id: str) -> list[dict[str, object]]:
        statement = (
            select(DiscoveredDevice)
            .where(DiscoveredDevice.run_id == run_id)
            .order_by(DiscoveredDevice.position, DiscoveredDevice.id)
        )
        with self._session_factory() as session:
            return [_device_to_dict(row) for row in session.scalars(statement).all()]

    def count_devices(self, run_id: str) -> int:
        return self._count(DiscoveredDevice, run_id)

    # -- points ---------------------------------------------------------------

    def replace_points(self, run_id: str, points: list[dict[str, object]]) -> int:
        with self._session_factory.begin() as session:
            session.execute(
                delete(DiscoveredPoint).where(DiscoveredPoint.run_id == run_id)
            )
            session.flush()
            for position, payload in enumerate(points):
                session.add(self._point_row(run_id, position, payload))
            session.flush()
        return len(points)

    def list_points(self, run_id: str) -> list[dict[str, object]]:
        statement = (
            select(DiscoveredPoint)
            .where(DiscoveredPoint.run_id == run_id)
            .order_by(DiscoveredPoint.position, DiscoveredPoint.id)
        )
        with self._session_factory() as session:
            return [_point_to_dict(row) for row in session.scalars(statement).all()]

    def count_points(self, run_id: str) -> int:
        return self._count(DiscoveredPoint, run_id)

    # -- topics ---------------------------------------------------------------

    def replace_topics(self, run_id: str, topics: list[dict[str, object]]) -> int:
        with self._session_factory.begin() as session:
            session.execute(
                delete(DiscoveredTopic).where(DiscoveredTopic.run_id == run_id)
            )
            session.flush()
            for position, payload in enumerate(topics):
                session.add(self._topic_row(run_id, position, payload))
            session.flush()
        return len(topics)

    def list_topics(self, run_id: str) -> list[dict[str, object]]:
        statement = (
            select(DiscoveredTopic)
            .where(DiscoveredTopic.run_id == run_id)
            .order_by(DiscoveredTopic.position, DiscoveredTopic.id)
        )
        with self._session_factory() as session:
            return [_topic_to_dict(row) for row in session.scalars(statement).all()]

    def count_topics(self, run_id: str) -> int:
        return self._count(DiscoveredTopic, run_id)

    # -- internals ------------------------------------------------------------

    def _count(self, model: type, run_id: str) -> int:
        statement = select(func.count()).select_from(model).where(model.run_id == run_id)
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    def _device_row(
        self, run_id: str, position: int, payload: dict[str, object]
    ) -> DiscoveredDevice:
        return DiscoveredDevice(
            run_id=run_id,
            position=position,
            attributes=dict(payload.get("attributes") or {}),
            **{key: payload.get(key) for key in self._DEVICE_COLUMNS},
        )

    def _point_row(
        self, run_id: str, position: int, payload: dict[str, object]
    ) -> DiscoveredPoint:
        return DiscoveredPoint(
            run_id=run_id,
            position=position,
            observed_value=dict(payload.get("observed_value") or {}),
            attributes=dict(payload.get("attributes") or {}),
            **{
                key: payload.get(key)
                for key in self._POINT_COLUMNS
                if key != "observed_value"
            },
        )

    def _topic_row(
        self, run_id: str, position: int, payload: dict[str, object]
    ) -> DiscoveredTopic:
        return DiscoveredTopic(
            run_id=run_id,
            position=position,
            topic=str(payload.get("topic") or ""),
            last_payload=dict(payload.get("last_payload") or {}),
            message_count=int(payload.get("message_count") or 0),
            attributes=dict(payload.get("attributes") or {}),
        )


class SyncRepository:
    """Edge<->hub synchronization persistence: watermarks + immutable ingest.

    This repository is the transport-agnostic bridge the sync layer
    (smart_commissioning_core.sync) builds on. It does two jobs:

    EDGE side (push):
        * :meth:`list_unsynced_terminal_runs` — run ids of TERMINAL runs not yet
          pushed from here (``synced_at IS NULL``), oldest-first.
        * :meth:`mark_synced` — stamp ``synced_at`` on the pushed run ids so the
          next bundle excludes them (the edge watermark).

    HUB side (ingest):
        * :meth:`run_exists` / :meth:`get_run_for_export` — read a full run
          payload (run + issues + discovery rows) for hashing/comparison.
        * :meth:`insert_run_record` — immutably insert a run and all its child
          rows in one transaction, stamping the originating ``edge_id``. Used by
          the hub when a run id is absent; never overwrites an existing run.

    Reuses the existing project/site get-or-create and the established row
    serializations so hub copies are byte-for-byte comparable with edge copies.
    """

    def __init__(self, engine: Engine) -> None:
        self._session_factory = session_factory(engine)
        self._discovery = DiscoveryRepository(engine)

    # -- edge: watermark / unsynced listing ----------------------------------

    def list_unsynced_terminal_runs(
        self,
        *,
        project_id: str | None = None,
        site_id: str | None = None,
        statuses: tuple[str, ...] = TERMINAL_RUN_STATUSES,
    ) -> list[str]:
        """Return ids of terminal runs not yet synced from here, oldest-first.

        A run is eligible when its status is terminal AND ``synced_at IS NULL``.
        Oldest-first (created_at, id) so a bundle preserves production order.
        """
        statement = (
            select(Run.id)
            .where(Run.status.in_(statuses))
            .where(Run.synced_at.is_(None))
            .order_by(Run.created_at.asc(), Run.id.asc())
        )
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        if site_id is not None:
            statement = statement.where(Run.site_id == site_id)
        with self._session_factory() as session:
            return [row for row in session.scalars(statement).all()]

    def mark_synced(self, run_ids: list[str], *, now: datetime) -> int:
        """Stamp ``synced_at = now`` on the given run ids; return rows updated.

        The edge calls this after a successful push so those runs drop out of the
        next :meth:`list_unsynced_terminal_runs`. Idempotent re-stamping is fine
        (it just advances the watermark). Unknown ids are ignored.
        """
        if not run_ids:
            return 0
        updated = 0
        with self._session_factory.begin() as session:
            runs = session.scalars(select(Run).where(Run.id.in_(run_ids))).all()
            for run in runs:
                run.synced_at = now
                updated += 1
        return updated

    # -- hub: read for hashing / comparison ----------------------------------

    def run_exists(self, run_id: str) -> bool:
        """True if a run with this id already exists (hub idempotency check)."""
        with self._session_factory() as session:
            return session.get(Run, run_id) is not None

    def get_run_for_export(self, run_id: str) -> dict[str, object] | None:
        """Return the full exportable payload for a run, or None if absent.

        Shape is the canonical sync content (see sync.build_run_content): the
        13-key run record plus its edge_id, issues, and discovery rows. Used both
        to export a run for a bundle and to recompute a stored run's content hash
        on the hub for idempotency / immutability checks.
        """
        from smart_commissioning_core.db.db_run_store import _run_to_dict

        with self._session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            record = _run_to_dict(run)
            edge_id = run.edge_id
        discovery = self._discovery
        return {
            "run": record,
            "edge_id": edge_id,
            "issues": list(record["issues"]),
            "devices": discovery.list_devices(run_id),
            "points": discovery.list_points(run_id),
            "topics": discovery.list_topics(run_id),
        }

    # -- hub: immutable insert -----------------------------------------------

    def insert_run_record(
        self,
        *,
        run: dict[str, object],
        issues: list[dict[str, object]],
        devices: list[dict[str, object]],
        points: list[dict[str, object]],
        topics: list[dict[str, object]],
        edge_id: str | None,
    ) -> None:
        """Insert a complete run + children in ONE transaction (hub ingest).

        Immutability is the caller's contract: the hub only calls this when the
        run id is ABSENT. ``edge_id`` (from the bundle manifest) is stamped onto
        the row so the hub knows the source. The run's own created_at/updated_at
        from the edge are preserved; ``synced_at`` is left NULL on the hub (the
        hub is a sink, it does not re-push by default).

        Raises if the run id already exists (the unique PK), so a concurrent
        double-insert can never silently overwrite.
        """
        with self._session_factory.begin() as session:
            get_or_create_project_and_site(
                session, str(run["project_id"]), str(run["site_id"])
            )
            session.add(self._run_row(run, edge_id=edge_id))
            session.flush()
            run_id = str(run["run_id"])
            for position, issue in enumerate(issues):
                session.add(self._issue_row(run_id, position, issue))
            for position, device in enumerate(devices):
                session.add(_insert_discovery_device(run_id, position, device))
            for position, point in enumerate(points):
                session.add(_insert_discovery_point(run_id, position, point))
            for position, topic in enumerate(topics):
                session.add(_insert_discovery_topic(run_id, position, topic))
            session.flush()

    # -- row builders --------------------------------------------------------

    def _run_row(self, run: dict[str, object], *, edge_id: str | None) -> Run:
        return Run(
            id=str(run["run_id"]),
            project_id=str(run["project_id"]),
            site_id=str(run["site_id"]),
            job_type=str(run["job_type"]),
            status=str(run["status"]),
            stage=str(run["stage"]),
            progress_percent=int(run.get("progress_percent") or 0),
            parameters=dict(run.get("parameters") or {}),
            result_summary=dict(run.get("result_summary") or {}),
            execution_mode=run.get("execution_mode"),
            error_message=run.get("error_message"),
            edge_id=edge_id,
            synced_at=None,
            created_at=_parse_dt(run.get("created_at")),
            updated_at=_parse_dt(run.get("updated_at")),
        )

    def _issue_row(self, run_id: str, position: int, issue: dict[str, object]) -> RunIssue:
        # Round-trip through the pydantic record so types (e.g. last_seen_at
        # datetime) match the native create path exactly.
        from smart_commissioning_core.records import ValidationIssueRecord

        record = ValidationIssueRecord.model_validate(issue)
        return RunIssue(run_id=run_id, position=position, **record.model_dump())


def _insert_discovery_device(
    run_id: str, position: int, payload: dict[str, object]
) -> DiscoveredDevice:
    columns = ("project_id", "site_id", "address", "device_type", "name", "vendor", "model")
    return DiscoveredDevice(
        run_id=run_id,
        position=position,
        attributes=dict(payload.get("attributes") or {}),
        **{key: payload.get(key) for key in columns},
    )


def _insert_discovery_point(
    run_id: str, position: int, payload: dict[str, object]
) -> DiscoveredPoint:
    columns = ("device_ref", "point_id", "point_name", "units")
    return DiscoveredPoint(
        run_id=run_id,
        position=position,
        observed_value=dict(payload.get("observed_value") or {}),
        attributes=dict(payload.get("attributes") or {}),
        **{key: payload.get(key) for key in columns},
    )


def _insert_discovery_topic(
    run_id: str, position: int, payload: dict[str, object]
) -> DiscoveredTopic:
    return DiscoveredTopic(
        run_id=run_id,
        position=position,
        topic=str(payload.get("topic") or ""),
        last_payload=dict(payload.get("last_payload") or {}),
        message_count=int(payload.get("message_count") or 0),
        attributes=dict(payload.get("attributes") or {}),
    )


def _parse_dt(value: object) -> datetime:
    """Parse an ISO timestamp from an exported run record back to a datetime.

    Edge records serialize created_at/updated_at via ``.isoformat()``; on ingest
    we parse them back so the hub preserves the edge's original timestamps. A
    missing value falls back to now-UTC (defensive; exports always carry both).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return datetime.now(UTC)
