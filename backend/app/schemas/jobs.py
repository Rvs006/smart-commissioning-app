from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

# ValidationIssueRecord moved to the shared core package; imported here so existing
# `from app.schemas.jobs import ValidationIssueRecord` consumers keep working.
from smart_commissioning_core.records import ValidationIssueRecord  # noqa: F401

_SECRET_SENTINEL = "********"
# Parameter key substrings whose values are broker/cert secrets. Redacted only
# when a RunRecord is SERIALIZED to an API client (model_dump/JSON response);
# attribute access (run.parameters, used server-side to execute a run and to
# re-publish on rollback) is untouched, so live runs and rollback still receive
# the real values. This closes the hole where a broker password / inline private
# key passed as a run parameter was echoed to any viewer via GET .../runs/{id}.
_SENSITIVE_PARAM_SUBSTRINGS = ("password", "private_key", "client_certificate", "ca_certificate")
# UDMI runs embed every uploaded nonpub schema set into their parameters (so the
# Dramatiq worker validates from the shared database alone). Serializing that
# content back out would re-serve every schema body to any viewer on every
# 1.5s status poll, so API responses carry a filenames-only summary instead.
_NONPUB_SCHEMA_SETS_KEY = "nonpub_schema_sets"


def _nonpub_schema_sets_summary(value: object) -> object:
    """``{label: sorted(filenames)}`` view of the embedded nonpub schema sets."""
    if not isinstance(value, dict):
        return value
    return {
        str(label): sorted(str(name) for name in files) if isinstance(files, dict) else files
        for label, files in value.items()
    }


def redact_sensitive_parameters(parameters: dict[str, object]) -> dict[str, object]:
    """Replace broker/cert secret values with a sentinel for API serialization."""
    redacted: dict[str, object] = {}
    for key, value in parameters.items():
        lowered = key.casefold()
        if key == _NONPUB_SCHEMA_SETS_KEY:
            redacted[key] = _nonpub_schema_sets_summary(value)
        elif any(token in lowered for token in _SENSITIVE_PARAM_SUBSTRINGS) and value not in (None, ""):
            redacted[key] = _SECRET_SENTINEL
        elif isinstance(value, str) and "-----BEGIN" in value:
            redacted[key] = _SECRET_SENTINEL  # inline PEM material
        else:
            redacted[key] = value
    return redacted


JobType = Literal[
    "ip_discovery",
    "bacnet_discovery",
    "mqtt_discovery",
    "udmi_validation",
    "mqtt_config_publish",
    "bacnet_validation",
    "mapping_validation",
    "report_generation",
]

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ReportFormat = Literal["zip", "xlsx", "docx", "pdf"]


class JobCreateRequest(BaseModel):
    project_id: str
    site_id: str
    job_type: JobType
    parameters: dict[str, object] = Field(default_factory=dict)


class JobAcceptedResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    message: str


class JobSummary(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    stage: str
    progress_percent: int
    created_at: datetime
    updated_at: datetime
    # Originating edge id (run attribution for the multi-project hub). NULL/None
    # for a run created on this local edge; populated when the run was ingested
    # from another edge's signed bundle. Additive: defaults to None so the field
    # is absent-as-null for local runs and existing callers that don't set it.
    edge_id: str | None = None


class ObservedPort(BaseModel):
    port: int
    protocol: Literal["tcp", "udp"]
    service: str | None = None


class DiscoveryAssetObservation(BaseModel):
    asset_id: str | None = None
    ip_address: str | None = None
    mac_address: str | None = None
    hostname: str | None = None
    observed_ports: list[ObservedPort] = Field(default_factory=list)
    # Engine-defined provenance for the observation. IP discovery uses
    # "mac"/"ip"/"hostname"/"none"; BACnet uses "bacnet_who_is"; MQTT may use
    # other labels. Kept as a free string so new engines are not blocked by a
    # rigid enum, while existing IP consumers keep their values unchanged.
    match_basis: str = "none"
    last_seen_at: datetime | None = None
    status_detail: str | None = None
    # Engines carry extra per-asset fields (BACnet device_instance/vendor,
    # point_count, ...) that consumers may use; allow them through unmodelled.
    model_config = ConfigDict(extra="allow")


class RunRecord(JobSummary):
    project_id: str
    site_id: str
    parameters: dict[str, object] = Field(default_factory=dict)
    result_summary: dict[str, object] = Field(default_factory=dict)
    issues: list[ValidationIssueRecord] = Field(default_factory=list)
    error_message: str | None = None

    @field_serializer("parameters")
    def _redact_parameters(self, parameters: dict[str, object]) -> dict[str, object]:
        # Runs on JSON/model_dump serialization (the API response) only — not on
        # attribute access — so broker/cert secrets never reach an API client
        # while server-side execution/rollback still read the real values.
        return redact_sensitive_parameters(parameters)


class RunListResponse(BaseModel):
    runs: list[JobSummary] = Field(default_factory=list)


class DiscoveryResultsResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    result_summary: dict[str, object] = Field(default_factory=dict)
    # Back-compat view derived from result_summary["discovered_assets"].
    discovered_assets: list[DiscoveryAssetObservation] = Field(default_factory=list)
    # Structured rows persisted via DiscoveryRepository (devices/points/topics).
    # Kept as plain dicts so per-engine attributes survive without a rigid model.
    devices: list[dict[str, object]] = Field(default_factory=list)
    points: list[dict[str, object]] = Field(default_factory=list)
    topics: list[dict[str, object]] = Field(default_factory=list)


class DiscoveryPointsResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    points: list[dict[str, object]] = Field(default_factory=list)


class DiscoveryTopicsResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    topics: list[dict[str, object]] = Field(default_factory=list)


class ValidationIssuesResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    issues: list[ValidationIssueRecord] = Field(default_factory=list)


class ImportBatchResponse(BaseModel):
    import_id: str
    import_type: str
    accepted_rows: int
    rejected_rows: int
    status: Literal["accepted", "rejected", "partial"]


class ReportRequest(BaseModel):
    project_id: str
    site_id: str
    report_type: Literal[
        "ip_discovery",
        "bacnet_discovery",
        "mqtt_discovery",
        "udmi_validation",
        "data_validation",
        "issue_report",
        "evidence_pack",
    ]
    output_format: ReportFormat = "zip"
    source_run_ids: list[str] = Field(default_factory=list)


class ReportSummary(BaseModel):
    report_id: str
    report_type: str
    output_format: ReportFormat
    status: JobStatus
    file_name: str
    # When the report run was created, and which runs it was scoped to. Both are
    # read straight off the stored run record (Run.created_at is non-null with a
    # utcnow default; source_run_ids is persisted in parameters at creation), so
    # this is a projection change, not a migration. created_at is deliberately
    # REQUIRED: every report run has one, and an Optional field would silently
    # mask a construction site that forgot to pass it.
    created_at: datetime
    source_run_ids: list[str] = Field(default_factory=list)


class ReportListResponse(BaseModel):
    reports: list[ReportSummary] = Field(default_factory=list)
