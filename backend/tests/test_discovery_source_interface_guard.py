"""API tests for the dispatch-time Source Interface availability guard.

Boots the FastAPI app in api_key auth mode against the shared temporary SQLite
database (harness.ApiTestCase: env overrides + cache clears BEFORE app.main is
imported, TestClient entered as a context manager so the startup lifespan
applies migrations).

Covered: every preview freezes a concrete NIC identity, while a real discovery
run whose frozen identity disappears is rejected with HTTP 400 before any run
record is persisted (no orphaned run and no fallback to another NIC). A dry
preview skips only the bind proof. A passing live run repeats the guard before
inline transport and persists the exact source identity for queued execution.

The resolver and shared runtime guard are patched at the module attributes the
route calls, so no test depends on the CI host's real NICs. A DEDICATED
project/site pair is used
because the configuration snapshot is shared per (project, site) across the
whole test process — writing a Source Interface into demo-project would leak
into the other API test modules' runs.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from harness import ApiTestCase

_SHARED_KEY = "test-source-interface-guard-admin-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _SHARED_KEY,
}

_PROJECT_ID = "nic-guard-project"
_SITE_ID = "nic-guard-site"
_SOURCE_INTERFACE = "192.168.77.5/24"

# The exact operator-facing message contract (mirrors interface_service).
_NOT_PRESENT_DETAIL = (
    "Source Interface 192.168.77.5 is not present on this host. Reconnect the adapter, "
    "or open the Configuration page and re-select a current network adapter as "
    "Source Interface (for IP and MQTT runs, 'Auto (OS default route)' also "
    "works; a BACnet scan requires a specific adapter)."
)

_GUARD_TARGET = "app.api.routes.discovery.interface_service.guard_frozen_source_interface"
_RESOLVER_TARGET = "app.services.engine_dispatch.interface_service.resolve_source_interface_identity"


def _frozen_identity() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "selection": "explicit",
        "executor_scope": "inline:smart-commissioning-local",
        "interface_id": "test-if:77",
        "interface_name": "Test field NIC",
        "source_ip": "192.168.77.5",
        "prefix_length": 24,
        "local_address": "192.168.77.5/24",
        "default_route_metric": None,
    }


class DiscoverySourceInterfaceGuardTests(ApiTestCase):
    env = _ENV_OVERRIDES

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        from app.core.db import get_engine
        from smart_commissioning_core.db.db_run_store import DbRunStore

        DbRunStore(get_engine()).create_run(
            project_id=_PROJECT_ID,
            site_id=_SITE_ID,
            job_type="ip_discovery",
            parameters={"requested_from": "test_discovery_source_interface_guard"},
        )

        # Runs are created as an engineer (run creation is engineer+), provisioned
        # once via the standalone shared admin key and explicitly granted this
        # suite's dedicated project/site.
        engineer = cls._provision_user("nic-guard-engineer", "engineer")
        cls._engineer_key = engineer["api_key"]
        grant = cls.client.post(
            f"/api/v1/users/{engineer['user']['id']}/scope-grants",
            headers=cls._admin_headers(),
            json={
                "project_id": _PROJECT_ID,
                "site_id": _SITE_ID,
                "reason": "Source-interface guard test fixture",
            },
        )
        assert grant.status_code == 201, grant.text

        # Save a configuration whose device."Source Interface" is a concrete NIC
        # for the DEDICATED guard project/site (see module docstring).
        configuration = cls.client.get(
            "/api/v1/configuration",
            headers=cls._engineer_headers_cls(),
            params={"project_id": _PROJECT_ID, "site_id": _SITE_ID},
        ).json()
        configuration["device"]["values"]["Source Interface"] = _SOURCE_INTERFACE
        response = cls.client.put(
            "/api/v1/configuration",
            headers=cls._engineer_headers_cls(),
            params={"project_id": _PROJECT_ID, "site_id": _SITE_ID},
            json=configuration,
        )
        assert response.status_code == 200, response.text

    @classmethod
    def _admin_headers(cls) -> dict[str, str]:
        return {"X-API-Key": _SHARED_KEY}

    @classmethod
    def _engineer_headers_cls(cls) -> dict[str, str]:
        return {"X-API-Key": cls._engineer_key}

    @classmethod
    def _provision_user(cls, username: str, role: str) -> dict:
        response = cls.client.post(
            "/api/v1/users",
            headers=cls._admin_headers(),
            json={"username": username, "role": role},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _engineer_headers(self) -> dict[str, str]:
        return {"X-API-Key": self._engineer_key}

    def _post_ip_run(
        self,
        parameters: dict,
        *,
        preview_run_id: str | None = None,
        scan_authorization_id: str | None = None,
    ) -> object:
        return self.client.post(
            "/api/v1/discovery/ip/runs",
            headers=self._engineer_headers(),
            json={
                "project_id": _PROJECT_ID,
                "site_id": _SITE_ID,
                "job_type": "ip_discovery",
                "parameters": parameters,
                "preview_run_id": preview_run_id,
                "scan_authorization_id": scan_authorization_id,
            },
        )

    def _authorized_live_payload(self, parameters: dict) -> tuple[str, str]:
        with patch(_RESOLVER_TARGET, return_value=_frozen_identity()):
            preview = self._post_ip_run({**parameters, "dry_run": True})
        self.assertEqual(preview.status_code, 200, preview.text)
        now = datetime.now(UTC)
        authorization = self.client.post(
            "/api/v1/discovery/scan-authorizations",
            headers=self._admin_headers(),
            json={
                "preview_run_id": preview.json()["run_id"],
                "ticket": "CHG-NIC-GUARD",
                "purpose": "Source-interface dispatch guard test",
                "not_before": (now - timedelta(minutes=1)).isoformat(),
                "not_after": (now + timedelta(hours=1)).isoformat(),
            },
        )
        self.assertEqual(authorization.status_code, 201, authorization.text)
        return preview.json()["run_id"], authorization.json()["authorization_id"]

    def _run_count(self) -> int:
        response = self.client.get("/api/v1/discovery/runs", headers=self._engineer_headers())
        self.assertEqual(response.status_code, 200, response.text)
        return len(response.json()["runs"])

    def test_unavailable_source_interface_rejected_before_run_creation(self) -> None:
        preview_run_id, authorization_id = self._authorized_live_payload(
            {"addresses": ["192.168.77.10"]}
        )
        runs_before = self._run_count()
        with patch(_GUARD_TARGET, side_effect=ValueError(_NOT_PRESENT_DETAIL)) as guard:
            response = self._post_ip_run(
                {},
                preview_run_id=preview_run_id,
                scan_authorization_id=authorization_id,
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], _NOT_PRESENT_DETAIL)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(
            guard.call_args.kwargs["expected_executor_scope"],
            "inline:smart-commissioning-local",
        )
        self.assertEqual(self._run_count(), runs_before, "a rejected request must not persist an orphaned run")

    def test_dry_run_freezes_identity_but_skips_bind_guard(self) -> None:
        with (
            patch(_RESOLVER_TARGET, return_value=_frozen_identity()) as resolver,
            patch(_GUARD_TARGET, side_effect=ValueError(_NOT_PRESENT_DETAIL)) as guard,
        ):
            response = self._post_ip_run({"dry_run": True, "addresses": ["192.168.77.10"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded")
        resolver.assert_called_once()
        guard.assert_not_called()

    def test_available_source_interface_creates_run_with_injected_parameters(self) -> None:
        preview_run_id, authorization_id = self._authorized_live_payload(
            {
                "addresses": ["192.168.77.10"],
                "ports": [9],
                "scan_connect_timeout_s": 1,
                "scan_rate_limit_per_sec": 1,
            }
        )
        with patch(_GUARD_TARGET, return_value=None) as guard:
            response = self._post_ip_run(
                {},
                preview_run_id=preview_run_id,
                scan_authorization_id=authorization_id,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(guard.call_count, 2)
        for call in guard.call_args_list:
            self.assertEqual(
                call.kwargs["expected_executor_scope"],
                "inline:smart-commissioning-local",
            )

        # The injected source NIC is persisted into run.parameters (the worker
        # path reads run.parameters, not the inline dict). The run itself may
        # honestly FAIL on this host (the engine's bind pre-check cannot bind
        # 192.168.77.5) — that engine-level honesty is covered in core tests.
        run_id = response.json()["run_id"]
        run = self.client.get(f"/api/v1/discovery/runs/{run_id}", headers=self._engineer_headers())
        self.assertEqual(run.status_code, 200, run.text)
        parameters = run.json()["parameters"]
        self.assertEqual(parameters["source_ip"], "192.168.77.5")
        self.assertEqual(parameters["local_address"], "192.168.77.5/24")
        self.assertEqual(parameters["source_interface_identity_v1"], _frozen_identity())

    def test_interface_disappearing_after_creation_fails_before_inline_transport(self) -> None:
        preview_run_id, authorization_id = self._authorized_live_payload(
            {"addresses": ["192.168.77.10"], "ports": [443]}
        )
        processor_target = "app.api.routes.discovery.process_ip_discovery_run"
        with (
            patch(
                _GUARD_TARGET,
                side_effect=[None, ValueError(_NOT_PRESENT_DETAIL)],
            ) as guard,
            patch(processor_target) as processor,
        ):
            response = self._post_ip_run(
                {},
                preview_run_id=preview_run_id,
                scan_authorization_id=authorization_id,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(guard.call_count, 2)
        processor.assert_not_called()
        run = self.client.get(
            f"/api/v1/discovery/runs/{response.json()['run_id']}",
            headers=self._engineer_headers(),
        )
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["status"], "failed")
        self.assertEqual(run.json()["parameters"]["source_ip"], "192.168.77.5")
        self.assertEqual(
            run.json()["parameters"]["source_interface_identity_v1"],
            _frozen_identity(),
        )


class QueuedNetworkExecutorPolicyTests(ApiTestCase):
    env = {
        "JOB_EXECUTION_MODE": "queue",
        "SMART_COMMISSIONING_NETWORK_EXECUTOR_ID": None,
        "AUTH_MODE": "api_key",
        "API_KEY": _SHARED_KEY,
    }

    def test_queue_preview_requires_an_explicit_network_executor(self) -> None:
        headers = {"X-API-Key": _SHARED_KEY}
        before = self.client.get("/api/v1/discovery/runs", headers=headers)
        self.assertEqual(before.status_code, 200, before.text)

        response = self.client.post(
            "/api/v1/discovery/ip/runs",
            headers=headers,
            json={
                "project_id": "queued-network-policy-project",
                "site_id": "queued-network-policy-site",
                "job_type": "ip_discovery",
                "parameters": {
                    "dry_run": True,
                    "addresses": ["192.0.2.10"],
                    "ports": [443],
                },
            },
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("SMART_COMMISSIONING_NETWORK_EXECUTOR_ID", response.json()["detail"])
        self.assertIn("same OS network namespace", response.json()["detail"])
        after = self.client.get("/api/v1/discovery/runs", headers=headers)
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["runs"], before.json()["runs"])


if __name__ == "__main__":
    unittest.main()
