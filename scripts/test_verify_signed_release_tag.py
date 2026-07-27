#!/usr/bin/env python3
"""Tests for the fail-closed GitHub annotated-tag verifier."""

from __future__ import annotations

import unittest

from verify_signed_release_tag import ReleaseTagError, validate_release_tag

VERSION = "v0.1.28"
COMMIT = "a" * 40
TAG_SHA = "b" * 40


def _valid_payloads() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {"ref": f"refs/tags/{VERSION}", "object": {"type": "tag", "sha": TAG_SHA}},
        {
            "sha": TAG_SHA,
            "tag": VERSION,
            "object": {"type": "commit", "sha": COMMIT},
            "verification": {"verified": True, "reason": "valid"},
        },
    )


class SignedReleaseTagTests(unittest.TestCase):
    def test_accepts_verified_annotated_commit_tag(self) -> None:
        ref_payload, tag_payload = _valid_payloads()
        validate_release_tag(
            ref_payload=ref_payload,
            tag_payload=tag_payload,
            version=VERSION,
            expected_commit=COMMIT,
        )

    def test_rejects_lightweight_tag(self) -> None:
        ref_payload, tag_payload = _valid_payloads()
        ref_payload["object"] = {"type": "commit", "sha": COMMIT}
        with self.assertRaisesRegex(ReleaseTagError, "annotated tag"):
            validate_release_tag(
                ref_payload=ref_payload,
                tag_payload=tag_payload,
                version=VERSION,
                expected_commit=COMMIT,
            )

    def test_rejects_wrong_name_or_commit(self) -> None:
        ref_payload, tag_payload = _valid_payloads()
        tag_payload["tag"] = "v0.1.27"
        with self.assertRaisesRegex(ReleaseTagError, "name"):
            validate_release_tag(
                ref_payload=ref_payload,
                tag_payload=tag_payload,
                version=VERSION,
                expected_commit=COMMIT,
            )
        _, tag_payload = _valid_payloads()
        tag_payload["object"] = {"type": "commit", "sha": "c" * 40}
        with self.assertRaisesRegex(ReleaseTagError, "RELEASE_SHA"):
            validate_release_tag(
                ref_payload=ref_payload,
                tag_payload=tag_payload,
                version=VERSION,
                expected_commit=COMMIT,
            )

    def test_rejects_unverified_signature(self) -> None:
        ref_payload, tag_payload = _valid_payloads()
        tag_payload["verification"] = {"verified": False, "reason": "unsigned"}
        with self.assertRaisesRegex(ReleaseTagError, "not verified"):
            validate_release_tag(
                ref_payload=ref_payload,
                tag_payload=tag_payload,
                version=VERSION,
                expected_commit=COMMIT,
            )


if __name__ == "__main__":
    unittest.main()
