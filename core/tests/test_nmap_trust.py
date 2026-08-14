from __future__ import annotations

import hashlib
import ntpath
import unittest

from smart_commissioning_core.engines.ip.nmap_detection import NmapDetectionCandidateV1
from smart_commissioning_core.engines.ip.nmap_profiles import NmapReviewedScriptV1
from smart_commissioning_core.engines.ip.nmap_trust import (
    NmapAuthenticodeEvidenceV1,
    NmapTrustPolicyV1,
    NmapTrustReason,
    NmapTrustResultV1,
    inspect_nmap_installation,
    inspect_nmap_installation_for_administrator_approval,
    revalidate_nmap_installation,
)


class _TrustBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {
            self._key(r"C:\Program Files\Nmap\nmap.exe"): b"signed-nmap-executable",
            self._key(r"C:\Program Files\Nmap\nmap-services"): b"http 80/tcp\nhttps 443/tcp\n",
            self._key(r"C:\Program Files\Nmap\scripts\http-title.nse"): b"description = 'title'\n",
            self._key(r"C:\Program Files\Nmap\LICENSE"): (b"Nmap Public Source License (NPSL) Version 0.95\n"),
        }
        self.directories = {
            self._key(r"C:\Program Files"),
            self._key(r"C:\Program Files\Nmap"),
            self._key(r"C:\Program Files\Nmap\scripts"),
        }
        self.reparse: set[str] = set()
        self.user_writable: set[str] = set()
        self.not_admin_owned: set[str] = set()
        self.publisher = "Insecure.Com LLC"
        self.version = "7.98"
        self.signer = "1" * 64
        self.signature_trusted = True

    @staticmethod
    def _key(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(path))

    def protected_install_roots(self) -> tuple[str, ...]:
        return (r"C:\Program Files", r"C:\Program Files (x86)")

    def canonicalize(self, path: str) -> str:
        return ntpath.normpath(path)

    def is_regular_file(self, path: str) -> bool:
        return self._key(path) in self.files

    def is_directory(self, path: str) -> bool:
        return self._key(path) in self.directories

    def has_reparse_component(self, path: str) -> bool:
        key = self._key(path)
        return any(key == item or key.startswith(item + "\\") for item in self.reparse)

    def is_user_writable(self, path: str) -> bool:
        return self._key(path) in self.user_writable

    def is_administrator_owned(self, path: str) -> bool:
        return self._key(path) not in self.not_admin_owned

    def authenticode(self, _path: str) -> NmapAuthenticodeEvidenceV1:
        return NmapAuthenticodeEvidenceV1(
            trusted=self.signature_trusted,
            publisher=self.publisher,
            signer_sha256=self.signer,
        )

    def file_version(self, _path: str) -> str:
        return self.version

    def iter_files(self, directory: str) -> tuple[str, ...]:
        prefix = self._key(directory) + "\\"
        return tuple(ntpath.normpath(path) for path in sorted(self.files) if path.startswith(prefix))

    def read_file(self, path: str, *, max_bytes: int) -> bytes:
        payload = self.files[self._key(path)]
        if len(payload) > max_bytes:
            raise ValueError("file too large")
        return payload


def _candidate() -> NmapDetectionCandidateV1:
    return NmapDetectionCandidateV1(
        registry_view="64",
        registry_key="Nmap",
        display_name="Nmap 7.98",
        registry_publisher="Insecure.Com LLC",
        version="7.98",
        install_root=r"C:\Program Files\Nmap",
        executable_path=r"C:\Program Files\Nmap\nmap.exe",
        data_directory=r"C:\Program Files\Nmap",
    )


def _policy() -> NmapTrustPolicyV1:
    return NmapTrustPolicyV1(
        permitted_publishers=("Insecure.Com LLC",),
        permitted_versions=("7.98",),
        permitted_signer_sha256=("1" * 64,),
        permitted_executable_sha256=(hashlib.sha256(b"signed-nmap-executable").hexdigest(),),
        permitted_npsl_versions=("0.95",),
    )


class NmapTrustTests(unittest.TestCase):
    def test_trust_result_rejects_every_inconsistent_state(self) -> None:
        trusted = inspect_nmap_installation(
            candidate=_candidate(),
            policy=_policy(),
            backend=_TrustBackend(),
        )
        assert trusted.fingerprint is not None

        with self.assertRaises(ValueError):
            NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.AVAILABLE,
                fingerprint=None,
            )
        with self.assertRaises(ValueError):
            NmapTrustResultV1(
                available=False,
                reason=NmapTrustReason.PATH_REJECTED,
                fingerprint=trusted.fingerprint,
            )

    def test_registry_display_strings_do_not_replace_signed_file_identity(self) -> None:
        candidate = _candidate().model_copy(
            update={
                "registry_publisher": "The Nmap Project",
                "version": "Nmap 7.98 installer",
            }
        )

        result = inspect_nmap_installation(
            candidate=candidate,
            policy=_policy(),
            backend=_TrustBackend(),
        )

        self.assertTrue(result.available)
        self.assertEqual(result.reason, NmapTrustReason.AVAILABLE)
        assert result.fingerprint is not None
        self.assertEqual(result.fingerprint.publisher, "Insecure.Com LLC")
        self.assertEqual(result.fingerprint.version, "7.98")

    def test_confirmation_records_exact_signed_binary_data_and_licence_identity(self) -> None:
        result = inspect_nmap_installation(
            candidate=_candidate(),
            policy=_policy(),
            backend=_TrustBackend(),
        )

        self.assertTrue(result.available)
        self.assertEqual(result.reason, NmapTrustReason.AVAILABLE)
        assert result.fingerprint is not None
        self.assertEqual(result.fingerprint.publisher, "Insecure.Com LLC")
        self.assertEqual(result.fingerprint.version, "7.98")
        self.assertEqual(result.fingerprint.data_file_count, 4)
        self.assertEqual(result.fingerprint.licence_relative_path, "license")
        self.assertEqual(result.fingerprint.npsl_version, "0.95")
        self.assertEqual(len(result.fingerprint.executable_sha256), 64)
        self.assertEqual(len(result.fingerprint.data_manifest_sha256), 64)
        self.assertEqual(len(result.fingerprint.fingerprint_sha256), 64)

    def test_administrator_approval_records_a_signed_local_installation_before_policy_exists(self) -> None:
        result = inspect_nmap_installation_for_administrator_approval(
            candidate=_candidate(),
            backend=_TrustBackend(),
        )

        self.assertTrue(result.available)
        self.assertEqual(result.reason, NmapTrustReason.AVAILABLE)
        assert result.fingerprint is not None
        self.assertEqual(result.fingerprint.publisher, "Insecure.Com LLC")
        self.assertEqual(result.fingerprint.version, "7.98")

    def test_administrator_approval_still_rejects_an_untrusted_or_unsafe_installation(self) -> None:
        cases: tuple[tuple[str, callable, NmapTrustReason], ...] = (
            (
                "user_writable",
                lambda backend: backend.user_writable.add(backend._key(r"C:\Program Files\Nmap")),
                NmapTrustReason.ACL_REJECTED,
            ),
            (
                "untrusted_signature",
                lambda backend: setattr(backend, "signature_trusted", False),
                NmapTrustReason.SIGNATURE_REJECTED,
            ),
            (
                "licence_missing",
                lambda backend: backend.files.__setitem__(
                    backend._key(r"C:\Program Files\Nmap\LICENSE"), b"different licence"
                ),
                NmapTrustReason.LICENCE_REJECTED,
            ),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                backend = _TrustBackend()
                mutate(backend)
                result = inspect_nmap_installation_for_administrator_approval(
                    candidate=_candidate(),
                    backend=backend,
                )
                self.assertFalse(result.available)
                self.assertEqual(result.reason, expected)

    def test_execution_time_revalidation_fails_closed_on_any_identity_drift(self) -> None:
        backend = _TrustBackend()
        initial = inspect_nmap_installation(
            candidate=_candidate(),
            policy=_policy(),
            backend=backend,
        )
        assert initial.fingerprint is not None

        backend.files[backend._key(r"C:\Program Files\Nmap\scripts\http-title.nse")] = b"changed"
        changed = revalidate_nmap_installation(
            candidate=_candidate(),
            confirmed=initial.fingerprint,
            policy=_policy(),
            backend=backend,
        )

        self.assertFalse(changed.available)
        self.assertEqual(changed.reason, NmapTrustReason.FINGERPRINT_DRIFT)
        self.assertIsNone(changed.fingerprint)

    def test_path_acl_signature_publisher_version_hash_and_licence_fail_closed(self) -> None:
        cases: tuple[tuple[str, callable, NmapTrustReason], ...] = (
            (
                "reparse",
                lambda backend: backend.reparse.add(backend._key(r"C:\Program Files\Nmap")),
                NmapTrustReason.REPARSE_REJECTED,
            ),
            (
                "user_writable",
                lambda backend: backend.user_writable.add(backend._key(r"C:\Program Files\Nmap")),
                NmapTrustReason.ACL_REJECTED,
            ),
            (
                "not_admin_owned",
                lambda backend: backend.not_admin_owned.add(backend._key(r"C:\Program Files\Nmap\nmap.exe")),
                NmapTrustReason.ACL_REJECTED,
            ),
            (
                "untrusted_signature",
                lambda backend: setattr(backend, "signature_trusted", False),
                NmapTrustReason.SIGNATURE_REJECTED,
            ),
            (
                "publisher",
                lambda backend: setattr(backend, "publisher", "Unknown Publisher"),
                NmapTrustReason.PUBLISHER_REJECTED,
            ),
            (
                "version",
                lambda backend: setattr(backend, "version", "8.00"),
                NmapTrustReason.VERSION_REJECTED,
            ),
            (
                "executable",
                lambda backend: backend.files.__setitem__(
                    backend._key(r"C:\Program Files\Nmap\nmap.exe"),
                    b"replacement",
                ),
                NmapTrustReason.EXECUTABLE_DIGEST_REJECTED,
            ),
            (
                "licence",
                lambda backend: backend.files.__setitem__(
                    backend._key(r"C:\Program Files\Nmap\LICENSE"),
                    b"different licence",
                ),
                NmapTrustReason.LICENCE_REJECTED,
            ),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                backend = _TrustBackend()
                mutate(backend)
                result = inspect_nmap_installation(
                    candidate=_candidate(),
                    policy=_policy(),
                    backend=backend,
                )
                self.assertFalse(result.available)
                self.assertEqual(result.reason, expected)
                self.assertIsNone(result.fingerprint)

    def test_reviewed_script_digest_is_bound_to_the_confirmed_installation(self) -> None:
        script_payload = b"description = 'title'\n"
        script = NmapReviewedScriptV1(
            name="http-title",
            sha256=hashlib.sha256(script_payload).hexdigest(),
        )
        policy = NmapTrustPolicyV1(
            **_policy().model_dump(exclude={"reviewed_scripts"}),
            reviewed_scripts=(script,),
        )
        backend = _TrustBackend()
        confirmed = inspect_nmap_installation(
            candidate=_candidate(),
            policy=policy,
            backend=backend,
        )
        self.assertTrue(confirmed.available)
        assert confirmed.fingerprint is not None
        self.assertEqual(confirmed.fingerprint.reviewed_scripts, (script,))

        backend.files[backend._key(r"C:\Program Files\Nmap\scripts\http-title.nse")] = b"changed"
        changed = inspect_nmap_installation(
            candidate=_candidate(),
            policy=policy,
            backend=backend,
        )
        self.assertEqual(changed.reason, NmapTrustReason.REVIEWED_SCRIPT_REJECTED)


if __name__ == "__main__":
    unittest.main()
