"""Stable edge identity for the local-first / central-hub architecture.

An *edge* is an on-site instance that produces immutable, signed run+evidence
records. Each edge has a stable identity made of two persisted pieces under a
caller-supplied root directory:

  * ``edge_id`` — a UUID generated once and persisted to ``<root>/edge_id``.
    Re-reading the file on subsequent calls returns the same id, so an edge keeps
    a single identity across restarts.
  * an Ed25519 signing key — persisted (owner-only) at ``<root>/edge_signing_key``
    via :class:`smart_commissioning_core.integrity.SigningKey`. The edge signs
    sync bundles with this key; the hub verifies them against the edge's public
    key fingerprint.

Determinism / testability: every path is a caller-supplied argument, the UUID is
generated only inside :func:`load_or_create_edge_identity` (never at import
scope), and a deterministic ``edge_id`` may be injected for tests. No network,
no clock, no global state.

Honesty note: signing requires the ``cryptography`` package (guarded in
integrity.py). The ``edge_id`` file is always created; the signing key is only
materialised when ``cryptography`` is available. :func:`load_or_create_edge_identity`
returns ``public_key_pem``/``public_key_fingerprint`` as ``None`` when crypto is
absent so the caller can still read a stable ``edge_id``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from smart_commissioning_core.integrity import SigningKey, cryptography_available

__all__ = [
    "DEFAULT_EDGE_ID_FILENAME",
    "DEFAULT_SIGNING_KEY_FILENAME",
    "EdgeIdentity",
    "edge_id_path",
    "edge_signing_key_path",
    "load_edge_signing_key",
    "load_or_create_edge_id",
    "load_or_create_edge_identity",
]

DEFAULT_EDGE_ID_FILENAME = "edge_id"
DEFAULT_SIGNING_KEY_FILENAME = "edge_signing_key"


@dataclass(frozen=True)
class EdgeIdentity:
    """A resolved edge identity: the stable id plus the public verifier material.

    ``public_key_pem`` / ``public_key_fingerprint`` are ``None`` only when the
    ``cryptography`` dependency is unavailable (the id is still stable). The
    private signing key is never exposed here — fetch it on demand via
    :func:`load_edge_signing_key` to sign a bundle.
    """

    edge_id: str
    public_key_pem: str | None
    public_key_fingerprint: str | None

    def as_dict(self) -> dict[str, str | None]:
        """Return the identity as a plain JSON-safe dict (for manifests/APIs)."""
        return {
            "edge_id": self.edge_id,
            "public_key_pem": self.public_key_pem,
            "public_key_fingerprint": self.public_key_fingerprint,
        }


def edge_id_path(root: str | os.PathLike[str], *, filename: str = DEFAULT_EDGE_ID_FILENAME) -> Path:
    """Absolute path of the persisted edge_id file under ``root``."""
    return Path(root) / filename


def edge_signing_key_path(
    root: str | os.PathLike[str], *, filename: str = DEFAULT_SIGNING_KEY_FILENAME
) -> Path:
    """Absolute path of the persisted edge signing key (PEM) under ``root``."""
    return Path(root) / filename


def load_or_create_edge_id(
    root: str | os.PathLike[str],
    *,
    filename: str = DEFAULT_EDGE_ID_FILENAME,
    edge_id: str | None = None,
) -> str:
    """Load the persisted edge id under ``root``, creating it once if absent.

    Idempotent: subsequent calls return the same id. ``edge_id`` lets a caller
    (tests / provisioning) pin a deterministic id; it is only used when the file
    does not yet exist — once persisted, the on-disk value wins so an explicit
    override can never silently rewrite an established identity.

    The UUID is generated here (only when no file and no override is supplied),
    never at module import scope.
    """
    path = edge_id_path(root, filename=filename)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    value = (edge_id or str(uuid.uuid4())).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def load_edge_signing_key(
    root: str | os.PathLike[str],
    *,
    filename: str = DEFAULT_SIGNING_KEY_FILENAME,
) -> SigningKey:
    """Load (or create on first use) the edge's Ed25519 signing key under ``root``.

    Thin wrapper over :meth:`SigningKey.load_or_create` so the key path is
    derived consistently from the identity root. Raises
    :class:`smart_commissioning_core.integrity.IntegrityUnavailableError` when
    ``cryptography`` is unavailable.
    """
    return SigningKey.load_or_create(edge_signing_key_path(root, filename=filename))


def load_or_create_edge_identity(
    root: str | os.PathLike[str],
    *,
    edge_id: str | None = None,
    id_filename: str = DEFAULT_EDGE_ID_FILENAME,
    key_filename: str = DEFAULT_SIGNING_KEY_FILENAME,
) -> EdgeIdentity:
    """Resolve (creating once) the full edge identity rooted at ``root``.

    Returns an :class:`EdgeIdentity` with the stable ``edge_id`` and, when
    ``cryptography`` is available, the public key PEM + fingerprint of the edge's
    persisted signing key. The signing key itself is created on disk as a side
    effect (owner-only) so a subsequent :func:`load_edge_signing_key` reuses it.

    Deterministic given the inputs: pass ``edge_id`` to pin the id and a stable
    ``root`` to pin the key path.
    """
    resolved_id = load_or_create_edge_id(root, filename=id_filename, edge_id=edge_id)
    if not cryptography_available():
        return EdgeIdentity(edge_id=resolved_id, public_key_pem=None, public_key_fingerprint=None)
    key = load_edge_signing_key(root, filename=key_filename)
    return EdgeIdentity(
        edge_id=resolved_id,
        public_key_pem=key.public_key_pem(),
        public_key_fingerprint=key.public_key_fingerprint(),
    )
