#!/usr/bin/env python3
"""Tests for hosted API and worker mTLS reference parity."""

from __future__ import annotations

import hashlib
import unittest

from check_hosted_mtls import REFERENCES, build_context, build_lease, verify_resolution


class HostedMtlsChecks(unittest.TestCase):
    def test_resolves_every_certificate_as_the_same_opaque_reference(self) -> None:
        material = {
            reference: f"fixture-{field}".encode()
            for field, reference in REFERENCES.items()
        }
        digests = {
            reference: hashlib.sha256(value).hexdigest()
            for reference, value in material.items()
        }
        context = build_context()
        verify_resolution(
            context,
            build_lease(context, owner_token="test-owner"),
            digests=digests,
            resolver=material.get,
            deployment_id="test-deployment",
        )

    def test_rejects_material_that_differs_between_resolvers(self) -> None:
        context = build_context()
        digests = {reference: "0" * 64 for reference in REFERENCES.values()}
        with self.assertRaisesRegex(AssertionError, "digest changed"):
            verify_resolution(
                context,
                build_lease(context, owner_token="test-owner"),
                digests=digests,
                resolver=lambda _reference: b"different",
                deployment_id="test-deployment",
            )


if __name__ == "__main__":
    unittest.main()
