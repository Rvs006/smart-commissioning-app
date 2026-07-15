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
from smart_commissioning_core.engines.bacnet_params import (
    MODE_FOREIGN_DEVICE,
    PARAM_BACNET_MODE,
    PARAM_BBMD_ADDRESS,
    PARAM_BBMD_PORT,
    PARAM_FD_TTL,
    bbmd_port,
    fd_ttl,
)
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
            # Empty means "never chosen" (behaves as Auto everywhere: validation
            # accepts it, engine dispatch binds nothing). The UI seeds its
            # wired-first default only for this empty value; the literal
            # "Auto (OS default route)" sentinel is stored only when picked in
            # the dropdown, so a stored sentinel is an explicit choice.
            "Source Interface": "",
        },
        status="Healthy",
    ),
    bacnet=ConfigurationSection(
        values={
            "BACnet Network Number": "1532",
            "UDP Port": "47808",
            "Device Instance Range": "1 - 4194303",
            # INFORMATIONAL ONLY — discovery never reads this toggle. It seeds
            # Disabled because Enabled used to LOCK the "Foreign Device" control
            # (UI) and be rejected alongside it (validation), so a default install
            # could not enable the one setting that makes cross-subnet discovery
            # work. Discovery gates STRICTLY on "Foreign Device" (see
            # bacnet_transport_defaults).
            # NOTE: changing this default does NOT touch an already-persisted
            # snapshot — _merge_with_defaults only fills MISSING keys, so an
            # existing install keeps whatever it saved until an operator edits
            # and saves the Configuration page.
            "BBMD": "Disabled",
            # Demo seed, NOT a real BBMD. Nothing may register against it: only
            # "Foreign Device" == Enabled triggers registration, and an operator
            # who enables it must type their real BBMD's address here.
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
            "Use TLS": "Enabled",
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
        # No cert material ships pre-installed: a fresh install has NOTHING
        # uploaded, so the fields start empty rather than seeding placeholder
        # secret:// refs + a fake expiry that the UI would render as a real,
        # in-use, valid certificate. TLS trust material is uploaded by the
        # operator (and is optional — plaintext / no-mutual-TLS is a valid setup).
        values={
            "CA Certificate": "",
            "Client Certificate": "",
            "Private Key": "",
            "Key Password": "********",
            "Certificate Expiry": "",
        },
        status="Not configured",
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

    def bacnet_transport_defaults(
        self, project_id: str = DEFAULT_PROJECT_ID, site_id: str = DEFAULT_SITE_ID
    ) -> dict:
        """BACnet transport run-parameters from saved config (mirrors :meth:`mqtt_subscribe_defaults`).

        Returns the flat, JSON-safe scalars the engine reads — ``bacnet_mode`` /
        ``bbmd_address`` / ``bbmd_port`` / ``fd_ttl``, spelled by importing the
        shared contract, never as literals — so they survive the Dramatiq
        round-trip to the worker unchanged. Addresses "I set the BBMD fields and
        the scan ignored them".

        THE TRIGGER IS "Foreign Device" == Enabled (casefolded) AND NOTHING ELSE.
        Not the confusingly-named "BBMD" toggle, not a non-empty "BBMD Address":
        both are seeded on a default install (with the FICTIONAL demo address
        10.10.25.20), so keying on either would make every default install
        register against a host that does not exist. Anything but Enabled returns
        ``{}`` and the run stays local-broadcast — byte-identical to today's
        behaviour, which is the zero-regression guarantee for the path that
        works.

        Strictness is deliberately split:

        * "BBMD Address" is LOAD-BEARING — an FD run cannot happen without a
          real one, and falling back to broadcast would report a clean scan for
          a run the operator explicitly asked to send through a BBMD. Blank or
          unparseable raises :class:`ValueError` with an actionable message; the
          route turns that into a 400 before any run is created.
        * "BBMD UDP Port" / "TTL" SOFT-DEFAULT to 47808 / 300 (via the contract's
          own readers, so the bounds cannot drift from the engine's). Old
          snapshots can hold junk in fields that were only ever validated on
          save, and neither is worth blocking a lab scan over.
        """
        values = self.load(project_id, site_id).bacnet.values
        if str(values.get("Foreign Device") or "").strip().casefold() != "enabled":
            return {}
        address = str(values.get("BBMD Address") or "").strip()
        if not address:
            raise ValueError(
                "Foreign Device is Enabled but BBMD Address is empty. Set your BBMD's IP "
                "address on the Configuration page (BACnet -> BBMD Address) and Save, then "
                "run discovery again."
            )
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as error:
            raise ValueError(
                f"Foreign Device is Enabled but BBMD Address '{address}' is not a valid IP "
                "address. Fix it on the Configuration page (BACnet -> BBMD Address) and Save, "
                "then run discovery again."
            ) from error
        # Read the soft-defaulted values through the contract's own readers so the
        # bounds and fallbacks are defined in exactly one place (the engine reads
        # the same functions back off the run parameters).
        stored = {PARAM_BBMD_PORT: values.get("BBMD UDP Port"), PARAM_FD_TTL: values.get("TTL")}
        return {
            PARAM_BACNET_MODE: MODE_FOREIGN_DEVICE,
            PARAM_BBMD_ADDRESS: str(parsed),
            PARAM_BBMD_PORT: bbmd_port(stored),
            PARAM_FD_TTL: fd_ttl(stored),
        }

    def _persist(self, configuration: ConfigurationSnapshot, project_id: str, site_id: str) -> ConfigurationSnapshot:
        configuration = self._merge_with_defaults(configuration.model_copy(deep=True))
        self._resolve_secret_sentinels(configuration, self._repository.get_current(project_id, site_id))
        self._drop_dangling_secret_refs(configuration)
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

    def _drop_dangling_secret_refs(self, configuration: ConfigurationSnapshot) -> None:
        """Blank imported secret:// cert refs that point at no file on disk.

        A cross-machine import (or a hand-edited payload) can carry a secret://
        reference whose encrypted material never came across, alongside a
        plaintext expiry. Left in place the UI would render the dangling ref as a
        real, in-use certificate. Refs whose .pem exists are untouched; if none
        of the three cert fields still holds a resolvable ref, the expiry (which
        otherwise implies a present cert) is cleared too. Runs on the persist
        path only — validation must still accept a not-yet-resolvable ref so a
        cross-machine PUT import is not rejected.
        """
        values = configuration.certificates.values
        # Only CA/Client certs carry a notAfter, so only a resolvable one of THOSE
        # justifies keeping the "Certificate Expiry" pill — a surviving Private Key
        # must not keep a stale expiry alive when both certs dangled away.
        any_cert_resolvable = False
        for field in ("CA Certificate", "Client Certificate", "Private Key"):
            value = str(values.get(field, ""))
            if not value.startswith("secret://"):
                continue
            try:
                path = _secret_path(value)
            except ValueError:
                values[field] = ""
                continue
            if path.exists():
                if field != "Private Key":
                    any_cert_resolvable = True
            else:
                values[field] = ""
        if not any_cert_resolvable:
            values["Certificate Expiry"] = ""

    def validate(self, configuration: ConfigurationSnapshot) -> ConfigurationValidationResult:
        errors: list[str] = []
        configuration = self._merge_with_defaults(configuration)

        self._validate_ip_field(errors, "Device IP Address", configuration.device.values.get("IP Address", ""))
        self._validate_ip_field(errors, "Device Gateway", configuration.device.values.get("Gateway", ""))
        self._validate_subnet_mask(errors, configuration.device.values.get("Subnet Mask", ""))
        self._validate_dns_servers(errors, configuration.device.values.get("DNS Servers", ""))
        self._validate_source_interface(errors, configuration.device.values.get("Source Interface", ""))
        self._validate_port(errors, "BACnet UDP Port", configuration.bacnet.values.get("UDP Port", ""))
        self._validate_port(errors, "BBMD UDP Port", configuration.bacnet.values.get("BBMD UDP Port", ""))
        self._validate_enabled_disabled(errors, "BACnet Foreign Device", configuration.bacnet.values.get("Foreign Device", ""))
        self._validate_enabled_disabled(errors, "BACnet BBMD", configuration.bacnet.values.get("BBMD", ""))
        # The FD/BBMD mutual-exclusion rule is GONE (was: "Foreign Device must be
        # Disabled when BBMD is Enabled."). It encoded a real BACnet constraint —
        # a node that IS a BBMD cannot also be a foreign device — but this app is
        # never a BBMD. Combined with the seeded BBMD=Enabled it made Foreign
        # Device unsettable on a default install, which is why the transport
        # config never reached a scan. "BBMD" is informational now.
        #
        # In its place: BBMD Address is only load-bearing when Foreign Device is
        # Enabled, so validate it as an IP exactly then. Validating it always
        # would fail every default install that never intends to use FD; not
        # validating it at all (the old behaviour) let garbage through to a
        # 400 at run time, far from the field the operator has to fix.
        if configuration.bacnet.values.get("Foreign Device", "").strip().casefold() == "enabled":
            self._validate_ip_field(
                errors, "BACnet BBMD Address", configuration.bacnet.values.get("BBMD Address", "")
            )
        self._validate_positive_int(errors, "BACnet TTL", configuration.bacnet.values.get("TTL", ""))
        self._validate_range_number(
            errors,
            "BACnet Network Number",
            configuration.bacnet.values.get("BACnet Network Number", ""),
            minimum=0,
            maximum=65535,
        )
        self._validate_port(errors, "MQTT Port", configuration.mqtt.values.get("Port", ""))
        self._validate_enabled_disabled(errors, "MQTT Use TLS", configuration.mqtt.values.get("Use TLS", ""))
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
        # Drive the read-only "Certificate Expiry" status pill (mq9ll2vf) from the
        # SOONEST notAfter across BOTH the CA and Client certificates, not just the
        # cert uploaded last — an expired cert must not be hidden behind a newer,
        # still-valid one. Private Key / unparseable uploads contribute nothing, so
        # a Private-Key-only store leaves the field untouched.
        cert_values = configuration.certificates.values
        expiries = [
            resolved
            for cert_field in ("CA Certificate", "Client Certificate")
            if (resolved := self._stored_certificate_expiry(cert_field, cert_values.get(cert_field, ""))) is not None
        ]
        if expiries:
            cert_values["Certificate Expiry"] = min(expiries)
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
        # A config saved before the "Use TLS" control existed carries no such key.
        # Derive it from the stored port here — BEFORE _merge_with_defaults unions
        # the new static "Use TLS": "Enabled" default in — so a legacy plaintext
        # (non-8883) broker is not silently forced onto TLS and left unable to
        # connect. This mirrors the historical port heuristic (8883 = TLS); a
        # config that has already chosen "Use TLS" keeps its explicit value.
        port = str(mqtt_values.get("Port", "")).strip()
        if "Use TLS" not in mqtt_values and port:
            mqtt_values["Use TLS"] = "Enabled" if port == "8883" else "Disabled"

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

    def _validate_source_interface(self, errors: list[str], value: str) -> None:
        """Accept empty / "Auto (OS default route)" (OS default route, bind
        nothing) or a parseable interface IP with an optional prefix
        (e.g. 192.168.1.10/24 or a bare 192.168.1.10)."""
        value = value.strip()
        if not value or value.casefold() == "auto (os default route)":
            return
        try:
            ipaddress.ip_interface(value)
        except ValueError:
            errors.append(
                "Source Interface must be 'Auto (OS default route)' or a valid "
                "interface IP (optionally with prefix, e.g. 192.168.1.10/24)."
            )

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
        value = value.strip()
        if not value:
            # Certificates are OPTIONAL: a plaintext MQTT connection (Use TLS
            # Disabled) or a broker needing no client certificate is a valid
            # setup, so an empty certificate field is not a validation error.
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

    def _stored_certificate_expiry(self, field: str, value: str) -> str | None:
        """Expiry (YYYY-MM-DD) for a certificate field's stored value, or None.

        None when the field is not a CA/Client certificate or the value is empty.
        A ``secret://`` reference is resolved to its PEM material (guarding a
        missing or unreadable file); any other value is treated as inline PEM
        content, then delegated to :meth:`_certificate_expiry`.
        """
        if field not in {"CA Certificate", "Client Certificate"} or not value:
            return None
        if value.startswith("secret://"):
            try:
                content = read_secret_material(value)
            except (FileNotFoundError, ValueError):
                return None
        else:
            content = value
        return self._certificate_expiry(field, content)
