from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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
