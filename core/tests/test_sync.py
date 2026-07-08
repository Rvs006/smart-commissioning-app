"""Edge->hub synchronization tests (smart_commissioning_core.sync + sync_identity).

Proves the round-trip IN-PROCESS across two temp SQLite databases (an "edge" DB
and a "hub" DB), both migrated to head, and via a FastAPI TestClient that merely
shuttles the bundle bytes. No real remote hub, Postgres, or network — the core is
transport-agnostic and these tests exercise the byte producer/consumer directly.

Covered:
  * edge identity create-once + deterministic id / key path
  * build on edge (2-3 terminal runs w/ issues + discovery + integrity) ->
    ingest into hub -> hub rows match edge (runs, issues, discovery, integrity,
    edge_id stamped)
  * idempotent re-ingest (all skipped_identical, no duplicates)
  * tampered run member bytes -> bad_hash reject, nothing written
  * signed with a DIFFERENT key not in trusted_edges -> rejected_untrusted
  * trusted edge_id but swapped public key -> rejected
  * mutate a run + rebuild same run_id -> rejected_immutable, hub copy unchanged
  * in-flight (non-terminal) runs never bundled
  * watermark: after mark_synced the un-synced list excludes them
  * offline file roundtrip (write bytes to a .scbundle, read back, ingest)
  * a FastAPI TestClient push round-trip
  * migration zero-drift + idempotent upgrade
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from smart_commissioning_core.db.base import Base
from smart_commissioning_core.db.db_run_store import DbRunStore
from smart_commissioning_core.db.engine import create_engine_from_url, default_sqlite_url
from smart_commissioning_core.db.migrate import upgrade_to_head
from smart_commissioning_core.db.repositories import (
    DiscoveryRepository,
    SyncRepository,
)
from smart_commissioning_core.integrity import SigningKey, cryptography_available
from smart_commissioning_core.sync import (
    IngestSummary,
    SyncError,
    build_sync_bundle,
    ingest_sync_bundle,
    read_manifest,
)
from smart_commissioning_core.sync_identity import (
    EdgeIdentity,
    edge_id_path,
    edge_signing_key_path,
    load_edge_signing_key,
    load_or_create_edge_id,
    load_or_create_edge_identity,
)
from sqlalchemy import inspect

_FIXED_NOW = datetime(2026, 6, 12, 8, 0, 0, tzinfo=UTC)


def _migrated_engine(root: Path, name: str):
    url = default_sqlite_url(root / name)
    (root / name).mkdir(parents=True, exist_ok=True)
    upgrade_to_head(url)
    return create_engine_from_url(url)


def _integrity_block() -> dict[str, object]:
    return {
        "algorithm": "sha256",
        "hash": "a" * 64,
        "signature_algorithm": "ed25519",
        "signature": "Zm9vYmFy",
        "public_key_pem": "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
        "public_key_fingerprint": "0123456789abcdef",
        "signed_at": "2026-06-12T07:00:00+00:00",
    }


def _issue(issue_id: str) -> dict[str, object]:
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


def _seed_terminal_run(
    store: DbRunStore,
    discovery: DiscoveryRepository,
    *,
    job_type: str,
    status: str = "succeeded",
    issue_ids: tuple[str, ...] = (),
    devices: tuple[dict, ...] = (),
    points: tuple[dict, ...] = (),
    topics: tuple[dict, ...] = (),
    integrity: bool = True,
) -> str:
    record = store.create_run(
        project_id="demo-project", site_id="demo-site", job_type=job_type
    )
    run_id = record["run_id"]
    summary: dict[str, object] = {"issue_count": len(issue_ids), "source": "fixture"}
    if integrity:
        summary["integrity"] = _integrity_block()
    store.update_result_summary(run_id, summary, merge=False)
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


class EdgeIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def test_edge_id_is_created_once_and_stable(self) -> None:
        first = load_or_create_edge_id(self.root)
        self.assertTrue(edge_id_path(self.root).exists())
        second = load_or_create_edge_id(self.root)
        self.assertEqual(first, second, "edge_id must persist across calls")

    def test_injected_edge_id_pins_value_but_disk_wins_afterwards(self) -> None:
        pinned = load_or_create_edge_id(self.root, edge_id="edge-fixed-001")
        self.assertEqual(pinned, "edge-fixed-001")
        # A later override never silently rewrites an established identity.
        again = load_or_create_edge_id(self.root, edge_id="edge-other-999")
        self.assertEqual(again, "edge-fixed-001")

    @unittest.skipUnless(cryptography_available(), "cryptography not installed")
    def test_identity_has_stable_key_and_fingerprint(self) -> None:
        identity = load_or_create_edge_identity(self.root, edge_id="edge-001")
        self.assertEqual(identity.edge_id, "edge-001")
        self.assertIn("BEGIN PUBLIC KEY", identity.public_key_pem)
        self.assertEqual(len(identity.public_key_fingerprint), 16)
        self.assertTrue(edge_signing_key_path(self.root).exists())

        # Re-resolving returns the same key (load_or_create is idempotent).
        again = load_or_create_edge_identity(self.root)
        self.assertEqual(again.public_key_fingerprint, identity.public_key_fingerprint)
        key = load_edge_signing_key(self.root)
        self.assertEqual(key.public_key_fingerprint(), identity.public_key_fingerprint)


@unittest.skipUnless(cryptography_available(), "cryptography not installed")
class SyncRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

        self.edge_engine = _migrated_engine(self.root, "edge")
        self.addCleanup(self.edge_engine.dispose)
        self.hub_engine = _migrated_engine(self.root, "hub")
        self.addCleanup(self.hub_engine.dispose)

        self.edge_store = DbRunStore(self.edge_engine)
        self.edge_discovery = DiscoveryRepository(self.edge_engine)
        self.edge_sync = SyncRepository(self.edge_engine)
        self.hub_store = DbRunStore(self.hub_engine)
        self.hub_discovery = DiscoveryRepository(self.hub_engine)
        self.hub_sync = SyncRepository(self.hub_engine)

        # Stable edge identity + signing key.
        self.identity = load_or_create_edge_identity(self.root / "edge_identity", edge_id="edge-001")
        self.signing_key = load_edge_signing_key(self.root / "edge_identity")
        self.trusted = {self.identity.edge_id: self.identity.public_key_fingerprint}

        # Seed three terminal runs with issues + discovery + integrity.
        self.run_a = _seed_terminal_run(
            self.edge_store,
            self.edge_discovery,
            job_type="udmi_validation",
            issue_ids=("iss-1", "iss-2"),
            devices=({"address": "10.0.0.1", "device_type": "ahu", "attributes": {"mac": "aa:bb"}},),
            points=({"point_name": "co2", "observed_value": {"present_value": 500}},),
        )
        self.run_b = _seed_terminal_run(
            self.edge_store,
            self.edge_discovery,
            job_type="ip_discovery",
            status="failed",
            issue_ids=("iss-x",),
            topics=({"topic": "a/b/c", "message_count": 3, "last_payload": {"k": 1}},),
        )
        self.run_c = _seed_terminal_run(
            self.edge_store, self.edge_discovery, job_type="bacnet_discovery", status="cancelled"
        )

    def _build(self, run_ids=None, since_watermark=None) -> bytes:
        return build_sync_bundle(
            self.edge_engine,
            run_ids=run_ids,
            since_watermark=since_watermark,
            signing_key=self.signing_key,
            edge_identity=self.identity,
            created_at=_FIXED_NOW,
        )

    def test_full_roundtrip_hub_matches_edge(self) -> None:
        bundle = self._build()
        summary = ingest_sync_bundle(
            self.hub_engine, bundle, trusted_edges=self.trusted, now=_FIXED_NOW
        )

        self.assertTrue(summary.accepted)
        self.assertEqual(summary.inserted, 3)
        self.assertEqual(summary.skipped_identical, 0)
        self.assertEqual(summary.rejected_immutable, 0)
        self.assertEqual(set(summary.inserted_run_ids), {self.run_a, self.run_b, self.run_c})

        for run_id in (self.run_a, self.run_b, self.run_c):
            edge_record = self.edge_store.get_run(run_id)
            hub_record = self.hub_store.get_run(run_id)
            self.assertEqual(hub_record, edge_record, f"run {run_id} record must match")
            # edge_id stamped on the hub, NULL on the edge.
            self.assertEqual(self.hub_store.get_edge_id(run_id), "edge-001")
            self.assertIsNone(self.edge_store.get_edge_id(run_id))

        # Issues, discovery, and integrity transferred faithfully.
        self.assertEqual(
            [i["issue_id"] for i in self.hub_store.get_run(self.run_a)["issues"]],
            ["iss-1", "iss-2"],
        )
        self.assertEqual(
            self.hub_store.get_run(self.run_a)["result_summary"]["integrity"],
            _integrity_block(),
        )
        self.assertEqual(len(self.hub_discovery.list_devices(self.run_a)), 1)
        self.assertEqual(self.hub_discovery.list_devices(self.run_a)[0]["attributes"], {"mac": "aa:bb"})
        self.assertEqual(self.hub_discovery.list_points(self.run_a)[0]["point_name"], "co2")
        self.assertEqual(self.hub_discovery.list_topics(self.run_b)[0]["message_count"], 3)

    def test_reingest_same_bundle_is_idempotent(self) -> None:
        bundle = self._build()
        ingest_sync_bundle(self.hub_engine, bundle, trusted_edges=self.trusted, now=_FIXED_NOW)
        second = ingest_sync_bundle(
            self.hub_engine, bundle, trusted_edges=self.trusted, now=_FIXED_NOW
        )

        self.assertTrue(second.accepted)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.skipped_identical, 3)
        self.assertEqual(second.rejected_immutable, 0)
        # No duplicate rows.
        self.assertEqual(len(self.hub_store.list_runs("demo-project", "demo-site")), 3)
        self.assertEqual(len(self.hub_discovery.list_devices(self.run_a)), 1)

    def test_tampered_member_bytes_rejected_nothing_written(self) -> None:
        bundle = self._build(run_ids=[self.run_a])
        tampered = _tamper_run_member(bundle, self.run_a)

        summary = ingest_sync_bundle(
            self.hub_engine, tampered, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertFalse(summary.accepted)
        self.assertEqual(summary.rejected_bad_hash, 1)
        self.assertEqual(summary.inserted, 0)
        self.assertFalse(self.hub_sync.run_exists(self.run_a), "tampered bundle must write nothing")

    def test_untrusted_key_rejected_nothing_written(self) -> None:
        # Build signed by a DIFFERENT key whose edge is unknown to the hub.
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
        summary = ingest_sync_bundle(
            self.hub_engine, bundle, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertFalse(summary.accepted)
        self.assertEqual(summary.rejected_untrusted, 1)
        self.assertEqual(summary.inserted, 0)
        self.assertFalse(self.hub_sync.run_exists(self.run_a))

    def test_trusted_edge_id_but_swapped_key_rejected(self) -> None:
        # Same edge_id the hub trusts, but signed with an entirely different key
        # (a forged-key attack). The embedded fingerprint won't match the pinned
        # one -> rejected_untrusted, nothing written.
        attacker_key = SigningKey.generate()
        swapped_identity = EdgeIdentity(
            edge_id="edge-001",  # claims to be the trusted edge
            public_key_pem=attacker_key.public_key_pem(),
            public_key_fingerprint=attacker_key.public_key_fingerprint(),
        )
        bundle = build_sync_bundle(
            self.edge_engine,
            run_ids=[self.run_a],
            signing_key=attacker_key,
            edge_identity=swapped_identity,
            created_at=_FIXED_NOW,
        )
        summary = ingest_sync_bundle(
            self.hub_engine, bundle, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertFalse(summary.accepted)
        self.assertEqual(summary.rejected_untrusted, 1)
        self.assertFalse(self.hub_sync.run_exists(self.run_a))

    def test_forged_fingerprint_string_does_not_fool_trust(self) -> None:
        # Attacker signs with their own key but rewrites the manifest's
        # self-reported fingerprint to the trusted one. Trust is derived from the
        # PEM itself, so this is still rejected.
        attacker_key = SigningKey.generate()
        bundle = build_sync_bundle(
            self.edge_engine,
            run_ids=[self.run_a],
            signing_key=attacker_key,
            edge_identity=EdgeIdentity(
                edge_id="edge-001",
                public_key_pem=attacker_key.public_key_pem(),
                public_key_fingerprint=attacker_key.public_key_fingerprint(),
            ),
            created_at=_FIXED_NOW,
        )
        forged = _rewrite_manifest_field(
            bundle, "edge_public_key_fingerprint", self.identity.public_key_fingerprint
        )
        summary = ingest_sync_bundle(
            self.hub_engine, forged, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertFalse(summary.accepted)
        self.assertEqual(summary.rejected_untrusted, 1)

    def test_mutated_run_same_id_rejected_immutable_hub_unchanged(self) -> None:
        # Ingest the original run_a into the hub.
        original = self._build(run_ids=[self.run_a])
        ingest_sync_bundle(self.hub_engine, original, trusted_edges=self.trusted, now=_FIXED_NOW)
        hub_before = self.hub_store.get_run(self.run_a)

        # Mutate run_a on the edge (rewrite issues), rebuild SAME run id.
        self.edge_store.replace_issues(self.run_a, [_issue("iss-MUTATED")])
        mutated = self._build(run_ids=[self.run_a])

        summary = ingest_sync_bundle(
            self.hub_engine, mutated, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertTrue(summary.accepted)
        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.rejected_immutable, 1)
        self.assertEqual(summary.rejected_immutable_run_ids, [self.run_a])
        # Hub copy is byte-for-byte unchanged (no overwrite).
        self.assertEqual(self.hub_store.get_run(self.run_a), hub_before)
        self.assertEqual(
            [i["issue_id"] for i in self.hub_store.get_run(self.run_a)["issues"]],
            ["iss-1", "iss-2"],
        )

    def test_inflight_runs_are_never_bundled(self) -> None:
        # A queued (non-terminal) run.
        queued = self.edge_store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="udmi_validation"
        )["run_id"]
        running = self.edge_store.create_run(
            project_id="demo-project", site_id="demo-site", job_type="ip_discovery"
        )["run_id"]
        self.edge_store.update_run_status(running, status="running", progress_percent=50)

        # Watermark build excludes them entirely.
        bundle = self._build(since_watermark=True)
        manifest = read_manifest(bundle)
        self.assertNotIn(queued, manifest["run_ids"])
        self.assertNotIn(running, manifest["run_ids"])
        self.assertEqual(set(manifest["run_ids"]), {self.run_a, self.run_b, self.run_c})

        # Explicitly naming an in-flight run is refused.
        with self.assertRaises(SyncError):
            self._build(run_ids=[queued])

    def test_watermark_excludes_synced_runs(self) -> None:
        self.assertEqual(
            set(self.edge_sync.list_unsynced_terminal_runs()),
            {self.run_a, self.run_b, self.run_c},
        )
        bundle = self._build(since_watermark=True)
        pushed = read_manifest(bundle)["run_ids"]

        updated = self.edge_sync.mark_synced(pushed, now=_FIXED_NOW)
        self.assertEqual(updated, 3)
        self.assertEqual(self.edge_sync.list_unsynced_terminal_runs(), [])
        # synced_at watermark is now set on the edge.
        self.assertEqual(self.edge_store.get_synced_at(self.run_a), _FIXED_NOW)

        # A subsequent watermark build is empty (nothing left to push).
        empty = self._build(since_watermark=True)
        self.assertEqual(read_manifest(empty)["run_ids"], [])

    def test_offline_file_roundtrip(self) -> None:
        bundle = self._build(run_ids=[self.run_a, self.run_b])
        path = self.root / "transfer.scbundle"
        path.write_bytes(bundle)

        read_back = path.read_bytes()
        self.assertEqual(read_back, bundle, "file write/read must be lossless")
        summary = ingest_sync_bundle(
            self.hub_engine, read_back, trusted_edges=self.trusted, now=_FIXED_NOW
        )
        self.assertTrue(summary.accepted)
        self.assertEqual(summary.inserted, 2)
        self.assertTrue(self.hub_sync.run_exists(self.run_a))
        self.assertTrue(self.hub_sync.run_exists(self.run_b))

    def test_trusted_edges_accepts_pem_value(self) -> None:
        # trusted_edges may map edge_id -> full PEM (not just a fingerprint).
        trusted_by_pem = {self.identity.edge_id: self.identity.public_key_pem}
        bundle = self._build(run_ids=[self.run_a])
        summary = ingest_sync_bundle(
            self.hub_engine, bundle, trusted_edges=trusted_by_pem, now=_FIXED_NOW
        )
        self.assertTrue(summary.accepted)
        self.assertEqual(summary.inserted, 1)

    def test_bundle_bytes_are_deterministic(self) -> None:
        first = self._build(run_ids=[self.run_a, self.run_b])
        second = self._build(run_ids=[self.run_a, self.run_b])
        self.assertEqual(first, second, "identical inputs must produce identical bytes")

    def test_fastapi_testclient_push_roundtrip(self) -> None:
        try:
            from fastapi import FastAPI, Request
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - fastapi optional in some installs
            self.skipTest("fastapi/httpx not installed")

        hub_engine = self.hub_engine
        trusted = self.trusted

        app = FastAPI()

        @app.post("/sync/ingest")
        async def ingest(request: Request) -> dict:
            body = await request.body()
            result = ingest_sync_bundle(
                hub_engine, body, trusted_edges=trusted, now=_FIXED_NOW
            )
            return result.as_dict()

        client = TestClient(app)
        bundle = self._build(run_ids=[self.run_a, self.run_b])
        response = client.post(
            "/sync/ingest", content=bundle, headers={"content-type": "application/octet-stream"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["inserted"], 2)
        self.assertTrue(self.hub_sync.run_exists(self.run_a))


def _tamper_run_member(bundle: bytes, run_id: str) -> bytes:
    """Return a copy of ``bundle`` with one run member's bytes mutated."""
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    target = f"runs/{run_id}.json"
    out = BytesIO()
    with ZipFile(BytesIO(bundle)) as src, ZipFile(out, "w", ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == target:
                payload = json.loads(data.decode("utf-8"))
                payload["run"]["status"] = "tampered"  # change content, keep valid JSON
                data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            info = ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            dst.writestr(info, data)
    return out.getvalue()


def _rewrite_manifest_field(bundle: bytes, key: str, value: str) -> bytes:
    """Return a copy of ``bundle`` with ``manifest[key]`` overwritten."""
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    out = BytesIO()
    with ZipFile(BytesIO(bundle)) as src, ZipFile(out, "w", ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "manifest.json":
                manifest = json.loads(data.decode("utf-8"))
                manifest[key] = value
                data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            info = ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            dst.writestr(info, data)
    return out.getvalue()


class SyncMigrationTests(unittest.TestCase):
    def test_upgrade_adds_edge_id_and_synced_at_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            upgrade_to_head(url)
            upgrade_to_head(url)  # idempotent second run

            engine = create_engine_from_url(url)
            try:
                run_columns = {c["name"] for c in inspect(engine).get_columns("runs")}
                self.assertIn("edge_id", run_columns)
                self.assertIn("synced_at", run_columns)
            finally:
                engine.dispose()

    def test_upgrade_to_head_has_zero_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            url = default_sqlite_url(Path(temp_dir))
            upgrade_to_head(url)

            engine = create_engine_from_url(url)
            try:
                with engine.connect() as connection:
                    context = MigrationContext.configure(connection)
                    diffs = compare_metadata(context, Base.metadata)
                self.assertEqual(diffs, [], f"schema drift detected after upgrade: {diffs}")
            finally:
                engine.dispose()


class IngestSummaryShapeTests(unittest.TestCase):
    def test_default_summary_is_rejected_with_zero_counts(self) -> None:
        summary = IngestSummary()
        self.assertFalse(summary.accepted)
        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.as_dict()["inserted_run_ids"], [])


if __name__ == "__main__":
    unittest.main()
