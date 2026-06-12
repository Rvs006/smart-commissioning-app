"""Evidence integrity primitives: hashing + Ed25519 detached signing/verification.

Pure and dependency-light: the only third-party requirement is ``cryptography``
(already a backend dependency). To keep this module importable on installs that
do not ship ``cryptography`` (the core package itself does not declare it), the
import is guarded — :func:`sha256_bytes` always works, and the signing helpers
raise a clear :class:`IntegrityUnavailableError` only when actually called
without the dependency present.

Signatures are *detached*: :func:`sign_bytes` returns the raw 64-byte Ed25519
signature over the artifact, never the artifact itself. A verifier (a hub or an
auditor) checks an artifact with :func:`verify_bytes`, given the artifact bytes,
the detached signature, and the exported public key.

The signing key is persisted with the same owner-only (0600) file pattern the
backend secret store uses, created lazily on first use under a caller-supplied
key path. Private key material never leaves disk except through the explicit
:class:`SigningKey` API.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

try:  # pragma: no cover - exercised indirectly by the availability guard test
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - only on installs without cryptography
    _CRYPTOGRAPHY_AVAILABLE = False


__all__ = [
    "IntegrityUnavailableError",
    "SigningKey",
    "cryptography_available",
    "export_public_key_pem",
    "public_key_fingerprint",
    "sha256_bytes",
    "sign_bytes",
    "verify_bytes",
]


class IntegrityUnavailableError(RuntimeError):
    """Raised when a signing/verification helper is used without ``cryptography``."""


def cryptography_available() -> bool:
    """True when the ``cryptography`` dependency is importable.

    Lets callers (and the API) degrade gracefully: hashing still works, signing
    is skipped, and the verify endpoint reports ``signature_valid=None`` rather
    than crashing.
    """
    return _CRYPTOGRAPHY_AVAILABLE


def _require_cryptography() -> None:
    if not _CRYPTOGRAPHY_AVAILABLE:  # pragma: no cover - simple guard
        raise IntegrityUnavailableError(
            "The 'cryptography' package is required for signing/verification."
        )


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``data`` (deterministic, pure)."""
    return sha256(data).hexdigest()


def _write_private_file(path: Path, content: bytes) -> None:
    """Write bytes owner-only (0o600), mirroring the backend secret store.

    On Windows the POSIX mode only maps onto the read-only attribute, so this is
    best-effort there; real isolation comes from the ACL on the key directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX best effort
        pass


class SigningKey:
    """An Ed25519 signing identity persisted at a key path, created on first use.

    The private key is stored as an unencrypted PKCS#8 PEM with 0600 permissions
    under ``key_path`` (the caller chooses the location — e.g. under the secrets
    root). Use :meth:`load_or_create` for the lazy create-on-first-use flow.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    # -- construction ---------------------------------------------------------

    @classmethod
    def generate(cls) -> SigningKey:
        """Return a fresh in-memory signing key (not persisted)."""
        _require_cryptography()
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, key_path: str | os.PathLike[str]) -> SigningKey:
        """Load the signing key from ``key_path``, generating + persisting it once.

        Idempotent: subsequent calls return the same key. The PEM is written
        owner-only the first time so the private key is never world-readable.
        """
        _require_cryptography()
        path = Path(key_path)
        if path.exists():
            return cls.from_pem_bytes(path.read_bytes())
        key = cls.generate()
        _write_private_file(path, key.private_pem_bytes())
        return key

    @classmethod
    def from_pem_bytes(cls, pem: bytes) -> SigningKey:
        """Reconstruct a signing key from a PKCS#8 PEM byte string."""
        _require_cryptography()
        private_key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise IntegrityUnavailableError("Key file is not an Ed25519 private key.")
        return cls(private_key)

    # -- serialization --------------------------------------------------------

    def private_pem_bytes(self) -> bytes:
        """Serialize the private key as an unencrypted PKCS#8 PEM."""
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def public_key_bytes(self) -> bytes:
        """Return the 32 raw public key bytes."""
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_pem(self) -> str:
        """Export the public key as a SubjectPublicKeyInfo PEM string.

        This is what a hub/verifier needs to check signatures produced by this
        key without ever seeing the private material.
        """
        return export_public_key_pem(self.public_key_bytes())

    def public_key_fingerprint(self) -> str:
        """Short, stable fingerprint of the public key (SHA-256 of raw bytes)."""
        return public_key_fingerprint(self.public_key_bytes())

    # -- operations -----------------------------------------------------------

    def sign(self, data: bytes) -> bytes:
        """Return the detached 64-byte Ed25519 signature over ``data``."""
        return self._private_key.sign(data)


def sign_bytes(data: bytes, signing_key: SigningKey) -> bytes:
    """Sign ``data`` with ``signing_key``, returning the detached signature."""
    _require_cryptography()
    return signing_key.sign(data)


def verify_bytes(data: bytes, signature: bytes, public_key: bytes | str) -> bool:
    """Verify a detached ``signature`` over ``data`` against ``public_key``.

    ``public_key`` may be the 32 raw public key bytes or a PEM string/bytes as
    produced by :func:`export_public_key_pem`. Returns False (never raises) for
    a bad signature, the wrong key, or tampered data, so callers can branch on
    the boolean rather than catching exceptions.
    """
    _require_cryptography()
    try:
        loaded = _load_public_key(public_key)
        loaded.verify(signature, data)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def export_public_key_pem(public_key: bytes) -> str:
    """Return the SubjectPublicKeyInfo PEM for 32 raw Ed25519 public key bytes."""
    _require_cryptography()
    loaded = Ed25519PublicKey.from_public_bytes(public_key)
    return loaded.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def public_key_fingerprint(public_key: bytes) -> str:
    """Return a short fingerprint (first 16 hex chars of SHA-256) for a key."""
    return sha256(public_key).hexdigest()[:16]


def _load_public_key(public_key: bytes | str) -> Ed25519PublicKey:
    """Coerce raw bytes or a PEM (str/bytes) into an Ed25519PublicKey."""
    if isinstance(public_key, str):
        public_key = public_key.encode("ascii")
    # Raw Ed25519 public keys are exactly 32 bytes; anything else is a PEM/DER.
    if isinstance(public_key, (bytes, bytearray)) and len(public_key) == 32:
        return Ed25519PublicKey.from_public_bytes(bytes(public_key))
    loaded = serialization.load_pem_public_key(bytes(public_key))
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("Public key is not an Ed25519 key.")
    return loaded
