"""Unit tests for engine_dispatch.resolve_ip_enrichment (reverse-DNS default).

HONESTY: no sockets, no DNS, no ARP. This exercises the pure parameter-defaulting
helper that makes an authorized, non-dry-run IP sweep resolve hostnames (reverse
DNS) best-effort, while leaving dry-run previews and an explicit operator opt-out
untouched. A blank hostname (no PTR) / blank MAC (no ARP entry) is honest and is
produced by the engine, not fabricated here.
"""

import unittest

from app.services.engine_dispatch import resolve_ip_enrichment


class ResolveIpEnrichmentTests(unittest.TestCase):
    def test_authorized_real_run_defaults_reverse_dns_on(self) -> None:
        # A real (non-dry-run) sweep resolves hostnames best-effort by default.
        parameters: dict = {"authorized": True}
        resolve_ip_enrichment(parameters)
        self.assertTrue(parameters["reverse_dns"])

    def test_dry_run_is_left_untouched(self) -> None:
        # Dry-run previews stay side-effect-free: no reverse_dns key is injected,
        # so the plan only advertises 'reverse-dns' if the operator opted in.
        for value in (True, "true", "1", "yes", "on"):
            parameters: dict = {"dry_run": value}
            resolve_ip_enrichment(parameters)
            self.assertNotIn("reverse_dns", parameters)

    def test_explicit_reverse_dns_false_override_is_preserved(self) -> None:
        # An operator opt-out wins (setdefault no-op).
        parameters: dict = {"authorized": True, "reverse_dns": False}
        resolve_ip_enrichment(parameters)
        self.assertFalse(parameters["reverse_dns"])

    def test_explicit_reverse_dns_true_is_preserved(self) -> None:
        parameters: dict = {"authorized": True, "reverse_dns": True}
        resolve_ip_enrichment(parameters)
        self.assertTrue(parameters["reverse_dns"])

    def test_no_other_parameters_are_added(self) -> None:
        # The helper only defaults reverse_dns; MAC enrichment needs no parameter
        # (the engine reads the OS ARP cache unconditionally per responsive host).
        parameters: dict = {"authorized": True}
        resolve_ip_enrichment(parameters)
        self.assertEqual(parameters, {"authorized": True, "reverse_dns": True})


if __name__ == "__main__":
    unittest.main()
