"""Response schema for the operator-uploaded non-published UDMI schema sets.

Summaries carry the label + filenames + upload metadata only — never the full
schema content (the stored files are consumed by the validator via the run
parameters, not read back over the API).
"""

from datetime import datetime

from pydantic import BaseModel


class UdmiSchemaSetSummary(BaseModel):
    version_label: str
    filenames: list[str]
    uploaded_at: datetime
    uploaded_by: str | None = None
