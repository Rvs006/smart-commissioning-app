"""Unit tests for interface_service.list_usable_interfaces and the gateway lookup.

Everything external is mocked (a fake ``psutil`` and a stubbed gateway lookup /
``subprocess``), so these run deterministically on any CI OS without touching the
host's real NICs or spawning PowerShell. They cover the derivation the API test
(which mocks the whole enumerator) never exercises: subnet-mask derivation,
gateway attach-by-IP, loopback/APIPA exclusion, is_up-first ordering, and the
Windows-only / best-effort guards around the routing-table lookup.
"""

import collections
import json
import socket
import subprocess
import unittest
from unittest.mock import patch

from app.services import interface_service

# interface_service only reads .family/.address/.netmask off each psutil address
# and .isup off each stat, so a minimal namedtuple stands in for snicaddr/snicstats.
_FakeAddr = collections.namedtuple("_FakeAddr", ["family", "address", "netmask"])
_FakeStats = collections.namedtuple("_FakeStats", ["isup"])


class _FakePsutil:
    def __init__(self, addrs, stats):
        self._addrs = addrs
        self._stats = stats

    def net_if_addrs(self):
        return self._addrs

    def net_if_stats(self):
        return self._stats


class ListUsableInterfacesTests(unittest.TestCase):
    def test_derives_mask_attaches_gateway_and_excludes_loopback_apipa(self) -> None:
        addrs = {
            "Ethernet 3": [
                _FakeAddr(socket.AF_INET, "192.168.1.10", "255.255.255.0"),
                _FakeAddr(socket.AF_INET6, "fe80::1", "ffff:ffff:ffff:ffff::"),
            ],
            "Wi-Fi": [_FakeAddr(socket.AF_INET, "10.0.0.5", "255.255.0.0")],
            "Loopback Pseudo-Interface 1": [_FakeAddr(socket.AF_INET, "127.0.0.1", "255.0.0.0")],
            "Ethernet 9": [_FakeAddr(socket.AF_INET, "169.254.3.4", "255.255.0.0")],
        }
        stats = {
            "Ethernet 3": _FakeStats(True),
            "Wi-Fi": _FakeStats(False),
            "Loopback Pseudo-Interface 1": _FakeStats(True),
            "Ethernet 9": _FakeStats(True),
        }
        with (
            patch.object(interface_service, "psutil", _FakePsutil(addrs, stats)),
            patch.object(interface_service, "_default_gateways_by_ip", return_value={"192.168.1.10": "192.168.1.1"}),
        ):
            result = interface_service.list_usable_interfaces()

        # Loopback (127/8) and APIPA (169.254/16) are dropped; up-first then by name.
        self.assertEqual([iface.name for iface in result], ["Ethernet 3", "Wi-Fi"])

        eth = result[0]
        self.assertEqual(eth.ipv4, "192.168.1.10")
        self.assertEqual(eth.prefix_length, 24)
        self.assertEqual(eth.subnet_mask, "255.255.255.0")
        self.assertEqual(eth.cidr, "192.168.1.10/24")
        self.assertEqual(eth.gateway, "192.168.1.1")
        self.assertTrue(eth.is_up)

        wifi = result[1]
        self.assertEqual(wifi.subnet_mask, "255.255.0.0")
        self.assertEqual(wifi.prefix_length, 16)
        # Not present in the gateway map -> None (no fabricated gateway).
        self.assertIsNone(wifi.gateway)
        self.assertFalse(wifi.is_up)

    def test_returns_empty_when_psutil_missing(self) -> None:
        with patch.object(interface_service, "psutil", None):
            self.assertEqual(interface_service.list_usable_interfaces(), [])


class DefaultGatewaysByIpTests(unittest.TestCase):
    def test_empty_off_windows(self) -> None:
        with patch.object(interface_service.sys, "platform", "linux"):
            self.assertEqual(interface_service._default_gateways_by_ip(), {})

    def test_parses_cim_json_and_maps_first_ipv4_gateway(self) -> None:
        payload = json.dumps(
            [
                {"IPAddress": ["192.168.1.10", "fe80::1"], "DefaultIPGateway": ["192.168.1.1", "fe80::1"]},
                {"IPAddress": ["10.0.0.5"], "DefaultIPGateway": None},  # no gateway -> skipped
                {"IPAddress": "172.16.0.9", "DefaultIPGateway": "172.16.0.1"},  # bare strings
            ]
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")
        with (
            patch.object(interface_service.sys, "platform", "win32"),
            patch.object(interface_service.subprocess, "run", return_value=completed),
        ):
            result = interface_service._default_gateways_by_ip()
        self.assertEqual(result, {"192.168.1.10": "192.168.1.1", "172.16.0.9": "172.16.0.1"})

    def test_handles_single_object_payload(self) -> None:
        # ConvertTo-Json emits a bare object (not a list) for a single adapter.
        payload = json.dumps({"IPAddress": ["192.168.1.10"], "DefaultIPGateway": ["192.168.1.1"]})
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")
        with (
            patch.object(interface_service.sys, "platform", "win32"),
            patch.object(interface_service.subprocess, "run", return_value=completed),
        ):
            result = interface_service._default_gateways_by_ip()
        self.assertEqual(result, {"192.168.1.10": "192.168.1.1"})

    def test_empty_on_subprocess_error(self) -> None:
        with (
            patch.object(interface_service.sys, "platform", "win32"),
            patch.object(interface_service.subprocess, "run", side_effect=OSError("powershell missing")),
        ):
            self.assertEqual(interface_service._default_gateways_by_ip(), {})

    def test_empty_on_nonzero_exit_or_unparseable_output(self) -> None:
        nonzero = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="denied")
        garbage = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
        for completed in (nonzero, garbage):
            with (
                patch.object(interface_service.sys, "platform", "win32"),
                patch.object(interface_service.subprocess, "run", return_value=completed),
            ):
                self.assertEqual(interface_service._default_gateways_by_ip(), {})


if __name__ == "__main__":
    unittest.main()
