"""Backend wiring of the live-MQTT secret:// cert resolver.

HONESTY: no real broker / no real TLS handshake. This exercises the resolver
the backend registers with the core MQTT transport against a REAL encrypted
secret store (tmp secrets root + tmp SQLite), asserting a stored secret://
reference round-trips to its DECRYPTED bytes through the core hook. The live
SSLContext build is unit-tested in core; the real handshake is on-site-untested.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.schemas.configuration import SecretMaterialRequest
from app.services import _resolve_secret
from app.services import configuration_service as configuration_service_module
from app.services.configuration_service import ConfigurationService
from smart_commissioning_core import mqtt_transport
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url

PEM_CONTENT = "-----BEGIN CERTIFICATE-----\nclient-material-abc123\n-----END CERTIFICATE-----"


class SecretResolverWiringTests(unittest.TestCase):
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

    def test_resolver_returns_decrypted_bytes_for_stored_secret(self) -> None:
        response = self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=PEM_CONTENT)
        )
        # Stored on disk encrypted; the backend resolver decrypts to real bytes.
        resolved = _resolve_secret(response.secret_ref)
        self.assertEqual(resolved, PEM_CONTENT.encode("utf-8"))

    def test_resolver_returns_none_for_non_secret_ref(self) -> None:
        self.assertIsNone(_resolve_secret("/etc/ssl/ca.pem"))
        self.assertIsNone(_resolve_secret(""))

    def test_resolver_returns_none_for_missing_secret_without_raising(self) -> None:
        # A reference that points at no file must degrade to None, never raise.
        self.assertIsNone(_resolve_secret("secret://does-not-exist"))

    def test_core_hook_consults_backend_resolver(self) -> None:
        """The core transport's _resolve_secret_material uses the backend hook."""
        response = self.service.store_secret(
            SecretMaterialRequest(field="Client Certificate", file_name="client.pem", content=PEM_CONTENT)
        )
        # Register exactly what app.services registers at import.
        mqtt_transport.set_secret_resolver(_resolve_secret)
        self.addCleanup(mqtt_transport.set_secret_resolver, None)
        material = mqtt_transport._resolve_secret_material(response.secret_ref)
        self.assertEqual(material, PEM_CONTENT.encode("utf-8"))


class ImportRegistersResolverTests(unittest.TestCase):
    def test_importing_services_registers_the_resolver(self) -> None:
        # Importing the package wires both hooks. app.services may already be
        # imported (cached) and a sibling test's cleanup may have reset the
        # global, so reload to re-run the module-level set_secret_resolver call.
        import importlib

        import app.services

        importlib.reload(app.services)
        self.assertIsNotNone(mqtt_transport._secret_resolver)
        self.addCleanup(mqtt_transport.set_secret_resolver, None)


if __name__ == "__main__":
    unittest.main()
