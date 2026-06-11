from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
ReportFormat = Literal["zip", "xlsx", "docx"]


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


class ValidationIssueRecord(BaseModel):
    issue_id: str
    asset_id: str | None = None
    issue_type: str
    severity: Literal["low", "medium", "high", "critical"]
    description: str
    status: str | None = None
    point_name: str | None = None
    topic: str | None = None
    expected_value: str | None = None
    observed_value: str | None = None
    match_basis: str | None = None
    suggested_action: str | None = None
    raw_evidence_uri: str | None = None
    status_detail: str | None = None
    last_seen_at: datetime | None = None


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
    match_basis: Literal["mac", "ip", "hostname", "none"] = "none"
    last_seen_at: datetime | None = None
    status_detail: str | None = None


class RunRecord(JobSummary):
    project_id: str
    site_id: str
    parameters: dict[str, object] = Field(default_factory=dict)
    result_summary: dict[str, object] = Field(default_factory=dict)
    issues: list[ValidationIssueRecord] = Field(default_factory=list)
    error_message: str | None = None


class RunListResponse(BaseModel):
    runs: list[JobSummary] = Field(default_factory=list)


class DiscoveryResultsResponse(BaseModel):
    run_id: str
    job_type: JobType
    status: JobStatus
    result_summary: dict[str, object] = Field(default_factory=dict)
    discovered_assets: list[DiscoveryAssetObservation] = Field(default_factory=list)


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


class ReportListResponse(BaseModel):
    reports: list[ReportSummary] = Field(default_factory=list)
