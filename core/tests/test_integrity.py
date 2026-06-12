"""Unit tests for smart_commissioning_core.integrity.

Pure in-process tests: no infra. Covers SHA-256 determinism, the sign/verify
roundtrip, tamper detection (mutated data or signature), wrong-key rejection,
key persistence (create-once, 0600), and public-key export/fingerprint.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from smart_commissioning_core.integrity import (
    SigningKey,
    cryptography_available,
    export_public_key_pem,
    public_key_fingerprint,
    sha256_bytes,
    sign_bytes,
    verify_bytes,
)


class Sha256Tests(unittest.TestCase):
    def test_sha256_is_deterministic_and_matches_known_vector(self) -> None:
        # SHA-256 of b"" is the well-known empty-input digest.
        self.assertEqual(
            sha256_bytes(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(sha256_bytes(b"abc"), sha256_bytes(b"abc"))
        self.assertNotEqual(sha256_bytes(b"abc"), sha256_bytes(b"abd"))

    def test_sha256_returns_64_hex_chars(self) -> None:
        digest = sha256_bytes(b"some-evidence-bytes")
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))


@unittest.skipUnless(cryptography_available(), "cryptography not installed")
class SignVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = SigningKey.generate()
        self.public_key = self.key.public_key_bytes()
        self.data = b"evidence-pack-artifact-bytes-v1"

    def test_sign_then_verify_roundtrip(self) -> None:
        signature = sign_bytes(self.data, self.key)
        self.assertTrue(verify_bytes(self.data, signature, self.public_key))
        # Detached: a 64-byte Ed25519 signature, never the artifact itself.
        self.assertEqual(len(signature), 64)
        self.assertNotIn(self.data, signature)

    def test_tampered_data_fails_verification(self) -> None:
        signature = sign_bytes(self.data, self.key)
        self.assertFalse(verify_bytes(self.data + b"!", signature, self.public_key))

    def test_tampered_signature_fails_verification(self) -> None:
        signature = bytearray(sign_bytes(self.data, self.key))
        signature[0] ^= 0xFF
        self.assertFalse(verify_bytes(self.data, bytes(signature), self.public_key))

    def test_wrong_key_is_rejected(self) -> None:
        signature = sign_bytes(self.data, self.key)
        other_key = SigningKey.generate()
        self.assertFalse(verify_bytes(self.data, signature, other_key.public_key_bytes()))

    def test_verify_accepts_pem_public_key(self) -> None:
        signature = sign_bytes(self.data, self.key)
        pem = export_public_key_pem(self.public_key)
        self.assertIn("BEGIN PUBLIC KEY", pem)
        self.assertTrue(verify_bytes(self.data, signature, pem))

    def test_fingerprint_is_stable_and_distinguishes_keys(self) -> None:
        fingerprint = public_key_fingerprint(self.public_key)
        self.assertEqual(fingerprint, self.key.public_key_fingerprint())
        self.assertEqual(len(fingerprint), 16)
        other = SigningKey.generate()
        self.assertNotEqual(fingerprint, other.public_key_fingerprint())


@unittest.skipUnless(cryptography_available(), "cryptography not installed")
class KeyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.key_path = Path(self._temp.name) / "nested" / "signing_key.pem"

    def test_load_or_create_persists_and_reuses_same_key(self) -> None:
        first = SigningKey.load_or_create(self.key_path)
        self.assertTrue(self.key_path.exists())
        second = SigningKey.load_or_create(self.key_path)
        self.assertEqual(first.public_key_bytes(), second.public_key_bytes())
        # The same artifact, signed by either handle, verifies under the key.
        data = b"persisted-key-data"
        self.assertTrue(verify_bytes(data, second.sign(data), first.public_key_bytes()))

    def test_persisted_key_file_is_owner_only(self) -> None:
        SigningKey.load_or_create(self.key_path)
        if sys.platform.startswith("win"):
            self.skipTest("POSIX mode bits are not enforced on Windows")
        mode = self.key_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_persisted_key_file_holds_a_private_pem(self) -> None:
        SigningKey.load_or_create(self.key_path)
        contents = self.key_path.read_bytes()
        self.assertIn(b"PRIVATE KEY", contents)


if __name__ == "__main__":
    unittest.main()
