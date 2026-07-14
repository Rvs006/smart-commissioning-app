"""Unit tests for the IP discovery engine.

HONESTY: there is NO real building network here. Everything runs against
``127.0.0.1`` plus an ephemeral loopback ``socket`` listener that the test
itself opens/closes, OR against an injected fake connect-probe. The real
remote-network sweep path (default ``asyncio.open_connection`` against site
hosts, reverse DNS against a real resolver) is NOT exercised — it is listed in
the task's ``live_untested`` output and requires on-site validation.
"""

import socket
import unittest
from typing import Any
from unittest import mock

from smart_commissioning_core.engines import ip_scan
from smart_commissioning_core.engines.base import ThrottleConfig


class FakeRunStore:
    """In-memory RunStore capturing run wrapper calls, with cancellation support."""

    def __init__(self, *, cancel_after: int | None = None) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self._cancel = False
        self._cancel_checks = 0
        self._cancel_after = cancel_after

    def update_run_status(self, run_id: str, *, status: str, stage: str | None = None,
                          progress_percent: int | None = None, error_message: str | None = None) -> dict[str, Any]:
        self.status_calls.append({"status": status, "stage": stage,
                                  "progress_percent": progress_percent, "error_message": error_message})
        self.last_status = status
        return {"run_id": run_id, "status": status, "stage": stage,
                "progress_percent": progress_percent, "error_message": error_message,
                "result_summary": dict(self.record_summary)}

    def update_result_summary(self, run_id: str, result_summary: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
        self.summary_calls.append(dict(result_summary))
        if merge:
            self.record_summary.update(result_summary)
        else:
            self.record_summary = dict(result_summary)
        return {"run_id": run_id, "result_summary": dict(self.record_summary)}

    def replace_issues(self, run_id: str, issues: list[Any]) -> dict[str, Any]:
        self.issues_calls.append(list(issues))
        return {"run_id": run_id}

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        self._cancel = True
        return {"run_id": run_id}

    def is_cancel_requested(self, run_id: str) -> bool:
        self._cancel_checks += 1
        if self._cancel_after is not None and self._cancel_checks >= self._cancel_after:
            self._cancel = True
        return self._cancel


_AUTH = {"authorized": True}


class SourceInterfaceTests(unittest.TestCase):
    """Source-NIC binding: the default probe must pass local_addr, and an
    unavailable source_ip must fail the run honestly (not scan empty)."""

    def test_default_probe_binds_local_addr_from_source_ip(self) -> None:
        # No connect injection: the module builds its default probe from
        # parameters["source_ip"]. Capture the kwargs the probe hands to
        # asyncio.open_connection by monkeypatching it on the ip_scan module.
        import asyncio

        captured: dict[str, Any] = {}

        class _FakeWriter:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def fake_open_connection(host: str, port: int, **kwargs: Any) -> tuple[Any, Any]:
            captured["local_addr"] = kwargs.get("local_addr")
            return object(), _FakeWriter()

        store = FakeRunStore()
        with mock.patch.object(asyncio, "open_connection", fake_open_connection):
            result = ip_scan.process_ip_discovery_run(
                "run_srcbind",
                {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80], "source_ip": "127.0.0.1"},
                run_store=store,
                execution_mode="x",
                # NO connect injection: exercises _make_default_connect.
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(captured["local_addr"], ("127.0.0.1", 0))

    def test_unavailable_source_ip_fails_run_not_empty_success(self) -> None:
        # 203.0.113.1 (TEST-NET-3, RFC 5737) is not assigned to this host, so the
        # bind pre-check raises and run_engine records a terminal failure — NOT a
        # silent empty sweep.
        store = FakeRunStore()
        contacted: list[tuple[str, int]] = []

        async def spy_connect(host: str, port: int, timeout: float) -> bool:
            contacted.append((host, port))  # pragma: no cover - must never run
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_badsrc",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "source_ip": "203.0.113.1"},
            run_store=store,
            execution_mode="x",
            connect=spy_connect,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [], "an unavailable source interface must not scan any host")


class TargetExpansionTests(unittest.TestCase):
    def test_cidr_expands_to_hosts(self) -> None:
        hosts = ip_scan._expand_hosts({"cidr": "10.0.0.0/30"})
        # /30 -> .1 and .2 (network/broadcast dropped)
        self.assertEqual(hosts, ["10.0.0.1", "10.0.0.2"])

    def test_slash_32_keeps_single_host(self) -> None:
        self.assertEqual(ip_scan._expand_hosts({"cidr": "192.168.1.5/32"}), ["192.168.1.5"])

    def test_range_inclusive(self) -> None:
        hosts = ip_scan._expand_hosts({"start": "10.0.0.1", "end": "10.0.0.3"})
        self.assertEqual(hosts, ["10.0.0.1", "10.0.0.2", "10.0.0.3"])

    def test_missing_spec_raises(self) -> None:
        with self.assertRaises(ValueError):
            ip_scan._expand_hosts({})

    def test_reversed_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            ip_scan._expand_hosts({"start": "10.0.0.5", "end": "10.0.0.1"})

    def test_oversized_cidr_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ip_scan._expand_hosts({"cidr": "10.0.0.0/8"})

    def test_ports_default_and_validation(self) -> None:
        self.assertEqual(ip_scan._resolve_ports({}), list(ip_scan.DEFAULT_PORTS))
        self.assertEqual(ip_scan._resolve_ports({"ports": [22, 22, 80]}), [22, 80])
        with self.assertRaises(ValueError):
            ip_scan._resolve_ports({"ports": [99999]})
        with self.assertRaises(ValueError):
            ip_scan._resolve_ports({"ports": []})


class PortSpecAndForbiddenTests(unittest.TestCase):
    def test_parse_port_spec_handles_ranges_and_protocols(self) -> None:
        self.assertEqual(ip_scan._parse_port_spec("443/tcp, 47808/udp"), [443, 47808])
        self.assertEqual(ip_scan._parse_port_spec("1-3, 80"), [1, 2, 3, 80])
        with self.assertRaises(ValueError):
            ip_scan._parse_port_spec("not-a-port")

    def test_resolve_ports_from_specification_and_cap(self) -> None:
        # The operator's port_specification string is honoured (was ignored).
        self.assertEqual(ip_scan._resolve_ports({"port_specification": "80, 8000-8002"}), [80, 8000, 8001, 8002])
        # A blank spec falls back to defaults rather than failing the run.
        self.assertEqual(ip_scan._resolve_ports({"port_specification": " "}), list(ip_scan.DEFAULT_PORTS))
        # A "scan everything" range is rejected by the per-sweep ceiling.
        with self.assertRaises(ValueError):
            ip_scan._resolve_ports({"port_specification": "1-65535"})

    def test_forbidden_open_port_is_flagged(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return port in (80, 23)  # 23 (telnet) is the forbidden one

        result = ip_scan.process_ip_discovery_run(
            "run_forbidden",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80, 23], "forbidden_ports": "23/tcp"},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_forbidden_open"], 1)
        self.assertIn("FORBIDDEN PORTS OPEN: 23", summary["discovered_assets"][0]["status_detail"])
        self.assertEqual(persisted[0][1][0]["attributes"]["forbidden_open_ports"], [23])

    def test_per_asset_forbidden_ports_flag_only_matching_host(self) -> None:
        # Both hosts have port 23 open, but only host A forbids it; host B forbids
        # a different port (8080), so its open 23 must NOT be flagged.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return port == 23  # 23 open on every host

        result = ip_scan.process_ip_discovery_run(
            "run_per_asset",
            {
                **_AUTH,
                "cidr": "10.0.0.0/30",  # -> 10.0.0.1 (A) and 10.0.0.2 (B)
                "ports": [23],
                "forbidden_ports_by_address": {"10.0.0.1": "23/tcp", "10.0.0.2": "8080/tcp"},
            },
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_forbidden_open"], 1)
        by_address = {a["ip_address"]: a["status_detail"] for a in summary["discovered_assets"]}
        self.assertIn("FORBIDDEN PORTS OPEN: 23", by_address["10.0.0.1"])
        self.assertNotIn("FORBIDDEN", by_address["10.0.0.2"])

    def test_unexpected_open_port_is_flagged(self) -> None:
        # 8080 is open but not in the host's "Expected services/ports" -> flagged.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return port in (80, 8080)

        result = ip_scan.process_ip_discovery_run(
            "run_unexpected",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80, 8080],
             "expected_ports_by_address": {"10.0.0.1": "80/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_unexpected_open"], 1)
        self.assertIn("UNEXPECTED PORTS OPEN: 8080", summary["discovered_assets"][0]["status_detail"])


class DryRunTests(unittest.TestCase):
    def test_dry_run_opens_no_socket_and_returns_plan(self) -> None:
        store = FakeRunStore()
        contacted: list[tuple[str, int]] = []

        async def spy_connect(host: str, port: int, timeout: float) -> bool:
            contacted.append((host, port))  # pragma: no cover - must never run
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_dry",
            {"cidr": "10.0.0.0/30", "ports": [80, 443], "reverse_dns": True},
            run_store=store,
            execution_mode="inline_local_fallback",
            dry_run=True,
            connect=spy_connect,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(contacted, [], "dry run must not open any socket")
        summary = store.summary_calls[-1]
        self.assertTrue(summary["dry_run"])
        plan = summary["dry_run_plan"]
        self.assertEqual(plan["engine"], "ip_discovery")
        self.assertTrue(plan["dry_run"])
        # 2 hosts x 2 ports = 4 (ip, port) targets
        self.assertEqual(plan["target_count"], 4)
        self.assertIn({"ip": "10.0.0.1", "port": 80}, plan["targets"])
        self.assertIn("reverse-dns", plan["actions"])
        self.assertEqual(summary["hosts_responsive"], 0)

    def test_dry_run_does_not_require_authorization(self) -> None:
        # A dry run is side-effect free, so previewing the plan without auth is OK.
        store = FakeRunStore()
        result = ip_scan.process_ip_discovery_run(
            "run_dry2", {"cidr": "10.0.0.0/31"},
            run_store=store, execution_mode="x", dry_run=True,
        )
        self.assertEqual(result["status"], "succeeded")


class AuthorizationTests(unittest.TestCase):
    def test_real_scan_without_authorization_fails(self) -> None:
        store = FakeRunStore()
        contacted: list[tuple[str, int]] = []

        async def spy_connect(host: str, port: int, timeout: float) -> bool:
            contacted.append((host, port))  # pragma: no cover
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_noauth", {"cidr": "10.0.0.0/31", "ports": [80]},
            run_store=store, execution_mode="x", connect=spy_connect,
        )
        # run_engine swallows the ScanNotAuthorized and marks failed with a
        # sanitized message — and crucially NO socket was contacted.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [], "unauthorized scan must not contact any target")
        self.assertNotIn("10.0.0", result["error_message"] or "")


class FakeConnectScanTests(unittest.TestCase):
    """Real scan logic against an injected deterministic connect probe."""

    def test_responsive_host_reported_with_open_ports(self) -> None:
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            # 10.0.0.1 has 80 open; 10.0.0.2 has nothing open.
            return host == "10.0.0.1" and port == 80

        result = ip_scan.process_ip_discovery_run(
            "run_fc", {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80, 443]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        assets = summary["discovered_assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["ip_address"], "10.0.0.1")
        self.assertEqual(assets[0]["match_basis"], "ip")
        self.assertEqual([p["port"] for p in assets[0]["observed_ports"]], [80])
        self.assertEqual(assets[0]["observed_ports"][0]["service"], "http")
        self.assertEqual(summary["hosts_scanned"], 2)
        self.assertEqual(summary["hosts_responsive"], 1)

    def test_closed_port_host_absent(self) -> None:
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return False  # nothing open anywhere

        result = ip_scan.process_ip_discovery_run(
            "run_closed", {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80, 443]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.summary_calls[-1]["discovered_assets"], [])
        self.assertEqual(store.summary_calls[-1]["hosts_responsive"], 0)

    def test_structured_records_built_for_persistence(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return port == 47808  # BACnet open on every host

        ip_scan.process_ip_discovery_run(
            "run_rec", {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80, 47808],
                        "project_id": "P1", "site_id": "S1"},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(len(persisted), 1)
        run_id, records = persisted[0]
        self.assertEqual(run_id, "run_rec")
        self.assertEqual(len(records), 2)  # both hosts have 47808 open
        rec = records[0]
        self.assertEqual(rec["device_type"], "ip_host")
        self.assertEqual(rec["project_id"], "P1")
        self.assertEqual(rec["attributes"]["open_ports"], [47808])

    def test_throttle_concurrency_bound_respected(self) -> None:
        store = FakeRunStore()
        state = {"in_flight": 0, "peak": 0}

        import asyncio

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            await asyncio.sleep(0.005)
            state["in_flight"] -= 1
            return False

        ip_scan.process_ip_discovery_run(
            # 4 hosts x 5 ports = 20 probe units; bound concurrency to 3.
            "run_thr", {**_AUTH, "cidr": "10.0.0.0/29", "ports": [80, 443, 47808, 1883, 502]},
            run_store=store, execution_mode="x",
            throttle=ThrottleConfig(max_concurrency=3, rate_limit_per_sec=None),
            connect=fake_connect,
        )
        self.assertLessEqual(state["peak"], 3, "concurrency bound exceeded")
        self.assertGreater(state["peak"], 1, "test did not exercise overlap")

    def test_cancellation_stops_sweep_early(self) -> None:
        # Cancel checker flips True after a couple of checks, so later hosts
        # are skipped and we get partial results / cancelled status.
        store = FakeRunStore(cancel_after=2)
        scanned_hosts: set[str] = set()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            scanned_hosts.add(host)
            return False

        result = ip_scan.process_ip_discovery_run(
            "run_cancel", {**_AUTH, "cidr": "10.0.0.0/28", "ports": [80]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "cancelled")
        # 10.0.0.0/28 has 14 hosts; we must NOT have scanned all of them.
        self.assertLess(len(scanned_hosts), 14, "cancellation must stop the sweep early")


class AssetIdAndLastSeenTests(unittest.TestCase):
    """The live "Asset" and "Last Seen" columns: a responsive host resolves its
    registered asset from ``asset_id_by_address`` and carries an observation
    timestamp; a host with no mapping keeps ``asset_id`` None (honest blank)."""

    def test_asset_id_and_last_seen_populated_from_mapping(self) -> None:
        from datetime import datetime

        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_asset",
            {**_AUTH, "addresses": ["127.0.0.1"], "ports": [80],
             "asset_id_by_address": {"127.0.0.1": "CAM-1"}},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        asset = store.summary_calls[-1]["discovered_assets"][0]
        self.assertEqual(asset["asset_id"], "CAM-1")
        # last_seen_at is a parseable ISO-8601 timestamp of the observation.
        parsed = datetime.fromisoformat(asset["last_seen_at"])
        self.assertIsNotNone(parsed.tzinfo, "last_seen_at must be timezone-aware UTC")

    def test_asset_id_none_without_mapping(self) -> None:
        # No asset_id_by_address supplied -> asset_id stays None (rendered "—"),
        # never a fabricated identity.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_asset_none",
            {**_AUTH, "addresses": ["127.0.0.1"], "ports": [80]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(store.summary_calls[-1]["discovered_assets"][0]["asset_id"])


class LoopbackSocketTests(unittest.TestCase):
    """Real asyncio.open_connection against a REAL ephemeral loopback listener.

    This exercises the production default connect probe (no injection) end to
    end on 127.0.0.1 — the honest, environment-safe slice of the real path.
    """

    def test_open_localhost_port_detected_and_closed_port_not(self) -> None:
        # Open a listener on an ephemeral port.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        open_port = listener.getsockname()[1]

        # Find a (very likely) closed port: bind+close to reserve, then reuse #.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()  # now nothing is listening on closed_port

        store = FakeRunStore()
        try:
            result = ip_scan.process_ip_discovery_run(
                "run_loop",
                {**_AUTH, "cidr": "127.0.0.1/32", "ports": [open_port, closed_port]},
                run_store=store,
                execution_mode="x",
                throttle=ThrottleConfig(max_concurrency=4, rate_limit_per_sec=None, connect_timeout_s=2.0),
                # NO connect injection: uses the real asyncio.open_connection probe.
            )
        finally:
            listener.close()

        self.assertEqual(result["status"], "succeeded")
        assets = store.summary_calls[-1]["discovered_assets"]
        self.assertEqual(len(assets), 1, "127.0.0.1 should be responsive on the open port")
        observed = [p["port"] for p in assets[0]["observed_ports"]]
        self.assertIn(open_port, observed, "the open loopback port must be detected")
        self.assertNotIn(closed_port, observed, "the closed port must not be reported open")

    def test_reverse_dns_injected(self) -> None:
        # Use an injected connect + injected reverse lookup so this is hermetic.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_rdns", {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80], "reverse_dns": True},
            run_store=store, execution_mode="x",
            connect=fake_connect,
            reverse_lookup=lambda ip: "localhost.test",
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(store.summary_calls[-1]["discovered_assets"][0]["hostname"], "localhost.test")

    def test_arp_mac_injected(self) -> None:
        # Inject connect + arp_lookup so this is hermetic (no subprocess / ARP).
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_arp", {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80]},
            run_store=store, execution_mode="x",
            connect=fake_connect,
            arp_lookup=lambda ip: "C0:A6:F3:F2:F3:2F",
        )
        self.assertEqual(result["status"], "succeeded")
        asset = store.summary_calls[-1]["discovered_assets"][0]
        self.assertEqual(asset["mac_address"], "C0:A6:F3:F2:F3:2F")
        # match_basis stays "ip": MAC is enrichment, not the discovery basis.
        self.assertEqual(asset["match_basis"], "ip")

    def test_arp_mac_none_degrades_to_blank(self) -> None:
        # Off-L2 / routed host with no ARP entry -> mac_address is None, never a
        # fabricated placeholder (honesty law: best-effort enrichment degrades to
        # blank, it does not invent a value).
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_arp_none", {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80]},
            run_store=store, execution_mode="x",
            connect=fake_connect,
            arp_lookup=lambda ip: None,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(store.summary_calls[-1]["discovered_assets"][0]["mac_address"])


class ExpectedHostnameTests(unittest.TestCase):
    """Reverse-DNS name vs the register's "Expected hostname" — warning-only:
    a blank on EITHER side (no PTR record, site DNS not configured, reverse_dns
    disabled, host absent from the register) must NEVER count as a mismatch,
    because commissioning networks often run without DNS."""

    def test_matching_hostname_not_flagged(self) -> None:
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_match",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.1": "ahu-l03-017"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            reverse_lookup=lambda ip: "ahu-l03-017",
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn("HOSTNAME MISMATCH", summary["discovered_assets"][0]["status_detail"])

    def test_mismatched_hostname_flagged_with_counter(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_mismatch",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.1": "ahu-l03-017"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            reverse_lookup=lambda ip: "boiler-b1-002",
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 1)
        self.assertIn(
            "HOSTNAME MISMATCH: expected ahu-l03-017, got boiler-b1-002",
            summary["discovered_assets"][0]["status_detail"],
        )
        # The register's expectation is persisted alongside the observation.
        self.assertEqual(persisted[0][1][0]["attributes"]["expected_hostname"], "ahu-l03-017")
        self.assertEqual(persisted[0][1][0]["attributes"]["hostname"], "boiler-b1-002")

    def test_domain_suffix_and_case_ignored(self) -> None:
        # Reverse DNS returns an FQDN while the register carries the short name;
        # the comparison strips the domain suffix and case, so this is a MATCH.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_fqdn",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.1": "ahu-l03-017"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            reverse_lookup=lambda ip: "AHU-L03-017.site.example.com",
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn("HOSTNAME MISMATCH", summary["discovered_assets"][0]["status_detail"])

    def test_missing_reverse_dns_result_never_mismatches(self) -> None:
        # PTR lookup failed (no DNS on the commissioning network) -> hostname is
        # None; an expected hostname alone must not flag a mismatch.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_noptr",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.1": "ahu-l03-017"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            reverse_lookup=lambda ip: None,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn("HOSTNAME MISMATCH", summary["discovered_assets"][0]["status_detail"])

    def test_host_absent_from_register_never_mismatches(self) -> None:
        # Reverse DNS produced a name, but this host has no registered expected
        # hostname -> nothing to compare against.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_unregistered",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.9": "other-asset"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            reverse_lookup=lambda ip: "rogue-host.site.example.com",
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn("HOSTNAME MISMATCH", summary["discovered_assets"][0]["status_detail"])


class RegisterPortUnionTests(unittest.TestCase):
    """Per-host probe union + expected-coverage verdicts.

    The ports actually probed for a host are the resolved base list (operator
    spec or defaults) UNION that host's register-declared expected + forbidden
    ports, so the register's "Expected services/ports" are genuinely connected
    to — previously they only fed the flagging maps, so a register expecting
    445/135/139/5985/7070 with a blank port field probed only the 4 defaults
    and reported "responsive: 443" with no findings. Coverage is verdicted both
    ways: expected-but-closed ports flag MISSING EXPECTED PORTS, and a fully
    clean host records an explicit EXPECTED PORTS OK pass instead of silence.
    """

    def test_register_expected_port_outside_base_list_is_probed(self) -> None:
        # Base list is just [80]; the register expects 445. The union must
        # actually CONNECT to 445 (the field bug: it never did), and the
        # register context is persisted next to the observation.
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []
        contacted: list[tuple[str, int]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            contacted.append((host, port))
            return port == 445

        result = ip_scan.process_ip_discovery_run(
            "run_union",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80],
             "expected_ports_by_address": {"10.0.0.1": "445/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(("10.0.0.1", 445), contacted, "register-expected port must be probed")
        asset = store.summary_calls[-1]["discovered_assets"][0]
        self.assertEqual([p["port"] for p in asset["observed_ports"]], [445])
        attributes = persisted[0][1][0]["attributes"]
        self.assertEqual(attributes["expected_ports"], [445])
        self.assertEqual(attributes["scanned_ports"], [80, 445])
        self.assertEqual(attributes["scanned_port_count"], 2)

    def test_missing_expected_port_flagged_with_counter(self) -> None:
        # 80 answers but the register also expects 445, which is closed ->
        # MISSING EXPECTED PORTS verdict + run-summary counter (previously
        # there was NO expected-port-closed verdict at all).
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return port == 80

        result = ip_scan.process_ip_discovery_run(
            "run_missing",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80],
             "expected_ports_by_address": {"10.0.0.1": "80/tcp, 445/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_missing_expected"], 1)
        detail = summary["discovered_assets"][0]["status_detail"]
        self.assertIn("MISSING EXPECTED PORTS: 445", detail)
        self.assertNotIn("EXPECTED PORTS OK", detail)
        self.assertEqual(persisted[0][1][0]["attributes"]["missing_expected_ports"], [445])

    def test_all_expected_open_records_explicit_pass(self) -> None:
        # Pete's field case: blank port field (-> defaults) with the register
        # expecting 445,135,139,443,5985,7070 and forbidding 23,21. Every
        # register port must be probed, and a fully-clean host records an
        # explicit EXPECTED PORTS OK decision instead of silence.
        store = FakeRunStore()
        expected = {445, 135, 139, 443, 5985, 7070}
        contacted: set[int] = set()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            contacted.add(port)
            return port in expected

        result = ip_scan.process_ip_discovery_run(
            "run_expected_ok",
            {**_AUTH, "cidr": "10.0.0.1/32",  # no "ports" -> DEFAULT_PORTS
             "expected_ports_by_address": {"10.0.0.1": "445,135,139,443,5985,7070"},
             "forbidden_ports_by_address": {"10.0.0.1": "23/tcp,21/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(expected | {21, 23} <= contacted, f"register ports not all probed: {contacted}")
        summary = store.summary_calls[-1]
        detail = summary["discovered_assets"][0]["status_detail"]
        self.assertIn("EXPECTED PORTS OK: 6/6 open", detail)
        self.assertNotIn("MISSING", detail)
        self.assertNotIn("FORBIDDEN", detail)
        self.assertNotIn("UNEXPECTED", detail)
        self.assertEqual(summary["hosts_with_missing_expected"], 0)
        self.assertEqual(summary["hosts_with_forbidden_open"], 0)

    def test_forbidden_port_outside_base_list_is_probed_and_flagged(self) -> None:
        # The register forbids telnet but the base list never included 23 —
        # the union must probe it anyway so the violation is actually caught.
        store = FakeRunStore()
        contacted: list[tuple[str, int]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            contacted.append((host, port))
            return port in (80, 23)

        result = ip_scan.process_ip_discovery_run(
            "run_forbidden_union",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80],
             "forbidden_ports_by_address": {"10.0.0.1": "23/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(("10.0.0.1", 23), contacted, "register-forbidden port must be probed")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_forbidden_open"], 1)
        self.assertIn("FORBIDDEN PORTS OPEN: 23", summary["discovered_assets"][0]["status_detail"])

    def test_capped_union_reports_dropped_register_ports(self) -> None:
        # The per-host union respects MAX_PORTS_CEILING, and any register-
        # declared ports the cap drops are reported honestly (never silently
        # truncated, and never verdicted MISSING — we did not probe them).
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []
        contacted: set[int] = set()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            contacted.add(port)
            return port in (1, 100)

        with mock.patch.object(ip_scan, "MAX_PORTS_CEILING", 6):
            result = ip_scan.process_ip_discovery_run(
                "run_capped",
                {**_AUTH, "cidr": "10.0.0.1/32", "ports": [1, 2, 3, 4, 5],
                 "expected_ports_by_address": {"10.0.0.1": "100/tcp, 101/tcp"}},
                run_store=store, execution_mode="x", connect=fake_connect,
                persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn(101, contacted, "a cap-dropped port must not be probed")
        detail = store.summary_calls[-1]["discovered_assets"][0]["status_detail"]
        self.assertIn("PROBE LIST CAPPED: register ports not probed: 101", detail)
        self.assertNotIn("MISSING EXPECTED", detail)
        attributes = persisted[0][1][0]["attributes"]
        self.assertEqual(attributes["register_ports_not_probed"], [101])
        self.assertEqual(attributes["scanned_port_count"], 6)

    def test_host_absent_from_register_keeps_exact_base_list(self) -> None:
        # Only 10.0.0.1 is in the register; 10.0.0.2 must be probed with
        # EXACTLY the base list (global behaviour unchanged) and carry no
        # expected-port verdict either way.
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []
        contacted_by_host: dict[str, set[int]] = {}

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            contacted_by_host.setdefault(host, set()).add(port)
            return port == 80

        result = ip_scan.process_ip_discovery_run(
            "run_unregistered_host",
            {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80],
             "expected_ports_by_address": {"10.0.0.1": "80/tcp, 445/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(contacted_by_host["10.0.0.1"], {80, 445})
        self.assertEqual(contacted_by_host["10.0.0.2"], {80}, "unregistered host must keep the base list")
        details = {a["ip_address"]: a["status_detail"] for a in store.summary_calls[-1]["discovered_assets"]}
        self.assertNotIn("EXPECTED", details["10.0.0.2"])
        attributes = {r["address"]: r["attributes"] for r in persisted[0][1]}
        self.assertIsNone(attributes["10.0.0.2"]["expected_ports"])
        self.assertEqual(attributes["10.0.0.2"]["scanned_ports"], [80])


class ArpLookupUnitTests(unittest.TestCase):
    """Pure ARP-cache parsing / normalisation (subprocess + /proc mocked)."""

    def test_normalise_dashes_to_colons_upper(self) -> None:
        self.assertEqual(ip_scan._normalise_mac("c0-a6-f3-f2-f3-2f"), "C0:A6:F3:F2:F3:2F")

    def test_all_zero_incomplete_entry_degrades_to_none(self) -> None:
        # An incomplete ARP entry must render blank, never a fabricated 00:00:...
        self.assertIsNone(ip_scan._normalise_mac("00:00:00:00:00:00"))
        self.assertIsNone(ip_scan._normalise_mac("00-00-00-00-00-00"))

    def test_arp_lookup_posix_parses_proc_table(self) -> None:
        proc = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "10.0.0.5         0x1         0x2         c0:a6:f3:f2:f3:2f     *        eth0\n"
            "10.0.0.6         0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        )
        with mock.patch("builtins.open", mock.mock_open(read_data=proc)):
            self.assertEqual(ip_scan._arp_lookup_posix("10.0.0.5"), "C0:A6:F3:F2:F3:2F")
        # Incomplete (all-zero) entry -> None, not a fabricated MAC.
        with mock.patch("builtins.open", mock.mock_open(read_data=proc)):
            self.assertIsNone(ip_scan._arp_lookup_posix("10.0.0.6"))
        # Absent host -> None.
        with mock.patch("builtins.open", mock.mock_open(read_data=proc)):
            self.assertIsNone(ip_scan._arp_lookup_posix("10.0.0.9"))

    def test_arp_lookup_windows_parses_arp_output(self) -> None:
        output = (
            "\nInterface: 10.0.0.2 --- 0x5\n"
            "  Internet Address      Physical Address      Type\n"
            "  10.0.0.5              c0-a6-f3-f2-f3-2f     dynamic\n"
        )
        completed = mock.Mock(stdout=output)
        with mock.patch.object(ip_scan.subprocess, "run", return_value=completed):
            self.assertEqual(ip_scan._arp_lookup_windows("10.0.0.5"), "C0:A6:F3:F2:F3:2F")
        # A "no entries" dump degrades to None.
        completed_none = mock.Mock(stdout="No ARP Entries Found.\n")
        with mock.patch.object(ip_scan.subprocess, "run", return_value=completed_none):
            self.assertIsNone(ip_scan._arp_lookup_windows("10.0.0.5"))

    def test_arp_lookup_never_raises_on_subprocess_failure(self) -> None:
        # Locked-down host: the arp binary is absent. The public entry point must
        # degrade to None, never propagate the error into the sweep.
        with mock.patch.object(ip_scan.subprocess, "run", side_effect=FileNotFoundError):
            with mock.patch("builtins.open", side_effect=FileNotFoundError):
                self.assertIsNone(ip_scan._arp_lookup("10.0.0.5"))


if __name__ == "__main__":
    unittest.main()
