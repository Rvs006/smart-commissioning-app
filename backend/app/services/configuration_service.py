import ipaddress
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Literal

from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken
from smart_commissioning_core.db.repositories import ConfigurationRepository
from sqlalchemy.engine import Engine

from app.core.db import get_engine
from app.core.runtime import SECRETS_ROOT, ensure_runtime_directories
from app.schemas.configuration import (
    ConfigurationSection,
    ConfigurationSnapshot,
    ConfigurationValidationResult,
    SecretMaterialRequest,
    SecretMaterialResponse,
)

DEFAULT_PROJECT_ID = "demo-project"
DEFAULT_SITE_ID = "demo-site"

DEFAULT_CONFIGURATION = ConfigurationSnapshot(
    device=ConfigurationSection(
        values={
            "Hostname": "sct-gateway-01",
            "IP Assignment": "Static IP",
            "IP Address": "10.10.25.50",
            "Subnet Mask": "255.255.255.0",
            "Gateway": "10.10.25.1",
            "DNS Servers": "10.10.25.10, 8.8.8.8",
            "VLAN ID": "25",
        },
        status="Healthy",
    ),
    bacnet=ConfigurationSection(
        values={
            "BACnet Network Number": "1532",
            "UDP Port": "47808",
            "Device Instance Range": "1 - 4194303",
            "BBMD": "Enabled",
            "BBMD Address": "10.10.25.20",
            "BBMD UDP Port": "47808",
            "Foreign Device": "Disabled",
            "TTL": "300",
        },
        status="Listening",
    ),
    mqtt=ConfigurationSection(
        values={
            "MQTT Broker FQDN or IP Address": "mqtt.electracom.local",
            "Port": "8883",
            "Client ID": "sct-gateway-01",
            "Root Topic": "electracom/sct/1532",
            "QoS": "1 - At least once",
            "Keep Alive Interval": "60",
            "MQTT Username": "",
            "MQTT Password": "********",
        },
        status="Connected",
    ),
    certificates=ConfigurationSection(
        values={
            "CA Certificate": "secret://bootstrap-ca-certificate",
            "Client Certificate": "secret://bootstrap-client-certificate",
            "Private Key": "secret://bootstrap-private-key",
            "Key Password": "********",
            "Certificate Expiry": "2027-05-20",
        },
        status="Valid",
    ),
    time=ConfigurationSection(
        values={
            "Timezone": "Europe/London",
            "Primary NTP Server": "0.pool.ntp.org",
            "Secondary NTP Server": "1.pool.ntp.org",
            "NTP Update Interval": "64",
        },
        status="Synchronised",
    ),
    backups=ConfigurationSection(
        values={
            "Backup Schedule": "Daily 02:00",
            "Backup Retention": "30 days",
            "Encrypted Backups": "Enabled",
            "Backup Location": "/data/backups",
            "Last Backup Status": "Success",
            "Restore Action": "Available",
        },
        status="Success",
    ),
    logging=ConfigurationSection(
        values={
            "Log Level": "Info",
            "Log Retention": "30 days",
            "Remote Syslog Target": "10.10.25.60",
            "Syslog Port": "514",
            "Diagnostics Mode": "Disabled",
        },
        status="Healthy",
    ),
)

SUPPORTED_CERT_EXTENSIONS = {".pem", ".crt", ".cer", ".key", ".p12", ".pfx"}

SECRET_SENTINEL = "********"
_SECRET_STORE_KEY_FILE = ".secret_store_key"


def _is_secret_sentinel(value: str) -> bool:
    """True for the all-asterisk placeholder used for stored password values.

    Matches the frontend's isSecretSentinel (/^\\*+$/) so any asterisk run the
    form echoes back is treated as "keep the stored secret".
    """
    return bool(value) and set(value) == {"*"}


def _password_kind_fields() -> dict[str, tuple[str, ...]]:
    """Password-kind fields per section, derived from DEFAULT_CONFIGURATION.

    The defaults mark password-kind fields (the frontend renders them with
    kind="password") with an all-asterisk value, e.g. "MQTT Password" and
    "Key Password". secret:// references are NOT password-kind: they are
    already-opaque pointers at secret material stored on disk.
    """
    fields: dict[str, tuple[str, ...]] = {}
    for section_name in ConfigurationSnapshot.model_fields:
        section = getattr(DEFAULT_CONFIGURATION, section_name)
        marked = tuple(field for field, value in section.values.items() if _is_secret_sentinel(value))
        if marked:
            fields[section_name] = marked
    return fields


PASSWORD_KIND_FIELDS = _password_kind_fields()


def _write_private_file(path: Path, content: bytes) -> None:
    """Write bytes with owner-only (0o600) permissions.

    On Windows POSIX mode bits only map onto the read-only attribute, so both
    the os.open mode and the chmod below are best-effort there; real isolation
    must come from the ACL on the secrets root directory.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def _secret_store_key() -> bytes:
    """Return the Fernet key for secret material at rest, creating it on first use."""
    SECRETS_ROOT.mkdir(parents=True, exist_ok=True)
    key_path = SECRETS_ROOT / _SECRET_STORE_KEY_FILE
    if key_path.exists():
        return key_path.read_bytes().strip()
    key = Fernet.generate_key()
    _write_private_file(key_path, key)
    return key


def _secret_path(secret_ref: str) -> Path:
    name = secret_ref.removeprefix("secret://").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid secret reference.")
    return SECRETS_ROOT / f"{name}.pem"


def write_secret_material(secret_ref: str, content: str) -> None:
    """Encrypt secret material with the store key and write it owner-only."""
    token = Fernet(_secret_store_key()).encrypt(content.encode("utf-8"))
    _write_private_file(_secret_path(secret_ref), token)


def read_secret_material(secret_ref: str) -> str:
    """Resolve a secret:// reference to its decrypted file contents.

    Legacy plaintext files (written before encryption-at-rest existed) stay
    readable via the fallback below; they only become encrypted if the
    material is uploaded again.
    """
    raw = _secret_path(secret_ref).read_bytes()
    try:
        return Fernet(_secret_store_key()).decrypt(raw).decode("utf-8")
    except InvalidToken:
        return raw.decode("utf-8")


def _previous_section_values(payload: dict[str, object] | None, section_name: str) -> dict[str, object]:
    """Values dict of one section from a stored payload, defensively typed."""
    if not isinstance(payload, dict):
        return {}
    section = payload.get(section_name)
    if not isinstance(section, dict):
        return {}
    values = section.get("values")
    return values if isinstance(values, dict) else {}


class ConfigurationService:
    """Versioned configuration snapshots stored per project+site in the database.

    Secret material stays file-based (encrypted) under the secrets root;
    configuration payloads only ever hold secret:// references. Password-kind
    values are stored unmasked in the DB payload but masked on every snapshot
    returned to the API routes; internal consumers (e.g. the MQTT connection
    builder via the configuration values provider) load with
    mask_secrets=False to see the real values.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        ensure_runtime_directories()
        self._repository = ConfigurationRepository(engine if engine is not None else get_engine())

    def load(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        site_id: str = DEFAULT_SITE_ID,
        *,
        mask_secrets: bool = True,
    ) -> ConfigurationSnapshot:
        payload = self._repository.get_current(project_id, site_id)
        if payload is None:
            snapshot = self._persist(DEFAULT_CONFIGURATION, project_id, site_id)
        else:
            snapshot = self._merge_with_defaults(ConfigurationSnapshot.model_validate(payload))
        return self._mask_for_api(snapshot) if mask_secrets else snapshot

    def save(
        self,
        configuration: ConfigurationSnapshot,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        site_id: str = DEFAULT_SITE_ID,
    ) -> ConfigurationSnapshot:
        """Persist a new version and return the API-safe (masked) snapshot."""
        return self._mask_for_api(self._persist(configuration, project_id, site_id))

    def read_secret(self, secret_ref: str) -> str:
        """Resolve a secret:// reference to its decrypted contents."""
        return read_secret_material(secret_ref)

    def mqtt_subscribe_defaults(self, project_id: str = DEFAULT_PROJECT_ID, site_id: str = DEFAULT_SITE_ID) -> dict:
        """Subscribe defaults from saved config so a run inherits them when the
        operator left them blank: the configured Root Topic becomes the default
        ``topic_filter`` (normalised to a ``prefix/#`` wildcard) and the QoS field
        becomes the subscribe ``qos`` (0-2). Addresses "I set the root topic / QoS
        and the scan ignored it".
        """
        values = self.load(project_id, site_id).mqtt.values
        try:
            qos = max(0, min(2, int(str(values.get("QoS")).strip().split()[0])))
        except (ValueError, IndexError):
            qos = 0
        defaults: dict = {"qos": qos}
        root = str(values.get("Root Topic") or "").strip()
        if root:
            defaults["topic_filter"] = root if ("#" in root or "+" in root) else root.rstrip("/") + "/#"
        return defaults

    def _persist(self, configuration: ConfigurationSnapshot, project_id: str, site_id: str) -> ConfigurationSnapshot:
        configuration = self._merge_with_defaults(configuration.model_copy(deep=True))
        self._resolve_secret_sentinels(configuration, self._repository.get_current(project_id, site_id))
        self._repository.save(project_id, site_id, configuration.model_dump(mode="json"))
        return configuration

    def _mask_for_api(self, configuration: ConfigurationSnapshot) -> ConfigurationSnapshot:
        """Mask password-kind values on snapshots that cross the API boundary.

        The stored DB payload and internal consumers keep the real values;
        only the serialized GET/PUT responses carry the sentinel. secret://
        references are already opaque and stay as-is.
        """
        masked = configuration.model_copy(deep=True)
        for section_name, field_names in PASSWORD_KIND_FIELDS.items():
            values = getattr(masked, section_name).values
            for field_name in field_names:
                if values.get(field_name, ""):
                    values[field_name] = SECRET_SENTINEL
        return masked

    def _resolve_secret_sentinels(
        self,
        configuration: ConfigurationSnapshot,
        previous_payload: dict[str, object] | None,
    ) -> None:
        """Write-only update semantics for password-kind fields.

        The frontend echoes the all-asterisk sentinel for untouched password
        fields, so an incoming sentinel keeps the previously stored value;
        a sentinel with no real prior value stores empty — asterisks are
        never persisted as the secret itself.
        """
        for section_name, field_names in PASSWORD_KIND_FIELDS.items():
            values = getattr(configuration, section_name).values
            previous_values = _previous_section_values(previous_payload, section_name)
            for field_name in field_names:
                if not _is_secret_sentinel(values.get(field_name, "")):
                    continue
                previous = str(previous_values.get(field_name) or "")
                values[field_name] = "" if _is_secret_sentinel(previous) else previous

    def validate(self, configuration: ConfigurationSnapshot) -> ConfigurationValidationResult:
        errors: list[str] = []
        configuration = self._merge_with_defaults(configuration)

        self._validate_ip_field(errors, "Device IP Address", configuration.device.values.get("IP Address", ""))
        self._validate_ip_field(errors, "Device Gateway", configuration.device.values.get("Gateway", ""))
        self._validate_subnet_mask(errors, configuration.device.values.get("Subnet Mask", ""))
        self._validate_dns_servers(errors, configuration.device.values.get("DNS Servers", ""))
        self._validate_port(errors, "BACnet UDP Port", configuration.bacnet.values.get("UDP Port", ""))
        self._validate_port(errors, "BBMD UDP Port", configuration.bacnet.values.get("BBMD UDP Port", ""))
        self._validate_enabled_disabled(errors, "BACnet Foreign Device", configuration.bacnet.values.get("Foreign Device", ""))
        self._validate_enabled_disabled(errors, "BACnet BBMD", configuration.bacnet.values.get("BBMD", ""))
        if (
            configuration.bacnet.values.get("BBMD", "").strip().casefold() == "enabled"
            and configuration.bacnet.values.get("Foreign Device", "").strip().casefold() == "enabled"
        ):
            errors.append("Foreign Device must be Disabled when BBMD is Enabled.")
        self._validate_positive_int(errors, "BACnet TTL", configuration.bacnet.values.get("TTL", ""))
        self._validate_range_number(
            errors,
            "BACnet Network Number",
            configuration.bacnet.values.get("BACnet Network Number", ""),
            minimum=0,
            maximum=65535,
        )
        self._validate_port(errors, "MQTT Port", configuration.mqtt.values.get("Port", ""))
        self._validate_non_empty(
            errors,
            "MQTT Broker FQDN or IP Address",
            configuration.mqtt.values.get("MQTT Broker FQDN or IP Address", ""),
        )
        self._validate_non_empty(errors, "MQTT Client ID", configuration.mqtt.values.get("Client ID", ""))
        self._validate_non_empty(errors, "MQTT Root Topic", configuration.mqtt.values.get("Root Topic", ""))
        self._validate_positive_int(errors, "MQTT Keep Alive Interval", configuration.mqtt.values.get("Keep Alive Interval", ""))
        self._validate_positive_int(errors, "NTP Update Interval", configuration.time.values.get("NTP Update Interval", ""))
        self._validate_certificate(errors, "CA Certificate", configuration.certificates.values.get("CA Certificate", ""))
        self._validate_certificate(errors, "Client Certificate", configuration.certificates.values.get("Client Certificate", ""))
        self._validate_certificate(errors, "Private Key", configuration.certificates.values.get("Private Key", ""))
        self._validate_non_empty(errors, "Backup Schedule", configuration.backups.values.get("Backup Schedule", ""))
        self._validate_positive_int_from_prefix(errors, "Backup Retention", configuration.backups.values.get("Backup Retention", ""))
        self._validate_enabled_disabled(errors, "Encrypted Backups", configuration.backups.values.get("Encrypted Backups", ""))
        self._validate_non_empty(errors, "Backup Location", configuration.backups.values.get("Backup Location", ""))
        self._validate_non_empty(errors, "Last Backup Status", configuration.backups.values.get("Last Backup Status", ""))
        self._validate_non_empty(errors, "Restore Action", configuration.backups.values.get("Restore Action", ""))

        return ConfigurationValidationResult(valid=not errors, errors=errors)

    def store_secret(
        self,
        request: SecretMaterialRequest,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        site_id: str = DEFAULT_SITE_ID,
    ) -> SecretMaterialResponse:
        field = request.field.strip()
        if field not in {"CA Certificate", "Client Certificate", "Private Key"}:
            raise ValueError("Only CA Certificate, Client Certificate, and Private Key can be stored as secret material.")

        content = request.content.strip()
        if not content:
            raise ValueError(f"{field} content must not be empty.")

        suffix = Path(request.file_name or "").suffix.lower()
        if suffix and suffix not in SUPPORTED_CERT_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_CERT_EXTENSIONS))
            raise ValueError(f"{field} must use a supported file type: {supported}.")

        digest = sha256(content.encode("utf-8")).hexdigest()
        secret_ref = f"secret://{field.lower().replace(' ', '-')}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{token_hex(4)}"
        write_secret_material(secret_ref, content)

        expiry = self._certificate_expiry(field, content)
        configuration = self.load(project_id, site_id, mask_secrets=False)
        configuration.certificates.values[field] = secret_ref
        # Reflect the real uploaded cert's expiry so the read-only "Certificate
        # Expiry" status pill (mq9ll2vf) is driven by the actual notAfter, not a
        # seeded placeholder. Private Key / unparseable uploads leave it as-is.
        if expiry is not None:
            configuration.certificates.values["Certificate Expiry"] = expiry
        self.save(configuration, project_id=project_id, site_id=site_id)

        return SecretMaterialResponse(
            secret_ref=secret_ref,
            field=field,
            file_name=request.file_name,
            fingerprint=digest[:16],
            validity=self._secret_validity(field, content),
            expiry=expiry,
            masked=True,
        )

    def _merge_with_defaults(self, configuration: ConfigurationSnapshot) -> ConfigurationSnapshot:
        self._migrate_mqtt_fields(configuration)
        for section_name in ConfigurationSnapshot.model_fields:
            loaded_section = getattr(configuration, section_name)
            default_section = getattr(DEFAULT_CONFIGURATION, section_name)
            loaded_section.values = {**default_section.values, **loaded_section.values}
            if not loaded_section.status:
                loaded_section.status = default_section.status
        return configuration

    def _migrate_mqtt_fields(self, configuration: ConfigurationSnapshot) -> None:
        mqtt_values = configuration.mqtt.values
        if "MQTT Broker" in mqtt_values and "MQTT Broker FQDN or IP Address" not in mqtt_values:
            mqtt_values["MQTT Broker FQDN or IP Address"] = mqtt_values.pop("MQTT Broker")
        if "Keep Alive" in mqtt_values and "Keep Alive Interval" not in mqtt_values:
            mqtt_values["Keep Alive Interval"] = mqtt_values.pop("Keep Alive")

    def _validate_ip_field(self, errors: list[str], label: str, value: str) -> None:
        value = value.strip()
        if not value:
            errors.append(f"{label} must not be empty.")
            return
        try:
            ipaddress.ip_address(value)
        except ValueError:
            errors.append(f"{label} must be a valid IPv4 or IPv6 address.")

    def _validate_subnet_mask(self, errors: list[str], value: str) -> None:
        value = value.strip()
        if not value:
            errors.append("Subnet Mask must not be empty.")
            return
        try:
            ipaddress.IPv4Network(f"0.0.0.0/{value}")
        except ValueError:
            errors.append("Subnet Mask must be a valid IPv4 subnet mask.")

    def _validate_dns_servers(self, errors: list[str], value: str) -> None:
        servers = [entry.strip() for entry in value.split(",") if entry.strip()]
        if not servers:
            errors.append("DNS Servers must include at least one address.")
            return
        for server in servers:
            try:
                ipaddress.ip_address(server)
            except ValueError:
                errors.append(f"DNS server '{server}' must be a valid IPv4 or IPv6 address.")

    def _validate_port(self, errors: list[str], label: str, value: str) -> None:
        if not value.isdigit():
            errors.append(f"{label} must be numeric.")
            return
        port = int(value)
        if port < 1 or port > 65535:
            errors.append(f"{label} must be between 1 and 65535.")

    def _validate_range_number(
        self,
        errors: list[str],
        label: str,
        value: str,
        *,
        minimum: int,
        maximum: int,
    ) -> None:
        if not value.isdigit():
            errors.append(f"{label} must be numeric.")
            return
        parsed = int(value)
        if parsed < minimum or parsed > maximum:
            errors.append(f"{label} must be between {minimum} and {maximum}.")

    def _validate_non_empty(self, errors: list[str], label: str, value: str) -> None:
        if not value.strip():
            errors.append(f"{label} must not be empty.")

    def _validate_positive_int(self, errors: list[str], label: str, value: str) -> None:
        if not value.isdigit():
            errors.append(f"{label} must be numeric.")
            return
        if int(value) <= 0:
            errors.append(f"{label} must be greater than zero.")

    def _validate_positive_int_from_prefix(self, errors: list[str], label: str, value: str) -> None:
        prefix = value.strip().split(" ", 1)[0]
        self._validate_positive_int(errors, label, prefix)

    def _validate_enabled_disabled(self, errors: list[str], label: str, value: str) -> None:
        normalized = value.strip().casefold()
        if normalized not in {"enabled", "disabled"}:
            errors.append(f"{label} must be Enabled or Disabled.")

    def _validate_certificate(self, errors: list[str], label: str, value: str) -> None:
        if not value.strip():
            errors.append(f"{label} must not be empty.")
            return
        if value.startswith("secret://"):
            return
        suffix = Path(value).suffix.lower()
        if suffix not in SUPPORTED_CERT_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_CERT_EXTENSIONS))
            errors.append(f"{label} must use a supported file type: {supported}.")

    def _secret_validity(self, field: str, content: str) -> Literal["stored", "stored_unparsed"]:
        markers = {
            "CA Certificate": "BEGIN CERTIFICATE",
            "Client Certificate": "BEGIN CERTIFICATE",
            "Private Key": "BEGIN",
        }
        expected_marker = markers[field]
        if expected_marker in content:
            return "stored"
        return "stored_unparsed"

    def _certificate_expiry(self, field: str, content: str) -> str | None:
        """The uploaded certificate's notAfter date (YYYY-MM-DD), or None.

        Returns None for a Private Key (no expiry) or content that does not parse
        as an X.509 PEM, so a non-cert upload never crashes the store path.
        """
        if field not in {"CA Certificate", "Client Certificate"}:
            return None
        try:
            certificate = x509.load_pem_x509_certificate(content.encode("utf-8"))
        except (ValueError, TypeError):
            return None
        not_after = getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after
        return not_after.date().isoformat()
