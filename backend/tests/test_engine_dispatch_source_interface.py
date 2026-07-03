"""Unit tests for engine_dispatch.resolve_source_interface (source-NIC selection).

HONESTY: no sockets, no network. This exercises the pure parameter-injection
resolver that maps the configured device."Source Interface" value into the run
parameters the active-scan engines read (source_ip for IP/MQTT, local_address for
BACnet). Whether binding those actually forces egress out a chosen physical NIC
is on-site-validation surface (see the proposal, section 8.3).
"""

import unittest

from app.services.engine_dispatch import resolve_source_interface


class ResolveSourceInterfaceTests(unittest.TestCase):
    def test_auto_is_a_no_op(self) -> None:
        # The literal "Auto (OS default route)" (any case) binds nothing.
        parameters: dict = {}
        resolve_source_interface(parameters, "Auto (OS default route)")
        self.assertEqual(parameters, {})
        resolve_source_interface(parameters, "auto (os default route)")
        self.assertEqual(parameters, {})

    def test_empty_and_none_are_no_ops(self) -> None:
        for value in (None, "", "   "):
            parameters: dict = {}
            resolve_source_interface(parameters, value)
            self.assertEqual(parameters, {})

    def test_ip_with_prefix_sets_both_keys(self) -> None:
        parameters: dict = {}
        resolve_source_interface(parameters, "1.2.3.4/24")
        self.assertEqual(parameters["source_ip"], "1.2.3.4")
        self.assertEqual(parameters["local_address"], "1.2.3.4/24")

    def test_bare_ip_defaults_local_address_to_slash_32(self) -> None:
        parameters: dict = {}
        resolve_source_interface(parameters, "1.2.3.4")
        self.assertEqual(parameters["source_ip"], "1.2.3.4")
        self.assertEqual(parameters["local_address"], "1.2.3.4/32")

    def test_malformed_value_raises_value_error(self) -> None:
        for value in ("not-an-ip", "1.2.3.4/33", "999.1.1.1", "1.2.3.4/abc"):
            with self.assertRaises(ValueError):
                resolve_source_interface({}, value)

    def test_existing_source_ip_and_local_address_are_not_clobbered(self) -> None:
        # An explicit run-level override wins over the configured interface.
        parameters: dict = {"source_ip": "10.0.0.9", "local_address": "10.0.0.9/8"}
        resolve_source_interface(parameters, "1.2.3.4/24")
        self.assertEqual(parameters["source_ip"], "10.0.0.9")
        self.assertEqual(parameters["local_address"], "10.0.0.9/8")


if __name__ == "__main__":
    unittest.main()
