"""Edge->hub sync wiring tests (API + CLIs), all in-process, no live infra.

Proves the edge->hub round-trip across TWO temp SQLite databases:

  * an "edge" DB, migrated to head, seeded with terminal runs (issues +
    discovery + integrity), from which a signed ``.scbundle`` is built with a
    stable edge identity (core build_sync_bundle); and
  * the "hub" DB — the shared backend test database the FastAPI app runs on
    (deployment_role=hub, api_key auth) — into which the bundle is POSTed to
    ``/api/v1/hub/runs/ingest``.

Covered (each test ACTUALLY runs here):
  * POST a valid signed bundle -> runs inserted (asserted via GET /runs and the
    summary), and idempotent on a repeat POST (skipped_identical, no dupes);
  * an untrusted-edge bundle -> rejected (counts), nothing inserted;
  * the ingest endpoint returns 404 when deployment_role != 'hub';
  * the ingest endpoint requires auth (401 without the key in api_key mode);
  * the sync CLI --dry-run lists un-synced terminal runs;
  * an offline build->file->ingest CLI roundtrip works.

The real HTTP push to a REMOTE hub over TLS (app.scripts.sync._push_bundle's
network call) is NOT exercised — there is no remote hub here. That path is
live_untested; the push *logic* is proven against the in-process ingest endpoint
by POSTing the built bundle bytes directly.
"""

import atexit
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from harness import ApiTestCase

_API_KEY = "test-hub-sync-api-key"

_ENV_OVERRIDES = {
    "JOB_EXECUTION_MODE": "inline",
    "AUTH_MODE": "api_key",
    "API_KEY": _API_KEY,
    "DEPLOYMENT_ROLE": "hub",
}

_FIXED_NOW = datetime(2026, 6, 12, 8, 0, 0, tzinfo=UTC)


# -- edge-side seeding (a separate temp SQLite DB) ---------------------------


def _integrity_block() -> dict:
    return {
        "algorithm": "sha256",
        "hash": "a" * 64,
        "signature_algorithm": "ed25519",
        "signature": "Zm9vYmFy",
        "public_key_pem": "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
        "public_key_fingerprint": "0123456789abcdef",
        "signed_at": "2026-06-12T07:00:00+00:00",
    }


def _issue(issue_id: str) -> dict:
    return {
        "issue_id": issue_id,
        "asset_id": "AHU-L03-017",
        "issue_type": "unit_mismatch",
        "severity": "high",
        "description": f"issue {issue_id}",
        "status": "open",
        "point_name": "supply_air_temperature_sensor",
        "topic": "electracom/sct/1532/ahu/l03/events/pointset",
        "expected_value": "degrees-celsius",
        "observed_value": "kelvin",
        "match_basis": "point_name",
        "suggested_action": "Fix the unit mapping.",
        "raw_evidence_uri": None,
        "status_detail": None,
        "last_seen_at": "2026-06-11T10:00:00+00:00",
    }


def _seed_terminal_run(store, discovery, *, job_type, status="succeeded", issue_ids=(), devices=(), points=(), topics=()):
    record = store.create_run(project_id="demo-project", site_id="demo-site", job_type=job_type)
    run_id = record["run_id"]
    store.update_result_summary(
        run_id, {"issue_count": len(issue_ids), "integrity": _integrity_block()}, merge=False
    )
    if issue_ids:
        store.replace_issues(run_id, [_issue(i) for i in issue_ids])
    if devices:
        discovery.replace_devices(run_id, list(devices))
    if points:
        discovery.replace_points(run_id, list(points))
    if topics:
        discovery.replace_topics(run_id, list(topics))
    store.update_run_status(run_id, status=status, progress_percent=100)
    return run_id


class HubSyncApiTests(ApiTestCase):
    client_headers = {"X-API-Key": _API_KEY}

    @classmethod
    def setUpClass(cls) -> None:
        from smart_commissioning_core.db.db_run_store import DbRunStore
        from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
        from smart_commissioning_core.db.migrate import upgrade_to_head
        from smart_commissioning_core.db.repositories import DiscoveryRepository, SyncRepository
        from smart_commissioning_core.integrity import cryptography_available
        from smart_commissioning_core.sync_identity import (
            load_edge_signing_key,
            load_or_create_edge_identity,
        )

        if not cryptography_available():  # pragma: no cover - crypto present in CI
            raise unittest.SkipTest("cryptography not installed")

        cls._temp = tempfile.mkdtemp(prefix="sct-hub-sync-")
        atexit.register(shutil.rmtree, cls._temp, ignore_errors=True)
        root = Path(cls._temp)

        # Edge identity + signing key under the edge dir (stable, deterministic id).
        cls.identity = load_or_create_edge_identity(root / "edge_identity", edge_id="edge-test-001")
        cls.signing_key = load_edge_signing_key(root / "edge_identity")

        # The edge DB: a SEPARATE temp SQLite database migrated to head.
        edge_url = default_sqlite_url(root / "edge")
        (root / "edge").mkdir(parents=True, exist_ok=True)
        upgrade_to_head(edge_url)
        cls.edge_engine = create_engine_from_url(edge_url)
        cls.edge_store = DbRunStore(cls.edge_engine)
        cls.edge_discovery = DiscoveryRepository(cls.edge_engine)
        cls.edge_sync = SyncRepository(cls.edge_engine)

        # Seed two terminal runs (issues + discovery + integrity) on the edge.
        cls.run_a = _seed_terminal_run(
            cls.edge_store,
            cls.edge_discovery,
            job_type="udmi_validation",
            issue_ids=("iss-1", "iss-2"),
            devices=({"address": "10.0.0.1", "device_type": "ahu", "attributes": {"mac": "aa:bb"}},),
            points=({"point_name": "co2", "observed_value": {"present_value": 500}},),
        )
        cls.run_b = _seed_terminal_run(
            cls.edge_store,
            cls.edge_discovery,
            job_type="ip_discovery",
            status="failed",
            topics=({"topic": "a/b/c", "message_count": 3, "last_payload": {"k": 1}},),
        )

        # Trust this edge by pinning its fingerprint, supplied inline to the hub.
        trusted_json = json.dumps(
            [{"edge_id": cls.identity.edge_id, "public_key_fingerprint": cls.identity.public_key_fingerprint}]
        )

        # Configure + start the hub app (the shared test DB is the hub DB).
        cls.env = {"TRUSTED_EDGES_INLINE": trusted_json, **_ENV_OVERRIDES}
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        cls.edge_engine.dispose()

    # -- helpers --------------------------------------------------------------

    def _build_bundle(self, run_ids=None) -> bytes:
        from smart_commissioning_core.sync import build_sync_bundle

        return build_sync_bundle(
            self.edge_engine,
            run_ids=run_ids,
            since_watermark=None if run_ids else True,
            signing_key=self.signing_key,
            edge_identity=self.identity,
            created_at=_FIXED_NOW,
        )

    def _post_bundle(self, bundle: bytes, **kwargs):
        return self.client.post(
            "/api/v1/hub/runs/ingest",
            content=bundle,
            headers={"Content-Type": "application/octet-stream"},
            **kwargs,
        )

    # -- tests ----------------------------------------------------------------

    def test_valid_bundle_ingests_runs_and_is_idempotent(self) -> None:
        bundle = self._build_bundle(run_ids=[self.run_a, self.run_b])

        first = self._post_bundle(bundle)
        self.assertEqual(first.status_code, 200, first.text)
        summary = first.json()
        self.assertTrue(summary["accepted"], summary)
        self.assertEqual(summary["inserted"], 2)
        self.assertEqual(summary["edge_id"], "edge-test-001")
        self.assertEqual(set(summary["inserted_run_ids"]), {self.run_a, self.run_b})

        # Assert the runs landed in the hub DB via the public GET /runs endpoint.
        listed = self.client.get("/api/v1/runs", params={"project_id": "demo-project"})
        self.assertEqual(listed.status_code, 200, listed.text)
        listed_ids = {run["run_id"] for run in listed.json()["runs"]}
        self.assertIn(self.run_a, listed_ids)
        self.assertIn(self.run_b, listed_ids)

        # run_a is a udmi_validation run -> readable via the validation run
        # detail endpoint, which returns the full RunRecord (issues + summary).
        detail = self.client.get(f"/api/v1/validation/runs/{self.run_a}")
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual([i["issue_id"] for i in body["issues"]], ["iss-1", "iss-2"])
        self.assertEqual(body["result_summary"]["integrity"], _integrity_block())

        # Repeat POST is idempotent: nothing new inserted, both skipped.
        second = self._post_bundle(bundle)
        self.assertEqual(second.status_code, 200, second.text)
        repeat = second.json()
        self.assertTrue(repeat["accepted"])
        self.assertEqual(repeat["inserted"], 0)
        self.assertEqual(repeat["skipped_identical"], 2)

        relisted = self.client.get("/api/v1/runs", params={"project_id": "demo-project"})
        relisted_ids = [run["run_id"] for run in relisted.json()["runs"]]
        self.assertEqual(relisted_ids.count(self.run_a), 1, "no duplicate run on re-ingest")

    def test_untrusted_edge_bundle_rejected_nothing_inserted(self) -> None:
        from smart_commissioning_core.integrity import SigningKey
        from smart_commissioning_core.sync import build_sync_bundle
        from smart_commissioning_core.sync_identity import EdgeIdentity

        rogue_key = SigningKey.generate()
        rogue_identity = EdgeIdentity(
            edge_id="rogue-edge",
            public_key_pem=rogue_key.public_key_pem(),
            public_key_fingerprint=rogue_key.public_key_fingerprint(),
        )
        bundle = build_sync_bundle(
            self.edge_engine,
            run_ids=[self.run_a],
            signing_key=rogue_key,
            edge_identity=rogue_identity,
            created_at=_FIXED_NOW,
        )
        response = self._post_bundle(bundle)
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["rejected_untrusted"], 1)
        self.assertEqual(summary["inserted"], 0)

        # The rogue edge id never appears among the hub's runs.
        detail = self.client.get("/api/v1/runs", params={"project_id": "demo-project"})
        # run_a may exist from the trusted test; the rejection just wrote nothing
        # new. The contract we assert is the summary counters above.
        self.assertEqual(detail.status_code, 200)

    def test_ingest_requires_auth(self) -> None:
        bundle = self._build_bundle(run_ids=[self.run_a])
        # A bare client without the X-API-Key header must be rejected (401).
        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as anon:
            response = anon.post(
                "/api/v1/hub/runs/ingest",
                content=bundle,
                headers={"Content-Type": "application/octet-stream"},
            )
        self.assertEqual(response.status_code, 401, response.text)

    def test_ingest_404_when_not_hub_role(self) -> None:
        from unittest import mock

        import app.api.routes.hub as hub_module

        standalone = hub_module.get_settings().model_copy(update={"deployment_role": "standalone"})
        bundle = self._build_bundle(run_ids=[self.run_a])
        with mock.patch.object(hub_module, "get_settings", return_value=standalone):
            response = self._post_bundle(bundle)
        self.assertEqual(response.status_code, 404, response.text)

    def test_new_run_is_stamped_with_local_edge_id(self) -> None:
        # Run attribution: a run created through the API records the local edge
        # id (provenance) even though edge_id is kept out of the public record.
        from app.core import db as db_module
        from app.core.config import edge_identity
        from smart_commissioning_core.db.db_run_store import DbRunStore

        created = self.client.post(
            "/api/v1/validation/udmi/runs",
            json={
                "project_id": "demo-project",
                "site_id": "demo-site",
                "job_type": "udmi_validation",
                "parameters": {"requested_from": "test_hub_sync_attribution"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run_id"]
        # The public record shape is unchanged (no edge_id key leaked into it).
        self.assertNotIn("edge_id", created.json())

        stamped = DbRunStore(db_module.get_engine()).get_edge_id(run_id)
        self.assertEqual(stamped, edge_identity().edge_id)

    def test_empty_body_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/hub/runs/ingest",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 400, response.text)


class SyncCliTests(unittest.TestCase):
    """Edge-side CLI behavior against a dedicated edge DB (no hub, no network)."""

    @classmethod
    def setUpClass(cls) -> None:
        from smart_commissioning_core.db.db_run_store import DbRunStore
        from smart_commissioning_core.db.engine import default_sqlite_url
        from smart_commissioning_core.db.migrate import upgrade_to_head
        from smart_commissioning_core.db.repositories import DiscoveryRepository
        from smart_commissioning_core.integrity import cryptography_available

        if not cryptography_available():  # pragma: no cover
            raise unittest.SkipTest("cryptography not installed")

        cls._temp = tempfile.mkdtemp(prefix="sct-sync-cli-")
        atexit.register(shutil.rmtree, cls._temp, ignore_errors=True)
        root = Path(cls._temp)

        # A dedicated edge DB and a dedicated runtime root for the edge identity.
        cls._edge_db_url = default_sqlite_url(root / "edge")
        (root / "edge").mkdir(parents=True, exist_ok=True)
        upgrade_to_head(cls._edge_db_url)
        cls._runtime_root = root / "runtime"
        cls._runtime_root.mkdir(parents=True, exist_ok=True)

        cls._previous_env = {}
        env = {
            "DATABASE_URL": cls._edge_db_url,
            "DEPLOYMENT_ROLE": "edge",
            "JOB_EXECUTION_MODE": "inline",
            "AUTH_MODE": "local",
        }
        for key, value in env.items():
            cls._previous_env[key] = os.environ.get(key)
            os.environ[key] = value

        from app.core import config as config_module
        from app.core import db as db_module

        # Point the edge identity at the temp runtime root (the config helpers
        # read RUNTIME_ROOT from the config module global at call time).
        cls._runtime_patcher = mock.patch.object(config_module, "RUNTIME_ROOT", cls._runtime_root)
        cls._runtime_patcher.start()
        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

        store = DbRunStore(db_module.get_engine())
        discovery = DiscoveryRepository(db_module.get_engine())
        cls.run_a = _seed_terminal_run(store, discovery, job_type="udmi_validation", issue_ids=("iss-1",))
        cls.run_b = _seed_terminal_run(store, discovery, job_type="ip_discovery", status="failed")
        # An in-flight run that must NOT appear in dry-run output.
        cls.queued = store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="bacnet_discovery"
        )["run_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        from app.core import config as config_module
        from app.core import db as db_module

        db_module.get_engine().dispose()
        cls._runtime_patcher.stop()
        for key, value in cls._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module.get_settings.cache_clear()
        db_module.get_engine.cache_clear()

    def test_dry_run_lists_unsynced_terminal_runs(self) -> None:
        from app.scripts import sync as sync_cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = sync_cli.main(["--dry-run"])
        self.assertEqual(exit_code, 0)
        out = buffer.getvalue()
        self.assertIn(self.run_a, out)
        self.assertIn(self.run_b, out)
        self.assertNotIn(self.queued, out, "in-flight runs are not eligible to sync")

    def test_offline_build_file_then_ingest_roundtrip(self) -> None:
        from app.scripts import sync as sync_cli
        from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
        from smart_commissioning_core.db.migrate import upgrade_to_head
        from smart_commissioning_core.db.repositories import SyncRepository
        from smart_commissioning_core.sync import ingest_sync_bundle
        from smart_commissioning_core.sync_identity import load_or_create_edge_identity

        # 1) Edge writes a .scbundle to a file (offline carry), NOT marking synced.
        out_path = Path(self._temp) / "transfer.scbundle"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = sync_cli.main(["--output", str(out_path)])
        self.assertEqual(exit_code, 0, buffer.getvalue())
        self.assertTrue(out_path.exists())
        self.assertGreater(out_path.stat().st_size, 0)

        # Without --mark-synced the edge watermark is unchanged.
        from app.core import db as db_module

        edge_sync = SyncRepository(db_module.get_engine())
        self.assertIn(self.run_a, edge_sync.list_unsynced_terminal_runs())

        # 2) A fresh hub DB ingests the carried file via the OFFLINE ingest path
        # (core ingest, same as scripts.ingest performs after its role guard).
        hub_root = Path(self._temp) / "hub"
        hub_root.mkdir(parents=True, exist_ok=True)
        hub_url = default_sqlite_url(hub_root)
        upgrade_to_head(hub_url)
        hub_engine = create_engine_from_url(hub_url)
        self.addCleanup(hub_engine.dispose)

        identity = load_or_create_edge_identity(self._runtime_root)
        trusted = {identity.edge_id: identity.public_key_fingerprint}
        summary = ingest_sync_bundle(
            hub_engine,
            out_path.read_bytes(),
            trusted_edges=trusted,
            now=datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC),
        )
        self.assertTrue(summary.accepted)
        self.assertEqual(summary.inserted, 2)
        self.assertEqual(set(summary.inserted_run_ids), {self.run_a, self.run_b})
        self.assertTrue(SyncRepository(hub_engine).run_exists(self.run_a))


if __name__ == "__main__":
    unittest.main()
