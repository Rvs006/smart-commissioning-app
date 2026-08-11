"""Canonical verification for lifecycle-v2 sealed run evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from smart_commissioning_core.discovery_observations import ObservationEvidenceV1
from smart_commissioning_core.records import ValidationIssueRecord
from smart_commissioning_core.run_context import (
    RunContextV1,
    canonical_context_sha256,
    canonical_sha256,
)
from smart_commissioning_core.run_lifecycle import RunSealViewV1, TerminalResultV1


class SealedRunIntegrityError(RuntimeError):
    """A lifecycle-v2 run no longer agrees with its immutable seal."""

    def __init__(self, component: str, detail: str) -> None:
        super().__init__(detail)
        self.component = component


@dataclass(frozen=True, slots=True)
class VerifiedSealedRun:
    """Canonical context, terminal result, and seal proven to agree."""

    context: RunContextV1
    terminal_result: TerminalResultV1
    seal: RunSealViewV1


@dataclass(frozen=True, slots=True)
class VerifiedReportEvidence:
    """Canonical frozen report snapshot and terminal seal proven to agree."""

    snapshot: Mapping[str, Any]
    terminal_result: TerminalResultV1
    seal: RunSealViewV1


@dataclass(frozen=True, slots=True)
class VerifiedLegacyReportEvidence:
    """An f6-classified historical report and its synthetic terminal seal."""

    terminal_result: TerminalResultV1
    seal: RunSealViewV1
    project_id: str
    site_id: str
    parameters: Mapping[str, Any]


def verify_sealed_run(
    *,
    run_id: str,
    run: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    seal: Mapping[str, Any] | None,
    issues: Sequence[Mapping[str, Any]] = (),
    devices: Sequence[Mapping[str, Any]] = (),
    points: Sequence[Mapping[str, Any]] = (),
    topics: Sequence[Mapping[str, Any]] = (),
) -> VerifiedSealedRun | None:
    """Verify one run's sealed lifecycle snapshot.

    A row with no lifecycle-v2 context, result, or seal remains an explicit
    legacy record and returns ``None``. Any partial lifecycle is rejected.
    Database-only identifiers, positions, and timestamps are removed before
    persisted projections are compared with the canonical result payload.
    """

    lifecycle_rows = (context, result, seal)
    if all(row is None for row in lifecycle_rows):
        modern_markers = (
            run.get("result_sha256"),
            run.get("terminal_at"),
            run.get("owner_token"),
            run.get("claimed_at"),
            run.get("heartbeat_at"),
            run.get("lease_expires_at"),
            run.get("attempt") or 0,
            run.get("state_version") or 0,
            bool(run.get("modern_lifecycle_component_present")),
        )
        if any(modern_markers):
            raise SealedRunIntegrityError(
                "lifecycle",
                "modern lifecycle markers exist without the sealed lifecycle tuple",
            )
        return None
    if any(row is None for row in lifecycle_rows):
        raise SealedRunIntegrityError(
            "lifecycle",
            "modern lifecycle metadata is only partially present",
        )

    assert context is not None
    assert result is not None
    assert seal is not None
    try:
        canonical_context = RunContextV1.model_validate(context.get("context_json"))
    except (TypeError, ValidationError) as error:
        raise SealedRunIntegrityError(
            "context",
            "stored execution context is malformed",
        ) from error
    stored_context_sha256 = context.get("context_sha256")
    if canonical_context_sha256(canonical_context) != stored_context_sha256:
        raise SealedRunIntegrityError(
            "context",
            "stored execution context digest is not canonical",
        )
    if seal.get("context_sha256") != stored_context_sha256:
        raise SealedRunIntegrityError(
            "context",
            "stored execution context digest is not bound to the run seal",
        )

    terminal, seal_view = _verify_terminal_evidence(
        run_id=run_id,
        run=run,
        result=result,
        seal=seal,
        issues=issues,
        devices=devices,
        points=points,
        topics=topics,
    )
    return VerifiedSealedRun(
        context=canonical_context,
        terminal_result=terminal,
        seal=seal_view,
    )


def verify_report_evidence_run(
    *,
    run_id: str,
    run: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    seal: Mapping[str, Any] | None,
    issues: Sequence[Mapping[str, Any]] = (),
    devices: Sequence[Mapping[str, Any]] = (),
    points: Sequence[Mapping[str, Any]] = (),
    topics: Sequence[Mapping[str, Any]] = (),
) -> VerifiedReportEvidence:
    """Verify the intentionally distinct report-snapshot seal contract."""

    if run.get("job_type") != "report_generation":
        raise SealedRunIntegrityError(
            "lifecycle",
            "report evidence verification requires authoritative report_generation type",
        )
    if context is not None:
        raise SealedRunIntegrityError(
            "lifecycle",
            "report evidence must not carry a lifecycle execution context",
        )
    if result is None or seal is None:
        raise SealedRunIntegrityError(
            "lifecycle",
            "modern report evidence is only partially present",
        )
    parameters = run.get("parameters")
    if not isinstance(parameters, Mapping):
        raise SealedRunIntegrityError("context", "report parameters are malformed")
    snapshot = parameters.get("report_snapshot_v2")
    expected_snapshot_sha256 = parameters.get("report_snapshot_sha256")
    if not isinstance(snapshot, Mapping) or not isinstance(expected_snapshot_sha256, str):
        raise SealedRunIntegrityError(
            "context",
            "report evidence has no complete frozen snapshot",
        )
    canonical_snapshot_sha256 = canonical_sha256(snapshot)
    if canonical_snapshot_sha256 != expected_snapshot_sha256:
        raise SealedRunIntegrityError(
            "context",
            "frozen report snapshot digest is not canonical",
        )
    if seal.get("context_sha256") != canonical_snapshot_sha256:
        raise SealedRunIntegrityError(
            "context",
            "frozen report snapshot digest is not bound to the run seal",
        )
    terminal, seal_view = _verify_terminal_evidence(
        run_id=run_id,
        run=run,
        result=result,
        seal=seal,
        issues=issues,
        devices=devices,
        points=points,
        topics=topics,
    )
    _verify_report_snapshot_bindings(
        run_id=run_id,
        run=run,
        snapshot=snapshot,
        snapshot_sha256=canonical_snapshot_sha256,
        terminal=terminal,
    )
    return VerifiedReportEvidence(
        snapshot=dict(snapshot),
        terminal_result=terminal,
        seal=seal_view,
    )


def verify_legacy_report_evidence_run(
    *,
    run_id: str,
    run: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    seal: Mapping[str, Any] | None,
    issues: Sequence[Mapping[str, Any]] = (),
    devices: Sequence[Mapping[str, Any]] = (),
    points: Sequence[Mapping[str, Any]] = (),
    topics: Sequence[Mapping[str, Any]] = (),
) -> VerifiedLegacyReportEvidence:
    """Verify the exact synthetic lifecycle shape produced by the f6 migration."""

    if run.get("job_type") != "report_generation":
        raise SealedRunIntegrityError("lifecycle", "legacy report contract requires report_generation type")
    parameters = run.get("parameters")
    summary = run.get("result_summary")
    if not isinstance(parameters, Mapping) or not isinstance(summary, Mapping):
        raise SealedRunIntegrityError("context", "legacy report projections are malformed")
    marker = summary.get("legacy_report_integrity")
    if not (
        isinstance(marker, Mapping)
        and marker.get("migration") == "v0.1.26"
        and marker.get("silently_resigned") is False
    ):
        raise SealedRunIntegrityError("lifecycle", "legacy report provenance marker is missing")
    modern_parameter_keys = {
        "report_snapshot_v2",
        "report_snapshot_sha256",
        "source_run_snapshots",
        "source_run_seals",
        "source_discovery_snapshots",
        "evidence_set_id",
        "udmi_scope",
        "udmi_report_snapshot",
        "source_raw_evidence",
    }
    if (
        context is not None
        or "artifact_manifest" in summary
        or bool(modern_parameter_keys.intersection(parameters))
        or bool(run.get("modern_lifecycle_component_present"))
        or bool(run.get("sync_artifact_present"))
    ):
        raise SealedRunIntegrityError(
            "lifecycle",
            "legacy report contains a modern evidence component",
        )
    if result is None or seal is None:
        raise SealedRunIntegrityError("lifecycle", "legacy report synthetic lifecycle tuple is incomplete")

    terminal, seal_view = _verify_terminal_evidence(
        run_id=run_id,
        run=run,
        result=result,
        seal=seal,
        issues=issues,
        devices=devices,
        points=points,
        topics=topics,
    )
    legacy_context = {
        "schema_version": "legacy-0",
        "run_id": run_id,
        "project_id": run.get("project_id"),
        "site_id": run.get("site_id"),
        "job_type": run.get("job_type"),
        "parameters": dict(parameters),
        "execution_mode": run.get("execution_mode"),
    }
    if seal.get("context_sha256") != canonical_sha256(legacy_context):
        raise SealedRunIntegrityError(
            "context",
            "legacy report parameters and scope do not match the synthetic seal",
        )
    project_id = run.get("project_id")
    site_id = run.get("site_id")
    if not isinstance(project_id, str) or not isinstance(site_id, str):
        raise SealedRunIntegrityError("context", "legacy report scope is malformed")
    return VerifiedLegacyReportEvidence(
        terminal_result=terminal,
        seal=seal_view,
        project_id=project_id,
        site_id=site_id,
        parameters=dict(parameters),
    )


_REPORT_MEDIA_TYPES = {
    "pdf": (".pdf", "application/pdf"),
    "xlsx": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "docx": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "zip": (".zip", "application/zip"),
}


def _verify_report_snapshot_bindings(
    *,
    run_id: str,
    run: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    snapshot_sha256: str,
    terminal: TerminalResultV1,
) -> None:
    parameters = run["parameters"]
    assert isinstance(parameters, Mapping)
    if snapshot.get("schema_version") != "2.0":
        raise SealedRunIntegrityError("context", "frozen report snapshot schema is unsupported")

    direct_bindings = {
        "project_id": run.get("project_id"),
        "site_id": run.get("site_id"),
        "output_format": parameters.get("output_format"),
        "report_type": parameters.get("report_type"),
        "source_run_ids": parameters.get("source_run_ids"),
        "renderer_version": parameters.get("renderer_version"),
    }
    required_parameter_keys = {
        "output_format",
        "report_type",
        "source_run_ids",
        "report_title_custom",
        "report_title",
        "report_generated_at",
        "renderer_version",
    }
    if not required_parameter_keys.issubset(parameters):
        raise SealedRunIntegrityError("context", "report parameters omit sealed snapshot metadata")
    for field, expected in direct_bindings.items():
        if snapshot.get(field) != expected:
            raise SealedRunIntegrityError(
                "context",
                f"report {field} does not match the frozen snapshot",
            )

    metadata = snapshot.get("report_metadata")
    if not isinstance(metadata, Mapping):
        raise SealedRunIntegrityError("context", "frozen report metadata is malformed")
    metadata_fields = (
        "output_format",
        "report_type",
        "report_title_custom",
        "report_title",
        "report_generated_at",
        "renderer_version",
    )
    for field in metadata_fields:
        if metadata.get(field) != parameters.get(field):
            raise SealedRunIntegrityError(
                "context",
                f"report {field} metadata does not match its parameter projection",
            )

    optional_metadata_fields = ("evidence_set_id", "udmi_report_variant")
    for field in optional_metadata_fields:
        present_in_snapshot = field in metadata or (
            field == "evidence_set_id" and field in snapshot
        )
        if field in parameters or present_in_snapshot:
            if field not in parameters or field not in metadata:
                raise SealedRunIntegrityError(
                    "context",
                    f"report {field} metadata projection is incomplete",
                )
            if metadata.get(field) != parameters.get(field):
                raise SealedRunIntegrityError(
                    "context",
                    f"report {field} metadata does not match its parameter projection",
                )
            if field == "evidence_set_id" and snapshot.get(field) != parameters.get(field):
                raise SealedRunIntegrityError(
                    "context",
                    "report evidence_set_id does not match the frozen snapshot",
                )

    required_snapshot_carried_fields = (
        "source_run_snapshots",
        "source_run_seals",
        "source_discovery_snapshots",
    )
    for field in required_snapshot_carried_fields:
        if (field in parameters or field in snapshot) and (
            field not in parameters
            or field not in snapshot
            or snapshot.get(field) != parameters.get(field)
        ):
            raise SealedRunIntegrityError(
                "context",
                f"report {field} does not match the frozen snapshot",
            )
    for field in ("udmi_scope", "udmi_report_snapshot"):
        parameter_value = parameters.get(field)
        snapshot_value = snapshot.get(field)
        if (
            parameter_value is not None
            or snapshot_value is not None
            or field in parameters
        ) and parameter_value != snapshot_value:
            raise SealedRunIntegrityError(
                "context",
                f"report {field} does not match the frozen snapshot",
            )

    manifest = terminal.summary.get("artifact_manifest")
    if not isinstance(manifest, Mapping):
        raise SealedRunIntegrityError("result", "sealed report has no artifact manifest")
    if manifest.get("snapshot_sha256") != snapshot_sha256:
        raise SealedRunIntegrityError(
            "context",
            "sealed artifact manifest is not bound to the frozen snapshot",
        )
    if manifest.get("report_id") != run_id:
        raise SealedRunIntegrityError("result", "sealed artifact manifest belongs to another report")
    if manifest.get("renderer_version") != parameters.get("renderer_version"):
        raise SealedRunIntegrityError("result", "sealed artifact renderer does not match report parameters")
    if "evidence_set_id" in parameters and manifest.get("evidence_set_id") != parameters.get("evidence_set_id"):
        raise SealedRunIntegrityError("result", "sealed artifact evidence set does not match report parameters")
    if run.get("sync_artifact_present"):
        synchronized_manifest = run.get("sync_artifact_manifest")
        if not isinstance(synchronized_manifest, Mapping) or dict(synchronized_manifest) != dict(manifest):
            raise SealedRunIntegrityError(
                "result",
                "synchronized artifact manifest does not match the sealed report manifest",
            )

    output_format = parameters.get("output_format")
    expected_output = _REPORT_MEDIA_TYPES.get(output_format)
    if expected_output is None:
        raise SealedRunIntegrityError("context", "report output format is unsupported")
    suffix, media_type = expected_output
    file_name = manifest.get("file_name")
    if not isinstance(file_name, str) or not file_name.casefold().endswith(suffix):
        raise SealedRunIntegrityError("result", "sealed artifact file name does not match its output format")
    if manifest.get("media_type") != media_type:
        raise SealedRunIntegrityError("result", "sealed artifact media type does not match its output format")
    byte_size = manifest.get("byte_size")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size <= 0:
        raise SealedRunIntegrityError("result", "sealed artifact byte size is invalid")
    artifact_sha256 = manifest.get("artifact_sha256")
    if not (
        isinstance(artifact_sha256, str)
        and len(artifact_sha256) == 64
        and all(character in "0123456789abcdef" for character in artifact_sha256)
    ):
        raise SealedRunIntegrityError("result", "sealed artifact digest is invalid")


def _verify_terminal_evidence(
    *,
    run_id: str,
    run: Mapping[str, Any],
    result: Mapping[str, Any],
    seal: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    devices: Sequence[Mapping[str, Any]],
    points: Sequence[Mapping[str, Any]],
    topics: Sequence[Mapping[str, Any]],
) -> tuple[TerminalResultV1, RunSealViewV1]:
    try:
        terminal = TerminalResultV1.model_validate(result.get("result_payload"))
    except (TypeError, ValidationError) as error:
        raise SealedRunIntegrityError(
            "result",
            "stored terminal result payload is malformed",
        ) from error

    canonical_result_sha256 = terminal.sha256()
    claimed_result_digests = {
        run.get("result_sha256"),
        result.get("result_sha256"),
        seal.get("result_sha256"),
    }
    if claimed_result_digests != {canonical_result_sha256}:
        raise SealedRunIntegrityError(
            "result",
            "terminal result canonical digest does not match every persisted binding",
        )

    metadata_is_coherent = (
        run.get("status") == terminal.status
        and run.get("stage") == terminal.stage
        and run.get("error_message") == terminal.error_message
        and result.get("schema_version") == terminal.schema_version
        and result.get("terminal_status") == terminal.status
        and result.get("terminal_stage") == terminal.stage
        and seal.get("terminal_status") == terminal.status
    )
    if not metadata_is_coherent:
        raise SealedRunIntegrityError(
            "result",
            "persisted terminal metadata does not match the sealed result payload",
        )

    if result.get("summary") != terminal.summary or run.get("result_summary") != terminal.summary:
        raise SealedRunIntegrityError(
            "result",
            "persisted summary projections do not match the sealed result payload",
        )

    raw_observation_evidence = terminal.summary.get("observation_evidence_v1")
    if raw_observation_evidence is not None:
        try:
            observation_evidence = ObservationEvidenceV1.model_validate(
                raw_observation_evidence
            )
        except (TypeError, ValidationError) as error:
            raise SealedRunIntegrityError(
                "result",
                "terminal discovery observation evidence is malformed",
            ) from error
        if run.get("attempt") != observation_evidence.attempt:
            raise SealedRunIntegrityError(
                "result",
                "terminal discovery observation attempt does not match the run",
            )
        if (observation_evidence.observation_count == 0) != (
            observation_evidence.terminal_cursor == 0
        ):
            raise SealedRunIntegrityError(
                "result",
                "terminal discovery observation count and cursor are inconsistent",
            )

    projection_pairs = (
        ("issues", _normalize_issues(terminal.issues), _normalize_issues(issues)),
        ("devices", _normalize_devices(terminal.devices), _normalize_devices(devices)),
        ("points", _normalize_points(terminal.points), _normalize_points(points)),
        ("topics", _normalize_topics(terminal.topics), _normalize_topics(topics)),
    )
    for label, sealed_projection, persisted_projection in projection_pairs:
        if persisted_projection != sealed_projection:
            raise SealedRunIntegrityError(
                "result",
                f"persisted {label} projection does not match the sealed result payload",
            )

    try:
        sealed_at = seal.get("sealed_at")
        if isinstance(sealed_at, str):
            sealed_at = datetime.fromisoformat(sealed_at)
        seal_view = RunSealViewV1.model_validate(
            {
                "run_id": run_id,
                "terminal_status": seal.get("terminal_status"),
                "context_sha256": seal.get("context_sha256"),
                "result_sha256": seal.get("result_sha256"),
                "sealed_at": sealed_at,
            }
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise SealedRunIntegrityError("seal", "stored run seal is malformed") from error
    return terminal, seal_view


def _normalize_issues(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    try:
        for row in rows:
            record = ValidationIssueRecord.model_validate(row)
            if record.last_seen_at is not None and record.last_seen_at.tzinfo is None:
                record = record.model_copy(update={"last_seen_at": record.last_seen_at.replace(tzinfo=UTC)})
            normalized.append(record.model_dump(mode="json"))
    except (TypeError, ValueError, ValidationError) as error:
        raise SealedRunIntegrityError("result", "issues projection is malformed") from error
    return normalized


def _normalize_devices(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    columns = ("project_id", "site_id", "address", "device_type", "name", "vendor", "model")
    return [
        {
            **{column: row.get(column) for column in columns},
            "attributes": _mapping_value(row.get("attributes"), label="devices attributes"),
        }
        for row in _mapping_rows(rows, label="devices")
    ]


def _normalize_points(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    columns = ("device_ref", "point_id", "point_name", "units")
    return [
        {
            **{column: row.get(column) for column in columns},
            "observed_value": _mapping_value(
                row.get("observed_value"),
                label="points observed_value",
            ),
            "attributes": _mapping_value(row.get("attributes"), label="points attributes"),
        }
        for row in _mapping_rows(rows, label="points")
    ]


def _normalize_topics(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in _mapping_rows(rows, label="topics"):
        try:
            message_count = int(row.get("message_count") or 0)
        except (TypeError, ValueError, OverflowError) as error:
            raise SealedRunIntegrityError(
                "result",
                "topics message_count is malformed",
            ) from error
        normalized.append(
            {
                "topic": str(row.get("topic") or ""),
                "last_payload": _mapping_value(
                    row.get("last_payload"),
                    label="topics last_payload",
                ),
                "message_count": message_count,
                "attributes": _mapping_value(
                    row.get("attributes"),
                    label="topics attributes",
                ),
            }
        )
    return normalized


def _mapping_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> list[Mapping[str, Any]]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise SealedRunIntegrityError("result", f"{label} projection is malformed")
    normalized = list(rows)
    if any(not isinstance(row, Mapping) for row in normalized):
        raise SealedRunIntegrityError("result", f"{label} projection is malformed")
    return normalized


def _mapping_value(value: object, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SealedRunIntegrityError("result", f"{label} is malformed")
    return dict(value)
