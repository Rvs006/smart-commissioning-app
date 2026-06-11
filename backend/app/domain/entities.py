from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Asset:
    asset_id: str
    project_id: str
    site_id: str
    system_id: str
    asset_name: str
    source_protocol: str
    location: str | None = None


@dataclass(slots=True)
class ValidationRun:
    run_id: str
    project_id: str
    site_id: str
    run_type: str
    status: str
    created_at: datetime


@dataclass(slots=True)
class Issue:
    issue_id: str
    run_id: str
    asset_id: str
    source: str
    issue_type: str
    severity: str
    description: str
    created_at: datetime

