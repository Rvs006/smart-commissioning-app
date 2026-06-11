import ipaddress
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Literal

from app.core.runtime import CONFIGURATION_PATH, SECRETS_ROOT, ensure_runtime_directories
from app.schemas.configuration import (
    ConfigurationSection,
    ConfigurationSnapshot,
    ConfigurationValidationResult,
    SecretMaterialRequest,
    SecretMaterialResponse,
)

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


class ConfigurationService:
    def __init__(self, path: Path = CONFIGURATION_PATH) -> None:
        self.path = path
        ensure_runtime_directories()

    def load(self) -> ConfigurationSnapshot:
        if not self.path.exists():
            self.save(DEFAULT_CONFIGURATION)
            return DEFAULT_CONFIGURATION
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        loaded = ConfigurationSnapshot.model_validate(payload)
        return self._merge_with_defaults(loaded)

    def save(self, configuration: ConfigurationSnapshot) -> ConfigurationSnapshot:
        configuration = self._merge_with_defaults(configuration)
        self.path.write_text(
            configuration.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return configuration

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

    def store_secret(self, request: SecretMaterialRequest) -> SecretMaterialResponse:
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
        secret_path = SECRETS_ROOT / f"{secret_ref.removeprefix('secret://')}.pem"
        secret_path.write_text(content, encoding="utf-8")

        configuration = self.load()
        configuration.certificates.values[field] = secret_ref
        self.save(configuration)

        return SecretMaterialResponse(
            secret_ref=secret_ref,
            field=field,
            file_name=request.file_name,
            fingerprint=digest[:16],
            validity=self._secret_validity(field, content),
            expiry=None,
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
