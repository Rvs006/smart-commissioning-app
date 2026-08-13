from __future__ import annotations

import concurrent.futures
import contextlib
import io
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from app.core.auth import hash_api_key
from app.scripts import bootstrap_admin
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.repositories import (
    ActiveAdminExistsError,
    UserRepository,
)


class BootstrapAdminCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="bootstrap-admin-cli-")
        self.runtime_root = Path(self._temporary.name)
        self.database_url = default_sqlite_url(self.runtime_root)
        upgrade_to_head(self.database_url)
        self.engine = create_engine_from_url(self.database_url)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._temporary.cleanup()

    def _run(self, username: str, *, deployment_role: str = "hub") -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        settings = SimpleNamespace(deployment_role=deployment_role)
        with (
            mock.patch.object(bootstrap_admin, "get_settings", return_value=settings),
            mock.patch.object(bootstrap_admin, "get_engine", return_value=self.engine),
            mock.patch.object(bootstrap_admin, "ensure_runtime_directories"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = bootstrap_admin.main(["--username", username])
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_hub_bootstrap_creates_trimmed_named_admin_and_prints_raw_key_once(self) -> None:
        exit_code, stdout, stderr = self._run("  release-operator  ")

        self.assertEqual(exit_code, 0, stderr)
        lines = [line for line in stdout.splitlines() if line]
        raw_key = lines[-1]
        self.assertGreaterEqual(len(raw_key), 32)
        self.assertEqual(stdout.count(raw_key), 1)
        self.assertNotIn(raw_key, stderr)

        users = UserRepository(self.engine)
        created = users.list_users()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["username"], "release-operator")
        self.assertEqual(created[0]["role"], "admin")
        self.assertTrue(created[0]["is_active"])

        stored = users.get_by_api_key_hash(hash_api_key(raw_key))
        self.assertIsNotNone(stored)
        self.assertEqual(stored["username"], "release-operator")
        self.assertNotEqual(stored["api_key_hash"], raw_key)

    def test_module_cli_bootstraps_against_configured_edge_database(self) -> None:
        environment = dict(os.environ)
        environment.update(
            {
                "DATABASE_URL": self.database_url,
                "DEPLOYMENT_ROLE": "edge",
                "SMART_COMMISSIONING_RUNTIME_ROOT": str(self.runtime_root),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.scripts.bootstrap_admin",
                "--username",
                "module-operator",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        raw_key = completed.stdout.splitlines()[-1]
        self.assertTrue(raw_key)
        self.assertEqual(completed.stdout.count(raw_key), 1)
        self.assertNotIn(raw_key, completed.stderr)
        stored = UserRepository(self.engine).get_by_api_key_hash(hash_api_key(raw_key))
        self.assertIsNotNone(stored)
        self.assertEqual(stored["username"], "module-operator")

    def test_bootstrap_refuses_standalone_and_invalid_usernames_without_creating_users(
        self,
    ) -> None:
        for username, role in (
            ("operator", "standalone"),
            ("   ", "hub"),
            ("x" * 256, "edge"),
        ):
            with self.subTest(username_length=len(username), role=role):
                exit_code, stdout, stderr = self._run(username, deployment_role=role)
                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("ERROR:", stderr)
        self.assertEqual(UserRepository(self.engine).list_users(), [])

    def test_bootstrap_refuses_when_an_active_named_admin_exists_without_leaking_key(
        self,
    ) -> None:
        first_exit, first_stdout, first_stderr = self._run("first-admin")
        self.assertEqual(first_exit, 0, first_stderr)
        first_key = first_stdout.splitlines()[-1]

        generated_but_refused_key = "refused-key-must-not-be-disclosed"
        with mock.patch.object(
            bootstrap_admin.secrets,
            "token_urlsafe",
            return_value=generated_but_refused_key,
        ):
            exit_code, stdout, stderr = self._run("second-admin")

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn(first_key, stderr)
        self.assertNotIn(generated_but_refused_key, stderr)
        self.assertNotIn(hash_api_key(generated_but_refused_key), stderr)
        self.assertEqual(len(UserRepository(self.engine).list_users()), 1)

    def test_bootstrap_recovers_when_active_admin_count_is_zero(self) -> None:
        users = UserRepository(self.engine)
        users.create_user(
            user_id=str(uuid4()),
            username="retired-admin",
            role="admin",
            api_key_hash=hash_api_key("retired-admin-key"),
            is_active=False,
        )

        exit_code, stdout, stderr = self._run(
            "replacement-admin",
            deployment_role="edge",
        )

        self.assertEqual(exit_code, 0, stderr)
        self.assertTrue(stdout.splitlines()[-1])
        self.assertEqual(users.count_active_admins(), 1)
        self.assertEqual(
            {user["username"] for user in users.list_users()},
            {"retired-admin", "replacement-admin"},
        )

    def test_bootstrap_duplicate_username_fails_closed_without_secret_output(self) -> None:
        users = UserRepository(self.engine)
        users.create_user(
            user_id=str(uuid4()),
            username="retired-admin",
            role="admin",
            api_key_hash=hash_api_key("retired-key"),
            is_active=False,
        )
        raw_key = "duplicate-key-must-not-be-disclosed"
        with mock.patch.object(
            bootstrap_admin.secrets,
            "token_urlsafe",
            return_value=raw_key,
        ):
            exit_code, stdout, stderr = self._run("retired-admin")

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn(raw_key, stderr)
        self.assertNotIn(hash_api_key(raw_key), stderr)
        self.assertEqual(users.count_active_admins(), 0)

    def test_concurrent_bootstrap_attempts_create_exactly_one_active_admin(self) -> None:
        def attempt(index: int) -> str:
            try:
                UserRepository(self.engine).create_bootstrap_admin(
                    user_id=str(uuid4()),
                    username=f"racing-admin-{index}",
                    api_key_hash=hash_api_key(secrets.token_urlsafe(32)),
                )
            except ActiveAdminExistsError:
                return "refused"
            return "created"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, range(2)))

        self.assertEqual(sorted(outcomes), ["created", "refused"])
        users = UserRepository(self.engine)
        self.assertEqual(users.count_active_admins(), 1)
        self.assertEqual(len(users.list_users()), 1)


if __name__ == "__main__":
    unittest.main()
