"""U3 public-API acceptance proofs for the built-in IP field candidate."""

from __future__ import annotations

import socket
import unittest
from datetime import UTC, datetime, timedelta

from harness import ApiTestCase

_API_KEY = "test-u3-ip-acceptance-api-key"


class U3IPAcceptanceTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "inline",
        "AUTH_MODE": "api_key",
        "API_KEY": _API_KEY,
    }
    client_headers = {"X-API-Key": _API_KEY}

    def test_a1_preview_and_linked_live_freeze_the_same_effective_packet_plan(
        self,
    ) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        try:
            preview = self.client.post(
                "/api/v1/discovery/ip/runs",
                json={
                    "project_id": "project-a",
                    "site_id": "site-a",
                    "job_type": "ip_discovery",
                    "parameters": {
                        "dry_run": True,
                        "target_expressions": [
                            {"kind": "cidr", "cidr": "127.0.0.0/30"},
                            {"kind": "cidr", "cidr": "127.0.0.0/29"},
                            {
                                "kind": "range",
                                "start": "127.0.0.4",
                                "end": "127.0.0.8",
                            },
                            {"kind": "address", "address": "127.0.0.8"},
                            {"kind": "address", "address": "127.0.0.10"},
                        ],
                        "exclusions": [
                            {"kind": "address", "address": "127.0.0.2"},
                            {
                                "kind": "range",
                                "start": "127.0.0.5",
                                "end": "127.0.0.7",
                            },
                        ],
                        "ports": [port],
                        "max_hosts": 16,
                        "scan_max_concurrency": 8,
                        "scan_rate_limit_per_sec": 10,
                        "scan_connect_timeout_s": 0.05,
                    },
                },
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            preview_run_id = preview.json()["run_id"]
            preview_record = self.client.get(
                f"/api/v1/discovery/runs/{preview_run_id}"
            ).json()
            preview_parameters = preview_record["parameters"]
            preview_contract = preview_parameters["scan_contract_v1"]
            targets = preview_contract["ip"]["targets"]

            expected_addresses = [
                "127.0.0.1",
                "127.0.0.3",
                "127.0.0.4",
                "127.0.0.8",
                "127.0.0.10",
            ]
            self.assertEqual(preview_parameters["addresses"], expected_addresses)
            self.assertEqual(targets["sample_addresses"], expected_addresses)
            self.assertEqual(targets["target_count"], 5)
            self.assertEqual(targets["excluded_count"], 4)
            self.assertEqual(
                targets["grouped_ranges"],
                [
                    {"start": "127.0.0.1", "end": "127.0.0.1", "count": 1},
                    {"start": "127.0.0.3", "end": "127.0.0.4", "count": 2},
                    {"start": "127.0.0.8", "end": "127.0.0.8", "count": 1},
                    {"start": "127.0.0.10", "end": "127.0.0.10", "count": 1},
                ],
            )
            self.assertEqual(
                [item["kind"] for item in targets["target_expressions"]],
                ["cidr", "cidr", "range", "address", "address"],
            )
            self.assertEqual(
                targets["exclusions"],
                [
                    {
                        "kind": "address",
                        "cidr": None,
                        "start": None,
                        "end": None,
                        "address": "127.0.0.2",
                    },
                    {
                        "kind": "range",
                        "cidr": None,
                        "start": "127.0.0.5",
                        "end": "127.0.0.7",
                        "address": None,
                    },
                ],
            )

            now = datetime.now(UTC)
            authorization = self.client.post(
                "/api/v1/discovery/scan-authorizations",
                json={
                    "preview_run_id": preview_run_id,
                    "ticket": "CHG-U3-A1",
                    "purpose": "A1 deterministic target normalization acceptance proof",
                    "not_before": (now - timedelta(minutes=1)).isoformat(),
                    "not_after": (now + timedelta(hours=1)).isoformat(),
                },
            )
            self.assertEqual(authorization.status_code, 201, authorization.text)
            approval = authorization.json()
            self.assertEqual(
                approval["packet_plan_sha256"],
                preview_contract["packet_plan_sha256"],
            )

            live = self.client.post(
                "/api/v1/discovery/ip/runs",
                json={
                    "project_id": "project-a",
                    "site_id": "site-a",
                    "job_type": "ip_discovery",
                    "preview_run_id": preview_run_id,
                    "scan_authorization_id": approval["authorization_id"],
                    "parameters": {},
                },
            )
            self.assertEqual(live.status_code, 200, live.text)
            self.assertEqual(live.json()["status"], "succeeded")
            live_run_id = live.json()["run_id"]
            live_record = self.client.get(
                f"/api/v1/discovery/runs/{live_run_id}"
            ).json()
            live_contract = live_record["parameters"]["scan_contract_v1"]
            self.assertEqual(
                live_contract["packet_plan_sha256"],
                approval["packet_plan_sha256"],
            )
            self.assertEqual(live_contract, preview_contract)

            first_listing = self.client.get("/api/v1/discovery/runs")
            self.assertEqual(first_listing.status_code, 200, first_listing.text)
            first_run_ids = [item["run_id"] for item in first_listing.json()["runs"]]
            reloaded = self.client.get(f"/api/v1/discovery/runs/{live_run_id}")
            self.assertEqual(reloaded.status_code, 200, reloaded.text)
            second_listing = self.client.get("/api/v1/discovery/runs")
            self.assertEqual(second_listing.status_code, 200, second_listing.text)
            self.assertEqual(
                [item["run_id"] for item in second_listing.json()["runs"]],
                first_run_ids,
            )
            self.assertEqual(first_run_ids.count(preview_run_id), 1)
            self.assertEqual(first_run_ids.count(live_run_id), 1)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
