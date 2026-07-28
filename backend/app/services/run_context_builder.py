"""Resolve and freeze the execution inputs used by every job runner."""

from __future__ import annotations

import hashlib
from typing import Any

from smart_commissioning_core.db.repositories import ImportRepository
from smart_commissioning_core.run_context import (
    ContextResourceV1,
    RunContextV1,
    SecretReferenceV1,
    canonical_bacnet_protocol_key,
    canonical_json_bytes,
    canonical_mqtt_protocol_key,
)

from app.services.configuration_service import (
    SECRET_SENTINEL,
    ConfigurationService,
    write_secret_material,
)

_APP_VERSION = "0.1.30"
_SENSITIVE_KEYS = {
    "password",
    "broker_password",
    "mqtt_password",
    "key_password",
    "token",
    "access_token",
    "api_token",
    "private_key",
    "client_certificate",
    "ca_certificate",
    "secret",
}
_MQTT_JOBS = {"mqtt_discovery", "udmi_validation", "mqtt_config_publish"}
_BACNET_JOBS = {"bacnet_discovery", "bacnet_validation"}


def build_run_context(
    *,
    engine,
    project_id: str,
    site_id: str,
    job_type: str,
    parameters: dict[str, Any],
    requesting_principal: str,
) -> RunContextV1:
    configuration = ConfigurationService(engine).load(
        project_id, site_id, mask_secrets=False
    ).model_dump(mode="json")
    references: dict[str, SecretReferenceV1] = {}
    frozen_configuration = _freeze_secrets(
        configuration,
        references=references,
        path=("configuration_snapshot",),
    )
    frozen_parameters = _freeze_secrets(
        dict(parameters),
        references=references,
        path=("engine_parameters",),
    )
    configuration_digest = hashlib.sha256(
        canonical_json_bytes(frozen_configuration)
    ).hexdigest()
    imports = _import_bindings(engine, frozen_parameters)
    connection_settings, protocol_key = _protocol_binding(
        job_type, frozen_parameters, frozen_configuration
    )
    return RunContextV1(
        project_id=project_id,
        site_id=site_id,
        configuration_snapshot=frozen_configuration,
        configuration_version=configuration_digest,
        registers=imports,
        imports=imports,
        schema_versions=_schema_versions(frozen_parameters),
        engine_parameters=frozen_parameters,
        network_interface=_optional_string(
            frozen_parameters.get("source_ip") or frozen_parameters.get("local_address")
        ),
        connection_settings=connection_settings,
        secret_references=references,
        requesting_principal=requesting_principal,
        application_version=_APP_VERSION,
        protocol_key=protocol_key,
    )


def _freeze_secrets(
    value: Any,
    *,
    references: dict[str, SecretReferenceV1],
    path: tuple[str, ...],
) -> Any:
    if isinstance(value, dict):
        frozen: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.strip().casefold().replace("-", "_").replace(" ", "_")
            location = ".".join((*path, key))
            if normalized in _SENSITIVE_KEYS:
                frozen[key] = _freeze_secret_value(child, location, references)
            else:
                frozen[key] = _freeze_secrets(
                    child, references=references, path=(*path, key)
                )
        return frozen
    if isinstance(value, list):
        return [
            _freeze_secrets(item, references=references, path=(*path, str(index)))
            for index, item in enumerate(value)
        ]
    return value


def _freeze_secret_value(
    value: Any,
    location: str,
    references: dict[str, SecretReferenceV1],
) -> Any:
    if value in (None, "", SECRET_SENTINEL):
        return ""
    text = str(value)
    service = ConfigurationService()
    if text.startswith("secret://"):
        try:
            material = service.read_secret(text)
        except (FileNotFoundError, ValueError):
            material = text
        version = hashlib.sha256(material.encode("utf-8")).hexdigest()
        references[location] = SecretReferenceV1(reference=text, version=version)
        return text
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    reference = f"secret://run-context-{version}"
    write_secret_material(reference, text)
    references[location] = SecretReferenceV1(reference=reference, version=version)
    return reference


def _import_bindings(engine, parameters: dict[str, Any]) -> tuple[ContextResourceV1, ...]:
    repository = ImportRepository(engine)
    bindings: list[ContextResourceV1] = []
    seen: set[str] = set()
    for key, raw_value in parameters.items():
        if not key.endswith("import_id") or not raw_value:
            continue
        import_id = str(raw_value)
        if import_id in seen:
            continue
        seen.add(import_id)
        try:
            record = repository.get(import_id)
        except FileNotFoundError:
            continue
        bindings.append(
            ContextResourceV1(
                resource_id=import_id,
                sha256=hashlib.sha256(canonical_json_bytes(record)).hexdigest(),
            )
        )
    return tuple(bindings)


def _schema_versions(parameters: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in ("schema_version", "udmi_version", "expected_schema_version"):
        value = parameters.get(key)
        if value:
            values[key] = str(value)
    embedded = parameters.get("nonpub_schema_sets")
    if isinstance(embedded, dict):
        values["nonpub_schema_sets_sha256"] = hashlib.sha256(
            canonical_json_bytes(embedded)
        ).hexdigest()
    return values


def _protocol_binding(
    job_type: str,
    parameters: dict[str, Any],
    configuration: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    if job_type in _MQTT_JOBS:
        if job_type == "mqtt_config_publish" and not _live_mqtt_config_requested(
            parameters
        ):
            return {}, None
        mqtt = _section_values(configuration, "mqtt")
        certificates = _section_values(configuration, "certificates")
        host = str(
            parameters.get("broker_host")
            or mqtt.get("MQTT Broker FQDN or IP Address")
            or ""
        ).strip()
        port = int(parameters.get("broker_port") or mqtt.get("Port") or 8883)
        tls_value = parameters.get("use_tls", mqtt.get("Use TLS"))
        tls = str(tls_value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}
        certificate = _optional_string(certificates.get("Client Certificate"))
        settings = {"host": host, "port": port, "tls": tls, "source": "frozen_context"}
        # An empty broker host is an incomplete configuration, not a canonical
        # broker identity. Keeping it unkeyed avoids unrelated draft or fixture
        # runs blocking each other before a real network endpoint exists.
        if not host:
            return settings, None
        return settings, canonical_mqtt_protocol_key(
            host=host,
            port=port,
            tls=tls,
            source="frozen_context",
            client_certificate_reference=certificate,
        )
    if job_type in _BACNET_JOBS:
        bacnet = _section_values(configuration, "bacnet")
        bind_address = str(
            parameters.get("local_address") or parameters.get("source_ip") or "0.0.0.0"
        )
        port = int(parameters.get("bacnet_port") or bacnet.get("UDP Port") or 47808)
        return {"bind_address": bind_address, "port": port}, canonical_bacnet_protocol_key(
            bind_address=bind_address, port=port
        )
    return {}, None


def _live_mqtt_config_requested(parameters: dict[str, Any]) -> bool:
    explicit_host = str(parameters.get("broker_host") or "").strip()
    raw_live = parameters.get("use_live_broker")
    live_requested = (
        raw_live
        if isinstance(raw_live, bool)
        else str(raw_live or "").strip().casefold()
        in {"1", "true", "yes", "on", "enabled"}
    )
    return bool(live_requested or explicit_host)


def _section_values(configuration: dict[str, Any], name: str) -> dict[str, Any]:
    section = configuration.get(name)
    if not isinstance(section, dict):
        return {}
    values = section.get("values")
    return values if isinstance(values, dict) else {}


def _optional_string(value: Any) -> str | None:
    if value in (None, "", SECRET_SENTINEL):
        return None
    return str(value)
