"""API tests for GET /api/v1/system/interfaces (NIC enumeration endpoint).

Boots the FastAPI app in api_key auth mode against a temporary SQLite database,
using the same harness as test_runs_api.py: the env vars (incl. the shared
SCT_TEST_DATABASE_URL) are set and the settings/engine caches cleared in
setUpClass BEFORE app.main is imported, and the TestClient is entered as a
context manager so the startup lifespan applies the Alembic migrations.

The enumerator ``interface_service.list_usable_interfaces`` is patched so the
tests do not depend on the CI host's real NICs. Covered here: the endpoint
returns the mocked list, requires auth (401 without a key), is allowed for a
viewer, and every returned object carries EXACTLY the allowed fields — gateway is
now deliberately included (operator-requested; section 5.3), but MAC / DNS /
driver strings are still never exposed.
"""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SHARED_KEY = "test-system-interfaces-shared-admin-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _SHARED_KEY,
}

_ALLOWED_KEYS = {"name", "ipv4", "prefix_length", "subnet_mask", "cidr", "gateway", "is_up"}

# Two fixed interfaces returned by the patched enumerator (is_up first, as the
# service sorts). Shaped exactly like SystemInterface; dicts serialize identically
# under the list[SystemInterface] response_model. The down Wi-Fi NIC has no
# resolvable gateway (None), exercising the nullable field.
_FIXED_INTERFACES = [
    {
        "name": "Ethernet 3",
        "ipv4": "192.168.1.10",
        "prefix_length": 24,
        "subnet_mask": "255.255.255.0",
        "cidr": "192.168.1.10/24",
        "gateway": "192.168.1.1",
        "is_up": True,
    },
    {
        "name": "Wi-Fi",
        "ipv4": "10.0.0.5",
        "prefix_length": 16,
        "subnet_mask": "255.255.0.0",
        "cidr": "10.0.0.5/16",
        "gateway": None,
        "is_up": False,
    },
]


def _shared_test_database_url() -> str:
    """Process-wide temporary SQLite database shared by all API test modules."""
    existing = os.environ.get("SCT_TEST_DATABASE_URL")
    if existing:
        return existing
    temp_dir = tempfile.mkdtemp(prefix="sct-test-db-")
    atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
    url = f"sqlite:///{(Path(temp_dir) / 'smart_commissioning.db').as_posix()}"
    os.environ["SCT_TEST_DATABASE_URL"] = url
    return url


class SystemInterfacesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_env = {}
        for key, value in {"DATABASE_URL": _shared_test_database_url(), **_ENV_OVERRIDES}.items():
            cls._previous_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Reset cached settings/engine so the app picks up the temporary database.
        from app.core import config as config_module
        from app.core import db as db_module

        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        from app.main import app
        from fastapi.testclient import TestClient

        cls.app = app
        # No default header: each request chooses its own actor (shared admin key
        # or a per-user viewer key) explicitly.
        cls._client_context = TestClient(app)
        cls.client = cls._client_context.__enter__()

        # A viewer user, provisioned once via the shared admin key.
        cls._viewer_key = cls._provision_user("sys-iface-viewer", "viewer")

    @classmethod
    def tearDownClass(cls) -> None:
        from app.core import config as config_module
        from app.core import db as db_module

        cls._client_context.__exit__(None, None, None)
        db_module.get_engine().dispose()
        for key, value in cls._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

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
                "endpoint must expose EXACTLY name/ipv4/prefix_length/subnet_mask/cidr/gateway/is_up "
                "(gateway is intentional; still no MAC/DNS/driver leak)",
            )


if __name__ == "__main__":
    unittest.main()
