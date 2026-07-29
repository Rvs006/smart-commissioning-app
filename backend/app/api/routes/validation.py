"""Validation routes: UDMI / MQTT config publish / BACnet point / mapping.

UDMI, BACnet point validation, and BACnet<->MQTT mapping comparison all route
through the shared queue-or-inline dispatcher. The validation/comparison engines
perform NO network I/O, so they require no scan authorization; they read their
expected register from an import (via ImportRepository) and observed values from
a discovery run (via DiscoveryRepository), or inline values supplied in
``parameters`` for testing.

MQTT config publish stays inline (its real broker publish path requires broker
egress) and additionally captures the prior retained config value for rollback;
see :mod:`smart_commissioning_core.mqtt_config_publish` and the ``rollback``
endpoint below.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from smart_commissioning_core.db.repositories import (
    DiscoveryRepository,
    ImportRepository,
    UdmiSchemaSetRepository,
)
from smart_commissioning_core.db.run_lifecycle import ProtocolConflictError
from smart_commissioning_core.engines.comparison import process_mapping_validation_run
from smart_commissioning_core.engines.point_validation import process_bacnet_validation_run
from smart_commissioning_core.mqtt_config_publish_processor import (
    process_mqtt_config_publish_run,
    process_mqtt_config_rollback_run,
)
from smart_commissioning_core.mqtt_settings import (
    INDEFINITE_BACKSTOP_SECONDS,
    parse_bool,
    parse_capture_seconds,
)
from smart_commissioning_core.rbac import Role
from smart_commissioning_core.udmi_run_processor import process_udmi_validation_run
from smart_commissioning_core.udmi_validation import DEFAULT_CAPTURE_SECONDS

from app.core.auth import AuthPrincipal, get_principal, require_role
from app.core.config import get_settings
from app.schemas.jobs import (
    JobAcceptedResponse,
    JobCreateRequest,
    JobType,
    RunListResponse,
    RunRecord,
    ValidationIssueRecord,
    ValidationIssuesResponse,
)
from app.services.configuration_service import ConfigurationService
from app.services.engine_dispatch import (
    make_cancel_checker,
    make_discovery_loader,
    make_import_loader,
)
from app.services.job_queue import JobQueueService

# Relocated to app.services.register_topics (shared with the MQTT discovery
# register-comparison); imported under the original private name so the single
# call site in _asset_entry_from_row and the direct test import in
# test_v1_review_contracts.py both keep working unchanged.
from app.services.register_topics import (
    capture_topics_from_expected as _capture_topics_from_expected,
)
from app.services.run_dispatch import dispatch_run
from app.services.run_service import VALIDATION_JOB_TYPES, RunService
from app.services.validation_export import stable_validation_export_bytes, validation_export_filename

router = APIRouter()
service = RunService()
queue_service = JobQueueService()
config_service = ConfigurationService()
settings = get_settings()

# RBAC: reading validation results/issues is viewer+; creating a validation run,
# publishing an MQTT config, or rolling one back is engineer+ (a publish/rollback
# is a live write, gated additionally by the scan/publish authorization consent).
require_viewer = require_role(Role.VIEWER)
require_engineer = require_role(Role.ENGINEER)

# Hard ceiling on an explicit UDMI capture window: 48 hours. Sourced from the
# single shared core constant (also the backstop for a blank/indefinite capture)
# so this cap and discovery.MQTT_MAX_CAPTURE_SECONDS can never drift. Tied to the
# worker actor's time limit (worker/app/tasks.py validate_udmi_payloads runs at
# 49h = this cap + 1h margin, and MUST stay above it) and to the frontend's
# udmiCaptureOverCap guard (frontend/src/features/workflow/ModulePage.tsx).
MAX_UDMI_CAPTURE_SECONDS = int(INDEFINITE_BACKSTOP_SECONDS)


def _validate_asset_topic_discovery_request(parameters: dict[str, object]) -> None:
    """Reject an unconfirmed/unknown broad diagnostic scope before creating a run."""

    if not parse_bool(parameters.get("topic_discovery_enabled")):
        return
    scope = str(parameters.get("topic_discovery_scope") or "").strip().casefold()
    if scope in {"", "bounded"}:
        return
    if scope == "all":
        if parse_bool(parameters.get("topic_discovery_all_scope_confirmed")):
            return
        raise HTTPException(
            status_code=400,
            detail=(
                "All-topic asset discovery requires topic_discovery_all_scope_confirmed = true. "
                "Use the bounded register scope unless this broader capture is approved."
            ),
        )
    raise HTTPException(
        status_code=400,
        detail="topic_discovery_scope must be 'bounded' or 'all'.",
    )


def _create_run(
    request: JobCreateRequest,
    expected_job_type: JobType,
    principal: AuthPrincipal,
) -> RunRecord:
    try:
        return service.create_job_run(
            request,
            expected_job_type=expected_job_type,
            requesting_principal=principal.username,
        )
    except ProtocolConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A run already owns this protocol connection.",
                "active_run_id": error.active_run_id,
            },
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _import_loader():
    return make_import_loader(ImportRepository(service.engine))


def _discovery_loader():
    return make_discovery_loader(DiscoveryRepository(service.engine))


def _dispatch(run: RunRecord, *, enqueue, run_inline, label: str) -> JobAcceptedResponse:
    return dispatch_run(
        run,
        service=service,
        enqueue=enqueue,
        run_inline=run_inline,
        inline_message=f"{label} run started (local inline execution). Follow progress in the run monitor.",
        queued_message=f"{label} job queued for worker execution.",
    )


def _expected_schedule_from_register_row(row: dict) -> dict:
    """Map an mqtt_register row to the UDMI matcher's expected_schedule.

    Make/Model/GUID/Serial/Firmware/Site/Room feed the metadata/state identity
    checks; comma-separated Expected points apply to both metadata and pointset,
    while Expected units apply only to metadata. Expected schema version drives
    the payload version match and the per-version structural checks. Blank
    fields are dropped so the matcher only checks what's set.
    """
    points = [p.strip() for p in str(row.get("Expected points", "")).split(",") if p.strip()]
    units = [u.strip() for u in str(row.get("Expected units", "")).split(",")]
    fields = {
        "asset_id": row.get("Asset ID") or row.get("Asset name"),
        "project_site": row.get("Project/site"),
        "system": row.get("System"),
        "manufacturer": row.get("Make"),
        "model": row.get("Model"),
        "serial": row.get("Serial number"),
        "firmware": row.get("Firmware"),
        "guid": row.get("GUID"),
        "site": row.get("Site"),
        "room": row.get("Room"),
        "udmi_version": row.get("Expected schema version"),
        "reporting_interval_seconds": row.get("Expected reporting interval"),
    }
    schedule = {key: value for key, value in fields.items() if value}
    if points:
        schedule["points"] = points
    units_map = {point: units[index] for index, point in enumerate(points) if index < len(units) and units[index]}
    if units_map:
        schedule["units"] = units_map
    return schedule


def _asset_entry_from_row(row: dict) -> dict:
    """One UDMI `assets` fan-out entry: expected_schedule + per-asset capture topics."""
    return {
        "expected_schedule": _expected_schedule_from_register_row(row),
        **_capture_topics_from_expected(row.get("Expected topic"), row.get("Payload type")),
    }


def _entry_topic_root(entry: dict) -> str:
    """The asset's topic prefix, used to tell per-payload-type rows of ONE
    device apart from same-Asset-ID rows that point at DIFFERENT devices
    (a register copy-paste error)."""
    for slot, suffix in (
        ("state_topic", "/state"),
        ("metadata_topic", "/metadata"),
        ("pointset_topic", "/events/pointset"),
        ("pointset_topic", "/event/pointset"),
    ):
        topic = str(entry.get(slot) or "")
        if topic.endswith(suffix):
            return topic.removesuffix(suffix)
    return str(entry.get("register_topic_filter") or "").removesuffix("/#").rstrip("/")


def _merge_entry_into(existing: dict, entry: dict) -> None:
    """Fold one register row's entry into an existing same-device entry:
    first-wins for singular fields, union for topics, points, and units."""
    for slot in ("state_topic", "metadata_topic", "pointset_topic", "register_topic_filter"):
        if entry.get(slot) and not existing.get(slot):
            existing[slot] = entry[slot]
    extra = [*existing.get("extra_capture_topics", []), *entry.get("extra_capture_topics", [])]
    if extra:
        existing["extra_capture_topics"] = list(dict.fromkeys(extra))
    existing_schedule = existing["expected_schedule"]
    for field, value in entry["expected_schedule"].items():
        if field == "points":
            existing_schedule["points"] = list(
                dict.fromkeys([*existing_schedule.get("points", []), *value])
            )
        elif field == "units":
            existing_schedule["units"] = {**value, **existing_schedule.get("units", {})}
        elif not existing_schedule.get(field):
            existing_schedule[field] = value


def _merge_asset_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(entries, duplicate_id_records): one assets entry per register DEVICE.

    A register may carry several rows for the same asset (one per payload type,
    or one per topic convention). Each row previously became its OWN assets[]
    entry with the same asset_id, so a payload captured through a shared
    wildcard was reviewed once per row and every issue appeared N times
    (on-site 2026-07-13: one asset showed duplicated issue lists). Rows are
    bucketed per (identity, topic root): a row joins the identity's first
    bucket whose root matches (or where either side has no root yet).

    An identity that ends up with MORE THAN ONE bucket is two different devices
    mislabelled with one ID — merging them would silently fuse two devices'
    evidence, and the UI's per-asset grouping already hides one of them. Each
    device still coalesces its own per-payload-type rows into one entry (also
    for legacy imports that predate the upload-time conflict gate), and the
    collision is reported via the returned duplicate records.
    """
    entries: list[dict] = []
    buckets_by_identity: dict[str, list[list]] = {}  # identity -> [[root, entry], ...]
    for index, row in enumerate(rows):
        entry = _asset_entry_from_row(row)
        identity = str(entry["expected_schedule"].get("asset_id") or "") or f"__row_{index}"
        root = _entry_topic_root(entry)
        buckets = buckets_by_identity.setdefault(identity, [])
        for bucket in buckets:
            if not bucket[0] or not root or bucket[0] == root:
                _merge_entry_into(bucket[1], entry)
                if root and not bucket[0]:
                    bucket[0] = root
                break
        else:
            buckets.append([root, entry])
            entries.append(entry)
    duplicate_records = [
        {
            "asset_id": identity,
            "topic_roots": [bucket[0] for bucket in buckets if bucket[0]],
        }
        for identity, buckets in buckets_by_identity.items()
        if len(buckets) > 1
    ]
    return entries, duplicate_records


def _register_rejection_info(record: dict) -> dict:
    """Rejected-row facts for the import a run is based on.

    A partial import silently narrowed the expected asset list (on-site
    2026-07-13: a publishing device was absent from the results with no
    explanation). The run parameters carry the rejection count and the first
    few row errors so the validator can report them as a real issue.
    """
    errors = [error for error in record.get("errors") or [] if isinstance(error, dict)]
    if not errors:
        return {}
    details = [
        f"row {error.get('row_number')}: {error.get('field')} — {error.get('message')}"
        for error in errors[:5]
    ]
    return {
        "register_rejected_rows": len({error.get("row_number") for error in errors}),
        "register_rejected_details": details,
        "register_import_filename": record.get("original_filename"),
    }


def _register_row_value(row: dict, *headings: str) -> object:
    """Read a canonical register field from an exact-source row."""

    lookup = {
        " ".join(str(key).strip().casefold().split()): value
        for key, value in row.items()
    }
    for heading in headings:
        key = " ".join(heading.strip().casefold().split())
        if key in lookup:
            return lookup[key]
    return None


def _expected_assets_from_register(project_id: str, site_id: str) -> tuple[list[dict], dict]:
    """(assets, register_info) from the newest mqtt_register import.

    One merged fan-out entry per register asset (empty list if no import), plus
    the rejected-row info for that same import so dropped rows are reportable.
    """
    imports = ImportRepository(service.engine).list(
        project_id=project_id, site_id=site_id, import_type="mqtt_register"
    )
    for record in imports:  # newest-first
        rows = record.get("accepted_rows", [])
        if rows:
            entries, duplicate_records = _merge_asset_rows(rows)
            register_info = _register_rejection_info(record)
            # Freeze the exact accepted register evidence on the validation run.
            # Reports must never look up whichever import happens to be newest at
            # render time, because that could annotate a different register from
            # the one the validator actually used. Dict insertion order preserves
            # the imported column order; the union handles defensive legacy rows
            # whose optional columns differ.
            raw_summary = record.get("summary")
            summary = raw_summary if isinstance(raw_summary, dict) else {}
            raw_source_columns = summary.get("source_columns")
            raw_source_rows = summary.get("accepted_source_rows")
            source_rows = (
                [dict(row) for row in raw_source_rows if isinstance(row, dict)]
                if isinstance(raw_source_rows, list)
                else []
            )
            # The source and canonical lists were stored in lockstep. Fall back
            # to legacy accepted rows if older data is incomplete.
            if len(source_rows) != len(rows):
                source_rows = [dict(row) for row in rows if isinstance(row, dict)]
                raw_source_columns = []
            register_columns = (
                [str(column) for column in raw_source_columns]
                if isinstance(raw_source_columns, list)
                else []
            )
            register_rows = [
                {str(key): value for key, value in source_row.items()}
                for source_row in source_rows
            ]
            for row in register_rows:
                for column in row:
                    if column not in register_columns:
                        register_columns.append(column)
            register_info.update(
                {
                    "register_import_id": record.get("import_id"),
                    "register_import_filename": record.get("original_filename"),
                    "register_columns": register_columns,
                    "register_rows": register_rows,
                }
            )
            if duplicate_records:
                register_info["register_duplicate_asset_ids"] = duplicate_records
            return entries, register_info
    return [], {}


@router.post("/udmi/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_udmi_validation_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    # When the operator hasn't pasted expected values, fill them from the imported
    # MQTT register so the workbench validates against Make/Model/GUID/points/units
    # without re-typing. Register rows always become an `assets` list (even a
    # single row) so each asset keeps its register-derived capture topics and the
    # matcher/live capture fan out per asset. An explicit asset_id narrows the
    # list to that row.
    parameters = dict(request.parameters)
    _validate_asset_topic_discovery_request(parameters)
    capture_seconds = parse_capture_seconds(
        parameters.get("capture_seconds"), default=DEFAULT_CAPTURE_SECONDS
    )
    if capture_seconds is not None and capture_seconds > MAX_UDMI_CAPTURE_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"capture_seconds may be at most {MAX_UDMI_CAPTURE_SECONDS} seconds "
                "(48 hours); the worker would kill a longer capture at its time limit."
            ),
        )
    parameters.setdefault("qos", config_service.mqtt_subscribe_defaults(request.project_id, request.site_id)["qos"])
    if not parameters.get("expected_schedule") and not parameters.get("assets"):
        assets, register_info = _expected_assets_from_register(request.project_id, request.site_id)
        asset_id = str(parameters.get("asset_id") or "").strip()
        if asset_id and assets:
            chosen = next((a for a in assets if asset_id == a["expected_schedule"].get("asset_id")), assets[0])
            assets = [chosen]
            chosen_asset_id = str(chosen["expected_schedule"].get("asset_id") or "")
            register_rows = register_info.get("register_rows")
            if isinstance(register_rows, list):
                register_info["register_rows"] = [
                    row
                    for row in register_rows
                    if isinstance(row, dict)
                    and chosen_asset_id
                    == str(_register_row_value(row, "Asset ID", "Asset name") or "")
                ]
        if assets:
            if len(assets) == 1:
                # A caller may pair the single register row with directly
                # supplied payloads; keep them reviewable against that row.
                for key in ("state_payload", "metadata_payload", "pointset_payload", "messages"):
                    if parameters.get(key) is not None:
                        assets[0].setdefault(key, parameters[key])
            parameters["assets"] = assets
            # Rejected rows from the SAME import: the validator reports them so
            # an asset dropped at import time is never silently "not a result".
            parameters.update(register_info)
        elif parameters.get("use_register"):
            # The operator explicitly asked to validate against the imported
            # register and there is none: refuse rather than silently falling
            # back to the packaged sample fixture and presenting it as a result.
            raise HTTPException(
                status_code=400,
                detail=(
                    "No accepted MQTT register import was found for this project/site. "
                    "Upload an mqtt_register file, or untick the register option to "
                    "validate pasted payloads instead."
                ),
            )
    # Embed every uploaded nonpub schema set into the run parameters so a
    # declared nonpub version validates identically on the inline path and on
    # the Dramatiq worker (which shares only the database, never a filesystem).
    # The DB-backed store is the SOLE source: a client-supplied copy would
    # bypass the upload route's validation (label shape, Draft 7, $ref closure,
    # size ceilings), so it is discarded before embedding.
    parameters.pop("nonpub_schema_sets", None)
    nonpub_schema_sets = UdmiSchemaSetRepository(service.engine).get_all_files()
    if nonpub_schema_sets:
        parameters["nonpub_schema_sets"] = nonpub_schema_sets
    run = _create_run(
        request.model_copy(update={"parameters": parameters}), "udmi_validation", principal
    )

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_udmi_validation_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            fallback_reason="JOB_EXECUTION_MODE is set to inline for local development.",
            # Synchronous inline (INLINE_RUN_ASYNC=0) blocks this request until the
            # run finishes, so the client never gets a run_id to reach Stop run — a
            # blank window must bound itself. Async inline is backgrounded (ITEM-4).
            run_is_backgrounded=get_settings().inline_run_async,
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_for("validate_udmi_payloads", "validation"),
        run_inline=run_inline,
        label="UDMI validation",
    )


@router.post(
    "/mqtt-config/runs",
    response_model=JobAcceptedResponse,
    dependencies=[Depends(require_engineer)],
)
def create_mqtt_config_publish_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    # A live publish actively writes to a broker, so gate it on the same scan
    # authorization contract used by the discovery engines. The local validate-only
    # path (use_live_broker not set) is side-effect free and needs no authorization.
    # Authorize BEFORE creating the run: the discovery routes validate first for
    # exactly this reason — a 403-rejected request must never leave an orphaned run
    # stranded at 'queued' (the startup sweep would never reclaim it, and it would
    # count as an active run and pin the run monitor forever).
    _require_publish_authorization(dict(request.parameters))

    run = _create_run(request, "mqtt_config_publish", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_mqtt_config_publish_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
        )

    return _dispatch(
        run,
        enqueue=None,
        run_inline=run_inline,
        label="MQTT config publish",
    )


@router.post(
    "/mqtt-config/runs/{run_id}/rollback",
    response_model=JobAcceptedResponse,
    dependencies=[Depends(require_engineer)],
)
def rollback_mqtt_config_publish(
    run_id: str,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    """Republish the previously-captured config value to roll back a publish.

    Re-publishes ``result_summary['previous_config']['payload']`` to the same
    config topic. Guarded by the SAME authorization + publish-confirmation gate
    as the forward publish (a rollback is itself a live write). If no prior
    value was captured the route returns 400 — there is nothing to roll back to.

    HONESTY: capturing the prior retained value requires a reachable broker, so
    in this environment the captured value is whatever the original publish
    recorded (often the request-supplied ``previous_config`` or none). The
    live-broker capture/replay path is on-site-validation surface.
    """
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type != "mqtt_config_publish":
        raise HTTPException(status_code=404, detail=f"MQTT config publish run '{run_id}' was not found.")

    previous = run.result_summary.get("previous_config")
    if not isinstance(previous, dict) or previous.get("payload") in (None, ""):
        raise HTTPException(
            status_code=400,
            detail=(
                "No previous config value was captured for this run, so there is "
                "nothing to roll back to. Rollback requires a recorded "
                "result_summary.previous_config.payload."
            ),
        )

    _require_publish_authorization(dict(run.parameters))

    rollback_parameters = {
        **dict(run.parameters),
        "rollback_of_run_id": run.run_id,
        "rollback_previous_config": previous,
    }
    rollback_run = _create_run(
        JobCreateRequest(
            project_id=run.project_id,
            site_id=run.site_id,
            job_type="mqtt_config_publish",
            parameters=rollback_parameters,
        ),
        "mqtt_config_publish",
        principal,
    )

    def run_inline(run_store, frozen_parameters: dict) -> object:
        frozen_previous = frozen_parameters.get("rollback_previous_config")
        return process_mqtt_config_rollback_run(
            rollback_run.run_id,
            frozen_parameters,
            previous_config=(frozen_previous if isinstance(frozen_previous, dict) else previous),
            run_store=run_store,
            execution_mode="inline_local_fallback",
        )

    return _dispatch(
        rollback_run,
        enqueue=None,
        run_inline=run_inline,
        label="MQTT config rollback",
    )


@router.post("/bacnet/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_bacnet_validation_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    run = _create_run(request, "bacnet_validation", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_bacnet_validation_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            import_loader=_import_loader(),
            discovery_loader=_discovery_loader(),
            is_cancelled=make_cancel_checker(run_store, run.run_id),
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_for("validate_bacnet_points", "validation"),
        run_inline=run_inline,
        label="BACnet validation",
    )


@router.post("/mapping/runs", response_model=JobAcceptedResponse, dependencies=[Depends(require_engineer)])
def create_mapping_validation_run(
    request: JobCreateRequest,
    principal: AuthPrincipal = Depends(get_principal),
) -> JobAcceptedResponse:
    run = _create_run(request, "mapping_validation", principal)

    def run_inline(run_store, frozen_parameters: dict) -> object:
        return process_mapping_validation_run(
            run.run_id,
            frozen_parameters,
            run_store=run_store,
            execution_mode="inline_local_fallback",
            import_loader=_import_loader(),
            discovery_loader=_discovery_loader(),
            is_cancelled=make_cancel_checker(run_store, run.run_id),
        )

    return _dispatch(
        run,
        enqueue=queue_service.enqueue_for("compare_bacnet_mqtt", "validation"),
        run_inline=run_inline,
        label="BACnet to MQTT mapping validation",
    )


@router.get("/runs", response_model=RunListResponse, dependencies=[Depends(require_viewer)])
def list_validation_runs() -> RunListResponse:
    return RunListResponse(runs=service.list_runs(job_types=VALIDATION_JOB_TYPES))


@router.get("/runs/{run_id}", response_model=RunRecord, dependencies=[Depends(require_viewer)])
def get_validation_run(run_id: str) -> RunRecord:
    return _load_validation_run(run_id)


@router.get("/runs/{run_id}/export.json", dependencies=[Depends(require_viewer)])
def export_udmi_validation_run(run_id: str) -> Response:
    """Download one stored UDMI validation snapshot as versioned JSON evidence."""
    run = _load_validation_run(run_id)
    if run.job_type != "udmi_validation":
        raise HTTPException(status_code=404, detail=f"UDMI validation run '{run_id}' was not found.")
    if run.status not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Raw validation JSON is available after the run reaches a terminal status.",
        )
    return Response(
        content=stable_validation_export_bytes(run),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{validation_export_filename(run)}"'
        },
    )


@router.get(
    "/runs/{run_id}/issues",
    response_model=ValidationIssuesResponse,
    dependencies=[Depends(require_viewer)],
)
def get_validation_issues(run_id: str) -> ValidationIssuesResponse:
    run = _load_validation_run(run_id)

    issues = run.issues
    if not issues:
        raw_issues = run.result_summary.get("issues", [])
        issues = raw_issues if isinstance(raw_issues, list) else []
    return ValidationIssuesResponse(
        run_id=run.run_id,
        job_type=run.job_type,
        status=run.status,
        issues=[ValidationIssueRecord.model_validate(issue) for issue in issues],
    )


def _load_validation_run(run_id: str) -> RunRecord:
    try:
        run = service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found.") from error
    if run.job_type not in VALIDATION_JOB_TYPES:
        raise HTTPException(status_code=404, detail=f"Validation run '{run_id}' was not found.")
    return run


# Actionable authorization message for live MQTT writes (publish + rollback).
_PUBLISH_AUTH_DETAIL = (
    "Live MQTT config publish requires authorization. Provide parameters.authorized = true, "
    "or parameters.scan_authorization = {\"authorized\": true, \"authorized_by\": \"<who>\"}. "
    "A validate-only run (use_live_broker not set) needs no authorization."
)


def _require_publish_authorization(parameters: dict) -> None:
    """Reject a LIVE publish that lacks the authorization contract.

    Only enforced when the run actually targets a live broker
    (``use_live_broker`` true or a ``broker_host`` present); the validate-only
    path is side-effect free.
    """
    from smart_commissioning_core.engines.safety import is_authorized
    from smart_commissioning_core.mqtt_settings import parse_bool

    targets_live_broker = parse_bool(parameters.get("use_live_broker")) or bool(
        str(parameters.get("broker_host") or "").strip()
    )
    if not targets_live_broker:
        return
    if not is_authorized(parameters):
        raise HTTPException(status_code=403, detail=_PUBLISH_AUTH_DETAIL)
