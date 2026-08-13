"""Request schemas for sealed-preview active scan approvals."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class CreateScanAuthorizationRequest(BaseModel):
    preview_run_id: str = Field(min_length=1, max_length=64)
    ticket: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2_000)
    not_before: datetime
    not_after: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "CreateScanAuthorizationRequest":
        if self.not_before.tzinfo is None or self.not_after.tzinfo is None:
            raise ValueError("authorization window timestamps must include a timezone")
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        return self


class RevokeScanAuthorizationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)
