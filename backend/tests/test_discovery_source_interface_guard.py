"""API tests for the dispatch-time Source Interface availability guard.

Boots the FastAPI app in api_key auth mode against the shared temporary SQLite
database (harness.ApiTestCase: env overrides + cache clears BEFORE app.main is
imported, TestClient entered as a context manager so the startup lifespan
applies migrations).

Covered: a real (non-dry-run) discovery run whose EFFECTIVE source_ip fails
``interface_service.ensure_source_ip_available`` is rejected with HTTP 400 and
the guard's exact actionable message, BEFORE any run record is persisted (no
orphaned runs, no silent fallback to another NIC); a dry_run skips the guard
(side-effect-free preview convention); and when the guard passes, the created
run persists the injected source_ip / local_address for the worker path.

The guard itself is patched at the module attribute the route looks up at call
time (``app.services.interface_service.ensure_source_ip_available``), so no
test depends on the CI host's real NICs. A DEDICATED project/site pair is used
because the configuration snapshot is shared per (project, site) across the
whole test process — writing a Source Interface into demo-project would leak
into the other API test modules' runs.
"""

import unittest
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
    "or set Source Interface to 'Auto (OS default route)' on the Configuration page."
)

_GUARD_TARGET = "app.services.interface_service.ensure_source_ip_available"


class DiscoverySourceInterfaceGuardTests(ApiTestCase):
    env = _ENV_OVERRIDES

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        # Runs are created as an engineer (run creation is engineer+), provisioned
        # once via the shared admin key.
        cls._engineer_key = cls._provision_user("nic-guard-engineer", "engineer")

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
    def _provision_user(cls, username: str, role: str) -> str:
        response = cls.client.post(
            "/api/v1/users",
            headers=cls._admin_headers(),
            json={"username": username, "role": role},
        )
        assert response.status_code == 201, response.text
        return response.json()["api_key"]

    def _engineer_headers(self) -> dict[str, str]:
        return {"X-API-Key": self._engineer_key}

    def _post_ip_run(self, parameters: dict) -> object:
        return self.client.post(
            "/api/v1/discovery/ip/runs",
            headers=self._engineer_headers(),
            json={
                "project_id": _PROJECT_ID,
                "site_id": _SITE_ID,
                "job_type": "ip_discovery",
                "parameters": parameters,
            },
        )

    def _run_count(self) -> int:
        response = self.client.get("/api/v1/discovery/runs", headers=self._engineer_headers())
        self.assertEqual(response.status_code, 200, response.text)
        return len(response.json()["runs"])

    def test_unavailable_source_interface_rejected_before_run_creation(self) -> None:
        runs_before = self._run_count()
        with patch(_GUARD_TARGET, side_effect=ValueError(_NOT_PRESENT_DETAIL)) as guard:
            response = self._post_ip_run({"authorized": True, "addresses": ["192.168.77.10"]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], _NOT_PRESENT_DETAIL)
        guard.assert_called_once_with("192.168.77.5")
        self.assertEqual(self._run_count(), runs_before, "a rejected request must not persist an orphaned run")

    def test_dry_run_skips_the_guard(self) -> None:
        # Previews are side-effect-free by convention: the guard must not run.
        with patch(_GUARD_TARGET, side_effect=ValueError(_NOT_PRESENT_DETAIL)) as guard:
            response = self._post_ip_run({"dry_run": True, "addresses": ["192.168.77.10"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "succeeded")
        guard.assert_not_called()

    def test_available_source_interface_creates_run_with_injected_parameters(self) -> None:
        with patch(_GUARD_TARGET, return_value=None) as guard:
            response = self._post_ip_run(
                {
                    "authorized": True,
                    "addresses": ["192.168.77.10"],
                    "ports": [9],
                    "scan_connect_timeout_s": 1,
                    "scan_rate_limit_per_sec": 0,
                }
            )
        self.assertEqual(response.status_code, 200, response.text)
        guard.assert_called_once_with("192.168.77.5")

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


if __name__ == "__main__":
    unittest.main()
