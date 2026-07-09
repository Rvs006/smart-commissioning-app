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
        # No cert material ships by default (fresh install is honest), so store a
        # CA reference explicitly (with its backing file so persist keeps it),
        # then confirm secret:// refs stay opaque.
        self.secrets_root.mkdir(parents=True, exist_ok=True)
        (self.secrets_root / "ca-certificate.pem").write_bytes(PEM_CONTENT.encode("utf-8"))
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.mqtt.values["MQTT Password"] = "broker-pass-2"
        configuration.certificates.values["CA Certificate"] = "secret://ca-certificate"
        self.service.save(configuration)

        with mock.patch.object(configuration_service_module, "ConfigurationService", return_value=self.service):
            mqtt_values, certificate_values = _configuration_values()

        self.assertEqual(mqtt_values["MQTT Password"], "broker-pass-2")
        self.assertTrue(str(certificate_values["CA Certificate"]).startswith("secret://"))

    def test_secret_references_stay_opaque_and_empty_passwords_stay_empty(self) -> None:
        self.secrets_root.mkdir(parents=True, exist_ok=True)
        (self.secrets_root / "ca-certificate.pem").write_bytes(PEM_CONTENT.encode("utf-8"))
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.certificates.values["CA Certificate"] = "secret://ca-certificate"
        self.service.save(configuration)

        api_snapshot = self.service.load()
        self.assertTrue(api_snapshot.certificates.values["CA Certificate"].startswith("secret://"))
        self.assertEqual(api_snapshot.mqtt.values["MQTT Password"], "")

    def test_fresh_install_ships_no_certificate_material(self) -> None:
        # Bug fix (audit): a fresh install must NOT present fabricated cert
        # placeholders as real, in-use, valid material. Cert fields start empty
        # and the config still validates (certificates are optional).
        api_snapshot = self.service.load()
        self.assertEqual(api_snapshot.certificates.values["CA Certificate"], "")
        self.assertEqual(api_snapshot.certificates.values["Client Certificate"], "")
        self.assertEqual(api_snapshot.certificates.values["Private Key"], "")
        self.assertEqual(api_snapshot.certificates.values["Certificate Expiry"], "")
        self.assertTrue(self.service.validate(api_snapshot).valid)


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


class CertificateExpiryTests(SecretStorageTestCase):
    @staticmethod
    def _self_signed_cert(not_after) -> str:
        from datetime import UTC, datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expiry-test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
            .not_valid_after(not_after)
            .sign(key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def test_storing_a_certificate_parses_and_records_its_expiry(self) -> None:
        from datetime import UTC, datetime

        pem = self._self_signed_cert(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))
        response = self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=pem)
        )
        # notAfter parsed and returned, and the config's expiry pill reflects it.
        self.assertEqual(response.expiry, "2030-01-02")
        snapshot = self.service.load(mask_secrets=False)
        self.assertEqual(snapshot.certificates.values["Certificate Expiry"], "2030-01-02")

    def test_private_key_has_no_expiry(self) -> None:
        response = self.service.store_secret(
            SecretMaterialRequest(field="Private Key", file_name="device.key", content=KEY_CONTENT)
        )
        self.assertIsNone(response.expiry)

    def test_expiry_pill_shows_soonest_across_both_certs(self) -> None:
        # An EXPIRED CA plus a still-VALID Client must surface the soonest
        # (expired) date, not last-write-wins — the expiry must not be hidden.
        from datetime import UTC, datetime

        expired_ca = self._self_signed_cert(datetime(2020, 1, 1, tzinfo=UTC))
        valid_client = self._self_signed_cert(datetime(2030, 1, 2, tzinfo=UTC))

        self.service.store_secret(
            SecretMaterialRequest(field="CA Certificate", file_name="ca.pem", content=expired_ca)
        )
        self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=valid_client)
        )

        snapshot = self.service.load(mask_secrets=False)
        self.assertEqual(snapshot.certificates.values["Certificate Expiry"], "2020-01-01")

    def test_expiry_pill_soonest_regardless_of_upload_order(self) -> None:
        # Same certs uploaded in the reverse order still resolve to the soonest.
        from datetime import UTC, datetime

        expired_ca = self._self_signed_cert(datetime(2020, 1, 1, tzinfo=UTC))
        valid_client = self._self_signed_cert(datetime(2030, 1, 2, tzinfo=UTC))

        self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=valid_client)
        )
        self.service.store_secret(
            SecretMaterialRequest(field="CA Certificate", file_name="ca.pem", content=expired_ca)
        )

        snapshot = self.service.load(mask_secrets=False)
        self.assertEqual(snapshot.certificates.values["Certificate Expiry"], "2020-01-01")


class DanglingSecretRefTests(SecretStorageTestCase):
    def test_imported_dangling_ref_is_blanked_and_expiry_cleared(self) -> None:
        # An imported payload can carry a secret:// ref whose encrypted file never
        # came across, plus a plaintext expiry. Persist must drop the dangling ref
        # so the UI never renders it as a real, in-use cert, and clear the orphan
        # expiry once no resolvable ref remains.
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.certificates.values["CA Certificate"] = "secret://missing-ca"
        configuration.certificates.values["Certificate Expiry"] = "2031-05-05"
        self.service.save(configuration)

        snapshot = self.service.load(mask_secrets=False)
        self.assertEqual(snapshot.certificates.values["CA Certificate"], "")
        self.assertEqual(snapshot.certificates.values["Certificate Expiry"], "")

    def test_existing_secret_ref_preserved_on_unrelated_save(self) -> None:
        # A ref whose file exists on disk must survive an unrelated edit+save.
        stored = self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=PEM_CONTENT)
        )
        configuration = self.service.load(mask_secrets=False)
        self.assertEqual(configuration.certificates.values["Client Certificate"], stored.secret_ref)

        configuration.device.values["Hostname"] = "edited-host"
        self.service.save(configuration)

        reloaded = self.service.load(mask_secrets=False)
        self.assertEqual(reloaded.certificates.values["Client Certificate"], stored.secret_ref)


if __name__ == "__main__":
    unittest.main()
