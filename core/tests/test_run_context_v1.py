import json
import unittest
from pathlib import Path

from pydantic import ValidationError
from smart_commissioning_core.execution_context import (
    ExecutionContextIntegrityError,
    SecretMaterialUnavailableError,
    resolve_context_parameters,
    verify_bound_import_rows,
    verify_stored_context,
)
from smart_commissioning_core.run_context import (
    ContextResourceV1,
    RunContextV1,
    SecretReferenceV1,
    canonical_bacnet_protocol_key,
    canonical_context_sha256,
    canonical_json_bytes,
    canonical_mqtt_protocol_key,
    mqtt_client_id,
)
from smart_commissioning_core.run_lifecycle import (
    RunLeaseV1,
    RunSealViewV1,
    StoredRunContextV1,
    TerminalResultV1,
)

_FIXTURES = Path(__file__).with_name("fixtures")


def _context(**overrides: object) -> RunContextV1:
    values: dict[str, object] = {
        "project_id": "project-north",
        "site_id": "site-17",
        "configuration_snapshot": {
            "mqtt": {"values": {"Broker host": "broker.example", "Port": 8883}}
        },
        "configuration_version": 7,
        "registers": [
            ContextResourceV1(resource_id="imp-register-1", sha256="a" * 64)
        ],
        "imports": [ContextResourceV1(resource_id="imp-map-2", sha256="b" * 64)],
        "schema_versions": {"udmi": "1.5.2", "context": "1.0"},
        "engine_parameters": {"authorized": True, "duration_seconds": 10},
        "network_interface": "192.0.2.10/24",
        "connection_settings": {
            "broker_host": "broker.example",
            "broker_port": 8883,
            "private_key": "secret://mqtt-key-v3",
        },
        "secret_references": {
            "mqtt_private_key": SecretReferenceV1(
                reference="secret://mqtt-key-v3", version="3"
            )
        },
        "requesting_principal": "user-42",
        "application_version": "0.1.26",
        "protocol_key": "mqtt:profile-hash",
    }
    values.update(overrides)
    return RunContextV1.model_validate(values)


class CanonicalContextTests(unittest.TestCase):
    def test_historical_v1_context_and_seal_hashes_remain_verifiable(self) -> None:
        fixture = json.loads(
            (_FIXTURES / "run_context_v1_historical.json").read_text(encoding="utf-8")
        )
        context = RunContextV1.model_validate(fixture["context"])
        result = TerminalResultV1.model_validate(fixture["terminal_result"])
        seal = RunSealViewV1.model_validate(fixture["seal"])

        self.assertEqual(context.sha256(), fixture["context_sha256"])
        self.assertEqual(result.sha256(), fixture["result_sha256"])
        self.assertEqual(seal.context_sha256, fixture["context_sha256"])
        self.assertEqual(seal.result_sha256, fixture["result_sha256"])
        self.assertNotIn("scan_contract_v1", context.engine_parameters)

    def test_hash_is_stable_across_mapping_order(self) -> None:
        first = _context(
            engine_parameters={"authorized": True, "duration_seconds": 10}
        )
        second = _context(
            engine_parameters={"duration_seconds": 10, "authorized": True}
        )

        self.assertEqual(canonical_context_sha256(first), canonical_context_sha256(second))
        self.assertEqual(len(canonical_context_sha256(first)), 64)

    def test_canonical_json_escapes_unpaired_surrogates_without_changing_unicode(self) -> None:
        encoded = canonical_json_bytes({"ordinary": "café", "hostile": "bad\ud800text"})

        self.assertEqual(
            json.loads(encoded),
            {"hostile": "bad\\uD800text", "ordinary": "café"},
        )
        self.assertEqual(
            encoded,
            canonical_json_bytes({"hostile": "bad\ud800text", "ordinary": "café"}),
        )

    def test_canonical_json_escapes_surrogates_inside_pydantic_models(self) -> None:
        result = TerminalResultV1(
            status="succeeded",
            stage="done",
            summary={"bad\udfffkey": "bad\ud800value"},
        )

        encoded = canonical_json_bytes(result)

        self.assertEqual(
            json.loads(encoded)["summary"],
            {"bad\\uDFFFkey": "bad\\uD800value"},
        )
        self.assertEqual(len(result.sha256()), 64)

    def test_canonical_json_normalizes_non_finite_model_values(self) -> None:
        result = TerminalResultV1(
            status="succeeded",
            stage="done",
            summary={
                "nan": float("nan"),
                "positive": float("inf"),
                "negative": float("-inf"),
            },
        )

        encoded = canonical_json_bytes(result)

        self.assertIn(b'"nan":"nan (non-standard JSON number)"', encoded)
        self.assertIn(b'"positive":"inf (non-standard JSON number)"', encoded)
        self.assertIn(b'"negative":"-inf (non-standard JSON number)"', encoded)
        self.assertEqual(len(result.sha256()), 64)

    def test_context_rejects_raw_secret_material(self) -> None:
        with self.assertRaises(ValidationError):
            _context(connection_settings={"password": "canary-secret-value"})

        for key in ("mqtt_password", "key_password"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                _context(
                    configuration_snapshot={
                        "mqtt": {"values": {key: "canary-secret-value"}}
                    }
                )

        with self.assertRaises(ValidationError):
            _context(
                secret_references={
                    "mqtt_password": {"reference": "plaintext-password", "version": "1"}
                }
            )

    def test_context_accepts_only_complete_sha256_bindings(self) -> None:
        with self.assertRaises(ValidationError):
            _context(imports=[{"resource_id": "imp-1", "sha256": "short"}])


class ProtocolIdentityTests(unittest.TestCase):
    def test_protocol_keys_are_canonical_and_do_not_expose_credentials(self) -> None:
        mqtt_a = canonical_mqtt_protocol_key(
            host="BROKER.Example ",
            port=8883,
            tls=True,
            source="site-17",
            client_certificate_reference="secret://client-v2",
        )
        mqtt_b = canonical_mqtt_protocol_key(
            host="broker.example",
            port=8883,
            tls=True,
            source="site-17",
            client_certificate_reference="secret://client-v2",
        )
        self.assertEqual(mqtt_a, mqtt_b)
        self.assertTrue(mqtt_a.startswith("mqtt:"))
        self.assertNotIn("broker", mqtt_a)
        self.assertNotIn("secret", mqtt_a)

        self.assertEqual(
            canonical_bacnet_protocol_key(bind_address="192.0.2.10/24", port=47808),
            canonical_bacnet_protocol_key(bind_address="192.0.2.10/24 ", port=47808),
        )

    def test_ten_thousand_mqtt_client_ids_are_unique_ascii_and_short(self) -> None:
        identifiers = {
            mqtt_client_id(
                deployment="hosted-eu-west",
                run_id=f"run-{index}",
                attempt=1,
                channel="capture",
            )
            for index in range(10_000)
        }

        self.assertEqual(len(identifiers), 10_000)
        for identifier in identifiers:
            self.assertTrue(identifier.isascii())
            self.assertLessEqual(len(identifier.encode("ascii")), 23)


class SharedContextResolutionTests(unittest.TestCase):
    def _lease(self) -> RunLeaseV1:
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        return RunLeaseV1(
            run_id="run-17",
            dispatch_id="dispatch-17",
            owner_token="owner-token",
            attempt=2,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=60),
            context_sha256="a" * 64,
        )

    def test_inline_and_worker_helper_resolves_text_only_in_memory(self) -> None:
        context = _context(
            connection_settings={
                "broker_host": "broker.example",
                "password": "secret://mqtt-password-v3",
            },
            secret_references={
                "mqtt_password": {
                    "reference": "secret://mqtt-password-v3",
                    "version": "3",
                }
            },
        )

        parameters = resolve_context_parameters(
            context,
            self._lease(),
            deployment_id="portable-a",
            channel="capture",
            secret_resolver=lambda _reference: b"ephemeral-password",
        )

        self.assertEqual(parameters["password"], "ephemeral-password")
        self.assertEqual(context.connection_settings["password"], "secret://mqtt-password-v3")

    def test_stored_context_digest_is_recomputed_against_claimed_lease(self) -> None:
        context = _context()
        digest = context.sha256()
        lease = self._lease().model_copy(update={"context_sha256": digest})
        stored = StoredRunContextV1(
            run_id=lease.run_id,
            context=context,
            context_sha256=digest,
            created_at=lease.claimed_at,
        )

        self.assertIs(verify_stored_context(stored, lease), context)

        tampered = stored.model_copy(
            update={
                "context": context.model_copy(
                    update={"engine_parameters": {"authorized": False}}
                )
            }
        )
        with self.assertRaises(ExecutionContextIntegrityError):
            verify_stored_context(tampered, lease)

    def test_stored_context_must_belong_to_claimed_run(self) -> None:
        context = _context()
        digest = context.sha256()
        lease = self._lease().model_copy(update={"context_sha256": digest})
        stored = StoredRunContextV1(
            run_id="run-other",
            context=context,
            context_sha256=digest,
            created_at=lease.claimed_at,
        )

        with self.assertRaises(ExecutionContextIntegrityError):
            verify_stored_context(stored, lease)

    def test_selected_scan_authority_rows_are_rehashed_at_execution(self) -> None:
        rows = [{"IP Address": "192.168.10.20"}]
        from smart_commissioning_core.run_context import canonical_sha256

        digest = canonical_sha256(rows)
        context = _context(
            imports=[{"resource_id": "imp-authority", "sha256": digest}],
            engine_parameters={
                "scan_contract_v1": {
                    "ip": {
                        "authority": {
                            "import_id": "imp-authority",
                            "accepted_rows_sha256": digest,
                            "accepted_count": 1,
                        }
                    }
                }
            },
        )

        verify_bound_import_rows(context, "imp-authority", rows)
        with self.assertRaisesRegex(ExecutionContextIntegrityError, "digest"):
            verify_bound_import_rows(
                context,
                "imp-authority",
                [{"IP Address": "192.168.10.99"}],
            )
        with self.assertRaisesRegex(ExecutionContextIntegrityError, "not bound"):
            verify_bound_import_rows(context, "imp-other", rows)

    def test_offline_config_publish_does_not_inherit_broker_defaults(self) -> None:
        context = _context(
            configuration_snapshot={
                "mqtt": {
                    "values": {
                        "MQTT Broker FQDN or IP Address": "saved-broker.example",
                        "Port": 8883,
                    }
                }
            },
            engine_parameters={
                "topic": "site/device/config",
                "payload": "{}",
                "confirmed": True,
            },
            connection_settings={
                "host": "saved-broker.example",
                "port": 8883,
                "password": "secret://unused-password",
            },
            secret_references={
                "configuration_snapshot.mqtt.values.MQTT Password": {
                    "reference": "secret://unused-password",
                    "version": "1",
                }
            },
            protocol_key=None,
        )

        parameters = resolve_context_parameters(
            context,
            self._lease(),
            deployment_id="portable-a",
            channel="mqtt_config_publish",
            secret_resolver=lambda _reference: self.fail(
                "offline validation must not resolve broker secrets"
            ),
        )

        self.assertEqual(parameters["topic"], "site/device/config")
        self.assertNotIn("broker_host", parameters)
        self.assertNotIn("password", parameters)
        self.assertNotIn("client_id", parameters)

    def test_missing_secret_fails_before_executor_entry(self) -> None:
        context = _context(
            connection_settings={"password": "secret://missing-v1"},
            secret_references={
                "mqtt_password": {
                    "reference": "secret://missing-v1",
                    "version": "1",
                }
            },
        )

        with self.assertRaises(SecretMaterialUnavailableError):
            resolve_context_parameters(
                context,
                self._lease(),
                deployment_id="hosted-a",
                channel="capture",
                secret_resolver=lambda _reference: None,
            )

    def test_frozen_configuration_is_mapped_without_current_config_fallback(self) -> None:
        context = _context(
            configuration_snapshot={
                "mqtt": {
                    "values": {
                        "MQTT Broker FQDN or IP Address": "frozen-broker.example",
                        "Port": 1884,
                        "Use TLS": "Disabled",
                        "MQTT Username": "field-user",
                        "MQTT Password": "secret://frozen-password-v9",
                    }
                },
                "certificates": {
                    "values": {"CA Certificate": "secret://frozen-ca-v2"}
                },
            },
            connection_settings={
                "host": "frozen-broker.example",
                "port": 1884,
                "tls": False,
            },
            secret_references={
                "configuration_snapshot.mqtt.values.MQTT Password": {
                    "reference": "secret://frozen-password-v9",
                    "version": "9",
                },
                "configuration_snapshot.certificates.values.CA Certificate": {
                    "reference": "secret://frozen-ca-v2",
                    "version": "2",
                },
            },
        )
        materials = {
            "secret://frozen-password-v9": b"field-password",
            "secret://frozen-ca-v2": b"PEM bytes are verified in memory",
        }

        parameters = resolve_context_parameters(
            context,
            self._lease(),
            deployment_id="hosted-a",
            channel="capture",
            secret_resolver=materials.get,
        )

        self.assertEqual(parameters["broker_host"], "frozen-broker.example")
        self.assertEqual(parameters["broker_port"], 1884)
        self.assertFalse(parameters["use_tls"])
        self.assertEqual(parameters["username"], "field-user")
        self.assertEqual(parameters["password"], "field-password")
        self.assertEqual(parameters["ca_certificate"], "secret://frozen-ca-v2")
        self.assertEqual(parameters["source_ip"], "192.0.2.10")


if __name__ == "__main__":
    unittest.main()
