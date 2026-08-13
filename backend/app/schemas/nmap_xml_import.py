"""Path-free public result for one operator Nmap XML import attempt."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NmapXmlImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1, max_length=64)
    status: Literal["succeeded", "failed"]
    diagnostic_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_size_bytes: int = Field(ge=0, le=16 * 1024 * 1024 + 1)
    capture_complete: bool
    nmap_version: str | None = Field(default=None, max_length=32)
    xml_output_version: str | None = Field(default=None, max_length=32)
    host_count: int = Field(default=0, ge=0, le=4096)
    port_count: int = Field(default=0, ge=0, le=50_000)
