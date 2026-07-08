"""API tests for GET /api/v1/system/interfaces (NIC enumeration endpoint).

Boots the FastAPI app in api_key auth mode against a temporary SQLite database,
using the same harness as test_runs_api.py: the env vars (incl. the shared
SCT_TEST_DATABASE_URL) are set and the settings/engine caches cleared in
setUpClass BEFORE app.main is imported, and the TestClient is entered as a
context manager so the startup lifespan applies the Alembic migrations.

The enumerator ``interface_service.list_usable_interfaces`` is patched so the
tests do not depend on the CI host's real NICs. Covered here: the endpoint
returns the mocked list, requires auth (401 without a key), is allowed for a
viewer, and every returned object carries EXACTLY the nine allowed fields.
Gateway/DNS are deliberately exposed (product-owner reversal of the proposal's
section-5.3 omission, 2026-07-03 meeting); the leak guard still proves MAC /
driver / InterfaceDescription strings never cross the API.
"""

import unittest
from unittest.mock import patch

from harness import ApiTestCase

_SHARED_KEY = "test-system-interfaces-shared-admin-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _SHARED_KEY,
}

_ALLOWED_KEYS = {
    "name",
    "ipv4",
    "prefix_length",
    "cidr",
    "is_up",
    "adapter_type",
    "subnet_mask",
    "gateway",
    "dns_servers",
}

# Two fixed interfaces returned by the patched enumerator (is_up first, as the
# service sorts). Shaped exactly like SystemInterface; dicts serialize identically
# under the list[SystemInterface] response_model.
_FIXED_INTERFACES = [
    {
        "name": "Ethernet 3",
        "ipv4": "192.168.1.10",
        "prefix_length": 24,
        "cidr": "192.168.1.10/24",
        "is_up": True,
        "adapter_type": "ethernet",
        "subnet_mask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "dns_servers": ["192.168.1.53", "8.8.8.8"],
    },
    {
        "name": "Wi-Fi",
        "ipv4": "10.0.0.5",
        "prefix_length": 16,
        "cidr": "10.0.0.5/16",
        "is_up": False,
        "adapter_type": "wifi",
        "subnet_mask": "255.255.0.0",
        "gateway": None,
        "dns_servers": [],
    },
]


class SystemInterfacesApiTests(ApiTestCase):
    # No default header: each request chooses its own actor (shared admin key
    # or a per-user viewer key) explicitly.
    env = _ENV_OVERRIDES

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()

        # A viewer user, provisioned once via the shared admin key.
        cls._viewer_key = cls._provision_user("sys-iface-viewer", "viewer")

    @classmethod
    def _admin_headers(cls) -> dict[str, str]:
        return {"X-API-Key": _SHARED_KEY}

    @classmethod
    def _provision_user(cls, username: str, role: str) -> str:
        response = cls.client.post(
            "/api/v1/users",
            headers=cls._admin_headers(),
            json={"username": username, "role": role},
        )
        assert response.status_code == 201, response.text
        return response.json()["api_key"]

    def _viewer_headers(self) -> dict[str, str]:
        return {"X-API-Key": self._viewer_key}

    def test_returns_mocked_interfaces(self) -> None:
        with patch(
            "app.services.interface_service.list_usable_interfaces",
            return_value=list(_FIXED_INTERFACES),
        ):
            response = self.client.get("/api/v1/system/interfaces", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), _FIXED_INTERFACES)

    def test_requires_authentication(self) -> None:
        # No key at all -> 401 (fail closed); the enumerator must not even run.
        with patch(
            "app.services.interface_service.list_usable_interfaces",
            return_value=list(_FIXED_INTERFACES),
        ) as enumerator:
            response = self.client.get("/api/v1/system/interfaces")
        self.assertEqual(response.status_code, 401, response.text)
        enumerator.assert_not_called()

    def test_viewer_is_allowed(self) -> None:
        with patch(
            "app.services.interface_service.list_usable_interfaces",
            return_value=list(_FIXED_INTERFACES),
        ):
            response = self.client.get("/api/v1/system/interfaces", headers=self._viewer_headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), len(_FIXED_INTERFACES))

    def test_response_exposes_only_allowed_fields(self) -> None:
        with patch(
            "app.services.interface_service.list_usable_interfaces",
            return_value=list(_FIXED_INTERFACES),
        ):
            response = self.client.get("/api/v1/system/interfaces", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload, "expected the mocked interfaces to be returned")
        for interface in payload:
            self.assertEqual(
                set(interface),
                _ALLOWED_KEYS,
                "endpoint must expose EXACTLY the nine contract fields — gateway/DNS are "
                "deliberately included (product-owner reversal of section 5.3) while "
                "MAC / driver / InterfaceDescription strings must never leak",
            )


if __name__ == "__main__":
    unittest.main()
