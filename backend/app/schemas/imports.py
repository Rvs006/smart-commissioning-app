from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ImportType = Literal[
    "ip_register",
    "bacnet_register",
    "mqtt_register",
    "asset_validation",
    "bacnet_points",
    "mqtt_points",
    "mapping",
    "tolerances",
]

ImportStatus = Literal["accepted", "rejected", "partial"]


class ImportProfileSummary(BaseModel):
    import_type: ImportType
    description: str
    required_columns: list[str]
    duplicate_key_fields: list[str]


class ImportErrorRecord(BaseModel):
    row_number: int | None = None
    field: str | None = None
    code: str
    message: str


class ImportBatchSummary(BaseModel):
    import_id: str
    import_type: ImportType
    file_name: str
    file_type: Literal["csv", "xlsx"]
    project_id: str | None = None
    site_id: str | None = None
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    status: ImportStatus
    missing_columns: list[str] = Field(default_factory=list)
    stored_file_name: str
    created_at: datetime


class ImportErrorReport(BaseModel):
    import_id: str
    errors: list[ImportErrorRecord] = Field(default_factory=list)

