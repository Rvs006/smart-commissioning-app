"""Worker MQTT config provider: mutual-TLS secret resolution + param fallback.

HONESTY: no real broker / no real TLS handshake. These tests drive the worker's
secret resolver against a FAKE/tmp secret store that mimics the backend's
on-disk layout (a Fernet-encrypted ``<name>.pem`` + a shared ``.secret_store_key``
under a tmp ``SMART_COMMISSIONING_SECRETS_ROOT``). They assert:

* when the shared secret store IS reachable, the worker resolves a secret://
  reference to its DECRYPTED bytes (so worker mutual-TLS is possible, not
  silently empty), and surfaces stored certificate references as defaults;
* when the store is NOT reachable, the resolver returns None and the provider
  surfaces NO certificate defaults (cert material then comes from run
  parameters) — the documented limitation.

Run explicitly (the worker has no packaged test suite):
    python -m unittest discover -s worker/tests  (with worker on sys.path)
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make the worker package importable when run from the repo root.
_WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))

from cryptography.fernet import Fernet  # noqa: E402

from app import mqtt_config_provider as provider  # noqa: E402

PEM_CONTENT = b"-----BEGIN CERTIFICATE-----\nworker-client-xyz\n-----END CERTIFICATE-----"
KEY_FILE = ".secret_store_key"


class WorkerSecretStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.secrets_root = Path(temp_dir.name) / "secrets"
        self.secrets_root.mkdir(parents=True, exist_ok=True)

        env_patcher = mock.patch.dict(
            os.environ, {"SMART_COMMISSIONING_SECRETS_ROOT": str(self.secrets_root)}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _write_key(self) -> bytes:
        key = Fernet.generate_key()
        (self.secrets_root / KEY_FILE).write_bytes(key)
        return key

    def _write_secret(self, name: str, content: bytes, key: bytes) -> str:
        token = Fernet(key).encrypt(content)
        (self.secrets_root / f"{name}.pem").write_bytes(token)
        return f"secret://{name}"


class ReachableStoreTests(WorkerSecretStoreTestCase):
    def test_resolves_secret_to_decrypted_bytes(self) -> None:
        key = self._write_key()
        ref = self._write_secret("client-cert-1", PEM_CONTENT, key)

        self.assertTrue(provider._secret_store_reachable())
        self.assertEqual(provider._resolve_secret(ref), PEM_CONTENT)

    def test_legacy_plaintext_secret_is_readable(self) -> None:
        self._write_key()  # key present so store is reachable
        (self.secrets_root / "legacy.pem").write_bytes(PEM_CONTENT)  # NOT encrypted

        self.assertEqual(provider._resolve_secret("secret://legacy"), PEM_CONTENT)

    def test_traversal_reference_rejected(self) -> None:
        self._write_key()
        self.assertIsNone(provider._resolve_secret("secret://../escape"))

    def test_certificate_values_surfaced_when_reachable(self) -> None:
        self._write_key()
        cert_refs = {
            "CA Certificate": "secret://bootstrap-ca",
            "Client Certificate": "secret://bootstrap-client",
            "Private Key": "secret://bootstrap-key",
        }
        payload = {"certificates": {"values": cert_refs}, "mqtt": {"values": {"Port": "8883"}}}
        with mock.patch(
            "smart_commissioning_core.db.repositories.ConfigurationRepository"
        ) as repo_cls, mock.patch("app.db.get_engine"):
            repo_cls.return_value.get_current.return_value = payload
            mqtt_values, certificate_values = provider._configuration_values()

        self.assertEqual(certificate_values, cert_refs)
        self.assertEqual(mqtt_values.get("Port"), "8883")


class UnreachableStoreTests(WorkerSecretStoreTestCase):
    def test_no_key_file_means_unreachable(self) -> None:
        # secrets dir exists but no .secret_store_key => cannot decrypt anything.
        self.assertFalse(provider._secret_store_reachable())
        self.assertIsNone(provider._resolve_secret("secret://anything"))

    def test_certificate_values_empty_when_unreachable(self) -> None:
        payload = {
            "certificates": {"values": {"CA Certificate": "secret://x"}},
            "mqtt": {"values": {"Port": "8883"}},
        }
        with mock.patch(
            "smart_commissioning_core.db.repositories.ConfigurationRepository"
        ) as repo_cls, mock.patch("app.db.get_engine"):
            repo_cls.return_value.get_current.return_value = payload
            mqtt_values, certificate_values = provider._configuration_values()

        # No shared store => no cert defaults advertised (run params must carry
        # cert material). MQTT values still resolve normally.
        self.assertEqual(certificate_values, {})
        self.assertEqual(mqtt_values.get("Port"), "8883")

    def test_resolver_registered_self_gates_on_reachability(self) -> None:
        from smart_commissioning_core import mqtt_transport

        provider.register_worker_mqtt_configuration_provider()
        self.addCleanup(mqtt_transport.set_secret_resolver, None)
        # Resolver IS registered, but returns None because the store is unreachable.
        self.assertIsNotNone(mqtt_transport._secret_resolver)
        self.assertIsNone(mqtt_transport._resolve_secret_material("secret://x"))


if __name__ == "__main__":
    unittest.main()
