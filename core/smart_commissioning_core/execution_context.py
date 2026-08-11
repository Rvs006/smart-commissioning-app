"""Shared frozen-context parameter resolution for inline and worker executors."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from smart_commissioning_core.run_context import (
    RunContextV1,
    canonical_context_sha256,
    canonical_sha256,
    mqtt_client_id,
)
from smart_commissioning_core.run_lifecycle import (
    RunLeaseV1,
    StoredRunContextV1,
)

SecretMaterialResolver = Callable[[str], bytes | None]

_CONNECTION_KEY_ALIASES = {
    "host": "broker_host",
    "port": "broker_port",
    "tls": "use_tls",
    "bind_address": "local_address",
}
_SECRET_KEY_ALIASES = {
    "mqtt_password": "password",
    "mqtt_username": "username",
    "mqtt_private_key": "private_key",
    "mqtt_client_certificate": "client_certificate",
    "mqtt_ca_certificate": "ca_certificate",
    "mqtt_key_password": "key_password",
}
_TEXT_SECRET_DESTINATIONS = frozenset(
    {"password", "username", "key_password", "token", "access_token", "api_token"}
)


class SecretMaterialUnavailableError(RuntimeError):
    """A required versioned secret could not be resolved before engine entry."""


class ExecutionContextIntegrityError(RuntimeError):
    """The insert-once stored context no longer matches its claimed digest."""


def scan_authority_bindings(
    context: RunContextV1,
) -> dict[str, tuple[str, int]]:
    """Return exact accepted-row digest/count bindings from a scan contract."""
    contract = context.engine_parameters.get("scan_contract_v1")
    if not isinstance(contract, Mapping):
        return {}
    candidates: list[object] = []
    ip_contract = contract.get("ip")
    if isinstance(ip_contract, Mapping):
        candidates.append(ip_contract.get("authority"))
    bacnet_contract = contract.get("bacnet")
    if isinstance(bacnet_contract, Mapping):
        authorities = bacnet_contract.get("authorities")
        if isinstance(authorities, Mapping):
            candidates.extend(authorities.values())

    context_digests = {
        binding.resource_id: binding.sha256
        for binding in (*context.registers, *context.imports)
    }
    bindings: dict[str, tuple[str, int]] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, Mapping):
            raise ExecutionContextIntegrityError(
                "scan authority metadata is malformed"
            )
        import_id = str(candidate.get("import_id") or "").strip()
        digest = str(candidate.get("accepted_rows_sha256") or "").strip().lower()
        try:
            accepted_count = int(candidate.get("accepted_count"))
        except (TypeError, ValueError, OverflowError) as error:
            raise ExecutionContextIntegrityError(
                "scan authority row count is malformed"
            ) from error
        if (
            not import_id
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or accepted_count < 0
        ):
            raise ExecutionContextIntegrityError(
                "scan authority binding is malformed"
            )
        if context_digests.get(import_id) != digest:
            raise ExecutionContextIntegrityError(
                "scan authority is not bound to the execution context digest"
            )
        previous = bindings.get(import_id)
        current = (digest, accepted_count)
        if previous is not None and previous != current:
            raise ExecutionContextIntegrityError(
                "scan authority has conflicting frozen bindings"
            )
        bindings[import_id] = current
    return bindings


def verify_bound_import_rows(
    context: RunContextV1,
    import_id: str,
    rows: Sequence[object],
) -> None:
    """Rehash one immutable authority snapshot immediately before it is used."""
    binding = scan_authority_bindings(context).get(import_id)
    if binding is None:
        raise ExecutionContextIntegrityError(
            "requested scan authority is not bound to this execution context"
        )
    expected_digest, expected_count = binding
    if len(rows) != expected_count:
        raise ExecutionContextIntegrityError(
            "scan authority accepted-row count changed after preview"
        )
    if canonical_sha256(list(rows)) != expected_digest:
        raise ExecutionContextIntegrityError(
            "scan authority accepted-row digest changed after preview"
        )


def verify_stored_context(
    stored: StoredRunContextV1,
    lease: RunLeaseV1,
) -> RunContextV1:
    """Verify one stored context against its row and claimed lease digests."""

    if stored.run_id != lease.run_id:
        raise ExecutionContextIntegrityError("stored execution context belongs to another run")
    actual_sha256 = canonical_context_sha256(stored.context)
    if (
        actual_sha256 != stored.context_sha256
        or stored.context_sha256 != lease.context_sha256
    ):
        raise ExecutionContextIntegrityError(
            "stored execution context failed integrity verification"
        )
    return stored.context


def resolve_context_parameters(
    context: RunContextV1,
    lease: RunLeaseV1,
    *,
    deployment_id: str,
    channel: str,
    secret_resolver: SecretMaterialResolver,
) -> dict[str, Any]:
    """Resolve one RunContextV1 into ephemeral engine parameters.

    Decrypted values exist only in the returned in-memory mapping. Certificate
    references are verified but remain opaque so the TLS adapter can resolve
    them into temporary files immediately before ``SSLContext`` construction.
    The caller must never log or persist the returned mapping.
    """
    use_connection = _uses_frozen_connection(
        channel=channel,
        engine_parameters=context.engine_parameters,
    )
    parameters = (
        _configuration_defaults(context.configuration_snapshot)
        if use_connection
        else {}
    )
    parameters.update(context.engine_parameters)
    if use_connection:
        for key, value in context.connection_settings.items():
            parameters[_CONNECTION_KEY_ALIASES.get(key, key)] = value
    if context.network_interface:
        if context.protocol_key and context.protocol_key.startswith("bacnet:"):
            parameters["local_address"] = context.network_interface
        else:
            parameters["source_ip"] = context.network_interface.split("/", 1)[0]
    references = {
        key: reference.reference for key, reference in context.secret_references.items()
    }
    for key, value in parameters.items():
        if isinstance(value, str) and value.startswith("secret://"):
            references.setdefault(key, value)
    for key, reference in references.items():
        leaf_key = key.rsplit(".", 1)[-1].strip().casefold().replace(" ", "_").replace("-", "_")
        destination = _SECRET_KEY_ALIASES.get(leaf_key, leaf_key)
        # References captured from an unused configuration path do not belong to
        # an offline execution and should not make it depend on secret storage.
        if destination not in parameters and key != leaf_key:
            continue
        material = secret_resolver(reference)
        if material is None:
            raise SecretMaterialUnavailableError(
                "required versioned secret material is unavailable"
            )
        if destination in _TEXT_SECRET_DESTINATIONS:
            try:
                parameters[destination] = material.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SecretMaterialUnavailableError(
                    "text secret material is not valid UTF-8"
                ) from error
        else:
            parameters[destination] = reference
    parameters["project_id"] = context.project_id
    parameters["site_id"] = context.site_id
    if context.protocol_key and context.protocol_key.startswith("mqtt:"):
        parameters["client_id"] = mqtt_client_id(
            deployment=deployment_id,
            run_id=lease.run_id,
            attempt=lease.attempt,
            channel=channel,
        )
    return parameters


def _uses_frozen_connection(
    *, channel: str, engine_parameters: dict[str, Any]
) -> bool:
    if channel != "mqtt_config_publish":
        return True
    explicit_host = str(engine_parameters.get("broker_host") or "").strip()
    raw_live = engine_parameters.get("use_live_broker")
    live_requested = (
        raw_live
        if isinstance(raw_live, bool)
        else str(raw_live or "").strip().casefold()
        in {"1", "true", "yes", "on", "enabled"}
    )
    return bool(live_requested or explicit_host)


def _configuration_defaults(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Map frozen configuration sections into the engines' explicit inputs."""
    mqtt = _section_values(snapshot, "mqtt")
    certificates = _section_values(snapshot, "certificates")
    defaults: dict[str, Any] = {}
    mappings = (
        (mqtt, "MQTT Broker FQDN or IP Address", "broker_host"),
        (mqtt, "Port", "broker_port"),
        (mqtt, "Use TLS", "use_tls"),
        (mqtt, "Keep Alive Interval", "keep_alive"),
        (mqtt, "MQTT Username", "username"),
        (mqtt, "MQTT Password", "password"),
        (certificates, "CA Certificate", "ca_certificate"),
        (certificates, "Client Certificate", "client_certificate"),
        (certificates, "Private Key", "private_key"),
        (certificates, "Key Password", "key_password"),
    )
    for source, source_key, destination in mappings:
        value = source.get(source_key)
        if value not in (None, ""):
            defaults[destination] = value
    return defaults


def _section_values(snapshot: dict[str, Any], section_name: str) -> dict[str, Any]:
    section = snapshot.get(section_name)
    if not isinstance(section, dict):
        return {}
    values = section.get("values")
    return values if isinstance(values, dict) else {}
