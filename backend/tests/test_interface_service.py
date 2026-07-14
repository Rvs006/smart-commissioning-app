"""Unit tests for interface_service (NIC UX v2: classification, facts, guard).

HONESTY: no subprocess, no sockets, no real NICs. The PowerShell boundary is
exercised only through its PURE parser (``_parse_windows_net_facts``), psutil
is replaced with an in-memory fake, and the Windows/Linux classification
dispatch is forced via the ``_IS_WINDOWS`` module flag — so the suite behaves
identically on Ubuntu CI and a Windows laptop, with no dependency on the host's
adapters or on PowerShell being runnable.
"""

import json
import socket
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import interface_service
from app.services.interface_service import (
    _classify_windows,
    _NetFacts,
    _parse_windows_net_facts,
    ensure_source_ip_available,
    list_usable_interfaces,
)


def _snic(address: str, netmask: str = "255.255.255.0", family: int = socket.AF_INET) -> SimpleNamespace:
    """Shape-compatible stand-in for a psutil snicaddr entry."""
    return SimpleNamespace(family=family, address=address, netmask=netmask)


def _fake_psutil(addrs: dict, stats: dict) -> SimpleNamespace:
    """Shape-compatible stand-in for the psutil module (net_if_addrs/net_if_stats)."""
    return SimpleNamespace(net_if_addrs=lambda: addrs, net_if_stats=lambda: stats)


class ParseWindowsNetFactsTests(unittest.TestCase):
    def test_parses_adapters_routes_and_dns_keyed_by_name(self) -> None:
        raw = json.dumps(
            {
                "adapters": [
                    {
                        "name": "Ethernet 3",
                        "ifIndex": 12,
                        "virtual": False,
                        "media": "802.3",
                        "pnp": "PCI\\VEN_8086&DEV_15F3",
                        "desc": "Intel(R) Ethernet Controller I225-V",
                    },
                    {
                        "name": "Wi-Fi",
                        "ifIndex": 7,
                        "virtual": False,
                        "media": "Native 802.11",
                        "pnp": "PCI\\VEN_8086&DEV_2725",
                        "desc": "Intel(R) Wi-Fi 6E AX211",
                    },
                ],
                "routes": [
                    {"ifIndex": 12, "nextHop": "192.168.10.1", "metric": 25},
                    # Higher metric on the same interface must lose.
                    {"ifIndex": 12, "nextHop": "192.168.10.254", "metric": 50},
                ],
                "dns": [
                    {"ifIndex": 12, "servers": ["192.168.10.5", "8.8.8.8", "not-an-ip"]},
                    {"ifIndex": 7, "servers": []},
                ],
            }
        )
        facts = _parse_windows_net_facts(raw)
        self.assertEqual(set(facts.adapter), {"Ethernet 3", "Wi-Fi"})
        self.assertEqual(facts.gateway, {"Ethernet 3": "192.168.10.1"}, "lowest-metric default route wins")
        self.assertEqual(facts.dns, {"Ethernet 3": ["192.168.10.5", "8.8.8.8"]}, "non-IPv4 entries filtered")
        self.assertEqual(facts.dns.get("Wi-Fi", []), [])

    def test_normalizes_single_object_collections(self) -> None:
        # PowerShell 5.1 can serialize single-element pipelines as one object
        # instead of a one-item array; the parser must normalize all three
        # collections (and a lone DNS server string).
        raw = json.dumps(
            {
                "adapters": {"name": "Ethernet", "ifIndex": 3, "virtual": False, "media": "802.3", "pnp": "", "desc": ""},
                "routes": {"ifIndex": 3, "nextHop": "10.0.0.1", "metric": 10},
                "dns": {"ifIndex": 3, "servers": "10.0.0.53"},
            }
        )
        facts = _parse_windows_net_facts(raw)
        self.assertEqual(facts.gateway, {"Ethernet": "10.0.0.1"})
        self.assertEqual(facts.dns, {"Ethernet": ["10.0.0.53"]})

    def test_garbage_input_degrades_to_empty(self) -> None:
        # Dataclass equality: EMPTY means "no adapter facts, no gateways, no DNS".
        for raw in ("", "not json {{{", "[1, 2, 3]", '"just a string"', json.dumps({"adapters": 42})):
            self.assertEqual(_parse_windows_net_facts(raw), _NetFacts.EMPTY, f"raw={raw!r}")


class ClassifyWindowsTests(unittest.TestCase):
    def test_media_802_3_is_ethernet(self) -> None:
        adapter = {"virtual": False, "media": "802.3", "pnp": "PCI\\VEN_8086", "desc": "Intel(R) Ethernet"}
        self.assertEqual(_classify_windows(adapter), "ethernet")

    def test_native_802_11_is_wifi(self) -> None:
        adapter = {"virtual": False, "media": "Native 802.11", "pnp": "PCI\\VEN_8086", "desc": "Intel(R) Wi-Fi"}
        self.assertEqual(_classify_windows(adapter), "wifi")

    def test_usb_pnp_with_802_3_is_usb_ethernet(self) -> None:
        adapter = {
            "virtual": False,
            "media": "802.3",
            "pnp": "USB\\VID_0B95&PID_1790\\000000",
            "desc": "ASIX AX88179 USB 3.0 to Gigabit Ethernet",
        }
        self.assertEqual(_classify_windows(adapter), "usb_ethernet")

    def test_virtual_flag_wins_over_media(self) -> None:
        adapter = {"virtual": True, "media": "802.3", "pnp": "ROOT\\NET\\0000", "desc": "Some Adapter"}
        self.assertEqual(_classify_windows(adapter), "virtual")

    def test_hyper_v_description_is_virtual(self) -> None:
        adapter = {"virtual": False, "media": "802.3", "pnp": "", "desc": "Hyper-V Virtual Ethernet Adapter"}
        self.assertEqual(_classify_windows(adapter), "virtual")

    def test_empty_adapter_facts_are_unknown(self) -> None:
        self.assertEqual(_classify_windows({}), "unknown")


_ADDRS = {
    "Ethernet 3": [_snic("192.168.10.20")],
    "Ethernet 4": [_snic("10.20.30.7")],
    "Wi-Fi": [_snic("172.16.4.55", "255.255.0.0")],
    "vEthernet (WSL)": [_snic("172.29.0.1", "255.255.240.0")],
    "Mystery": [_snic("192.0.2.9")],
    "Ethernet 2": [_snic("192.168.99.4")],
}

_FACTS = _NetFacts(
    adapter={
        "Ethernet 3": {"virtual": False, "media": "802.3", "pnp": "PCI\\VEN_8086", "desc": "Intel(R) Ethernet"},
        "Ethernet 2": {"virtual": False, "media": "802.3", "pnp": "PCI\\VEN_8086", "desc": "Intel(R) Ethernet"},
        "Ethernet 4": {"virtual": False, "media": "802.3", "pnp": "USB\\VID_0B95&PID_1790", "desc": "ASIX USB NIC"},
        "Wi-Fi": {"virtual": False, "media": "Native 802.11", "pnp": "PCI\\VEN_8086", "desc": "Intel(R) Wi-Fi"},
        "vEthernet (WSL)": {"virtual": True, "media": "802.3", "pnp": "", "desc": "Hyper-V Virtual Ethernet Adapter"},
        # "Mystery" deliberately absent -> classified "unknown".
    },
    gateway={"Ethernet 3": "192.168.10.1", "Wi-Fi": "172.16.0.1"},
    dns={"Ethernet 3": ["192.168.10.5", "8.8.8.8"], "Wi-Fi": ["172.16.0.53"]},
)


class ListUsableInterfacesTests(unittest.TestCase):
    def _list(self, facts: _NetFacts) -> list:
        stats = {name: SimpleNamespace(isup=True) for name in _ADDRS}
        stats["Ethernet 2"] = SimpleNamespace(isup=False)
        with (
            patch.object(interface_service, "psutil", _fake_psutil(_ADDRS, stats)),
            patch.object(interface_service, "_IS_WINDOWS", True),
            patch.object(interface_service, "_get_net_facts", return_value=facts),
        ):
            return list_usable_interfaces()

    def test_virtual_listed_last_ordering_and_details_attached(self) -> None:
        interfaces = self._list(_FACTS)
        names = [interface.name for interface in interfaces]
        self.assertEqual(
            names,
            ["Ethernet 3", "Ethernet 4", "Mystery", "Wi-Fi", "vEthernet (WSL)", "Ethernet 2"],
            "up-first, then ethernet < usb_ethernet < unknown < wifi < virtual, then name",
        )

        by_name = {interface.name: interface for interface in interfaces}
        self.assertEqual(by_name["Ethernet 3"].adapter_type, "ethernet")
        self.assertEqual(by_name["Ethernet 4"].adapter_type, "usb_ethernet")
        self.assertEqual(by_name["Mystery"].adapter_type, "unknown")
        self.assertEqual(by_name["Wi-Fi"].adapter_type, "wifi")
        # Virtual adapters are LISTED and honestly classified (ranked last),
        # not hidden: on Hyper-V vSwitch hosts they can be the only routable
        # NIC, and hiding them made the real egress unselectable (2026-07-14).
        self.assertEqual(by_name["vEthernet (WSL)"].adapter_type, "virtual")
        self.assertFalse(by_name["Ethernet 2"].is_up)

        self.assertEqual(by_name["Ethernet 3"].subnet_mask, "255.255.255.0")
        self.assertEqual(by_name["Wi-Fi"].subnet_mask, "255.255.0.0")
        self.assertEqual(by_name["Ethernet 3"].gateway, "192.168.10.1")
        self.assertEqual(by_name["Ethernet 3"].dns_servers, ["192.168.10.5", "8.8.8.8"])
        self.assertIsNone(by_name["Ethernet 4"].gateway, "no default route -> null, never fabricated")
        self.assertEqual(by_name["Ethernet 4"].dns_servers, [])

    def test_degraded_facts_still_serve_psutil_data(self) -> None:
        # PowerShell missing/denied/garbage -> EMPTY facts: every adapter still
        # comes back from psutil with unknown/None/[] details (never a 500, and
        # virtual adapters can no longer be told apart, so they stay listed as
        # "unknown" rather than being guessed at).
        interfaces = self._list(_NetFacts.EMPTY)
        self.assertEqual(len(interfaces), len(_ADDRS))
        for interface in interfaces:
            self.assertEqual(interface.adapter_type, "unknown")
            self.assertIsNone(interface.gateway)
            self.assertEqual(interface.dns_servers, [])
        by_name = {interface.name: interface for interface in interfaces}
        self.assertEqual(by_name["Wi-Fi"].subnet_mask, "255.255.0.0", "subnet mask still real (from psutil)")

    def test_no_psutil_degrades_to_empty_list(self) -> None:
        with patch.object(interface_service, "psutil", None):
            self.assertEqual(list_usable_interfaces(), [])


class EnsureSourceIpAvailableTests(unittest.TestCase):
    def _patched_psutil(self):
        addrs = {
            "Ethernet 3": [_snic("192.168.10.20")],
            "Ethernet 2": [_snic("192.168.99.4")],
        }
        stats = {
            "Ethernet 3": SimpleNamespace(isup=True),
            "Ethernet 2": SimpleNamespace(isup=False),
        }
        return patch.object(interface_service, "psutil", _fake_psutil(addrs, stats))

    def test_assigned_and_up_returns_none(self) -> None:
        with self._patched_psutil():
            self.assertIsNone(ensure_source_ip_available("192.168.10.20"))

    def test_unknown_ip_raises_not_present(self) -> None:
        with self._patched_psutil():
            with self.assertRaises(ValueError) as context:
                ensure_source_ip_available("10.9.9.9")
        message = str(context.exception)
        self.assertIn("10.9.9.9", message)
        self.assertIn("not present on this host", message)
        self.assertIn("Auto (OS default route)", message)

    def test_assigned_but_down_raises_is_down_with_adapter_name(self) -> None:
        with self._patched_psutil():
            with self.assertRaises(ValueError) as context:
                ensure_source_ip_available("192.168.99.4")
        message = str(context.exception)
        self.assertIn("is down", message)
        self.assertIn("Ethernet 2", message)
        self.assertIn("Auto (OS default route)", message)

    def test_without_psutil_failed_bind_probe_raises_not_present(self) -> None:
        fake_socket_module = MagicMock()
        fake_socket_module.AF_INET = socket.AF_INET
        fake_socket_module.SOCK_DGRAM = socket.SOCK_DGRAM
        fake_socket_module.socket.return_value.bind.side_effect = OSError("cannot assign requested address")
        with (
            patch.object(interface_service, "psutil", None),
            patch.object(interface_service, "socket", fake_socket_module),
        ):
            with self.assertRaises(ValueError) as context:
                ensure_source_ip_available("203.0.113.9")
        self.assertIn("not present on this host", str(context.exception))
        fake_socket_module.socket.return_value.close.assert_called_once()

    def test_without_psutil_successful_bind_probe_returns_none(self) -> None:
        fake_socket_module = MagicMock()
        fake_socket_module.AF_INET = socket.AF_INET
        fake_socket_module.SOCK_DGRAM = socket.SOCK_DGRAM
        with (
            patch.object(interface_service, "psutil", None),
            patch.object(interface_service, "socket", fake_socket_module),
        ):
            self.assertIsNone(ensure_source_ip_available("192.168.10.20"))
        fake_socket_module.socket.return_value.close.assert_called_once()


class RunPowershellNetFactsTests(unittest.TestCase):
    """The frozen exe measured the net-facts call at ~9.5s; a 5s timeout made it
    always time out and silently degrade the NIC picker. Guard the timeout floor
    and that failures are logged (not swallowed) so it can't regress unseen."""

    def test_timeout_is_above_realistic_floor(self) -> None:
        # The measured worst case was ~9.5s; keep comfortable headroom.
        self.assertGreaterEqual(interface_service._POWERSHELL_TIMEOUT_S, 15.0)

    def test_ttl_not_shorter_than_timeout(self) -> None:
        # A slow-but-successful call must be cached, not re-forked each window.
        self.assertGreaterEqual(
            interface_service._NET_FACTS_TTL_S, interface_service._POWERSHELL_TIMEOUT_S
        )

    def test_timeout_returns_none_and_logs(self) -> None:
        with patch.object(
            interface_service.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=20.0),
        ):
            with self.assertLogs(interface_service._logger, level="WARNING") as logs:
                self.assertIsNone(interface_service._run_powershell_net_facts())
        self.assertTrue(any("timed out" in m for m in logs.output))

    def test_nonzero_exit_returns_none_and_logs_stderr(self) -> None:
        completed = SimpleNamespace(returncode=1, stdout="", stderr="access denied")
        with patch.object(interface_service.subprocess, "run", return_value=completed):
            with self.assertLogs(interface_service._logger, level="WARNING") as logs:
                self.assertIsNone(interface_service._run_powershell_net_facts())
        self.assertTrue(any("access denied" in m for m in logs.output))

    def test_success_returns_stdout(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout='{"adapters":[]}', stderr="")
        with patch.object(interface_service.subprocess, "run", return_value=completed):
            self.assertEqual(
                interface_service._run_powershell_net_facts(), '{"adapters":[]}'
            )


if __name__ == "__main__":
    unittest.main()
