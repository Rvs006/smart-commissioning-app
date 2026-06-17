"""Unit tests for secret:// cert materialization on the live MQTT TLS path.

HONESTY: there is NO real MQTT broker and NO real TLS handshake here. These
tests drive the SSLContext build path of MqttClient with a FAKE SSLContext (so
no certificate is ever parsed by OpenSSL) and a FAKE socket (so no socket is
ever wrapped or connected). They assert that:

* a registered secret resolver IS consulted for secret:// cert references;
* a secret:// CA is loaded IN MEMORY via load_verify_locations(cadata=...);
* a secret:// client cert/key is materialized to a temp file that EXISTS at the
  moment load_cert_chain is called, and is cleaned up afterwards;
* plain filesystem paths keep today's path-based behavior and never hit the
  resolver.

The real TLS handshake against a live broker is on-site-untested and listed in
the task's live_untested output.
"""

import os
import ssl
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from smart_commissioning_core import mqtt_transport
from smart_commissioning_core.mqtt_transport import (
    MqttClient,
    MqttConnectionSettings,
    set_secret_resolver,
)

CA_PEM = b"-----BEGIN CERTIFICATE-----\nfake-ca\n-----END CERTIFICATE-----\n"
CLIENT_PEM = b"-----BEGIN CERTIFICATE-----\nfake-client\n-----END CERTIFICATE-----\n"
KEY_PEM = b"-----BEGIN PRIVATE KEY-----\nfake-key\n-----END PRIVATE KEY-----\n"


class FakeSSLContext:
    """Records what cert material was loaded; never parses anything."""

    def __init__(self) -> None:
        self.cadata: str | None = None
        self.cafile: object = "UNSET"
        self.cert_chain: dict[str, Any] | None = None
        # cert chain files must EXIST when load_cert_chain is called.
        self.cert_chain_existed: dict[str, bool] = {}

    def load_verify_locations(self, *, cadata: str | None = None) -> None:
        self.cadata = cadata

    def load_cert_chain(self, *, certfile: str, keyfile: str | None = None) -> None:
        self.cert_chain = {"certfile": certfile, "keyfile": keyfile}
        self.cert_chain_existed = {
            "certfile": bool(certfile) and Path(certfile).is_file(),
            "keyfile": keyfile is None or Path(keyfile).is_file(),
        }

    def wrap_socket(self, raw_socket: object, *, server_hostname: str | None = None) -> object:
        # Return a fake TLS socket; never touch a real socket.
        return FakeSocket(server_hostname=server_hostname, wrapped=True)


class FakeSocket:
    def __init__(self, *, server_hostname: str | None = None, wrapped: bool = False) -> None:
        self.server_hostname = server_hostname
        self.wrapped = wrapped

    def settimeout(self, _t: float) -> None:  # pragma: no cover - trivial
        pass

    def sendall(self, _data: bytes) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _settings(**overrides: Any) -> MqttConnectionSettings:
    base = dict(host="broker.test", port=8883, client_id="test-client", use_tls=True)
    base.update(overrides)
    return MqttConnectionSettings(**base)


class SecretResolverTlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_context = FakeSSLContext()
        ctx_patcher = mock.patch.object(
            ssl, "create_default_context", return_value=self.fake_context
        )
        self._create_ctx = ctx_patcher.start()
        self.addCleanup(ctx_patcher.stop)
        # Always reset the global resolver after each test.
        self.addCleanup(set_secret_resolver, None)

    def _client(self, settings: MqttConnectionSettings) -> MqttClient:
        # A socket_factory that returns a fake socket so __enter__ never connects.
        return MqttClient(settings, socket_factory=lambda _addr, _t: FakeSocket())

    def test_resolver_resolves_secret_ref_to_bytes(self) -> None:
        """The resolver hook returns DECRYPTED bytes for a secret:// ref."""
        seen: list[str] = []

        def resolver(ref: str) -> bytes | None:
            seen.append(ref)
            return {"secret://ca": CA_PEM}.get(ref)

        set_secret_resolver(resolver)
        material = mqtt_transport._resolve_secret_material("secret://ca")
        self.assertEqual(material, CA_PEM)
        self.assertEqual(seen, ["secret://ca"])

    def test_secret_ca_loaded_in_memory_via_cadata(self) -> None:
        calls: list[str] = []

        def resolver(ref: str) -> bytes | None:
            calls.append(ref)
            return CA_PEM if ref == "secret://ca-cert" else None

        set_secret_resolver(resolver)
        client = self._client(_settings(ca_certificate="secret://ca-cert"))
        wrapped = client._wrap_tls(FakeSocket())

        # Resolver consulted for the CA ref; CA loaded in memory, no temp file.
        self.assertIn("secret://ca-cert", calls)
        self.assertEqual(self.fake_context.cadata, CA_PEM.decode("utf-8"))
        self.assertIsNone(self.fake_context.cert_chain)
        self.assertEqual(client._temp_cert_files, [])
        self.assertTrue(wrapped.wrapped)
        self.assertEqual(wrapped.server_hostname, "broker.test")

    def test_secret_client_cert_materialized_to_existing_temp_file(self) -> None:
        observed_paths: dict[str, str] = {}

        def resolver(ref: str) -> bytes | None:
            return {
                "secret://client-cert": CLIENT_PEM,
                "secret://client-key": KEY_PEM,
            }.get(ref)

        set_secret_resolver(resolver)
        settings = _settings(
            ca_certificate="secret://ca-cert-x",
            client_certificate="secret://client-cert",
            private_key="secret://client-key",
        )
        # Resolver returns None for the CA ref here -> create_default_context()
        # with no cadata, which is fine.

        def recording_resolver(ref: str) -> bytes | None:
            value = resolver(ref)
            if value is not None:
                observed_paths.setdefault(ref, "resolved")
            return value

        set_secret_resolver(recording_resolver)

        client = self._client(settings)
        # Capture temp-file existence AT load_cert_chain time before cleanup.
        client._wrap_tls(FakeSocket())

        self.assertIsNotNone(self.fake_context.cert_chain)
        # certfile + keyfile were real, existing temp files when loaded.
        self.assertTrue(self.fake_context.cert_chain_existed["certfile"])
        self.assertTrue(self.fake_context.cert_chain_existed["keyfile"])
        # Two temp files were tracked for cleanup (cert + key).
        self.assertEqual(len(client._temp_cert_files), 2)

    def test_temp_files_cleaned_up_on_context_exit(self) -> None:
        def resolver(ref: str) -> bytes | None:
            return {
                "secret://cc": CLIENT_PEM,
                "secret://ck": KEY_PEM,
            }.get(ref)

        set_secret_resolver(resolver)
        settings = _settings(client_certificate="secret://cc", private_key="secret://ck")
        client = self._client(settings)
        with mock.patch.object(MqttClient, "_connect", lambda _self: None):
            with client:
                # Inside the context, the temp files have already been cleaned
                # up by __enter__ (wrap_socket consumed them).
                self.assertEqual(client._temp_cert_files, [])
        # After exit, still no leftover temp files tracked.
        self.assertEqual(client._temp_cert_files, [])

    def test_plain_path_certs_do_not_consult_resolver(self) -> None:
        calls: list[str] = []
        set_secret_resolver(lambda ref: calls.append(ref) or None)

        with tempfile.TemporaryDirectory() as tmp:
            ca_path = Path(tmp) / "ca.pem"
            cert_path = Path(tmp) / "client.pem"
            key_path = Path(tmp) / "client.key"
            ca_path.write_bytes(CA_PEM)
            cert_path.write_bytes(CLIENT_PEM)
            key_path.write_bytes(KEY_PEM)

            settings = _settings(
                ca_certificate=str(ca_path),
                client_certificate=str(cert_path),
                private_key=str(key_path),
            )
            client = self._client(settings)
            client._wrap_tls(FakeSocket())

        # Plain paths must NOT reach the resolver.
        self.assertEqual(calls, [])
        # CA passed as cafile (path), not cadata.
        self.assertIsNone(self.fake_context.cadata)
        self.assertEqual(self.fake_context.cafile, "UNSET")  # we patched create_default_context
        # Cert chain loaded by the real plain paths.
        self.assertEqual(self.fake_context.cert_chain["certfile"], str(cert_path))
        self.assertEqual(self.fake_context.cert_chain["keyfile"], str(key_path))
        self.assertEqual(client._temp_cert_files, [])

    def test_resolver_failure_degrades_to_no_material(self) -> None:
        """A raising resolver must not abort context build with a leak."""

        def boom_resolver(_ref: str) -> bytes | None:
            raise RuntimeError("password=hunter2 inside resolver")

        set_secret_resolver(boom_resolver)
        client = self._client(_settings(ca_certificate="secret://ca", client_certificate="secret://cc"))
        # Must not raise; no cert chain loaded since material could not resolve.
        client._wrap_tls(FakeSocket())
        self.assertIsNone(self.fake_context.cadata)
        self.assertIsNone(self.fake_context.cert_chain)
        self.assertEqual(client._temp_cert_files, [])

    def test_no_resolver_registered_resolves_to_nothing(self) -> None:
        set_secret_resolver(None)
        self.assertIsNone(mqtt_transport._resolve_secret_material("secret://anything"))

    def test_materialized_temp_file_is_owner_only_on_posix(self) -> None:
        if os.name != "posix":
            self.skipTest("0600 mode bits only meaningful on POSIX")
        client = self._client(_settings())
        path = client._materialize_temp_cert(CLIENT_PEM)
        try:
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            self.assertEqual(Path(path).read_bytes(), CLIENT_PEM)
        finally:
            client._cleanup_temp_cert_files()
        self.assertFalse(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
