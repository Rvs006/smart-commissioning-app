"""RBAC + per-user identity tests (app.core.auth, /api/v1/me, /api/v1/users).

Boots the app in api_key auth mode against a temporary SQLite database, using
the same harness as test_auth.py / test_runs_api.py: env vars (incl. the shared
SCT_TEST_DATABASE_URL) are set and the settings/engine caches cleared in
setUpClass BEFORE app.main is imported, and the TestClient is entered as a
context manager so the startup lifespan applies the Alembic migrations (creating
the users table).

What is covered here:
  * the legacy shared key acts as an ADMIN principal (bootstrap), and can create
    the first user (POST /users) — backward compatibility preserved;
  * a created user's ONE-TIME plaintext key is returned and authenticates;
  * /me reflects the authenticating principal's username/role/source for the
    shared key and for each created role;
  * require_role: a non-admin user key gets 403 on POST /users; admin gets 201;
  * an inactive user's key -> 401; an unknown key -> 401 (fail-closed);
  * the api_key_hash is stored, never the plaintext, and never leaked in any
    response (list, create, /me);
  * the Role total-order helper.
"""

import atexit
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

_SHARED_KEY = "test-rbac-shared-admin-key"


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


class RbacApiTests(unittest.TestCase):
    """api_key mode with a shared bootstrap key + per-user keys."""

    @classmethod
    def setUpClass(cls) -> None:
        overrides = {
            "DATABASE_URL": _shared_test_database_url(),
            "JOB_EXECUTION_MODE": "inline",
            "AUTH_MODE": "api_key",
            "API_KEY": _SHARED_KEY,
        }
        cls._previous_env = {}
        for key, value in overrides.items():
            cls._previous_env[key] = os.environ.get(key)
            os.environ[key] = value

        from app.core import config as config_module
        from app.core import db as db_module

        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        from app.main import app
        from fastapi.testclient import TestClient

        cls.app = app
        cls._client_context = TestClient(app)
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

    # -- helpers --------------------------------------------------------------

    def _admin_headers(self) -> dict[str, str]:
        return {"X-API-Key": _SHARED_KEY}

    def _create_user(self, username: str, role: str) -> dict:
        """Create a user via the shared-admin key; return the JSON body."""
        response = self.client.post(
            "/api/v1/users",
            headers=self._admin_headers(),
            json={"username": username, "role": role},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _repo(self):
        """A UserRepository bound to the test engine (used for direct assertions).

        The tests share one SQLite DB across the class and accumulate admin rows,
        so last-admin assertions reduce the live count to exactly ONE active admin
        row by deactivating the extras, then exercise the guard against that row.
        """
        from app.core.db import get_engine
        from smart_commissioning_core.db.repositories import UserRepository

        return UserRepository(get_engine())

    def _reduce_to_single_active_admin(self) -> str:
        """Leave exactly one active admin USER ROW; return that user's id.

        Creates a fresh admin to act as the survivor, then deactivates every
        OTHER currently-active admin row directly via the repository (bypassing
        the route guard, which would itself refuse the final deactivation). This
        isolates the last-admin test from admin rows other tests have created.
        """
        repo = self._repo()
        # Unique survivor per call: tests share one class-level SQLite DB, so a
        # fixed username would 409 on the second test that reduces to one admin.
        survivor = self._create_user(f"last-admin-survivor-{uuid.uuid4().hex[:8]}", "admin")
        survivor_id = survivor["user"]["id"]
        for user in repo.list_users():
            if (
                user["role"] == "admin"
                and user["is_active"]
                and user["id"] != survivor_id
            ):
                repo.set_active(user["id"], is_active=False)
        self.assertEqual(repo.count_active_admins(), 1)
        return survivor_id

    # -- bootstrap / shared-key admin -----------------------------------------

    def test_shared_key_is_admin_and_me_reports_source(self) -> None:
        response = self.client.get("/api/v1/me", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["role"], "admin")
        self.assertEqual(body["source"], "shared_key")

    def test_shared_key_can_create_first_user(self) -> None:
        body = self._create_user("bootstrap-engineer", "engineer")
        self.assertEqual(body["user"]["username"], "bootstrap-engineer")
        self.assertEqual(body["user"]["role"], "engineer")
        # The one-time plaintext key is present and non-trivial.
        self.assertIn("api_key", body)
        self.assertGreaterEqual(len(body["api_key"]), 20)

    # -- per-user key authentication + /me ------------------------------------

    def test_engineer_key_authenticates_and_me_returns_role(self) -> None:
        created = self._create_user("eng-alice", "engineer")
        key = created["api_key"]
        response = self.client.get("/api/v1/me", headers={"X-API-Key": key})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["username"], "eng-alice")
        self.assertEqual(body["role"], "engineer")
        self.assertEqual(body["source"], "user_key")

    def test_me_works_for_each_role(self) -> None:
        for role in ("viewer", "reviewer", "engineer", "admin"):
            created = self._create_user(f"me-{role}", role)
            response = self.client.get(
                "/api/v1/me", headers={"X-API-Key": created["api_key"]}
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["role"], role, role)
            self.assertEqual(body["source"], "user_key", role)

    def test_user_key_via_bearer_authorization(self) -> None:
        created = self._create_user("eng-bearer", "engineer")
        response = self.client.get(
            "/api/v1/me", headers={"Authorization": f"Bearer {created['api_key']}"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["username"], "eng-bearer")

    # -- require_role enforcement on /users -----------------------------------

    def test_viewer_key_gets_403_on_admin_route(self) -> None:
        created = self._create_user("viewer-vera", "viewer")
        response = self.client.post(
            "/api/v1/users",
            headers={"X-API-Key": created["api_key"]},
            json={"username": "should-not-exist", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        # 403 states the required role and leaks nothing else.
        detail = response.json()["detail"]
        self.assertIn("admin", detail)
        self.assertNotIn("viewer-vera", detail)
        self.assertNotIn(created["api_key"], response.text)

    def test_engineer_key_gets_403_on_admin_route(self) -> None:
        created = self._create_user("eng-no-admin", "engineer")
        response = self.client.post(
            "/api/v1/users",
            headers={"X-API-Key": created["api_key"]},
            json={"username": "blocked", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_admin_user_key_can_create_users(self) -> None:
        admin = self._create_user("admin-amy", "admin")
        response = self.client.post(
            "/api/v1/users",
            headers={"X-API-Key": admin["api_key"]},
            json={"username": "made-by-admin-user", "role": "reviewer"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["user"]["role"], "reviewer")

    def test_viewer_key_gets_403_listing_users(self) -> None:
        created = self._create_user("viewer-list", "viewer")
        response = self.client.get(
            "/api/v1/users", headers={"X-API-Key": created["api_key"]}
        )
        self.assertEqual(response.status_code, 403, response.text)

    # -- inactive / unknown keys fail closed ----------------------------------

    def test_inactive_user_key_is_401(self) -> None:
        created = self._create_user("to-deactivate", "engineer")
        user_id = created["user"]["id"]
        # Deactivate via the shared admin key.
        deactivate = self.client.post(
            f"/api/v1/users/{user_id}/deactivate", headers=self._admin_headers()
        )
        self.assertEqual(deactivate.status_code, 200, deactivate.text)
        self.assertFalse(deactivate.json()["is_active"])
        # The deactivated user's key no longer authenticates.
        response = self.client.get(
            "/api/v1/me", headers={"X-API-Key": created["api_key"]}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_unknown_key_is_401(self) -> None:
        response = self.client.get(
            "/api/v1/me", headers={"X-API-Key": "definitely-not-a-real-key"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_no_key_is_401(self) -> None:
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    # -- role updates ---------------------------------------------------------

    def test_admin_can_update_role(self) -> None:
        created = self._create_user("promote-me", "viewer")
        user_id = created["user"]["id"]
        response = self.client.post(
            f"/api/v1/users/{user_id}/role",
            headers=self._admin_headers(),
            json={"role": "engineer"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "engineer")
        # The user now passes an engineer-gated check via /me showing the new role.
        me = self.client.get("/api/v1/me", headers={"X-API-Key": created["api_key"]})
        self.assertEqual(me.json()["role"], "engineer")

    # -- last-admin self-lockout guard ----------------------------------------

    def test_count_active_admins_helper_is_correct(self) -> None:
        repo = self._repo()
        before = repo.count_active_admins()

        # A new active admin row increments the count by exactly one.
        admin = self._create_user("count-admin", "admin")
        self.assertEqual(repo.count_active_admins(), before + 1)

        # A non-admin user does not change the active-admin count.
        self._create_user("count-viewer", "viewer")
        self.assertEqual(repo.count_active_admins(), before + 1)

        # Deactivating that admin (another active admin still exists) decrements it.
        repo.set_active(admin["user"]["id"], is_active=False)
        self.assertEqual(repo.count_active_admins(), before)

    def test_cannot_deactivate_last_active_admin_returns_409(self) -> None:
        survivor_id = self._reduce_to_single_active_admin()
        response = self.client.post(
            f"/api/v1/users/{survivor_id}/deactivate", headers=self._admin_headers()
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("last active admin", response.json()["detail"])
        # The guard did not mutate the row: the survivor is still an active admin.
        self.assertEqual(self._repo().count_active_admins(), 1)
        survivor = self._repo().get(survivor_id)
        self.assertTrue(survivor["is_active"])
        self.assertEqual(survivor["role"], "admin")

    def test_cannot_demote_last_active_admin_returns_409(self) -> None:
        survivor_id = self._reduce_to_single_active_admin()
        response = self.client.post(
            f"/api/v1/users/{survivor_id}/role",
            headers=self._admin_headers(),
            json={"role": "engineer"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("last active admin", response.json()["detail"])
        # The row is unchanged: still an active admin.
        survivor = self._repo().get(survivor_id)
        self.assertTrue(survivor["is_active"])
        self.assertEqual(survivor["role"], "admin")

    def test_can_deactivate_admin_when_another_active_admin_exists(self) -> None:
        repo = self._repo()
        first = self._create_user("two-admins-a", "admin")
        second = self._create_user("two-admins-b", "admin")
        # With at least two active admins, deactivating one is allowed.
        self.assertGreaterEqual(repo.count_active_admins(), 2)
        response = self.client.post(
            f"/api/v1/users/{first['user']['id']}/deactivate",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_active"])
        # The second admin remains active and administrable.
        self.assertTrue(repo.get(second["user"]["id"])["is_active"])

    def test_can_demote_admin_when_another_active_admin_exists(self) -> None:
        repo = self._repo()
        first = self._create_user("demote-admins-a", "admin")
        self._create_user("demote-admins-b", "admin")
        self.assertGreaterEqual(repo.count_active_admins(), 2)
        response = self.client.post(
            f"/api/v1/users/{first['user']['id']}/role",
            headers=self._admin_headers(),
            json={"role": "engineer"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "engineer")

    def test_deactivating_a_non_admin_is_unaffected_even_as_only_admin_low(self) -> None:
        # The guard is about ADMIN rows only: a non-admin can always be
        # deactivated regardless of how many admins exist.
        viewer = self._create_user("guard-irrelevant-viewer", "viewer")
        response = self.client.post(
            f"/api/v1/users/{viewer['user']['id']}/deactivate",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_active"])

    def test_demoting_a_non_admin_is_unaffected(self) -> None:
        engineer = self._create_user("guard-irrelevant-eng", "engineer")
        response = self.client.post(
            f"/api/v1/users/{engineer['user']['id']}/role",
            headers=self._admin_headers(),
            json={"role": "viewer"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "viewer")

    def test_last_admin_repo_guard_raises_last_admin_error(self) -> None:
        # Direct repository-level contract: the guard raises LastAdminError
        # (a ValueError subclass) rather than silently mutating.
        from smart_commissioning_core.db.repositories import LastAdminError

        survivor_id = self._reduce_to_single_active_admin()
        repo = self._repo()
        with self.assertRaises(LastAdminError):
            repo.set_active(survivor_id, is_active=False)
        with self.assertRaises(LastAdminError):
            repo.update_role(survivor_id, role="viewer")
        self.assertIsInstance(LastAdminError("x"), ValueError)

    def test_deactivate_unknown_user_is_404(self) -> None:
        response = self.client.post(
            "/api/v1/users/00000000-0000-0000-0000-000000000000/deactivate",
            headers=self._admin_headers(),
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_duplicate_username_is_409(self) -> None:
        self._create_user("dup-user", "viewer")
        response = self.client.post(
            "/api/v1/users",
            headers=self._admin_headers(),
            json={"username": "dup-user", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    # -- secrecy: hash stored, plaintext never leaked -------------------------

    def test_api_key_hash_stored_not_plaintext(self) -> None:
        from app.core.auth import hash_api_key
        from app.core.db import get_engine
        from smart_commissioning_core.db.repositories import UserRepository

        created = self._create_user("secrecy-sam", "viewer")
        raw_key = created["api_key"]
        repo = UserRepository(get_engine())
        stored = repo.get_by_api_key_hash(hash_api_key(raw_key))
        self.assertIsNotNone(stored)
        # What is stored is the hash, not the plaintext.
        self.assertEqual(stored["api_key_hash"], hash_api_key(raw_key))
        self.assertNotEqual(stored["api_key_hash"], raw_key)

    def test_list_users_never_leaks_key_or_hash(self) -> None:
        created = self._create_user("nokey-nora", "viewer")
        raw_key = created["api_key"]
        response = self.client.get("/api/v1/users", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200, response.text)
        text = response.text
        self.assertNotIn(raw_key, text)
        self.assertNotIn("api_key_hash", text)
        self.assertNotIn("api_key", text)
        # The user is present (by username), with no key material.
        usernames = {u["username"] for u in response.json()}
        self.assertIn("nokey-nora", usernames)


class RoleOrderingUnitTests(unittest.TestCase):
    """Unit coverage of the total-order helper backing require_role."""

    def test_role_total_order(self) -> None:
        from smart_commissioning_core.rbac import ROLE_ORDER, Role, role_at_least

        self.assertEqual([r.value for r in ROLE_ORDER], ["viewer", "reviewer", "engineer", "admin"])
        self.assertTrue(Role.ADMIN.at_least(Role.ENGINEER))
        self.assertTrue(Role.ENGINEER.at_least(Role.ENGINEER))
        self.assertFalse(Role.VIEWER.at_least(Role.REVIEWER))
        # String-accepting helper.
        self.assertTrue(role_at_least("admin", "viewer"))
        self.assertFalse(role_at_least("viewer", "admin"))

    def test_role_equals_its_string_value(self) -> None:
        from smart_commissioning_core.rbac import Role

        self.assertEqual(Role.VIEWER, "viewer")
        self.assertEqual(Role.ADMIN.value, "admin")

    def test_unknown_role_raises(self) -> None:
        from smart_commissioning_core.rbac import Role

        with self.assertRaises(ValueError):
            Role.from_value("superuser")


if __name__ == "__main__":
    unittest.main()
