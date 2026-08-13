from __future__ import annotations

import unittest

from smart_commissioning_core.engines.ip.nmap_xml import (
    NmapXmlLimitsV1,
    NmapXmlParseError,
    parse_nmap_xml,
)

_NORMAL_XML = b"""<?xml version='1.0' encoding='UTF-8'?>
<nmaprun scanner='nmap' version='7.95' xmloutputversion='1.05'>
  <host starttime='1' endtime='2'>
    <status state='up' reason='syn-ack'/>
    <address addr='192.0.2.10' addrtype='ipv4'/>
    <hostnames><hostname name='ahu-10.example.internal' type='user'/></hostnames>
    <ports>
      <port protocol='tcp' portid='443'>
        <state state='open' reason='syn-ack'/>
        <service name='https' product='Example Service' version='1.2.3'/>
        <script id='banner' output='opaque output that must not be public'/>
      </port>
      <port protocol='udp' portid='47808'>
        <state state='open|filtered' reason='no-response'/>
      </port>
    </ports>
    <future-extension><nested value='ignored'/></future-extension>
  </host>
  <runstats><finished time='3' elapsed='2.0' exit='success'/><hosts up='1' down='0' total='1'/></runstats>
</nmaprun>"""


class NmapXmlParserTests(unittest.TestCase):
    def test_normal_ipv4_and_unknown_elements_are_bounded_and_normalized(self) -> None:
        parsed = parse_nmap_xml(_NORMAL_XML)
        self.assertEqual(parsed.nmap_version, "7.95")
        self.assertEqual(parsed.xml_output_version, "1.05")
        self.assertEqual(parsed.finished_exit, "success")
        self.assertEqual(len(parsed.hosts), 1)
        host = parsed.hosts[0]
        self.assertEqual(host.address, "192.0.2.10")
        self.assertEqual(host.state, "up")
        self.assertEqual(host.hostname, "ahu-10.example.internal")
        self.assertEqual([port.state for port in host.ports], ["open", "open|filtered"])
        self.assertEqual(host.ports[0].detected_service, "https")
        self.assertEqual(host.ports[0].detected_product, "Example Service")
        self.assertEqual(host.ports[0].script_ids, ("banner",))
        self.assertEqual(len(host.ports[0].script_output_sha256), 64)
        self.assertNotIn("opaque output", parsed.model_dump_json())

    def test_unknown_extensions_cannot_spoof_known_nmap_elements(self) -> None:
        payload = _NORMAL_XML.replace(
            b"<future-extension><nested value='ignored'/></future-extension>",
            b"<future-extension>"
            b"<address addr='203.0.113.250' addrtype='ipv4'/>"
            b"<status state='down' reason='user-set'/>"
            b"<port protocol='tcp' portid='22'><state state='open'/></port>"
            b"<finished exit='error'/><hosts up='0' down='1' total='1'/>"
            b"</future-extension>",
        )

        parsed = parse_nmap_xml(payload)

        self.assertEqual(parsed.finished_exit, "success")
        self.assertEqual(parsed.hosts[0].address, "192.0.2.10")
        self.assertEqual(parsed.hosts[0].state, "up")
        self.assertEqual(
            [(port.protocol, port.port) for port in parsed.hosts[0].ports],
            [("tcp", 443), ("udp", 47808)],
        )

    def test_empty_complete_document_is_valid(self) -> None:
        parsed = parse_nmap_xml(
            b"<nmaprun scanner='nmap' version='7.95' xmloutputversion='1.05'>"
            b"<runstats><finished exit='success'/><hosts up='0' down='0' total='0'/></runstats>"
            b"</nmaprun>"
        )
        self.assertEqual(parsed.hosts, ())
        self.assertEqual(parsed.total_hosts, 0)

    def test_unsafe_xml_forms_are_rejected_before_normalization(self) -> None:
        cases = {
            "doctype": b"<!DOCTYPE nmaprun><nmaprun/>",
            "entity": b"<!DOCTYPE nmaprun [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><nmaprun>&x;</nmaprun>",
            "stylesheet": b"<?xml-stylesheet href='https://example.test/x.xsl'?><nmaprun/>",
            "processing_instruction": b"<?danger data?><nmaprun/>",
            "xinclude": b"<nmaprun xmlns:xi='http://www.w3.org/2001/XInclude'><xi:include href='file:///x'/></nmaprun>",
        }
        for label, payload in cases.items():
            with self.subTest(label=label), self.assertRaises(NmapXmlParseError):
                parse_nmap_xml(payload)

    def test_duplicate_or_misplaced_terminal_structure_is_rejected(self) -> None:
        cases = (
            b"<nmaprun scanner='nmap' version='7.95' xmloutputversion='1.05'>"
            b"<finished exit='success'/><runstats><hosts up='0' down='0' total='0'/>"
            b"</runstats></nmaprun>",
            b"<nmaprun scanner='nmap' version='7.95' xmloutputversion='1.05'>"
            b"<runstats><finished exit='success'/><finished exit='success'/>"
            b"<hosts up='0' down='0' total='0'/></runstats></nmaprun>",
            b"<nmaprun scanner='nmap' version='7.95' xmloutputversion='1.05'>"
            b"<host><status state='up'/><address addr='192.0.2.1' addrtype='ipv4'/></host>"
            b"<host><status state='up'/><address addr='192.0.2.1' addrtype='ipv4'/></host>"
            b"<runstats><finished exit='success'/><hosts up='2' down='0' total='2'/>"
            b"</runstats></nmaprun>",
        )
        for payload in cases:
            with self.subTest(payload=payload[:80]), self.assertRaises(NmapXmlParseError):
                parse_nmap_xml(payload)

    def test_invalid_truncated_and_incomplete_results_are_rejected(self) -> None:
        cases = (
            b"\xff\xfe<nmaprun/>",
            b"<nmaprun>",
            b"<nmaprun scanner='nmap' version='7.95'></nmaprun>",
            b"<nmaprun scanner='nmap' version='7.95'><runstats><finished exit='error'/></runstats></nmaprun>",
        )
        for payload in cases:
            with self.subTest(payload=payload[:40]), self.assertRaises(NmapXmlParseError):
                parse_nmap_xml(payload)

    def test_size_depth_text_attribute_host_and_port_limits_fail_closed(self) -> None:
        limits = NmapXmlLimitsV1(
            max_bytes=512,
            max_depth=4,
            max_elements=24,
            max_attributes=12,
            max_text_bytes=16,
            max_hosts=1,
            max_ports_per_host=1,
            max_total_ports=1,
            max_parse_seconds=1.0,
        )
        cases = (
            b"<nmaprun>" + b"x" * 600 + b"</nmaprun>",
            b"<nmaprun><a><b><c><d><e/></d></c></b></a></nmaprun>",
            b"<nmaprun><host><address addr='192.0.2.1' addrtype='ipv4'/><ports>"
            b"<port protocol='tcp' portid='80'/><port protocol='tcp' portid='81'/>"
            b"</ports></host><runstats><finished exit='success'/></runstats></nmaprun>",
            b"<nmaprun><host><address addr='192.0.2.1' addrtype='ipv4'/></host>"
            b"<host><address addr='192.0.2.2' addrtype='ipv4'/></host>"
            b"<runstats><finished exit='success'/></runstats></nmaprun>",
        )
        for payload in cases:
            with self.subTest(payload=payload[:60]), self.assertRaises(NmapXmlParseError):
                parse_nmap_xml(payload, limits=limits)

    def test_public_strings_reject_paths_credentials_and_controls(self) -> None:
        unsafe_values = (
            b"C:\\Users\\operator\\secret.txt",
            b"/Users/operator/secret.txt",
            b"password=hunter2",
            b"line&#10;break",
        )
        for value in unsafe_values:
            payload = _NORMAL_XML.replace(b"Example Service", value)
            with self.subTest(value=value), self.assertRaises(NmapXmlParseError):
                parse_nmap_xml(payload)


if __name__ == "__main__":
    unittest.main()
