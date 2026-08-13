from __future__ import annotations

import unittest

from pydantic import ValidationError
from smart_commissioning_core.engines.ip.nmap_profiles import (
    NmapProfileName,
    NmapReviewedScriptV1,
    NmapScanPlanV1,
    build_nmap_scan_plan,
    build_nmap_subprocess_arguments,
    nmap_profile_fingerprint,
    render_nmap_target_list,
)


class NmapProfileContractTests(unittest.TestCase):
    def test_server_defaults_build_the_exact_fixed_profile_limits(self) -> None:
        plan = build_nmap_scan_plan(
            profile=NmapProfileName.TCP_CONNECT_INVENTORY,
            targets=("192.0.2.10",),
            tcp_ports=(443,),
            source_ip="192.0.2.5",
            interface_id="if:1",
            interface_name="Field NIC",
            packet_plan_sha256="a" * 64,
        )

        self.assertEqual(plan.max_rate_per_second, 8)
        self.assertEqual(plan.retries, 0)
        self.assertEqual(plan.host_timeout_seconds, 3)
        self.assertEqual(plan.parent_deadline_seconds, 2700)
        self.assertEqual(plan.output_max_bytes, 16 * 1024 * 1024)

    def test_connect_plan_is_numeric_deterministic_and_list_form(self) -> None:
        plan = build_nmap_scan_plan(
            profile=NmapProfileName.TCP_CONNECT_INVENTORY,
            targets=["192.0.2.10", "192.0.2.2", "192.0.2.10"],
            tcp_ports=[443, 80, 443],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Ethernet 2",
            packet_plan_sha256="a" * 64,
            max_rate_per_second=8.0,
            retries=0,
            host_timeout_seconds=3.0,
            parent_deadline_seconds=60.0,
            output_max_bytes=1_048_576,
        )

        self.assertEqual(plan.targets, ("192.0.2.2", "192.0.2.10"))
        self.assertEqual(plan.tcp_ports, (80, 443))
        self.assertEqual(
            plan.profile_fingerprint,
            nmap_profile_fingerprint(NmapProfileName.TCP_CONNECT_INVENTORY),
        )
        self.assertEqual(render_nmap_target_list(plan), b"192.0.2.2\n192.0.2.10\n")
        arguments = build_nmap_subprocess_arguments(
            plan,
            data_directory=r"C:\Program Files (x86)\Nmap",
            target_list_path=r"C:\ProgramData\SmartCommissioning\private\targets.txt",
        )
        self.assertIsInstance(arguments, tuple)
        self.assertEqual(
            arguments[:5],
            ("--noninteractive", "--no-stylesheet", "-n", "-oX", "-"),
        )
        self.assertIn("-sT", arguments)
        self.assertIn("-Pn", arguments)
        self.assertIn("-iL", arguments)
        self.assertEqual(arguments[arguments.index("-S") + 1], "192.0.2.1")
        self.assertNotIn("-e", arguments)
        self.assertNotIn("Ethernet 2", arguments)
        self.assertNotIn("192.0.2.2", arguments)
        self.assertNotIn("192.0.2.10", arguments)

    def test_raw_profile_uses_canonical_nmap_device_not_friendly_name(self) -> None:
        plan = build_nmap_scan_plan(
            profile=NmapProfileName.TCP_SYN_INVENTORY,
            targets=["192.0.2.10"],
            tcp_ports=[443],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Building Controls Friendly Name",
            packet_plan_sha256="a" * 64,
            max_rate_per_second=2.0,
            retries=0,
            host_timeout_seconds=3.0,
            parent_deadline_seconds=60.0,
            output_max_bytes=1_048_576,
        )

        arguments = build_nmap_subprocess_arguments(
            plan,
            data_directory=r"C:\Program Files (x86)\Nmap",
            target_list_path=r"C:\ProgramData\SmartCommissioning\private\targets.txt",
        )

        expected_device = r"\Device\NPF_{00000000-0000-0000-0000-000000000001}"
        self.assertEqual(plan.nmap_device_name, expected_device)
        self.assertEqual(arguments[arguments.index("-S") + 1], "192.0.2.1")
        self.assertEqual(arguments[arguments.index("-e") + 1], expected_device)
        self.assertNotIn("Building Controls Friendly Name", arguments)

    def test_operator_values_cannot_become_targets_options_or_files(self) -> None:
        invalid_targets = (
            "example.com",
            "192.0.2.1;--script=all",
            "-iL",
            "192.0.2.0/24",
            "192.0.2.1-192.0.2.8",
            "../targets.txt",
        )
        for target in invalid_targets:
            with self.subTest(target=target), self.assertRaises((ValidationError, ValueError)):
                build_nmap_scan_plan(
                    profile=NmapProfileName.TCP_CONNECT_INVENTORY,
                    targets=[target],
                    tcp_ports=[443],
                    source_ip="192.0.2.1",
                    interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
                    interface_name="Ethernet 2",
                    packet_plan_sha256="a" * 64,
                    max_rate_per_second=8.0,
                    retries=0,
                    host_timeout_seconds=3.0,
                    parent_deadline_seconds=60.0,
                    output_max_bytes=1_048_576,
                )

    def test_profile_specific_capability_and_argument_snapshots(self) -> None:
        cases = {
            NmapProfileName.TCP_CONNECT_INVENTORY: (("-sT", "-Pn", "--disable-arp-ping"), False),
            NmapProfileName.TCP_SYN_INVENTORY: (("-sS", "-Pn", "--disable-arp-ping"), True),
            NmapProfileName.SELECTED_UDP: (("-sU", "-Pn", "--disable-arp-ping"), True),
            NmapProfileName.SERVICE_VERSION_INVENTORY: (
                ("-sT", "-Pn", "-sV", "--version-light", "--disable-arp-ping"),
                False,
            ),
            NmapProfileName.OS_INVENTORY: (("-sS", "-Pn", "-O", "--osscan-limit", "--disable-arp-ping"), True),
            NmapProfileName.TRACEROUTE_INVENTORY: (("-sS", "-Pn", "--traceroute", "--disable-arp-ping"), True),
        }
        for profile, (expected, raw_required) in cases.items():
            with self.subTest(profile=profile):
                plan = build_nmap_scan_plan(
                    profile=profile,
                    targets=["192.0.2.10"],
                    tcp_ports=[443] if profile is not NmapProfileName.SELECTED_UDP else [],
                    udp_ports=[47808] if profile is NmapProfileName.SELECTED_UDP else [],
                    source_ip="192.0.2.1",
                    interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
                    interface_name="Ethernet 2",
                    packet_plan_sha256="a" * 64,
                    max_rate_per_second=2.0,
                    retries=0,
                    host_timeout_seconds=3.0,
                    parent_deadline_seconds=60.0,
                    output_max_bytes=1_048_576,
                )
                self.assertEqual(plan.profile_arguments, expected)
                self.assertEqual(plan.raw_capability_required, raw_required)
                arguments = build_nmap_subprocess_arguments(
                    plan,
                    data_directory=r"C:\Program Files (x86)\Nmap",
                    target_list_path=r"C:\ProgramData\SmartCommissioning\private\targets.txt",
                )
                self.assertIn("--max-parallelism", arguments)
                self.assertIn("--max-hostgroup", arguments)
                self.assertIn("--scan-delay", arguments)
                self.assertIn("--disable-arp-ping", arguments)

        host_discovery = build_nmap_scan_plan(
            profile=NmapProfileName.HOST_DISCOVERY,
            targets=["192.0.2.10"],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Ethernet 2",
            packet_plan_sha256="a" * 64,
            max_rate_per_second=2.0,
            retries=0,
            host_timeout_seconds=3.0,
            parent_deadline_seconds=60.0,
            output_max_bytes=1_048_576,
        )
        self.assertEqual(host_discovery.planned_attempts, 2)
        self.assertEqual(
            host_discovery.profile_arguments,
            ("-sn", "-PE", "-PS443", "--disable-arp-ping"),
        )

        reviewed = build_nmap_scan_plan(
            profile=NmapProfileName.REVIEWED_SCRIPT_INVENTORY,
            targets=["192.0.2.10"],
            tcp_ports=[443],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Ethernet 2",
            packet_plan_sha256="a" * 64,
            max_rate_per_second=2.0,
            retries=0,
            host_timeout_seconds=3.0,
            parent_deadline_seconds=60.0,
            output_max_bytes=1_048_576,
            reviewed_scripts=(NmapReviewedScriptV1(name="banner", sha256="c" * 64),),
        )
        self.assertEqual(reviewed.planned_attempts, 32)
        self.assertIn(
            "--script=banner",
            build_nmap_subprocess_arguments(
                reviewed,
                data_directory=r"C:\Program Files (x86)\Nmap",
                target_list_path=r"C:\ProgramData\SmartCommissioning\private\targets.txt",
            ),
        )

    def test_direct_plan_construction_cannot_forge_profile_or_interface(self) -> None:
        plan = build_nmap_scan_plan(
            profile=NmapProfileName.TCP_CONNECT_INVENTORY,
            targets=["192.0.2.10"],
            tcp_ports=[443],
            source_ip="192.0.2.1",
            interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
            interface_name="Ethernet 2",
            packet_plan_sha256="a" * 64,
            max_rate_per_second=2.0,
            retries=0,
            host_timeout_seconds=3.0,
            parent_deadline_seconds=60.0,
            output_max_bytes=1_048_576,
        )
        forged = plan.model_dump(mode="json")
        forged["profile_arguments"] = ["--script=all"]
        with self.assertRaises(ValidationError):
            NmapScanPlanV1.model_validate(forged)
        forged = plan.model_dump(mode="json")
        forged["interface_name"] = "--script=all"
        with self.assertRaises(ValidationError):
            NmapScanPlanV1.model_validate(forged)

    def test_profiles_reject_wrong_protocol_and_unsafe_scale(self) -> None:
        with self.assertRaises(ValueError):
            build_nmap_scan_plan(
                profile=NmapProfileName.SELECTED_UDP,
                targets=["192.0.2.10"],
                tcp_ports=[443],
                source_ip="192.0.2.1",
                interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
                interface_name="Ethernet 2",
                packet_plan_sha256="a" * 64,
                max_rate_per_second=8.0,
                retries=0,
                host_timeout_seconds=3.0,
                parent_deadline_seconds=60.0,
                output_max_bytes=1_048_576,
            )
        with self.assertRaises(ValueError):
            build_nmap_scan_plan(
                profile=NmapProfileName.TCP_CONNECT_INVENTORY,
                targets=[f"192.0.{index // 254}.{index % 254 + 1}" for index in range(257)],
                tcp_ports=list(range(1, 65)),
                source_ip="192.0.2.1",
                interface_id="windows-guid:00000000-0000-0000-0000-000000000001",
                interface_name="Ethernet 2",
                packet_plan_sha256="a" * 64,
                max_rate_per_second=8.0,
                retries=0,
                host_timeout_seconds=3.0,
                parent_deadline_seconds=60.0,
                output_max_bytes=1_048_576,
            )


if __name__ == "__main__":
    unittest.main()
