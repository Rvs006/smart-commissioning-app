"""Local-file logging, secret masking, the log bundle, config migration, and the
engineer-gated /logs upload route.

Python cannot run on the locked-down dev machine (ThreatLocker), so these are
CI-verified. They avoid live infra: file logging writes to a tmp dir, masking
and bundling are pure, config tests use a tmp SECRETS_ROOT + tmp SQLite, and the
upload route mocks httpx.post (no network).
"""

import json
import logging
import os
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

import httpx

from app.core.logging import (
    _SCT_FILE_HANDLER_FLAG,
    configure_file_logging,
    purge_old_logs,
)
from app.services import configuration_service as configuration_service_module
from app.services import log_service as log_service_module
from app.services.configuration_service import (
    DEFAULT_CONFIGURATION,
    DEFAULT_PROJECT_ID,
    DEFAULT_SITE_ID,
    PASSWORD_KIND_FIELDS,
    SECRET_SENTINEL,
    ConfigurationService,
)
from app.services.log_service import (
    build_log_bundle,
    effective_log_level,
    mask_log_text,
    retention_days,
)
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.repositories import ConfigurationRepository


def _remove_sct_file_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _SCT_FILE_HANDLER_FLAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass


class FileLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_dir = Path(temp_dir.name)
        # Root defaults to WARNING; INFO records would be dropped before reaching
        # the file handler, so open the root gate and restore it afterwards.
        original_level = logging.getLogger().level
        self.addCleanup(lambda: logging.getLogger().setLevel(original_level))
        # Remove our file handler BEFORE the tmp dir is cleaned (Windows lock).
        self.addCleanup(_remove_sct_file_handlers)
        logging.getLogger().setLevel(logging.DEBUG)

    def _read_lines(self) -> list[str]:
        text = (self.log_dir / "app.log").read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]

    def test_writes_one_json_line_per_record(self) -> None:
        configure_file_logging(self.log_dir, logging.DEBUG)
        logging.getLogger("sct.test.file").info("hello file logging")

        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertLessEqual(
            {"timestamp", "level", "logger", "message"}, set(payload)
        )
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["message"], "hello file logging")

    def test_configure_file_logging_is_idempotent(self) -> None:
        configure_file_logging(self.log_dir)
        configure_file_logging(self.log_dir)
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if getattr(h, _SCT_FILE_HANDLER_FLAG, False)]
        self.assertEqual(len(file_handlers), 1)

    def test_handler_level_filters_lower_records(self) -> None:
        configure_file_logging(self.log_dir, level="Warning")
        logger = logging.getLogger("sct.test.level")
        logger.info("this info must be filtered out of the file")
        logger.warning("this warning is kept")

        lines = self._read_lines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["level"], "WARNING")

    def test_purge_old_logs_removes_only_stale_files(self) -> None:
        old = self.log_dir / "app.log.9"
        fresh = self.log_dir / "app.log"
        old.write_text("old", encoding="utf-8")
        fresh.write_text("fresh", encoding="utf-8")
        stale = time.time() - 40 * 86_400
        os.utime(old, (stale, stale))

        purge_old_logs(self.log_dir, 30)

        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_purge_old_logs_swallows_unlink_error(self) -> None:
        old = self.log_dir / "crash-old.log"
        old.write_text("boom", encoding="utf-8")
        stale = time.time() - 90 * 86_400
        os.utime(old, (stale, stale))

        with mock.patch.object(Path, "unlink", side_effect=OSError("locked")):
            # Must not raise even though the only stale file cannot be deleted.
            purge_old_logs(self.log_dir, 30)

    def test_purge_disabled_for_non_positive_retention(self) -> None:
        old = self.log_dir / "app.log.1"
        old.write_text("keep", encoding="utf-8")
        stale = time.time() - 100 * 86_400
        os.utime(old, (stale, stale))

        purge_old_logs(self.log_dir, 0)
        self.assertTrue(old.exists())


class MaskingTests(unittest.TestCase):
    def test_masks_json_credential_keys(self) -> None:
        self.assertEqual(mask_log_text('"password": "hunter2"'), '"password": "********"')
        self.assertEqual(mask_log_text('"api_key": "abc123"'), '"api_key": "********"')
        self.assertEqual(
            mask_log_text('"authorization": "Bearer xyz"'),
            '"authorization": "********"',
        )

    def test_masks_key_value_credential_forms(self) -> None:
        self.assertEqual(mask_log_text("token=abc123"), "token=********")
        self.assertEqual(mask_log_text("secret = topsecret"), "secret = ********")

    def test_leaves_ordinary_and_secret_refs_untouched(self) -> None:
        for text in (
            "connecting to broker mqtt.local:8883",
            "loaded CA from secret://ca-certificate-20260101-abcd",
            '"host": "mqtt.local"',
            '"CA Certificate": "secret://client-cert-1"',
        ):
            self.assertEqual(mask_log_text(text), text)

    def test_masks_only_offending_lines(self) -> None:
        document = 'ordinary line here\n"password": "p"\nanother ordinary line'
        masked = mask_log_text(document)
        lines = masked.splitlines()
        self.assertEqual(lines[0], "ordinary line here")
        self.assertEqual(lines[1], '"password": "********"')
        self.assertEqual(lines[2], "another ordinary line")


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_dir = Path(temp_dir.name)
        patcher = mock.patch.object(log_service_module, "LOG_DIR", self.log_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bundle_contains_only_log_files_and_masks_secrets(self) -> None:
        (self.log_dir / "app.log").write_text('{"password": "topsecret"}', encoding="utf-8")
        (self.log_dir / "app.log.1").write_text("rotated line", encoding="utf-8")
        (self.log_dir / "crash-20260101-000000.log").write_text("crash", encoding="utf-8")
        (self.log_dir / "notes.txt").write_text("decoy not a log", encoding="utf-8")

        zip_bytes, members = build_log_bundle()

        self.assertEqual(
            sorted(members),
            ["app.log", "app.log.1", "crash-20260101-000000.log"],
        )
        with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["app.log", "app.log.1", "crash-20260101-000000.log"],
            )
            app_log = archive.read("app.log").decode("utf-8")
        self.assertIn("********", app_log)
        self.assertNotIn("topsecret", app_log)
        self.assertNotIn("notes.txt", members)


class EffectiveLevelTests(unittest.TestCase):
    def test_diagnostics_enabled_forces_debug(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_LEVEL", None)
            self.assertEqual(
                effective_log_level({"Diagnostics Mode": "Enabled", "Log Level": "Error"}),
                "DEBUG",
            )

    def test_env_overrides_stored_level(self) -> None:
        with mock.patch.dict(os.environ, {"LOG_LEVEL": "WARNING"}):
            self.assertEqual(
                effective_log_level({"Diagnostics Mode": "Disabled", "Log Level": "Info"}),
                "WARNING",
            )

    def test_stored_level_used_when_no_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_LEVEL", None)
            self.assertEqual(
                effective_log_level({"Diagnostics Mode": "Disabled", "Log Level": "Info"}),
                "INFO",
            )

    def test_retention_days_prefix_and_default(self) -> None:
        self.assertEqual(retention_days({"Log Retention": "45 days"}), 45)
        self.assertEqual(retention_days({"Log Retention": "monthly"}), 30)
        self.assertEqual(retention_days({}), 30)


class _ConfigTestCase(unittest.TestCase):
    """Tmp SECRETS_ROOT + tmp SQLite, mirroring test_secret_storage.py."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        temp_path = Path(temp_dir.name)

        self.secrets_root = temp_path / "secrets"
        patcher = mock.patch.object(configuration_service_module, "SECRETS_ROOT", self.secrets_root)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.engine = create_engine_from_url(default_sqlite_url(temp_path))
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        self.service = ConfigurationService(engine=self.engine)


class ConfigMigrationTests(_ConfigTestCase):
    def test_syslog_fields_dropped_and_upload_fields_backfilled(self) -> None:
        # Persist a raw legacy snapshot that still carries the syslog fields and
        # lacks the new upload fields, then load through the service.
        payload = DEFAULT_CONFIGURATION.model_dump(mode="json")
        payload["logging"]["values"] = {
            "Log Level": "Info",
            "Log Retention": "30 days",
            "Remote Syslog Target": "10.0.0.9",
            "Syslog Port": "514",
            "Diagnostics Mode": "Disabled",
        }
        ConfigurationRepository(self.engine).save(DEFAULT_PROJECT_ID, DEFAULT_SITE_ID, payload)

        loaded = self.service.load()
        values = loaded.logging.values
        self.assertNotIn("Remote Syslog Target", values)
        self.assertNotIn("Syslog Port", values)
        self.assertIn("Log Upload URL", values)
        self.assertIn("Log Upload Token", values)

    def test_upload_token_is_password_kind(self) -> None:
        self.assertIn("logging", PASSWORD_KIND_FIELDS)
        self.assertIn("Log Upload Token", PASSWORD_KIND_FIELDS["logging"])

    def test_upload_token_write_only_and_masked(self) -> None:
        # Store a real token.
        configuration = self.service.load(mask_secrets=False)
        configuration.logging.values["Log Upload Token"] = "real-upload-token"
        self.service.save(configuration)

        raw = ConfigurationRepository(self.engine).get_current(DEFAULT_PROJECT_ID, DEFAULT_SITE_ID)
        self.assertEqual(raw["logging"]["values"]["Log Upload Token"], "real-upload-token")

        # API snapshot renders the sentinel, never the real token.
        masked = self.service.load()
        self.assertEqual(masked.logging.values["Log Upload Token"], SECRET_SENTINEL)

        # Re-saving the echoed sentinel keeps the stored token (write-only).
        echoed = self.service.load()  # masked -> token is the sentinel
        self.service.save(echoed)
        raw_again = ConfigurationRepository(self.engine).get_current(DEFAULT_PROJECT_ID, DEFAULT_SITE_ID)
        self.assertEqual(raw_again["logging"]["values"]["Log Upload Token"], "real-upload-token")

    def test_validate_rejects_bad_logging_values(self) -> None:
        cases = [
            ("Log Level", "Verbose", "Log Level"),
            ("Log Upload URL", "ftp://collector/upload", "Log Upload URL"),
            ("Log Upload URL", "https://user:pw@host/up", "Log Upload URL"),
            ("Log Retention", "monthly", "Log Retention"),
        ]
        for field, value, expected_label in cases:
            with self.subTest(field=field, value=value):
                configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
                configuration.logging.values[field] = value
                result = self.service.validate(configuration)
                self.assertFalse(result.valid)
                self.assertTrue(
                    any(expected_label in error for error in result.errors),
                    result.errors,
                )

    def test_validate_accepts_valid_logging_values(self) -> None:
        configuration = DEFAULT_CONFIGURATION.model_copy(deep=True)
        configuration.logging.values["Log Level"] = "Debug"
        configuration.logging.values["Log Retention"] = "30 days"
        configuration.logging.values["Log Upload URL"] = ""
        result = self.service.validate(configuration)
        self.assertTrue(result.valid, result.errors)


class UploadServiceTests(unittest.TestCase):
    """upload_log_bundle honesty contract, with httpx.post mocked (no network)."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.log_dir = Path(temp_dir.name)
        (self.log_dir / "app.log").write_text('{"token": "leakme"}', encoding="utf-8")
        patcher = mock.patch.object(log_service_module, "LOG_DIR", self.log_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_2xx_is_uploaded(self) -> None:
        with mock.patch.object(log_service_module.httpx, "post", return_value=httpx.Response(200)) as post:
            outcome = log_service_module.upload_log_bundle("https://logs.example/up", "tok")
        self.assertEqual(outcome.outcome, "uploaded")
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    def test_4xx_is_rejected_with_status(self) -> None:
        response = httpx.Response(403, text="Forbidden: bad key")
        with mock.patch.object(log_service_module.httpx, "post", return_value=response):
            outcome = log_service_module.upload_log_bundle("https://logs.example/up", "tok")
        self.assertEqual(outcome.outcome, "rejected")
        self.assertEqual(outcome.status_code, 403)
        self.assertIn("403", outcome.detail)

    def test_transport_error_is_no_response(self) -> None:
        with mock.patch.object(
            log_service_module.httpx, "post", side_effect=httpx.ConnectError("no route")
        ):
            outcome = log_service_module.upload_log_bundle("https://logs.example/up", "tok")
        self.assertEqual(outcome.outcome, "no_response")
        self.assertIsNone(outcome.status_code)
        # Token never appears in the detail.
        self.assertNotIn("tok", outcome.detail)

    def test_no_token_sends_no_authorization_header(self) -> None:
        with mock.patch.object(log_service_module.httpx, "post", return_value=httpx.Response(200)) as post:
            log_service_module.upload_log_bundle("https://logs.example/up", "")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_invalid_url_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            log_service_module.upload_log_bundle("ftp://collector/up", "tok")
        with self.assertRaises(ValueError):
            log_service_module.upload_log_bundle("https://user:pw@host/up", "tok")


_API_KEY = "test-logs-api-key"


class UploadRouteTests(unittest.TestCase):
    """POST /logs/upload end to end (api_key auth), httpx.post mocked."""

    @classmethod
    def setUpClass(cls) -> None:
        from harness import ApiTestCase  # local import: keeps pure tests app-free

        class _LogsApi(ApiTestCase):
            env = {
                "JOB_EXECUTION_MODE": "inline",
                "AUTH_MODE": "api_key",
                "API_KEY": _API_KEY,
            }
            client_headers = {"X-API-Key": _API_KEY}

        cls._api = _LogsApi
        cls._api.setUpClass()
        cls.client = cls._api.client

        # Point the log dir at a tmp folder with one log file for the whole class.
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls._log_dir = Path(cls._temp_dir.name)
        (cls._log_dir / "app.log").write_text('{"token": "leakme"}', encoding="utf-8")
        cls._log_dir_patch = mock.patch.object(log_service_module, "LOG_DIR", cls._log_dir)
        cls._log_dir_patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._log_dir_patch.stop()
        # A config save during a test re-points the live file handler at the
        # patched (temp) LOG_DIR, so it holds app.log open. Close it BEFORE the
        # temp dir is removed or Windows refuses the deletion (WinError 32) and
        # the Windows Compatibility CI job goes red — same guard FileLoggingTests
        # uses. No-op when no handler is attached.
        _remove_sct_file_handlers()
        cls._temp_dir.cleanup()
        cls._api.tearDownClass()

    def _configure_upload(self, url: str, token: str) -> None:
        snapshot = self.client.get("/api/v1/configuration").json()
        snapshot["logging"]["values"]["Log Upload URL"] = url
        snapshot["logging"]["values"]["Log Upload Token"] = token
        response = self.client.put("/api/v1/configuration", json=snapshot)
        self.assertEqual(response.status_code, 200, response.text)

    def test_blank_url_returns_400_naming_the_field(self) -> None:
        self._configure_upload("", "")
        response = self.client.post("/api/v1/logs/upload")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Log Upload URL", response.json()["detail"])

    def test_uploaded_outcome_and_bearer_header(self) -> None:
        self._configure_upload("https://logs.example/up", "secret-upload-token")
        with mock.patch.object(
            log_service_module.httpx, "post", return_value=httpx.Response(200)
        ) as post:
            response = self.client.post("/api/v1/logs/upload")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["outcome"], "uploaded")
        self.assertEqual(body["status_code"], 200)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret-upload-token")
        # The token must never be echoed back in the response body.
        self.assertNotIn("secret-upload-token", response.text)

    def test_rejected_outcome_surfaces_status(self) -> None:
        self._configure_upload("https://logs.example/up", "tok")
        response = httpx.Response(403, text="denied")
        with mock.patch.object(log_service_module.httpx, "post", return_value=response):
            api_response = self.client.post("/api/v1/logs/upload")
        self.assertEqual(api_response.status_code, 200)
        body = api_response.json()
        self.assertEqual(body["outcome"], "rejected")
        self.assertEqual(body["status_code"], 403)

    def test_no_response_is_200_with_outcome(self) -> None:
        self._configure_upload("https://logs.example/up", "tok")
        with mock.patch.object(
            log_service_module.httpx, "post", side_effect=httpx.ConnectError("boom")
        ):
            response = self.client.post("/api/v1/logs/upload")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "no_response")


if __name__ == "__main__":
    unittest.main()
