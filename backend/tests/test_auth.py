"""Authentication tests for the API (app.core.auth.require_auth).

Each test class boots the app with its own auth environment: env vars are set
and the settings cache is cleared in setUpClass before the TestClient is
created, mirroring the DATABASE_URL pattern in test_runs_api.py.

The database is shared per process (see _shared_test_database_url): route
modules instantiate their services -- and therefore the SQLAlchemy engine --
at the first app.main import, so every test class in the test run must point
at the same database file.

Client addresses are simulated with Starlette's TestClient(client=(host, port))
parameter. The default "testclient" host is treated as loopback by
app.core.auth because it is the ASGI test transport's synthetic host and can
never appear as a real TCP peer address.
"""

import atexit
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

_API_KEY = "test-auth-secret-key"

# A documentation-range (TEST-NET-3, RFC 5737) address: clearly non-loopback.
_NON_LOOPBACK = ("203.0.113.9", 51234)
_LOOPBACK = ("127.0.0.1", 51234)

# Any authenticated route works as a probe; /blueprint is cheap (no DB writes).
_PROTECTED_PATH = "/api/v1/blueprint"


def _shared_test_database_url() -> str:
    """Process-wide temporary SQLite database shared by all API test modules.

    The directory is removed at interpreter exit (best effort: lingering
    SQLite handles can block deletion on Windows, hence ignore_errors).
    """
    existing = os.environ.get("SCT_TEST_DATABASE_URL")
    if existing:
        return existing
    temp_dir = tempfile.mkdtemp(prefix="sct-test-db-")
    atexit.register(shutil.rmtree, temp_dir, ignore_errors=True)
    url = f"sqlite:///{(Path(temp_dir) / 'smart_commissioning.db').as_posix()}"
    os.environ["SCT_TEST_DATABASE_URL"] = url
    return url


class _AuthClientTestCase(unittest.TestCase):
    """Shared scaffolding: env overrides + cache reset + lifespan-entered client."""

    # Subclasses override; None means "ensure the variable is unset".
    auth_env: dict[str, str | None] = {}
    client_addr: tuple[str, int] = ("testclient", 50000)

    @classmethod
    def setUpClass(cls) -> None:
        overrides: dict[str, str | None] = {
            "DATABASE_URL": _shared_test_database_url(),
            "JOB_EXECUTION_MODE": "inline",
            **cls.auth_env,
        }
        cls._previous_env = {}
        for key, value in overrides.items():
            cls._previous_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        from app.core import config as config_module
        from app.core import db as db_module

        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        from app.main import app
        from fastapi.testclient import TestClient

        cls.app = app
        cls._client_context = TestClient(app, client=cls.client_addr)
        cls.client = cls._client_context.__enter__()

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

    def _client_for(self, addr: tuple[str, int]):
        """Extra client simulating a different peer address.

        Not entered as a context manager: the lifespan (migrations) already
        ran for cls.client, and requests work without re-entering it.
        """
        from fastapi.testclient import TestClient

        return TestClient(self.app, client=addr)


class ApiKeyModeTests(_AuthClientTestCase):
    auth_env = {"AUTH_MODE": "api_key", "API_KEY": _API_KEY}

    def test_missing_key_is_401(self) -> None:
        response = self.client.get(_PROTECTED_PATH)
        self.assertEqual(response.status_code, 401)
        self.assertIn("detail", response.json())

    def test_wrong_key_is_401_and_does_not_echo_key_material(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"X-API-Key": "wrong-key"})
        self.assertEqual(response.status_code, 401)
        body = response.text
        self.assertNotIn(_API_KEY, body)
        self.assertNotIn("wrong-key", body)

    def test_valid_key_via_x_api_key_header(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"X-API-Key": _API_KEY})
        self.assertEqual(response.status_code, 200, response.text)

    def test_valid_key_via_bearer_authorization(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"Authorization": f"Bearer {_API_KEY}"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_bearer_authorization_scheme_is_401(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"Authorization": f"Basic {_API_KEY}"})
        self.assertEqual(response.status_code, 401)

    def test_health_endpoints_reachable_without_key(self) -> None:
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/ready").status_code, 200)

    def test_import_format_helpers_reachable_without_key(self) -> None:
        # The import profile list and blank templates are public format helpers
        # (import-type names, required column headers, one example row -- no
        # project data), so they answer without a key even in api_key mode,
        # unlike _PROTECTED_PATH above which 401s. Regression guard for the
        # "Template download failed -- Authentication required" report.
        self.assertEqual(self.client.get("/api/v1/imports/profiles").status_code, 200)
        for fmt in ("csv", "xlsx"):
            response = self.client.get(f"/api/v1/imports/templates/ip_register.{fmt}")
            self.assertEqual(response.status_code, 200, f"{fmt}: {response.text}")
            self.assertTrue(response.content)

    def test_template_bad_inputs_return_400_not_422(self) -> None:
        # Both an unknown import type and an unknown file extension return 400
        # (import_type is validated in-handler, not via an enum path param that
        # would 422). The route stays public -- no key required to be rejected.
        bad_type = self.client.get("/api/v1/imports/templates/not_a_type.csv")
        self.assertEqual(bad_type.status_code, 400, bad_type.text)
        bad_ext = self.client.get("/api/v1/imports/templates/ip_register.pdf")
        self.assertEqual(bad_ext.status_code, 400, bad_ext.text)

    def test_schema_endpoints_hidden_in_api_key_mode(self) -> None:
        # Hosted deployments must not disclose the API surface to
        # unauthenticated clients: schema endpoints answer 404.
        for path in ("/openapi.json", "/docs", "/redoc"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


class ApiKeyModeFailClosedTests(_AuthClientTestCase):
    """AUTH_MODE=api_key with no key configured rejects everything."""

    auth_env = {"AUTH_MODE": "api_key", "API_KEY": None}

    def test_request_without_key_is_401(self) -> None:
        self.assertEqual(self.client.get(_PROTECTED_PATH).status_code, 401)

    def test_request_with_any_key_is_401(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"X-API-Key": "anything"})
        self.assertEqual(response.status_code, 401)

    def test_empty_configured_key_also_fails_closed(self) -> None:
        # Even an empty presented key never matches an unset configured key.
        response = self.client.get(_PROTECTED_PATH, headers={"Authorization": "Bearer "})
        self.assertEqual(response.status_code, 401)

    def test_health_endpoints_stay_probeable(self) -> None:
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/ready").status_code, 200)


class LocalModeTests(_AuthClientTestCase):
    auth_env = {"AUTH_MODE": "local", "API_KEY": None}

    def test_testclient_host_is_treated_as_loopback(self) -> None:
        self.assertEqual(self.client.get(_PROTECTED_PATH).status_code, 200)

    def test_loopback_ipv4_client_is_allowed(self) -> None:
        response = self._client_for(_LOOPBACK).get(_PROTECTED_PATH)
        self.assertEqual(response.status_code, 200, response.text)

    def test_loopback_ipv6_client_is_allowed(self) -> None:
        response = self._client_for(("::1", 51234)).get(_PROTECTED_PATH)
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_loopback_client_is_401(self) -> None:
        response = self._client_for(_NON_LOOPBACK).get(_PROTECTED_PATH)
        self.assertEqual(response.status_code, 401)

    def test_non_loopback_client_with_key_is_401_when_no_key_configured(self) -> None:
        response = self._client_for(_NON_LOOPBACK).get(_PROTECTED_PATH, headers={"X-API-Key": "anything"})
        self.assertEqual(response.status_code, 401)

    def test_health_reachable_from_non_loopback(self) -> None:
        self.assertEqual(self._client_for(_NON_LOOPBACK).get("/api/v1/health").status_code, 200)

    def test_schema_endpoints_served_in_local_mode(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("paths", response.json())


class LocalModeInactiveUserKeyTests(_AuthClientTestCase):
    """local mode: a key matching a DEACTIVATED user is rejected outright (401).

    Regression guard for the loopback fall-through bug: _resolve_user_principal
    used to return None for an inactive user's key, so on the portable (local)
    profile the request fell through to the keyless-loopback trust and a
    deactivated user's key kept granting synthetic ADMIN from the laptop. The
    module contract (app.core.auth docstring) has always said an inactive key
    is rejected and never falls through; these tests pin the code to it while
    proving the keyless-loopback bootstrap path is unchanged.
    """

    auth_env = {"AUTH_MODE": "local", "API_KEY": None}

    def _create_user(self, role: str) -> dict:
        """Create a user via the keyless-loopback admin; return the JSON body.

        Usernames are unique per call: the SQLite database is shared across the
        whole test process, so a fixed name would 409 on a re-run in-process.
        """
        response = self.client.post(
            "/api/v1/users",
            json={"username": f"local-{role}-{uuid.uuid4().hex[:8]}", "role": role},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_active_user_key_from_loopback_resolves_to_that_user(self) -> None:
        created = self._create_user("viewer")
        me = self.client.get(
            "/api/v1/me", headers={"X-API-Key": created["api_key"]}
        )
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["username"], created["user"]["username"])
        self.assertEqual(me.json()["source"], "user_key")

    def test_inactive_user_key_from_loopback_is_401_not_local_admin(self) -> None:
        created = self._create_user("engineer")
        deactivated = self.client.post(
            f"/api/v1/users/{created['user']['id']}/deactivate"
        )
        self.assertEqual(deactivated.status_code, 200, deactivated.text)
        # The deactivated user's key must NOT fall through to loopback admin.
        for path in (_PROTECTED_PATH, "/api/v1/me"):
            response = self.client.get(path, headers={"X-API-Key": created["api_key"]})
            self.assertEqual(response.status_code, 401, f"{path}: {response.text}")

    def test_keyless_loopback_admin_is_preserved(self) -> None:
        # The bootstrap path is untouched: a loopback client with NO key is
        # still the synthetic local admin.
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "admin")
        self.assertEqual(response.json()["source"], "local")

    def test_remote_deactivated_key_indistinguishable_from_unknown_key(self) -> None:
        # No key-validity oracle: from a NON-loopback client in local mode, a
        # key matching a deactivated user row must be indistinguishable from a
        # key matching no row — same status AND the same generic detail.
        created = self._create_user("viewer")
        deactivated = self.client.post(
            f"/api/v1/users/{created['user']['id']}/deactivate"
        )
        self.assertEqual(deactivated.status_code, 200, deactivated.text)
        remote = self._client_for(_NON_LOOPBACK)
        deactivated_key = remote.get(_PROTECTED_PATH, headers={"X-API-Key": created["api_key"]})
        unknown_key = remote.get(_PROTECTED_PATH, headers={"X-API-Key": "matches-no-user-row"})
        self.assertEqual(deactivated_key.status_code, 401)
        self.assertEqual(unknown_key.status_code, 401)
        self.assertEqual(deactivated_key.json()["detail"], unknown_key.json()["detail"])

    def test_unknown_key_from_loopback_still_falls_through_to_local_admin(self) -> None:
        # Existing (deliberate) behaviour, preserved: a key matching NO user row
        # does not block the loopback trust in local mode. Only a key that
        # matches a real user row must resolve as that user or be rejected.
        response = self.client.get(
            "/api/v1/me", headers={"X-API-Key": "matches-no-user-row"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "local")


class LocalModeWithApiKeyTests(_AuthClientTestCase):
    """local mode + configured key: valid key is accepted from anywhere."""

    auth_env = {"AUTH_MODE": "local", "API_KEY": _API_KEY}
    client_addr = _NON_LOOPBACK

    def test_non_loopback_without_key_is_401(self) -> None:
        self.assertEqual(self.client.get(_PROTECTED_PATH).status_code, 401)

    def test_non_loopback_with_valid_key_is_allowed(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"X-API-Key": _API_KEY})
        self.assertEqual(response.status_code, 200, response.text)

    def test_non_loopback_with_wrong_key_is_401(self) -> None:
        response = self.client.get(_PROTECTED_PATH, headers={"X-API-Key": "wrong-key"})
        self.assertEqual(response.status_code, 401)

    def test_loopback_still_allowed_without_key(self) -> None:
        response = self._client_for(_LOOPBACK).get(_PROTECTED_PATH)
        self.assertEqual(response.status_code, 200, response.text)


class LoopbackHostUnitTests(unittest.TestCase):
    """Unit coverage of the ip-address logic backing local mode."""

    def test_loopback_hosts(self) -> None:
        from app.core.auth import is_loopback_host

        for host in ("127.0.0.1", "127.0.0.5", "127.255.255.254", "::1", "::ffff:127.0.0.1", "testclient"):
            self.assertTrue(is_loopback_host(host), host)

    def test_non_loopback_hosts(self) -> None:
        from app.core.auth import is_loopback_host

        for host in ("192.168.1.10", "10.0.0.1", "203.0.113.9", "fe80::1", "::ffff:192.168.1.10", "evil", ""):
            self.assertFalse(is_loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
