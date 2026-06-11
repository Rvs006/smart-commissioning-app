from pydantic import BaseModel, Field


class ConfigurationSection(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)
    status: str = "Not Checked"


class ConfigurationSnapshot(BaseModel):
    device: ConfigurationSection
    bacnet: ConfigurationSection
    mqtt: ConfigurationSection
    certificates: ConfigurationSection
    time: ConfigurationSection
    backups: ConfigurationSection
    logging: ConfigurationSection


class ConfigurationValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class SecretMaterialRequest(BaseModel):
    section: str = "certificates"
    field: str
    content: str
    file_name: str | None = None


class SecretMaterialResponse(BaseModel):
    secret_ref: str
    field: str
    file_name: str | None = None
    fingerprint: str
    validity: str
    expiry: str | None = None
    masked: bool = True
