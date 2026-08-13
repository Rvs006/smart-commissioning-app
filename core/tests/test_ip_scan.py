"""Unit tests for the IP discovery engine.

HONESTY: there is NO real building network here. Everything runs against
``127.0.0.1`` plus an ephemeral loopback ``socket`` listener that the test
itself opens/closes, OR against an injected fake connect-probe. The real
remote-network sweep path (default ``asyncio.open_connection`` against site
hosts, reverse DNS against a real resolver) is NOT exercised — it is listed in
the task's ``live_untested`` output and requires on-site validation.
"""

import socket
import time
import unittest
from copy import deepcopy
from datetime import datetime
from typing import Any
from unittest import mock

from smart_commissioning_core.engines import ip_scan
from smart_commissioning_core.engines.base import ThrottleConfig
from smart_commissioning_core.engines.ip import ProviderIdentityEvidenceV1
from smart_commissioning_core.owned_run_store import OwnershipLostError
from smart_commissioning_core.run_context import canonical_sha256


class FakeRunStore:
    """In-memory RunStore capturing run wrapper calls, with cancellation support."""

    def __init__(
        self,
        *,
        cancel_after: int | None = None,
        observations_enabled: bool = False,
    ) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.summary_calls: list[dict[str, Any]] = []
        self.issues_calls: list[list[Any]] = []
        self.record_summary: dict[str, Any] = {}
        self.last_status: str | None = None
        self._cancel = False
        self._cancel_checks = 0
        self._cancel_after = cancel_after
        self.observations: list[Any] = []
        if not observations_enabled:
            self.append_observation = None  # type: ignore[method-assign]

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

    def append_observation(self, observation: Any) -> Any:
        self.observations.append(observation)
        return {"cursor": len(self.observations), "idempotent": False}


_AUTH = {"authorized": True}


def _frozen_ip_contract(
    *,
    authority: dict[str, object] | None = None,
    attempt_ceiling: int = 4,
) -> dict[str, object]:
    return {
        "scan_contract_version": "1.0",
        "job_type": "ip_discovery",
        "ip": {
            "provider": "builtin_tcp_connect",
            "profile": "gentle",
            "provider_state": {
                "provider": "builtin_tcp_connect",
                "execution_enabled": True,
            },
            "authority": authority,
            "policy": {
                "profile": "gentle",
                "per_target_concurrency": 1,
                "min_target_spacing_ms": 0,
                "retries": 0,
                "retry_backoff_ms": 0,
                "total_dispatch_attempt_ceiling": attempt_ceiling,
                "dispatch_phase_seconds": 10,
                "run_deadline_seconds": 20,
            },
        },
    }


def _live_frozen_ip_parameters(
    *,
    address: str = "127.0.0.1",
    ports: list[int] | None = None,
) -> dict[str, Any]:
    """One complete production-shaped frozen TCP plan for dispatch guards."""

    tcp_ports = ports or [443]
    source_identity = {
        "schema_version": "1.0",
        "selection": "explicit",
        "executor_scope": "test-executor",
        "interface_id": "if:1",
        "interface_name": "loopback",
        "source_ip": "127.0.0.1",
        "prefix_length": 8,
        "local_address": "127.0.0.1/8",
        "default_route_metric": None,
    }
    targets = {
        "target_expressions": [{"kind": "address", "address": address}],
        "exclusions": [],
        "target_count": 1,
        "excluded_count": 0,
        "expanded_target_sha256": canonical_sha256([address]),
        "grouped_ranges": [{"start": address, "end": address, "count": 1}],
        "sample_addresses": [address],
    }
    contract: dict[str, Any] = {
        "scan_contract_version": "1.0",
        "job_type": "ip_discovery",
        "source_interface": source_identity,
        "effective_throttle": {
            "max_concurrency": 1,
            "rate_limit_per_sec": 1.0,
            "connect_timeout_s": 1.0,
        },
        "ip": {
            "provider": "builtin_tcp_connect",
            "profile": "gentle",
            "targets": targets,
            "ports": [{"port": port, "protocol": "tcp"} for port in tcp_ports],
            "not_attempted_ports_by_address": {},
            "provider_state": {
                "provider": "builtin_tcp_connect",
                "execution_enabled": True,
            },
            "policy": {
                "profile": "gentle",
                "max_targets": 256,
                "max_protocol_ports_per_target": 64,
                "total_dispatch_attempt_ceiling": 6000,
                "profile_max_concurrency": 8,
                "per_target_concurrency": 1,
                "profile_max_rate_limit_per_sec": 10.0,
                "min_target_spacing_ms": 100.0,
                "retries": 0,
                "retry_backoff_ms": 0.0,
                "dispatch_phase_seconds": 60,
                "cleanup_margin_seconds": 0,
                "run_deadline_seconds": 60,
                "risk_acknowledgement_required": False,
                "risk_acknowledged": False,
            },
        },
    }
    contract["packet_plan_sha256"] = canonical_sha256(contract)
    return {
        **_AUTH,
        "addresses": [address],
        "ports": tcp_ports,
        "source_ip": "127.0.0.1",
        "local_address": "127.0.0.1/8",
        "source_interface_identity_v1": source_identity,
        "scan_contract_v1": contract,
    }


class U3IPAcceptanceTests(unittest.TestCase):
    class _Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    def test_a4_icmp_blocked_tcp_443_connected_is_reachable(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        class HTTPSOnlyTransport:
            async def connect(
                self,
                target_ip: str,
                port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                self.target = (target_ip, port, source_ip)
                return U3IPAcceptanceTests._Writer()

        transport = HTTPSOnlyTransport()
        with mock.patch.object(
            ip_scan,
            "_icmp_probe",
            create=True,
        ) as forbidden_icmp:
            result = ip_scan.process_ip_discovery_run(
                "run_a4_tcp_liveness",
                {**_AUTH, "addresses": ["192.0.2.44"], "ports": [443]},
                run_store=store,
                execution_mode="test",
                tcp_transport=transport,
            )

        self.assertEqual(result["status"], "succeeded")
        forbidden_icmp.assert_not_called()
        self.assertEqual(transport.target, ("192.0.2.44", 443, None))
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["reachability_state"], "reachable")
        self.assertEqual(asset["observed_ports"], [
            {"port": 443, "protocol": "tcp", "service": "https"}
        ])

    def test_a5_all_timeouts_keep_each_reason_and_host_unconfirmed(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        class TimeoutTransport:
            async def connect(
                self,
                _target_ip: str,
                _port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                del source_ip
                raise TimeoutError

        result = ip_scan.process_ip_discovery_run(
            "run_a5_all_timeouts",
            {
                **_AUTH,
                "addresses": ["192.0.2.45"],
                "ports": [80, 443, 502],
                "expected_ports_by_address": {
                    "192.0.2.45": "80/tcp,443/tcp,502/tcp"
                },
            },
            run_store=store,
            execution_mode="test",
            tcp_transport=TimeoutTransport(),
        )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["reachability_state"], "unconfirmed")
        self.assertEqual(asset["policy_verdict"], "unconfirmed")
        self.assertEqual(asset["missing_expected_ports"], [])
        self.assertEqual(
            [row["port"] for row in asset["port_observations"]],
            [80, 443, 502],
        )
        for row in asset["port_observations"]:
            self.assertEqual(row["probe_outcome"], "timed_out")
            self.assertEqual(row["reachability_state"], "unconfirmed")
            self.assertEqual(row["policy_verdict"], "unconfirmed")
            self.assertEqual(
                row["reason"],
                "The TCP connection did not complete before the application deadline.",
            )
        self.assertNotIn(
            "expected_closed",
            [row["policy_verdict"] for row in asset["port_observations"]],
        )

    def test_a6_forbidden_tcp_23_preserves_frozen_provenance(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        rows: list[dict[str, object]] = [
            {
                "Asset ID": "BMS-23",
                "Expected IP address": "192.0.2.46",
                "Forbidden ports": "23/tcp",
            }
        ]
        rows_digest = canonical_sha256(rows)
        packet_digest = "6" * 64
        contract = _frozen_ip_contract(
            authority={
                "import_id": "imp-a6-register",
                "import_type": "ip_register",
                "accepted_rows_sha256": rows_digest,
                "accepted_count": 1,
            }
        )
        contract["packet_plan_sha256"] = packet_digest
        contract["source_interface"] = {
            "source_ip": "127.0.0.1",
            "local_address": "127.0.0.1/8",
        }
        loaded: list[str] = []

        class TelnetTransport:
            async def connect(
                self,
                _target_ip: str,
                port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                self.bound = (port, source_ip)
                return U3IPAcceptanceTests._Writer()

        transport = TelnetTransport()

        def load(import_id: str) -> list[dict[str, object]]:
            loaded.append(import_id)
            return rows

        result = ip_scan.process_ip_discovery_run(
            "run_a6_forbidden_telnet",
            {
                **_AUTH,
                "addresses": ["192.0.2.46"],
                "ports": [23],
                "source_ip": "127.0.0.1",
                "forbidden_ports_by_address": {"192.0.2.46": "23/tcp"},
                "scan_contract_v1": contract,
            },
            run_store=store,
            execution_mode="test",
            tcp_transport=transport,
            import_loader=load,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(loaded, ["imp-a6-register"])
        self.assertEqual(transport.bound, (23, "127.0.0.1"))
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["policy_verdict"], "forbidden_open")
        self.assertEqual(asset["register_match"], "expected_match")
        [port_event] = [
            event
            for event in store.observations
            if event.entity_kind == "port"
            and event.payload["ip_v1"]["attempts"] == 1
        ]
        payload = port_event.payload["ip_v1"]
        self.assertEqual(payload["provider"], "builtin_tcp_connect")
        self.assertEqual(payload["provider_contract_version"], "1.0")
        self.assertEqual(payload["probe_outcome"], "connected")
        self.assertEqual(payload["policy_verdict"], "forbidden_open")
        self.assertEqual(
            payload["provenance"],
            {
                "profile": "gentle",
                "source_ip": "127.0.0.1",
                "source_interface": "127.0.0.1/8",
                "packet_plan_sha256": packet_digest,
                "register_import_id": "imp-a6-register",
                "register_rows_sha256": rows_digest,
            },
        )
        self.assertIsNotNone(port_event.observed_at.tzinfo)
        dispatched_at = datetime.fromisoformat(
            payload["last_packet_dispatched_at"]
        )
        self.assertIsNotNone(dispatched_at.tzinfo)
        self.assertIsNotNone(datetime.fromisoformat(asset["last_seen_at"]).tzinfo)

    def test_a9_stop_mid_host_meets_dispatch_and_terminal_bounds(self) -> None:
        class TimingStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.stop_persisted_at: float | None = None
                self.terminal_at: float | None = None

            def request_cancel(self, run_id: str) -> dict[str, Any]:
                self.stop_persisted_at = time.monotonic()
                return super().request_cancel(run_id)

            def update_run_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                updated = super().update_run_status(*args, **kwargs)
                if kwargs.get("status") in {"cancelled", "failed", "succeeded"}:
                    self.terminal_at = time.monotonic()
                return updated

        store = TimingStore()
        dispatch_times: list[float] = []
        application_deadline_s = 0.1

        class StopAfterFirstTransport:
            async def connect(
                self,
                _target_ip: str,
                port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                del source_ip
                dispatch_times.append(time.monotonic())
                if port == 80:
                    store.request_cancel("run_a9_stop_timing")
                    return U3IPAcceptanceTests._Writer()
                self.fail("Stop must prevent the untouched port from dispatching")

        transport = StopAfterFirstTransport()
        transport.fail = self.fail  # type: ignore[attr-defined]
        result = ip_scan.process_ip_discovery_run(
            "run_a9_stop_timing",
            {
                **_AUTH,
                "addresses": ["192.0.2.47"],
                "ports": [80, 443],
                "expected_ports_by_address": {
                    "192.0.2.47": "80/tcp,443/tcp"
                },
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(
                max_concurrency=1,
                rate_limit_per_sec=None,
                connect_timeout_s=application_deadline_s,
            ),
            tcp_transport=transport,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(dispatch_times), 1)
        self.assertIsNotNone(store.stop_persisted_at)
        self.assertIsNotNone(store.terminal_at)
        stop_at = store.stop_persisted_at or 0.0
        terminal_at = store.terminal_at or float("inf")
        self.assertLessEqual(max(dispatch_times), stop_at + 2.0)
        longest_in_flight_deadline = max(dispatch_times) + application_deadline_s
        self.assertLessEqual(terminal_at, longest_in_flight_deadline + 2.0)
        [asset] = store.record_summary["discovered_assets"]
        by_port = {row["port"]: row for row in asset["port_observations"]}
        self.assertEqual(by_port[80]["probe_outcome"], "connected")
        self.assertEqual(by_port[80]["attempts"], 1)
        self.assertEqual(by_port[443]["coverage_state"], "not_attempted")
        self.assertEqual(by_port[443]["attempts"], 0)
        self.assertIsNone(by_port[443]["probe_outcome"])
        self.assertEqual(asset["missing_expected_ports"], [])


class RegisterAuthorityIntegrationTests(unittest.TestCase):
    def test_duplicate_expected_ip_requires_ambiguous_review(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        rows: list[dict[str, object]] = [
            {"Asset ID": "AHU-1", "Expected IP address": "192.0.2.18"},
            {"Asset ID": "AHU-2", "Expected IP address": "192.0.2.18"},
        ]

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_duplicate_expected_ip",
            {
                **_AUTH,
                "addresses": ["192.0.2.18"],
                "ports": [443],
                "scan_contract_v1": _frozen_ip_contract(
                    authority={
                        "import_id": "imp-duplicate-ip",
                        "import_type": "ip_register",
                        "accepted_rows_sha256": canonical_sha256(rows),
                        "accepted_count": 2,
                    }
                ),
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
            import_loader=lambda _import_id: rows,
        )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["register_match"], "ambiguous_review")
        self.assertEqual(asset["candidate_device_keys"], ["AHU-1", "AHU-2"])
        self.assertIsNone(asset["matched_device_key"])

    def test_expected_ip_wins_without_calling_identity_observer(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        rows: list[dict[str, object]] = [
            {
                "Asset ID": "AHU-18",
                "Asset name": "Roof AHU 18",
                "Expected IP address": "192.0.2.18",
            }
        ]
        digest = canonical_sha256(rows)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        async def forbidden_identity_observer(
            _host: str,
        ) -> ProviderIdentityEvidenceV1:
            self.fail("expected-IP precedence must not call identity observation")

        result = ip_scan.process_ip_discovery_run(
            "run_expected_ip_precedence",
            {
                **_AUTH,
                "addresses": ["192.0.2.18"],
                "ports": [443],
                "scan_contract_v1": _frozen_ip_contract(
                    authority={
                        "import_id": "imp-ip-18",
                        "import_type": "ip_register",
                        "accepted_rows_sha256": digest,
                        "accepted_count": 1,
                    }
                ),
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
            import_loader=lambda _import_id: rows,
            identity_observer=forbidden_identity_observer,
        )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["register_match"], "expected_match")
        self.assertEqual(asset["expected_ip"], "192.0.2.18")
        self.assertEqual(asset["observed_ip"], "192.0.2.18")
        self.assertEqual(asset["match_basis"], "expected_ip")
        metrics = store.record_summary["ip_headline_metrics_v1"]["metrics"]
        self.assertEqual(metrics[2]["value"], 1)
        self.assertEqual(metrics[3]["value"], 0)

    def test_unique_high_confidence_identity_surfaces_wrong_ip_review(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        rows: list[dict[str, object]] = [
            {
                "Project/site": "demo",
                "System": "air",
                "Asset ID": "AHU-17",
                "Asset name": "Roof AHU 17",
                "Expected IP address": "192.0.2.17",
                "Expected hostname": "ahu-17",
            }
        ]
        original_rows = deepcopy(rows)
        digest = canonical_sha256(rows)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        async def observe_identity(_host: str) -> ProviderIdentityEvidenceV1:
            return ProviderIdentityEvidenceV1(
                evidence_kind="approved_provider",
                confidence="high",
                asset_id="AHU-17",
                corroborating_fields=("asset_id",),
            )

        result = ip_scan.process_ip_discovery_run(
            "run_wrong_ip_review",
            {
                **_AUTH,
                "addresses": ["192.0.2.99"],
                "ports": [443],
                "scan_contract_v1": _frozen_ip_contract(
                    authority={
                        "import_id": "imp-ip-17",
                        "import_type": "ip_register",
                        "accepted_rows_sha256": digest,
                        "accepted_count": 1,
                    }
                ),
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
            import_loader=lambda import_id: rows if import_id == "imp-ip-17" else [],
            identity_observer=observe_identity,
        )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["register_match"], "wrong_ip_review")
        self.assertEqual(asset["expected_ip"], "192.0.2.17")
        self.assertEqual(asset["observed_ip"], "192.0.2.99")
        self.assertEqual(asset["match_basis"], "asset_id")
        self.assertEqual(asset["comparison_reason"], "unique_identity_wrong_ip")
        self.assertEqual(asset["candidate_device_keys"], ["AHU-17"])

        [finalized] = [
            observation
            for observation in store.observations
            if observation.entity_kind == "host" and observation.phase == "finalize"
        ]
        record = finalized.payload["projection_v1"]["record"]
        self.assertEqual(record["address"], "192.0.2.99")
        self.assertEqual(record["attributes"]["register_address"], "192.0.2.17")
        self.assertEqual(record["attributes"]["register_asset_id"], "AHU-17")
        self.assertEqual(
            record["attributes"]["register_asset_name"],
            "Roof AHU 17",
        )
        self.assertEqual(
            record["attributes"]["register_match"],
            "wrong_ip_review",
        )
        self.assertEqual(rows, original_rows)
        self.assertNotIn("accepted_rows", store.record_summary)
        metrics = store.record_summary["ip_headline_metrics_v1"]["metrics"]
        self.assertEqual(metrics[2]["value"], 0)
        self.assertEqual(metrics[3]["value"], 1)

    def test_authority_count_or_digest_mismatch_fails_before_any_probe(self) -> None:
        rows: list[dict[str, object]] = [
            {
                "Asset ID": "AHU-17",
                "Asset name": "Roof AHU 17",
                "Expected IP address": "192.0.2.17",
            }
        ]
        valid_digest = canonical_sha256(rows)

        for label, accepted_count, digest in (
            ("count", 2, valid_digest),
            ("digest", 1, "0" * 64),
        ):
            with self.subTest(label=label):
                store = FakeRunStore(observations_enabled=True)
                forbidden_probe = mock.AsyncMock(return_value=True)

                result = ip_scan.process_ip_discovery_run(
                    f"run_bad_authority_{label}",
                    {
                        **_AUTH,
                        "addresses": ["192.0.2.99"],
                        "ports": [443],
                        "scan_contract_v1": _frozen_ip_contract(
                            authority={
                                "import_id": "imp-ip-17",
                                "import_type": "ip_register",
                                "accepted_rows_sha256": digest,
                                "accepted_count": accepted_count,
                            }
                        ),
                    },
                    run_store=store,
                    execution_mode="test",
                    connect=forbidden_probe,
                    import_loader=lambda _import_id: rows,
                )

                self.assertEqual(result["status"], "failed")
                forbidden_probe.assert_not_awaited()
                self.assertEqual(store.observations, [])

    def test_missing_frozen_authority_fails_while_present_empty_is_valid(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        contacted: list[tuple[str, int]] = []

        async def forbidden_probe(
            host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            contacted.append((host, port))
            return True

        def missing_import(_import_id: str) -> list[dict[str, Any]]:
            raise FileNotFoundError("frozen import was deleted")

        result = ip_scan.process_ip_discovery_run(
            "run_missing_authority",
            {
                **_AUTH,
                "addresses": ["192.0.2.31"],
                "ports": [443],
                "scan_contract_v1": _frozen_ip_contract(
                    authority={
                        "import_id": "imp-missing",
                        "import_type": "ip_register",
                        "accepted_rows_sha256": canonical_sha256([]),
                        "accepted_count": 0,
                    }
                ),
            },
            run_store=store,
            execution_mode="test",
            connect=forbidden_probe,
            import_loader=missing_import,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [])
        self.assertEqual(store.observations, [])


class HeadlineMetricIntegrationTests(unittest.TestCase):
    def test_no_authority_configures_only_reachable_devices(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        async def one_reachable(host: str, _port: int, _timeout: float) -> bool:
            return host == "192.0.2.20"

        result = ip_scan.process_ip_discovery_run(
            "run_metrics_without_authority",
            {
                **_AUTH,
                "addresses": ["192.0.2.20", "192.0.2.21"],
                "ports": [443],
            },
            run_store=store,
            execution_mode="test",
            connect=one_reachable,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            store.record_summary["ip_headline_metrics_v1"],
            {
                "schema_version": "1.0",
                "metrics": [
                    {
                        "schema_version": "1.0",
                        "heading": "Expected Devices",
                        "configured": False,
                        "value": None,
                        "denominator": None,
                        "percentage": None,
                        "pending_count": None,
                        "finalized_count": None,
                    },
                    {
                        "schema_version": "1.0",
                        "heading": "Reachable Devices",
                        "configured": True,
                        "value": 1,
                        "denominator": 2,
                        "percentage": 50.0,
                        "pending_count": 0,
                        "finalized_count": 2,
                    },
                    {
                        "schema_version": "1.0",
                        "heading": "Register Matches",
                        "configured": False,
                        "value": None,
                        "denominator": None,
                        "percentage": None,
                        "pending_count": None,
                        "finalized_count": None,
                    },
                    {
                        "schema_version": "1.0",
                        "heading": "Unexpected / Unregistered Hosts",
                        "configured": False,
                        "value": None,
                        "denominator": None,
                        "percentage": None,
                        "pending_count": None,
                        "finalized_count": None,
                    },
                ],
            },
        )

    def test_selected_empty_authority_keeps_register_metrics_configured(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        rows: list[dict[str, object]] = []
        digest = canonical_sha256(rows)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_metrics_empty_authority",
            {
                **_AUTH,
                "addresses": ["192.0.2.30"],
                "ports": [443],
                "scan_contract_v1": _frozen_ip_contract(
                    authority={
                        "import_id": "imp-empty",
                        "import_type": "ip_register",
                        "accepted_rows_sha256": digest,
                        "accepted_count": 0,
                    }
                ),
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
            import_loader=lambda _import_id: rows,
        )

        self.assertEqual(result["status"], "succeeded")
        metrics = store.record_summary["ip_headline_metrics_v1"]["metrics"]
        self.assertEqual(
            [metric["heading"] for metric in metrics],
            [
                "Expected Devices",
                "Reachable Devices",
                "Register Matches",
                "Unexpected / Unregistered Hosts",
            ],
        )
        self.assertEqual(
            [metric["configured"] for metric in metrics],
            [True, True, True, True],
        )
        self.assertEqual(
            [metric["value"] for metric in metrics],
            [0, 1, 0, 1],
        )
        self.assertEqual(
            [metric["denominator"] for metric in metrics],
            [0, 1, 0, 1],
        )
        self.assertIsNone(metrics[0]["percentage"])
        self.assertIsNone(metrics[2]["percentage"])


class ProgressiveObservationTests(unittest.TestCase):
    def test_status_detail_bounds_all_large_port_categories_deterministically(self) -> None:
        ports = tuple(range(10_000, 11_000))

        detail = ip_scan.format_ip_status_detail(
            "reachable: TCP connection refused on probed ports",
            responsive_ports=ports,
            forbidden_open_ports=ports,
            unexpected_open_ports=ports,
            missing_expected_ports=ports,
        )

        self.assertLessEqual(len(detail), ip_scan.IP_STATUS_DETAIL_MAX_CHARS)
        self.assertIn("responsive: 1000 total; sample: 10000,10001", detail)
        self.assertIn("FORBIDDEN PORTS OPEN: 1000 total; sample: 10000,10001", detail)
        self.assertIn("UNEXPECTED PORTS OPEN: 1000 total; sample: 10000,10001", detail)
        self.assertIn("MISSING EXPECTED PORTS: 1000 total; sample: 10000,10001", detail)

    def test_stop_on_maximum_host_plan_bounds_durable_cutoff_writes(self) -> None:
        class SlowObservationStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.stop_persisted_at: float | None = None
                self.terminal_at: float | None = None

            def request_cancel(self, run_id: str) -> dict[str, Any]:
                self.stop_persisted_at = time.monotonic()
                return super().request_cancel(run_id)

            def append_observation(self, observation: Any) -> Any:
                time.sleep(0.002)
                return super().append_observation(observation)

            def update_run_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                updated = super().update_run_status(*args, **kwargs)
                if kwargs.get("status") in {"cancelled", "failed", "succeeded"}:
                    self.terminal_at = time.monotonic()
                return updated

        store = SlowObservationStore()
        store.request_cancel("run_maximum_stop")
        hosts = [
            f"10.20.{index // 256}.{index % 256}"
            for index in range(ip_scan.MAX_HOSTS_CEILING)
        ]

        async def forbidden_dispatch(
            _host: str,
            _port: int,
            _timeout: float,
        ) -> bool:
            self.fail("a persisted Stop must prevent every packet")

        result = ip_scan.process_ip_discovery_run(
            "run_maximum_stop",
            {**_AUTH, "addresses": hosts, "ports": [443]},
            run_store=store,
            execution_mode="test",
            connect=forbidden_dispatch,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(store.record_summary["discovered_assets"]), len(hosts))
        self.assertLessEqual(len(store.observations), 5)
        self.assertIsNotNone(store.stop_persisted_at)
        self.assertIsNotNone(store.terminal_at)
        self.assertLessEqual(
            store.terminal_at or float("inf"),
            (store.stop_persisted_at or 0.0) + 2.0,
        )

    def test_stop_on_4096_port_plan_skips_untouched_durable_writes(self) -> None:
        class SlowObservationStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.stop_persisted_at: float | None = None
                self.terminal_at: float | None = None

            def request_cancel(self, run_id: str) -> dict[str, Any]:
                self.stop_persisted_at = time.monotonic()
                return super().request_cancel(run_id)

            def append_observation(self, observation: Any) -> Any:
                time.sleep(0.002)
                return super().append_observation(observation)

            def update_run_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                updated = super().update_run_status(*args, **kwargs)
                if kwargs.get("status") in {"cancelled", "failed", "succeeded"}:
                    self.terminal_at = time.monotonic()
                return updated

        store = SlowObservationStore()
        contacted: list[int] = []

        async def stop_after_first(_host: str, port: int, _timeout: float) -> bool:
            contacted.append(port)
            if port == 1:
                store.request_cancel("run_4096_port_stop")
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_4096_port_stop",
            {**_AUTH, "addresses": ["192.0.2.88"], "ports": list(range(1, 4097))},
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=stop_after_first,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(contacted, [1])
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(len(asset["port_observations"]), 4096)
        self.assertEqual(asset["port_observations"][0]["probe_outcome"], "connected")
        self.assertTrue(
            all(
                item["coverage_state"] == "not_attempted"
                for item in asset["port_observations"][1:]
            )
        )
        durable_ports = [
            item for item in store.observations if item.entity_kind == "port"
        ]
        self.assertEqual(len(durable_ports), 1)
        self.assertIsNotNone(store.stop_persisted_at)
        self.assertIsNotNone(store.terminal_at)
        self.assertLessEqual(
            store.terminal_at or float("inf"),
            (store.stop_persisted_at or 0.0) + 2.0,
        )

    def test_one_target_emits_a_stable_staged_trace(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_progressive_ip",
            {
                **_AUTH,
                "addresses": ["192.0.2.10"],
                "ports": [443],
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            [
                (item.phase, item.entity_kind, item.outcome)
                for item in store.observations
            ],
            [
                ("planned", "host", "planned"),
                ("reachability", "host", "started"),
                ("reachability", "port", "connected"),
                ("comparison", "host", "evaluated"),
                ("finalize", "host", "observed"),
            ],
        )
        self.assertEqual(
            [item.entity_version for item in store.observations],
            [1, 2, 1, 3, 4],
        )
        self.assertEqual(
            store.observations[-1].payload["ip_v1"]["target"],
            "192.0.2.10",
        )

    def test_concurrent_port_completion_keeps_frozen_plan_order(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        async def completes_out_of_order(
            _host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            if port == 80:
                import asyncio

                await asyncio.sleep(0.02)
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_progressive_order",
            {
                **_AUTH,
                "addresses": ["192.0.2.11"],
                "ports": [80, 443],
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=2, rate_limit_per_sec=None),
            connect=completes_out_of_order,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            [
                item.payload["ip_v1"]["port"]
                for item in store.observations
                if item.entity_kind == "port"
            ],
            [80, 443],
        )

    def test_stop_while_waiting_for_a_slot_returns_cancelled_partial_evidence(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        contacted: list[int] = []

        async def stop_after_first(
            _host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            contacted.append(port)
            if port == 80:
                import asyncio

                await asyncio.sleep(0)
                store._cancel = True
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_progressive_stop",
            {
                **_AUTH,
                "addresses": ["192.0.2.12"],
                "ports": [80, 443],
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=stop_after_first,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(contacted, [80])
        not_attempted = [
            item
            for item in store.observations
            if item.entity_kind == "port" and item.outcome == "not_attempted"
        ]
        self.assertEqual(not_attempted, [])
        [asset] = store.record_summary["discovered_assets"]
        by_port = {item["port"]: item for item in asset["port_observations"]}
        self.assertEqual(list(by_port), [80, 443])
        self.assertEqual(by_port[443]["attempts"], 0)
        self.assertEqual(by_port[443]["coverage_state"], "not_attempted")
        self.assertIsNone(by_port[443]["probe_outcome"])
        self.assertEqual(by_port[443]["policy_verdict"], "not_attempted")
        self.assertEqual(by_port[443]["control_reason"], "stop_requested")
        host_final = [
            item
            for item in store.observations
            if item.entity_kind == "host" and item.phase == "finalize"
        ][0]
        self.assertEqual(host_final.payload["ip_v1"]["coverage_state"], "cancelled")
        self.assertEqual(
            host_final.payload["ip_v1"]["control_reason"],
            "stop_requested",
        )
        reachable_metric = store.record_summary["ip_headline_metrics_v1"][
            "metrics"
        ][1]
        self.assertEqual(reachable_metric["value"], 0)
        self.assertEqual(reachable_metric["finalized_count"], 1)

    def test_stop_finishes_the_frozen_plan_with_unattempted_rows(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        contacted: list[tuple[str, int]] = []

        async def stop_after_first(
            host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            contacted.append((host, port))
            store._cancel = True
            return True

        hosts = ["192.0.2.40", "192.0.2.41"]
        result = ip_scan.process_ip_discovery_run(
            "run_stop_across_hosts",
            {
                **_AUTH,
                "addresses": hosts,
                "ports": [80, 443],
                "expected_ports_by_address": {
                    host: "80/tcp,443/tcp" for host in hosts
                },
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=stop_after_first,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(contacted, [("192.0.2.40", 80)])
        self.assertEqual(store.record_summary["control_reason"], "stop_requested")
        self.assertEqual(store.record_summary["hosts_scanned"], 1)
        assets = store.record_summary["discovered_assets"]
        self.assertEqual([asset["ip_address"] for asset in assets], hosts)
        self.assertEqual(
            [
                (asset["ip_address"], port["port"])
                for asset in assets
                for port in asset["port_observations"]
            ],
            [
                ("192.0.2.40", 80),
                ("192.0.2.40", 443),
                ("192.0.2.41", 80),
                ("192.0.2.41", 443),
            ],
        )
        unattempted = [
            port
            for asset in assets
            for port in asset["port_observations"]
            if port["attempts"] == 0
        ]
        self.assertEqual(len(unattempted), 3)
        for port in unattempted:
            self.assertEqual(port["coverage_state"], "not_attempted")
            self.assertIsNone(port["probe_outcome"])
            self.assertEqual(port["policy_verdict"], "not_attempted")
            self.assertEqual(port["control_reason"], "stop_requested")
        self.assertTrue(
            all(asset["missing_expected_ports"] == [] for asset in assets)
        )
        tcp_entities = [
            item.entity_key
            for item in store.observations
            if item.entity_kind == "port"
            and item.payload["ip_v1"]["transport"] == "tcp"
        ]
        self.assertEqual(
            tcp_entities,
            [
                "port:192.0.2.40:80:tcp",
            ],
        )
        finalized_hosts = [
            item.entity_key
            for item in store.observations
            if item.entity_kind == "host" and item.phase == "finalize"
        ]
        self.assertEqual(
            finalized_hosts,
            ["host:192.0.2.40"],
        )

    def test_stop_before_first_host_emits_the_entire_unattempted_plan(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        store._cancel = True
        hosts = ["192.0.2.42", "192.0.2.43"]

        async def forbidden_dispatch(
            _host: str,
            _port: int,
            _timeout: float,
        ) -> bool:
            self.fail("Stop before the first host must prevent every packet")

        result = ip_scan.process_ip_discovery_run(
            "run_stop_before_first_host",
            {
                **_AUTH,
                "addresses": hosts,
                "ports": [80, 443],
            },
            run_store=store,
            execution_mode="test",
            connect=forbidden_dispatch,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(store.record_summary["control_reason"], "stop_requested")
        self.assertEqual(store.record_summary["hosts_scanned"], 0)
        assets = store.record_summary["discovered_assets"]
        self.assertEqual([asset["ip_address"] for asset in assets], hosts)
        planned_ports = [
            (asset["ip_address"], port["port"])
            for asset in assets
            for port in asset["port_observations"]
        ]
        self.assertEqual(
            planned_ports,
            [
                ("192.0.2.42", 80),
                ("192.0.2.42", 443),
                ("192.0.2.43", 80),
                ("192.0.2.43", 443),
            ],
        )
        for asset in assets:
            self.assertEqual(asset["coverage_state"], "not_attempted")
            self.assertEqual(asset["missing_expected_ports"], [])
            for port in asset["port_observations"]:
                self.assertEqual(port["attempts"], 0)
                self.assertEqual(port["coverage_state"], "not_attempted")
                self.assertIsNone(port["probe_outcome"])
                self.assertEqual(port["control_reason"], "stop_requested")
        finalized_hosts = [
            item.entity_key
            for item in store.observations
            if item.entity_kind == "host" and item.phase == "finalize"
        ]
        self.assertEqual(
            finalized_hosts,
            ["host:192.0.2.42"],
        )

    def test_frozen_target_spacing_applies_between_port_dispatches(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        dispatched: list[float] = []

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            import asyncio

            dispatched.append(asyncio.get_running_loop().time())
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_progressive_spacing",
            {
                **_AUTH,
                "addresses": ["192.0.2.13"],
                "ports": [80, 443],
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "builtin_tcp_connect",
                        "profile": "gentle",
                        "provider_state": {
                            "provider": "builtin_tcp_connect",
                            "execution_enabled": True,
                        },
                        "policy": {
                            "profile": "gentle",
                            "per_target_concurrency": 2,
                            "min_target_spacing_ms": 30,
                            "retries": 0,
                            "retry_backoff_ms": 0,
                            "total_dispatch_attempt_ceiling": 2,
                            "dispatch_phase_seconds": 10,
                            "run_deadline_seconds": 20,
                        },
                    },
                },
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=2, rate_limit_per_sec=None),
            connect=connected,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(dispatched), 2)
        self.assertGreaterEqual(dispatched[1] - dispatched[0], 0.025)

    def test_builtin_scan_never_runs_dns_or_arp_enrichment(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        with (
            mock.patch(
                "socket.gethostbyaddr",
                side_effect=AssertionError("DNS must not be called"),
            ),
            mock.patch(
                "subprocess.run",
                side_effect=AssertionError("ARP subprocess must not be started"),
            ),
        ):
            result = ip_scan.process_ip_discovery_run(
                "run_no_enrichment",
                {
                    **_AUTH,
                    "addresses": ["192.0.2.20"],
                    "ports": [443],
                    "reverse_dns": True,
                },
                run_store=store,
                execution_mode="test",
                connect=connected,
            )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertIsNone(asset["hostname"])
        self.assertIsNone(asset["mac_address"])

    def test_bacnet_udp_omission_carries_the_capability_action(self) -> None:
        store = FakeRunStore(observations_enabled=True)

        async def connected(_host: str, _port: int, _timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_bacnet_capability",
            {
                **_AUTH,
                "addresses": ["192.0.2.21"],
                "ports": [443],
                "not_attempted_ports_by_address": {
                    "192.0.2.21": [
                        {
                            "protocol": "udp",
                            "port": 47808,
                            "reason": "unsupported_protocol",
                            "capability": "use_bacnet_discovery",
                        }
                    ]
                },
            },
            run_store=store,
            execution_mode="test",
            connect=connected,
        )

        self.assertEqual(result["status"], "succeeded")
        [omission] = [
            item
            for item in store.observations
            if item.entity_kind == "port"
            and item.payload["ip_v1"]["transport"] == "udp"
        ]
        self.assertEqual(omission.outcome, "not_attempted")
        self.assertEqual(
            omission.payload["ip_v1"]["capability_action"],
            "use_bacnet_discovery",
        )


class TypedProbeSemanticsTests(unittest.TestCase):
    def test_pre_dispatch_stop_records_zero_attempts_and_no_packet_time(self) -> None:
        store = FakeRunStore(cancel_after=5, observations_enabled=True)

        class ForbiddenTransport:
            async def connect(
                self,
                _target_ip: str,
                _port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                del source_ip
                self.fail("pre-dispatch Stop must not reach the TCP transport")

        transport = ForbiddenTransport()
        transport.fail = self.fail  # type: ignore[attr-defined]
        result = ip_scan.process_ip_discovery_run(
            "run_pre_dispatch_stop",
            {**_AUTH, "addresses": ["192.0.2.3"], "ports": [443]},
            run_store=store,
            execution_mode="test",
            tcp_transport=transport,
        )

        self.assertEqual(result["status"], "cancelled")
        [asset] = store.record_summary["discovered_assets"]
        [port] = asset["port_observations"]
        self.assertEqual(port["attempts"], 0)
        self.assertIsNone(port["last_packet_dispatched_at"])
        self.assertIsNone(port["probe_outcome"])
        self.assertEqual(port["coverage_state"], "not_attempted")
        self.assertEqual(port["control_reason"], "stop_requested")

    def test_control_reason_preserves_the_denial_that_caused_the_cutoff(self) -> None:
        class CancelledContext:
            def is_cancelled(self) -> bool:
                return True

        class ActiveContext:
            def is_cancelled(self) -> bool:
                return False

        cancelled = CancelledContext()
        active = ActiveContext()
        cases = [
            (
                OwnershipLostError("run_control_reason"),
                cancelled,
                "ownership_lost",
            ),
            (
                RuntimeError("scan authorization was revoked"),
                cancelled,
                "authorization_revoked",
            ),
            (
                RuntimeError("scan authorization window has ended"),
                cancelled,
                "authorization_expired",
            ),
            (
                RuntimeError(
                    "initiating user's project/site scope grant is not active"
                ),
                cancelled,
                "grant_revoked",
            ),
            (
                RuntimeError("database read failed"),
                cancelled,
                "control_store_error",
            ),
            (
                RuntimeError(
                    "run owner, attempt, status, cancellation, or lease is no "
                    "longer active"
                ),
                cancelled,
                "stop_requested",
            ),
            (
                RuntimeError(
                    "run owner, attempt, status, cancellation, or lease is no "
                    "longer active"
                ),
                active,
                "ownership_lost",
            ),
        ]

        for error, context, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(ip_scan._control_reason(error, context), expected)

    def test_retry_reacquires_control_and_keeps_attempt_versions(self) -> None:
        class ControlledStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.active_checks = 0

            def require_active_control(self) -> None:
                self.active_checks += 1

        class Writer:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        class RetryTransport:
            def __init__(self) -> None:
                self.calls = 0

            async def connect(
                self,
                _target_ip: str,
                _port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError
                return Writer()

        store = ControlledStore()
        transport = RetryTransport()
        result = ip_scan.process_ip_discovery_run(
            "run_typed_retry",
            {
                **_AUTH,
                "addresses": ["192.0.2.27"],
                "ports": [443],
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "builtin_tcp_connect",
                        "profile": "gentle",
                        "provider_state": {
                            "provider": "builtin_tcp_connect",
                            "execution_enabled": True,
                        },
                        "policy": {
                            "profile": "gentle",
                            "per_target_concurrency": 1,
                            "min_target_spacing_ms": 1,
                            "retries": 1,
                            "retry_backoff_ms": 0,
                            "total_dispatch_attempt_ceiling": 2,
                            "dispatch_phase_seconds": 10,
                            "run_deadline_seconds": 20,
                        },
                    },
                },
            },
            run_store=store,
            execution_mode="test",
            tcp_transport=transport,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(transport.calls, 2)
        self.assertGreaterEqual(store.active_checks, 2)
        port_events = [
            item for item in store.observations if item.entity_kind == "port"
        ]
        self.assertEqual([item.entity_version for item in port_events], [1, 2])
        self.assertEqual(
            [item.payload["ip_v1"]["probe_outcome"] for item in port_events],
            ["timed_out", "connected"],
        )

    def test_authorization_loss_keeps_partial_results_and_fails(self) -> None:
        class RevokedStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.active_checks = 0

            def require_active_control(self) -> None:
                self.active_checks += 1
                if self.active_checks >= 3:
                    raise RuntimeError("scan authorization was revoked")

        store = RevokedStore()
        contacted: list[int] = []

        async def connected(_host: str, port: int, _timeout: float) -> bool:
            contacted.append(port)
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_authorization_lost",
            {
                **_AUTH,
                "addresses": ["192.0.2.26"],
                "ports": [80, 443],
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=connected,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [80])
        self.assertEqual(
            store.record_summary["control_reason"],
            "authorization_revoked",
        )
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(
            [item["port"] for item in asset["port_observations"]],
            [80, 443],
        )
        unattempted = asset["port_observations"][1]
        self.assertEqual(unattempted["attempts"], 0)
        self.assertEqual(unattempted["coverage_state"], "not_attempted")
        self.assertIsNone(unattempted["probe_outcome"])
        self.assertEqual(unattempted["policy_verdict"], "not_attempted")
        self.assertEqual(
            unattempted["control_reason"],
            "authorization_revoked",
        )
        durable_unattempted = [
            item
            for item in store.observations
            if item.entity_kind == "port" and item.outcome == "not_attempted"
        ]
        self.assertEqual(durable_unattempted, [])
        diagnostic = [
            item for item in store.observations if item.entity_kind == "diagnostic"
        ][0]
        self.assertEqual(
            diagnostic.payload["ip_v1"]["control_reason"],
            "authorization_revoked",
        )

    def test_post_drain_control_loss_preserves_final_probe_as_partial_failure(self) -> None:
        """Control is checked again after the last in-flight connect returns."""

        class RevokedAfterDrainStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.active_checks = 0

            def require_active_control(self) -> None:
                self.active_checks += 1
                # One check occurs when the throttle slot is acquired and one
                # immediately before the packet dispatch.  The third is the
                # post-drain fence under test.
                if self.active_checks == 3:
                    raise RuntimeError("scan authorization was revoked")

        store = RevokedAfterDrainStore()
        contacted: list[int] = []

        async def connected(_host: str, port: int, _timeout: float) -> bool:
            contacted.append(port)
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_post_drain_authorization_loss",
            {**_AUTH, "addresses": ["192.0.2.48"], "ports": [443]},
            run_store=store,
            execution_mode="test",
            connect=connected,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [443])
        self.assertEqual(store.record_summary["control_reason"], "authorization_revoked")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["coverage_state"], "attempted")
        self.assertEqual(asset["port_observations"][0]["probe_outcome"], "connected")
        diagnostic = next(
            item for item in store.observations if item.entity_kind == "diagnostic"
        )
        self.assertEqual(diagnostic.payload["ip_v1"]["control_reason"], "authorization_revoked")

    def test_ownership_loss_is_not_relabelled_as_stop_requested(self) -> None:
        class OwnershipStore(FakeRunStore):
            def __init__(self) -> None:
                super().__init__(observations_enabled=True)
                self.active_checks = 0
                self.ownership_lost = False

            def require_active_control(self) -> None:
                self.active_checks += 1
                if self.active_checks >= 3:
                    self.ownership_lost = True
                    raise OwnershipLostError("run_ownership_lost")

            def is_cancel_requested(self, run_id: str) -> bool:
                del run_id
                return self.ownership_lost

        store = OwnershipStore()
        contacted: list[int] = []

        async def connected(_host: str, port: int, _timeout: float) -> bool:
            contacted.append(port)
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_ownership_lost",
            {
                **_AUTH,
                "addresses": ["192.0.2.31"],
                "ports": [80, 443],
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=connected,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [80])
        self.assertEqual(store.record_summary["control_reason"], "ownership_lost")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(
            [item["port"] for item in asset["port_observations"]],
            [80, 443],
        )
        self.assertEqual(
            asset["port_observations"][1]["control_reason"],
            "ownership_lost",
        )

    def test_stop_interrupts_retry_backoff_without_another_dispatch(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        contacted: list[int] = []

        async def time_out_then_stop(
            _host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            contacted.append(port)
            store._cancel = True
            return False

        started = time.monotonic()
        result = ip_scan.process_ip_discovery_run(
            "run_stop_during_backoff",
            {
                **_AUTH,
                "addresses": ["192.0.2.32"],
                "ports": [443],
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "builtin_tcp_connect",
                        "profile": "gentle",
                        "provider_state": {
                            "provider": "builtin_tcp_connect",
                            "execution_enabled": True,
                        },
                        "policy": {
                            "profile": "gentle",
                            "per_target_concurrency": 1,
                            "min_target_spacing_ms": 0,
                            "retries": 1,
                            "retry_backoff_ms": 1500,
                            "total_dispatch_attempt_ceiling": 2,
                            "dispatch_phase_seconds": 10,
                            "run_deadline_seconds": 20,
                        },
                    },
                },
            },
            run_store=store,
            execution_mode="test",
            connect=time_out_then_stop,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(contacted, [443])
        self.assertLess(elapsed, 1.0)

    def test_stop_interrupts_target_pacing_without_another_dispatch(self) -> None:
        store = FakeRunStore(observations_enabled=True)
        contacted: list[int] = []

        async def connect_then_stop(
            _host: str,
            port: int,
            _timeout: float,
        ) -> bool:
            contacted.append(port)
            if port == 80:
                store._cancel = True
            return True

        started = time.monotonic()
        result = ip_scan.process_ip_discovery_run(
            "run_stop_during_pacing",
            {
                **_AUTH,
                "addresses": ["192.0.2.33"],
                "ports": [80, 443],
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "builtin_tcp_connect",
                        "profile": "gentle",
                        "provider_state": {
                            "provider": "builtin_tcp_connect",
                            "execution_enabled": True,
                        },
                        "policy": {
                            "profile": "gentle",
                            "per_target_concurrency": 2,
                            "min_target_spacing_ms": 1500,
                            "retries": 0,
                            "retry_backoff_ms": 0,
                            "total_dispatch_attempt_ceiling": 2,
                            "dispatch_phase_seconds": 10,
                            "run_deadline_seconds": 20,
                        },
                    },
                },
            },
            run_store=store,
            execution_mode="test",
            throttle=ThrottleConfig(max_concurrency=2, rate_limit_per_sec=None),
            connect=connect_then_stop,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(contacted, [80])
        self.assertLess(elapsed, 1.0)
        [asset] = store.record_summary["discovered_assets"]
        unattempted = asset["port_observations"][1]
        self.assertEqual(unattempted["port"], 443)
        self.assertEqual(unattempted["coverage_state"], "not_attempted")
        self.assertIsNone(unattempted["probe_outcome"])

    def test_frozen_non_builtin_provider_cannot_fall_through_without_state(self) -> None:
        store = FakeRunStore()
        contacted: list[tuple[str, int]] = []

        async def legacy(host: str, port: int, _timeout: float) -> bool:
            contacted.append((host, port))
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_disabled_provider",
            {
                **_AUTH,
                "addresses": ["192.0.2.28"],
                "ports": [443],
                "scan_contract_v1": {
                    "scan_contract_version": "1.0",
                    "job_type": "ip_discovery",
                    "ip": {
                        "provider": "operator_managed_nmap",
                        "policy": {
                            "dispatch_phase_seconds": 10,
                            "run_deadline_seconds": 20,
                        },
                    },
                },
            },
            run_store=store,
            execution_mode="test",
            connect=legacy,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [])

    def test_live_contract_failures_dispatch_zero_packets(self) -> None:
        """Live modes must never fall back to flat legacy TCP parameters."""

        cases: dict[str, Any] = {
            "absent": {**_AUTH, "addresses": ["127.0.0.1"], "ports": [443]},
            "malformed": {**_live_frozen_ip_parameters(), "scan_contract_v1": []},
            "wrong-job": _live_frozen_ip_parameters(),
            "forged-digest": _live_frozen_ip_parameters(),
            "missing-source": _live_frozen_ip_parameters(),
        }
        cases["wrong-job"]["scan_contract_v1"]["job_type"] = "bacnet_discovery"
        cases["forged-digest"]["scan_contract_v1"]["packet_plan_sha256"] = "0" * 64
        del cases["missing-source"]["source_interface_identity_v1"]

        for name, parameters in cases.items():
            with self.subTest(name=name):
                store = FakeRunStore(observations_enabled=True)
                contacted: list[tuple[str, int]] = []

                async def spy(
                    host: str,
                    port: int,
                    _timeout: float,
                    contacted: list[tuple[str, int]] = contacted,
                ) -> bool:
                    contacted.append((host, port))
                    return True

                result = ip_scan.process_ip_discovery_run(
                    f"run_live_contract_{name}",
                    parameters,
                    run_store=store,
                    execution_mode="inline",
                    connect=spy,
                )

                self.assertEqual(result["status"], "failed")
                self.assertEqual(contacted, [])

    def test_live_contract_rejects_flat_target_or_port_drift_before_dispatch(self) -> None:
        parameters = _live_frozen_ip_parameters()
        parameters["ports"] = [80]
        store = FakeRunStore(observations_enabled=True)
        contacted: list[tuple[str, int]] = []

        async def spy(host: str, port: int, _timeout: float) -> bool:
            contacted.append((host, port))
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_live_flat_drift",
            parameters,
            run_store=store,
            execution_mode="inline",
            connect=spy,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(contacted, [])

    def test_live_contract_keeps_trusted_udp_omission_out_of_tcp_dispatch(self) -> None:
        parameters = _live_frozen_ip_parameters()
        contract = parameters["scan_contract_v1"]
        contract["ip"]["ports"].append({"port": 47808, "protocol": "udp"})
        contract["ip"]["not_attempted_ports_by_address"] = {
            "127.0.0.1": [
                {
                    "port": 47808,
                    "protocol": "udp",
                    "source": "expected",
                    "reason": "unsupported_protocol",
                    "capability": "use_bacnet_discovery",
                }
            ]
        }
        parameters["not_attempted_ports_by_address"] = deepcopy(
            contract["ip"]["not_attempted_ports_by_address"]
        )
        contract["packet_plan_sha256"] = canonical_sha256(
            {key: value for key, value in contract.items() if key != "packet_plan_sha256"}
        )
        store = FakeRunStore(observations_enabled=True)
        contacted: list[int] = []

        async def spy(_host: str, port: int, _timeout: float) -> bool:
            contacted.append(port)
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_live_trusted_udp_omission",
            parameters,
            run_store=store,
            execution_mode="inline",
            connect=spy,
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(contacted, [443])

    def test_conflicting_transport_adapters_fail_inside_the_run_lifecycle(self) -> None:
        store = FakeRunStore()

        async def legacy(_host: str, _port: int, _timeout: float) -> bool:
            return True

        class Transport:
            async def connect(
                self,
                _target_ip: str,
                _port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                raise AssertionError(source_ip)

        result = ip_scan.process_ip_discovery_run(
            "run_conflicting_adapters",
            {**_AUTH, "addresses": ["192.0.2.29"], "ports": [443]},
            run_store=store,
            execution_mode="test",
            connect=legacy,
            tcp_transport=Transport(),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.last_status, "failed")

    def test_refusal_proves_closed_and_timeout_stays_unconfirmed(self) -> None:
        store = FakeRunStore()

        class MixedTransport:
            async def connect(
                self,
                _target_ip: str,
                port: int,
                *,
                source_ip: str | None,
            ) -> Any:
                self.assert_source = source_ip
                if port == 80:
                    raise ConnectionRefusedError(10061, "refused")
                raise TimeoutError

        result = ip_scan.process_ip_discovery_run(
            "run_typed_negative",
            {
                **_AUTH,
                "addresses": ["192.0.2.30"],
                "ports": [80, 443],
                "expected_ports_by_address": {"192.0.2.30": "80/tcp,443/tcp"},
            },
            run_store=store,
            execution_mode="test",
            tcp_transport=MixedTransport(),
        )

        self.assertEqual(result["status"], "succeeded")
        [asset] = store.record_summary["discovered_assets"]
        self.assertEqual(asset["reachability_state"], "unconfirmed")
        by_port = {item["port"]: item for item in asset["port_observations"]}
        self.assertEqual(by_port[80]["probe_outcome"], "connection_refused")
        self.assertEqual(by_port[80]["policy_verdict"], "expected_closed")
        self.assertEqual(by_port[443]["probe_outcome"], "timed_out")
        self.assertEqual(by_port[443]["policy_verdict"], "unconfirmed")
        self.assertEqual(asset["missing_expected_ports"], [80])
        reachable_metric = store.record_summary["ip_headline_metrics_v1"][
            "metrics"
        ][1]
        self.assertEqual(reachable_metric["value"], 0)


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

    def test_huge_cidr_and_range_reject_before_materializing_hosts(self) -> None:
        sentinel_network = mock.Mock(num_addresses=2**32)
        sentinel_network.hosts.side_effect = AssertionError(
            "huge CIDRs must be rejected before host iteration"
        )
        with mock.patch.object(
            ip_scan.ipaddress,
            "ip_network",
            return_value=sentinel_network,
        ):
            with self.assertRaisesRegex(ValueError, "exceeding max_hosts=1"):
                ip_scan._expand_hosts({"cidr": "0.0.0.0/0", "max_hosts": 1})
        sentinel_network.hosts.assert_not_called()

        original_ip_address = ip_scan.ipaddress.ip_address
        parsed_addresses = 0

        def parse_only(value: object):
            nonlocal parsed_addresses
            parsed_addresses += 1
            if parsed_addresses > 2:
                raise AssertionError("huge ranges must be rejected before expansion")
            return original_ip_address(value)

        with mock.patch.object(ip_scan.ipaddress, "ip_address", side_effect=parse_only):
            with self.assertRaisesRegex(ValueError, "exceeding max_hosts=1"):
                ip_scan._expand_hosts(
                    {
                        "start": "0.0.0.0",
                        "end": "255.255.255.255",
                        "max_hosts": 1,
                    }
                )
        self.assertEqual(parsed_addresses, 2)

    def test_ports_default_and_validation(self) -> None:
        self.assertEqual(ip_scan._resolve_ports({}), list(ip_scan.DEFAULT_PORTS))
        self.assertEqual(ip_scan._resolve_ports({"ports": [22, 22, 80]}), [22, 80])
        with self.assertRaises(ValueError):
            ip_scan._resolve_ports({"ports": [99999]})
        with self.assertRaises(ValueError):
            ip_scan._resolve_ports({"ports": []})


class PortSpecAndForbiddenTests(unittest.TestCase):
    def test_parse_port_spec_handles_ranges_and_protocols(self) -> None:
        self.assertEqual(ip_scan._parse_port_spec("443/tcp, 80"), [443, 80])
        self.assertEqual(ip_scan._parse_port_spec("1-3, 80"), [1, 2, 3, 80])
        with self.assertRaises(ValueError):
            ip_scan._parse_port_spec("not-a-port")
        with self.assertRaisesRegex(ValueError, "UDP"):
            ip_scan._parse_port_spec("47808/udp")

    def test_udp_specs_never_reach_tcp_transport_from_top_level_or_host_maps(self) -> None:
        for label, parameters in (
            ("top-level", {**_AUTH, "addresses": ["192.0.2.70"], "port_specification": "47808/udp"}),
            (
                "per-address",
                {
                    **_AUTH,
                    "addresses": ["192.0.2.70"],
                    "ports": [443],
                    "expected_ports_by_address": {"192.0.2.70": "47808/udp"},
                },
            ),
        ):
            with self.subTest(label=label):
                store = FakeRunStore()
                contacted: list[tuple[str, int]] = []

                async def spy(
                    host: str,
                    port: int,
                    _timeout: float,
                    contacted: list[tuple[str, int]] = contacted,
                ) -> bool:
                    contacted.append((host, port))
                    return True

                result = ip_scan.process_ip_discovery_run(
                    f"run_udp_{label}",
                    parameters,
                    run_store=store,
                    execution_mode="test",
                    connect=spy,
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(contacted, [])

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
        self.assertNotIn("reverse-dns", plan["actions"])
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
            # 10.0.0.1 has 80 open; 10.0.0.2 has nothing open (silent).
            return host == "10.0.0.1" and port == 80

        result = ip_scan.process_ip_discovery_run(
            "run_fc", {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80, 443]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        assets = summary["discovered_assets"]
        # Every scanned host now gets a row: the responder AND the silent host.
        # Select by ip_address — silent rows interleave in host order, so
        # positional indexing would silently test the wrong row.
        self.assertEqual(len(assets), 2)
        by_address = {a["ip_address"]: a for a in assets}
        responder = by_address["10.0.0.1"]
        self.assertEqual(responder["match_basis"], "ip")
        self.assertEqual([p["port"] for p in responder["observed_ports"]], [80])
        self.assertEqual(responder["observed_ports"][0]["service"], "http")
        self.assertIsNotNone(responder["last_seen_at"])
        # The silent host is reported honestly, not dropped.
        silent = by_address["10.0.0.2"]
        self.assertEqual(silent["observed_ports"], [])
        self.assertEqual(silent["match_basis"], "none")
        self.assertIsNone(silent["last_seen_at"])
        self.assertTrue(silent["status_detail"].startswith("no response on scanned ports"))
        self.assertEqual(summary["hosts_scanned"], 2)
        self.assertEqual(summary["hosts_responsive"], 1)

    def test_silent_hosts_reported_not_dropped(self) -> None:
        # Rewritten from test_closed_port_host_absent: silent hosts are no longer
        # dropped. An all-closed /30 succeeds and reports BOTH hosts honestly as
        # "no response on scanned ports" (not proof they are absent).
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return False  # nothing open anywhere

        result = ip_scan.process_ip_discovery_run(
            "run_closed", {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80, 443]},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        assets = summary["discovered_assets"]
        self.assertEqual(len(assets), 2)
        for asset in assets:
            self.assertEqual(asset["observed_ports"], [])
            self.assertEqual(asset["match_basis"], "none")
            self.assertIsNone(asset["last_seen_at"])
            self.assertTrue(asset["status_detail"].startswith("no response on scanned ports"))
            # Unregistered silence stays neutral — no register marker, no red
            # MISSING verdict.
            self.assertNotIn("EXPECTED BY REGISTER", asset["status_detail"])
            self.assertNotIn("MISSING EXPECTED PORTS", asset["status_detail"])
        self.assertEqual(summary["hosts_responsive"], 0)
        self.assertEqual(summary["hosts_scanned"], 2)

    def test_register_listed_silent_host_marked_inconclusive(self) -> None:
        # Two silent hosts: 10.0.0.1 is in the register (asset mapping + expected
        # ports), 10.0.0.2 is not. The registered one carries the EXPECTED BY
        # REGISTER inconclusive note plus its asset_id; the unregistered one does
        # not. Neither is verdicted MISSING EXPECTED PORTS — a TCP-connect miss is
        # not a closed-port finding.
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return False  # both hosts silent

        result = ip_scan.process_ip_discovery_run(
            "run_reg_silent",
            {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80],
             "asset_id_by_address": {"10.0.0.1": "AHU-1"},
             "expected_ports_by_address": {"10.0.0.1": "445/tcp"}},
            run_store=store, execution_mode="x", connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        by_address = {a["ip_address"]: a for a in store.summary_calls[-1]["discovered_assets"]}
        registered = by_address["10.0.0.1"]
        self.assertIn("EXPECTED BY REGISTER", registered["status_detail"])
        self.assertIn("inconclusive, not proof the host is offline", registered["status_detail"])
        self.assertNotIn("MISSING EXPECTED PORTS", registered["status_detail"])
        self.assertEqual(registered["asset_id"], "AHU-1")
        unregistered = by_address["10.0.0.2"]
        self.assertNotIn("EXPECTED BY REGISTER", unregistered["status_detail"])
        self.assertIsNone(unregistered["asset_id"])

    def test_persist_records_only_for_responders(self) -> None:
        # A mixed /30: 10.0.0.1 answers on 80, 10.0.0.2 is silent. persist_records
        # must receive a structured record for the RESPONDER ONLY — a host we
        # never heard from is not a discovered device and must not enter the
        # device inventory or reports — while both hosts appear on the display
        # surface (discovered_assets).
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return host == "10.0.0.1" and port == 80

        result = ip_scan.process_ip_discovery_run(
            "run_persist_split",
            {**_AUTH, "cidr": "10.0.0.0/30", "ports": [80]},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(persisted), 1)
        _rid, records = persisted[0]
        self.assertEqual(
            [r["address"] for r in records],
            ["10.0.0.1"],
            "only the responder becomes a device record",
        )
        self.assertEqual(len(store.summary_calls[-1]["discovered_assets"]), 2)

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

    def test_mid_batch_cancel_does_not_fabricate_missing_expected(self) -> None:
        # REGRESSION: a Stop that cut a responsive host's port batch short verdicted
        # missing_expected against the PLANNED port list, so a never-probed expected
        # port got a false "MISSING EXPECTED PORTS" red chip and scanned_ports
        # over-claimed the sweep. Only ports actually probed may be verdicted.
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            # 10.0.0.1:80 is open; probing it flips cancel so port 8080 (dispatched
            # serially after it) is never probed on this host.
            if host == "10.0.0.1" and port == 80:
                store._cancel = True
                return True
            return False

        result = ip_scan.process_ip_discovery_run(
            "run_midbatch_cancel",
            {
                **_AUTH,
                "cidr": "10.0.0.0/30",
                "ports": [80, 8080],
                "expected_ports_by_address": {"10.0.0.1": "80/tcp,8080/tcp"},
            },
            run_store=store,
            execution_mode="x",
            throttle=ThrottleConfig(max_concurrency=1, rate_limit_per_sec=None),
            connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )

        self.assertEqual(result["status"], "cancelled")
        host_row = next(a for a in store.summary_calls[-1]["discovered_assets"] if a["ip_address"] == "10.0.0.1")
        # 8080 was never probed, so it must NOT be verdicted a missing expected port.
        self.assertNotIn("MISSING EXPECTED PORTS", host_row["status_detail"])
        _rid, records = persisted[0]
        record = next(r for r in records if r["address"] == "10.0.0.1")
        self.assertEqual(record["attributes"]["scanned_ports"], [80])
        self.assertEqual(record["attributes"]["missing_expected_ports"], [])


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

    def test_reverse_dns_injection_is_ignored(self) -> None:
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_rdns", {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80], "reverse_dns": True},
            run_store=store, execution_mode="x",
            connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(store.summary_calls[-1]["discovered_assets"][0]["hostname"])

    def test_arp_injection_is_ignored(self) -> None:
        store = FakeRunStore()

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_arp", {**_AUTH, "cidr": "127.0.0.1/32", "ports": [80]},
            run_store=store, execution_mode="x",
            connect=fake_connect,
        )
        self.assertEqual(result["status"], "succeeded")
        asset = store.summary_calls[-1]["discovered_assets"][0]
        self.assertIsNone(asset["mac_address"])
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
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn("HOSTNAME MISMATCH", summary["discovered_assets"][0]["status_detail"])

    def test_expected_hostname_is_not_compared_without_provider_evidence(self) -> None:
        store = FakeRunStore()
        persisted: list[tuple[str, list[dict[str, Any]]]] = []

        async def fake_connect(host: str, port: int, timeout: float) -> bool:
            return True

        result = ip_scan.process_ip_discovery_run(
            "run_host_mismatch",
            {**_AUTH, "cidr": "10.0.0.1/32", "ports": [80], "reverse_dns": True,
             "expected_hostname_by_address": {"10.0.0.1": "ahu-l03-017"}},
            run_store=store, execution_mode="x", connect=fake_connect,
            persist_records=lambda rid, recs: persisted.append((rid, list(recs))),
        )
        self.assertEqual(result["status"], "succeeded")
        summary = store.summary_calls[-1]
        self.assertEqual(summary["hosts_with_hostname_mismatch"], 0)
        self.assertNotIn(
            "HOSTNAME MISMATCH",
            summary["discovered_assets"][0]["status_detail"],
        )
        # The register's expectation is persisted alongside the observation.
        self.assertEqual(persisted[0][1][0]["attributes"]["expected_hostname"], "ahu-l03-017")
        self.assertIsNone(persisted[0][1][0]["attributes"]["hostname"])

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

    def test_legacy_false_result_does_not_claim_expected_port_is_closed(self) -> None:
        # The compatibility callback returns only a Boolean. False can mean a
        # timeout, routing failure, or refusal, so it cannot prove that 445 is
        # closed. The typed provider has a separate refusal regression above.
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
        self.assertEqual(summary["hosts_with_missing_expected"], 0)
        detail = summary["discovered_assets"][0]["status_detail"]
        self.assertNotIn("MISSING EXPECTED PORTS", detail)
        self.assertNotIn("EXPECTED PORTS OK", detail)
        self.assertEqual(
            persisted[0][1][0]["attributes"]["missing_expected_ports"],
            [],
        )

    def test_all_expected_open_records_explicit_pass(self) -> None:
        # field engineer's field case: blank port field (-> defaults) with the register
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


if __name__ == "__main__":
    unittest.main()
