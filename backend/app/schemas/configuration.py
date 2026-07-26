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


class ConfigurationSecretMaterial(BaseModel):
    """Legacy import material accepted for one compatibility release.

    v0.1.26 never emits this content in an API response. An older export may
    still be imported, and the receiving machine immediately re-encrypts it.
    """

    secret_ref: str
    content: str
    file_name: str | None = None


class ConfigurationExportEnvelope(BaseModel):
    """A compatibility export envelope containing no secret values."""

    kind: str = "smart-commissioning-configuration"
    version: int = 2
    exported_at: str
    project_id: str
    site_id: str
    secrets_included: bool = False
    configuration: ConfigurationSnapshot
    secret_material: dict[str, ConfigurationSecretMaterial] = Field(default_factory=dict)


class ConfigurationImportRequest(BaseModel):
    """Import configuration and, for legacy v2 envelopes, restore secret material."""

    configuration: ConfigurationSnapshot
    secret_material: dict[str, ConfigurationSecretMaterial] = Field(default_factory=dict)
