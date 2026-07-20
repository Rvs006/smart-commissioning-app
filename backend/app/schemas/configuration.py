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
    """One cert field's importable material: its ref plus the PLAIN-TEXT PEM.

    Carried in the with-secrets export so another engineer can import a working
    configuration on their own machine (field decision 2026-07-20). The receiving
    machine re-encrypts ``content`` into its own secret store under the same
    ``secret_ref``.
    """

    secret_ref: str
    content: str
    file_name: str | None = None


class ConfigurationExportEnvelope(BaseModel):
    """A shareable configuration export INCLUDING its secrets (engineer action).

    The ``configuration`` snapshot carries password-kind values (MQTT password,
    tokens, key password) in PLAIN TEXT, and ``secret_material`` carries the
    CA/client-certificate/private-key PEM material, keyed by field name. This is a
    deliberate, engineer-gated departure from the default masked export.
    """

    kind: str = "smart-commissioning-configuration"
    version: int = 2
    exported_at: str
    project_id: str
    site_id: str
    secrets_included: bool = True
    configuration: ConfigurationSnapshot
    secret_material: dict[str, ConfigurationSecretMaterial] = Field(default_factory=dict)


class ConfigurationImportRequest(BaseModel):
    """Import a configuration, optionally restoring exported secret material."""

    configuration: ConfigurationSnapshot
    secret_material: dict[str, ConfigurationSecretMaterial] = Field(default_factory=dict)
