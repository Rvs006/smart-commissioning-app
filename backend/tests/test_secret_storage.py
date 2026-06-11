"""Secret handling contracts: encryption at rest, masking on read, write-only updates."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.schemas.configuration import SecretMaterialRequest
from app.services import _configuration_values
from app.services import configuration_service as configuration_service_module
from app.services.configuration_service import (
    DEFAULT_CONFIGURATION,
    DEFAULT_PROJECT_ID,
    DEFAULT_SITE_ID,
    SECRET_SENTINEL,
    ConfigurationService,
)
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.repositories import ConfigurationRepository

PEM_CONTENT = "-----BEGIN CERTIFICATE-----\nsecret-material-abc123\n-----END CERTIFICATE-----"
KEY_CONTENT = "-----BEGIN PRIVATE KEY-----\nprivate-material-xyz789\n-----END PRIVATE KEY-----"


class SecretStorageTestCase(unittest.TestCase):
    """Per-test temporary secrets root and SQLite database."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        temp_path = Path(temp_dir.name)

        self.secrets_root = temp_path / "secrets"
        patcher = mock.patch.object(configuration_service_module, "SECRETS_ROOT", self.secrets_root)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.engine = create_engine_from_url(default_sqlite_url(temp_path))
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        self.service = ConfigurationService(engine=self.engine)

    def save_with_mqtt_password(self, value: str) -> None:
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.mqtt.values["MQTT Password"] = value
        self.service.save(configuration)

    def stored_payload(self) -> dict[str, object]:
        payload = ConfigurationRepository(self.engine).get_current(DEFAULT_PROJECT_ID, DEFAULT_SITE_ID)
        assert payload is not None
        return payload


class EncryptionAtRestTests(SecretStorageTestCase):
    def test_store_secret_writes_encrypted_bytes_and_roundtrips(self) -> None:
        response = self.service.store_secret(
            SecretMaterialRequest(field="CA Certificate", file_name="ca.pem", content=PEM_CONTENT)
        )

        secret_files = sorted(self.secrets_root.glob("*.pem"))
        self.assertEqual(len(secret_files), 1)
        stored_bytes = secret_files[0].read_bytes()
        self.assertNotIn(PEM_CONTENT.encode("utf-8"), stored_bytes)
        self.assertNotIn(b"secret-material-abc123", stored_bytes)
        self.assertEqual(self.service.read_secret(response.secret_ref), PEM_CONTENT)

    def test_legacy_plaintext_secret_remains_readable(self) -> None:
        self.secrets_root.mkdir(parents=True, exist_ok=True)
        (self.secrets_root / "ca-certificate-legacy.pem").write_bytes(PEM_CONTENT.encode("utf-8"))

        self.assertEqual(self.service.read_secret("secret://ca-certificate-legacy"), PEM_CONTENT)

    def test_key_file_created_once_and_reused(self) -> None:
        first = self.service.store_secret(
            SecretMaterialRequest(field="CA Certificate", file_name="ca.pem", content=PEM_CONTENT)
        )
        key_path = self.secrets_root / ".secret_store_key"
        self.assertTrue(key_path.exists())
        key_bytes = key_path.read_bytes()

        second = self.service.store_secret(
            SecretMaterialRequest(field="Private Key", file_name="device.key", content=KEY_CONTENT)
        )

        self.assertEqual(key_path.read_bytes(), key_bytes)
        self.assertEqual(self.service.read_secret(first.secret_ref), PEM_CONTENT)
        self.assertEqual(self.service.read_secret(second.secret_ref), KEY_CONTENT)

    def test_secret_reference_cannot_escape_secrets_root(self) -> None:
        with self.assertRaises(ValueError):
            self.service.read_secret("secret://../outside")


class MaskOnReadTests(SecretStorageTestCase):
    def test_api_snapshots_mask_password_kinds_while_internal_load_sees_real_values(self) -> None:
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.mqtt.values["MQTT Password"] = "broker-pass-1"
        configuration.certificates.values["Key Password"] = "key-pass-1"
        saved = self.service.save(configuration)

        self.assertEqual(saved.mqtt.values["MQTT Password"], SECRET_SENTINEL)
        self.assertEqual(saved.certificates.values["Key Password"], SECRET_SENTINEL)

        api_snapshot = self.service.load()
        self.assertEqual(api_snapshot.mqtt.values["MQTT Password"], SECRET_SENTINEL)
        self.assertEqual(api_snapshot.certificates.values["Key Password"], SECRET_SENTINEL)
        serialized = api_snapshot.model_dump_json()
        self.assertNotIn("broker-pass-1", serialized)
        self.assertNotIn("key-pass-1", serialized)

        internal_snapshot = self.service.load(mask_secrets=False)
        self.assertEqual(internal_snapshot.mqtt.values["MQTT Password"], "broker-pass-1")
        self.assertEqual(internal_snapshot.certificates.values["Key Password"], "key-pass-1")

        payload = self.stored_payload()
        self.assertEqual(payload["mqtt"]["values"]["MQTT Password"], "broker-pass-1")

    def test_mqtt_provider_hook_receives_unmasked_values(self) -> None:
        self.save_with_mqtt_password("broker-pass-2")

        with mock.patch.object(configuration_service_module, "ConfigurationService", return_value=self.service):
            mqtt_values, certificate_values = _configuration_values()

        self.assertEqual(mqtt_values["MQTT Password"], "broker-pass-2")
        self.assertTrue(str(certificate_values["CA Certificate"]).startswith("secret://"))

    def test_secret_references_stay_opaque_and_empty_passwords_stay_empty(self) -> None:
        api_snapshot = self.service.load()  # seeds defaults: sentinel resolves to empty

        self.assertTrue(api_snapshot.certificates.values["CA Certificate"].startswith("secret://"))
        self.assertEqual(api_snapshot.mqtt.values["MQTT Password"], "")


class WriteOnlyUpdateTests(SecretStorageTestCase):
    def test_sentinel_preserves_previous_secret_across_versions(self) -> None:
        self.save_with_mqtt_password("original-secret")
        self.save_with_mqtt_password(SECRET_SENTINEL)
        self.save_with_mqtt_password("****")  # frontend sentinel matches any /^\*+$/ length

        self.assertEqual(self.service.load(mask_secrets=False).mqtt.values["MQTT Password"], "original-secret")
        self.assertEqual(self.stored_payload()["mqtt"]["values"]["MQTT Password"], "original-secret")

    def test_new_value_replaces_previous_secret(self) -> None:
        self.save_with_mqtt_password("original-secret")
        self.save_with_mqtt_password("rotated-secret")

        self.assertEqual(self.service.load(mask_secrets=False).mqtt.values["MQTT Password"], "rotated-secret")

    def test_sentinel_with_no_prior_value_stores_empty(self) -> None:
        self.save_with_mqtt_password(SECRET_SENTINEL)  # first version: nothing stored before

        self.assertEqual(self.stored_payload()["mqtt"]["values"]["MQTT Password"], "")
        self.assertEqual(self.service.load(mask_secrets=False).mqtt.values["MQTT Password"], "")


if __name__ == "__main__":
    unittest.main()
