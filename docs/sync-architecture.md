# Edge → Hub Synchronization Architecture

How on-site **edge** instances of the Smart Commissioning App push immutable,
signed run+evidence records to a central **hub** that aggregates commissioning
results across projects and sites. This document is accurate to the real
mechanism implemented in:

- `core/smart_commissioning_core/sync.py` — the transport-agnostic bundle
  builder + ingest verifier (`build_sync_bundle` / `ingest_sync_bundle`).
- `core/smart_commissioning_core/sync_identity.py` — the per-edge identity
  (`edge_id` + Ed25519 key) loader/creator.
- `core/smart_commissioning_core/integrity.py` — the SHA-256 + detached Ed25519
  signing/verification primitives the bundle reuses (the same primitives the
  evidence-pack signing path uses, see `docs/backup-restore.md`).
- `core/smart_commissioning_core/db/repositories.py` — `SyncRepository`
  (watermark listing, `mark_synced`, hub-side `run_exists` / `get_run_for_export`
  / `insert_run_record`) and `TERMINAL_RUN_STATUSES`.
- `core/alembic/versions/c998144d98d4_edge_hub_sync_columns.py` — the migration
  adding `runs.edge_id` and `runs.synced_at` (head revision `c998144d98d4`,
  down-revision `c4a7ced176a9`).

It is consistent with the hub vision in `docs/production-architecture.md`, the
trust boundaries in `docs/security-posture.md`, and the bundle/signing pattern in
`docs/backup-restore.md`.

> **Honesty up front.** The whole edge→hub round-trip is **proven in-process**
> across two temporary SQLite databases (an "edge" DB and a "hub" DB) and via a
> FastAPI `TestClient` that merely shuttles the bundle bytes — see
> `core/tests/test_sync.py` (19 tests, all passing). There is **no real remote
> hub, no Postgres hub, and no network transport** asserted here. The online HTTP
> push, the Postgres-backed hub, and large/real-site bundles require on-site /
> live validation. Those paths are called out explicitly in
> [§7 What is validated vs. what needs live validation](#7-what-is-validated-vs-what-needs-live-validation).

---

## 1. The model: local-first edge, central hub

The app runs from **one codebase** in three intended roles. The role a process
plays is a deployment concept (planned settings key `deployment_role` =
`standalone` | `edge` | `hub`); today the same binary serves all three and the
sync layer itself does not branch on it — `build_sync_bundle` and
`ingest_sync_bundle` are pure functions over an `Engine` and bytes.

- **`standalone`** — a single instance that both runs jobs and keeps its own
  records; never syncs. This is the default portable profile today.
- **`edge`** — an on-site or portable instance (technician laptop / gateway near
  the building network). It runs the discovery/validation engines against the
  live OT network, stores runs locally in **SQLite**, and **pushes** finished
  runs to a hub.
- **`hub`** — a central, shared server (intended on **Postgres**, multi-project
  aggregation) that **ingests** bundles from many edges and offers a combined
  read view across projects/sites/edges. The hub does not run site engines; it
  is a sink for evidence.

```text
   ┌─────────── Site A (air-gapped) ───────────┐      ┌──────── Central hub ────────┐
   │  edge-A1  (SQLite)                         │      │  hub  (Postgres, intended)  │
   │   runs ──build_sync_bundle──► .scbundle ───┼──┐   │                             │
   │                                            │  │   │  ingest_sync_bundle:        │
   └────────────────────────────────────────────  │   │   verify trust + signature  │
                                       offline ────┼──►│   + per-run hashes          │
   ┌─────────── Site B (online) ───────────────┐  │   │   then immutable insert     │
   │  edge-B1  (SQLite)                         │  │   │                             │
   │   runs ──build_sync_bundle──► bytes ───────┼──┘   │  multi-project read view    │
   │                          online HTTP push  │      │  (GET /runs by project/...) │
   └────────────────────────────────────────────      └─────────────────────────────┘
```

The unit of synchronization is a **run**: the 13-key run record (including
`result_summary['integrity']`, which carries the signed evidence-pack hash +
signature), its validation issues, and its discovery devices/points/topics. Only
**terminal** runs sync (see §3).

---

## 2. Trust: edge identity and the trusted-edges allowlist

### 2.1 Each edge has a cryptographic identity

An edge's identity is two persisted files under an identity root
(`load_or_create_edge_identity`, `sync_identity.py`):

- **`edge_id`** — a UUID generated once and written to `<root>/edge_id`. Stable
  across restarts (re-reading the file wins, so re-provisioning can never
  silently rewrite an established id). A deterministic id may be injected for
  provisioning/tests.
- **An Ed25519 signing key** — written owner-only to `<root>/edge_signing_key`.
  The edge **signs** every bundle manifest with this key. Its **public key** is
  exported as a PEM and reduced to a **16-char fingerprint**
  (`public_key_fingerprint`); the private key never leaves the edge.

The resolved `EdgeIdentity` is a frozen dataclass with `edge_id`,
`public_key_pem`, and `public_key_fingerprint` (the public fields are `None`
only when the `cryptography` package is unavailable — the id is still stable).

### 2.2 The hub maintains a trusted-edges allowlist

The hub decides trust from a **trusted-edges allowlist**: a mapping of
`edge_id → trust value`, where the trust value is **either** the edge's 16-char
public-key fingerprint **or** its full public-key PEM. In code this is the
`trusted_edges: dict[str, str]` argument to `ingest_sync_bundle`. The wiring
agent exposes it as a hub-side **trusted-edges allowlist** config (exact file
format is being finalized in parallel; the behavior is fixed — `edge_id` maps to
a pinned fingerprint or PEM).

Trust is **derived from the embedded key, never self-reported.** On ingest the
hub loads the PEM embedded in the manifest, computes its authoritative
fingerprint from the raw key bytes, and compares that to the pinned trust value.
A bundle that rewrites its self-reported `edge_public_key_fingerprint` to a
trusted value cannot fool the check (proven by
`test_forged_fingerprint_string_does_not_fool_trust`).

### 2.3 Enrolling a new edge

1. **On the edge**, resolve its identity once (the app does this on first run via
   `load_or_create_edge_identity`). Export the **`edge_id`** and the **public key
   fingerprint** (or the full public-key PEM). The private signing key stays on
   the edge — never export it.
2. **Hand the fingerprint to the hub operator** over a trusted channel (the same
   way you would hand over any pinned key — out of band, verified). The
   fingerprint is not secret, but pinning the wrong one is the whole attack, so
   confirm it.
3. **On the hub**, add a line to the trusted-edges allowlist mapping that
   `edge_id` to that fingerprint (or PEM). Until this entry exists, every bundle
   from that edge is rejected as `rejected_untrusted` and **nothing is written**.
4. **De-enroll** by removing the edge's allowlist entry. Previously ingested runs
   remain on the hub (they are immutable records); new bundles from that edge are
   then rejected.

Bundles are signed by the edge and verified by the hub. **Untrusted or tampered
bundles are rejected and nothing is written** — see §4.

---

## 3. Immutability and idempotency

The hub treats evidence as **tamper-evident and append-only**. Three rules:

- **Only terminal runs sync.** `build_sync_bundle` bundles only runs whose status
  is in `TERMINAL_RUN_STATUSES` = `('succeeded', 'failed', 'cancelled')`. An
  in-flight run (`queued`/`running`/…) is **never** bundled: the watermark build
  skips it, and naming one explicitly raises `SyncError`
  (`test_inflight_runs_are_never_bundled`).
- **Re-ingesting an identical bundle is a no-op.** Each run's canonical content is
  hashed (SHA-256 over a deterministic JSON encoding). If the hub already holds a
  run with the **same id and the same content hash**, that run is **skipped**
  (`skipped_identical`) — no duplicate rows, no error
  (`test_reingest_same_bundle_is_idempotent`).
- **A run that already exists with different content is REJECTED, not
  overwritten.** Same `run_id`, different content hash → `rejected_immutable`;
  the hub copy is left byte-for-byte unchanged
  (`test_mutated_run_same_id_rejected_immutable_hub_unchanged`). The hub never
  mutates an existing record.

### Why the content hash excludes provenance

For the hub's recomputed hash to equal the edge's, the **canonical content must
be identical on both sides**. So `build_run_content` (in `sync.py`) deliberately:

- **Excludes `edge_id`** from the hashed content. `edge_id` is provenance: `NULL`
  on the edge, stamped on the hub at ingest. It travels in the **manifest** only,
  not in the per-run content. Including it would make every re-ingest look like
  an immutability violation.
- **Strips DB-assigned bookkeeping** (`id`, `run_id`, `created_at`) from
  discovery device/point/topic rows — those are server-assigned and differ
  between edge and hub. `position` **is** retained (it is content-meaningful
  ordering).

This is what makes idempotency and immutability actually hold across two
independent databases.

---

## 4. The bundle and the fail-closed ingest pipeline

### 4.1 Bundle format (`.scbundle`)

A bundle is a reproducible ZIP (`build_sync_bundle` returns the bytes):

```text
manifest.json          # metadata + per-run content hashes + detached signature
runs/<run_id>.json     # canonical JSON of one run's full content
```

The manifest records the schema/format versions, the `edge_id`, the edge's
public-key PEM + fingerprint, the caller-supplied `created_at`, the ordered
`run_ids`, a per-run **content → SHA-256** map, the `include_reports` flag, and a
**base64 detached Ed25519 signature** over the canonical manifest body (the
manifest minus the `signature` field itself). Given identical inputs the bytes
are **deterministic** — stable key order, canonical JSON, fixed ZIP member
timestamps (`test_bundle_bytes_are_deterministic`).

> `include_reports` is recorded in the manifest for forward-compatibility. Signed
> report artifacts already live in `result_summary['integrity']` inside the run
> record and travel with it, so **no separate report member is emitted today**.

### 4.2 Ingest verification order (fail-closed)

`ingest_sync_bundle` verifies in a strict order and **fails closed** — steps
(a)–(d) are all-or-nothing for the whole bundle (nothing is written if any
fails):

| Step | Check | Failure outcome (whole bundle) |
| --- | --- | --- |
| (a) | Parse the manifest. | `rejected_reason = unparseable_bundle`, `accepted=False`. |
| (b) | `edge_id` is in `trusted_edges` **and** the embedded PEM's authoritative fingerprint equals the pinned trust value. | `rejected_untrusted = 1`. |
| (c) | Detached Ed25519 signature verifies over the canonical manifest body, against the now-trusted key. | `rejected_bad_signature = 1`. |
| (d) | Every `runs/<run_id>.json` member's SHA-256 matches the manifest's content map. | `rejected_bad_hash = 1`. |
| (e) | **Per-run** immutable upsert (only reached when a–d pass). | per-run: `inserted` / `skipped_identical` / `rejected_immutable`. |

On a trust failure (a/b/c) `accepted=False` and **all per-run counters stay
zero**. A bad hash (d) also rejects the whole bundle — a tampered bundle writes
nothing (`test_tampered_member_bytes_rejected_nothing_written`). Only after a–d
pass does the hub insert runs one at a time per §3.

### 4.3 The ingest summary

`ingest_sync_bundle` returns an `IngestSummary` (`.as_dict()` for an API
response / log line) with: `accepted`, `rejected_reason`, `edge_id`, the counters
`inserted` / `skipped_identical` / `rejected_immutable` / `rejected_bad_hash` /
`rejected_untrusted` / `rejected_bad_signature`, and the id lists
`inserted_run_ids` / `skipped_run_ids` / `rejected_immutable_run_ids`. This is the
authoritative record of "what landed and what was rejected, and why".

---

## 5. Two transports: online push and offline carry

The core is **transport-agnostic**: `build_sync_bundle` produces bytes and
`ingest_sync_bundle` consumes bytes. A caller may POST those bytes over HTTP
(online) or write them to a `.scbundle` file and carry them out (offline). Both
transports use the **same** signed bytes and the **same** verification.

### 5.1 ONLINE push (edge → hub over HTTP)

Use when the edge has network egress to the hub.

1. **Select + build on the edge.** Build a bundle of everything not yet pushed —
   the watermark set (`since_watermark=True`, i.e. every terminal run with
   `synced_at IS NULL`, oldest-first) — or an explicit `run_ids` list.
2. **Push the bytes to the hub** over HTTP with the edge's **API key** (the hub
   runs in `api_key` auth mode; see `docs/security-posture.md` §2). The wiring
   agent exposes a hub ingest endpoint, referenced generically as
   `POST /api/v1/hub/runs/ingest`, that accepts the raw bundle bytes and calls
   `ingest_sync_bundle` server-side. (A FastAPI `TestClient` exercising exactly
   this shape is in `test_fastapi_testclient_push_roundtrip`.)
3. **The hub verifies and responds** with the `IngestSummary` (§4.3): which runs
   were inserted, skipped, or rejected.
4. **Mark synced on the edge — only after a confirmed-accepted push.** Call
   `SyncRepository.mark_synced(pushed_run_ids, now=<utc>)` so those runs drop out
   of the next watermark build. **Do not mark synced if the bundle was rejected**
   (untrusted/bad-signature/bad-hash) or the push failed — leave the watermark
   `NULL` so the run is retried on the next sync. Marking synced is safe to
   re-run (it just advances the watermark) and a `rejected_immutable` run on the
   hub still means the edge's copy was delivered, so marking those synced is
   correct.

The edge's CLI for this flow is referenced generically as
`python -m app.scripts.sync` (the parallel wiring agent finalizes the exact
flags; the behavior is: select terminal/unsynced runs, build, push, and mark
synced on success).

### 5.2 OFFLINE carry (`.scbundle` for air-gapped sites)

Use when the edge has **no** egress (a secured/air-gapped site). The bundle is a
file you physically carry out.

1. **Build on the edge** exactly as in §5.1 (watermark set or explicit
   `run_ids`).
2. **Export the bytes to a `.scbundle` file** on removable media. Writing then
   reading the bytes is lossless (`test_offline_file_roundtrip`). Treat the media
   per site policy; the bundle is signed and tamper-evident, but evidence is
   still sensitive — carry it like any handover artifact.
3. **Carry the file out** of the air-gapped site to a machine that can reach the
   hub.
4. **Ingest at the hub with the offline ingest CLI.** Read the file bytes and
   call `ingest_sync_bundle` on the hub engine with the hub's trusted-edges
   allowlist. Referenced generically, this is the offline mode of the hub ingest
   path (same `ingest_sync_bundle`, just fed from a file instead of an HTTP body).
   Inspect the returned `IngestSummary`.
5. **Mark synced on the edge — only after you have confirmed the hub accepted the
   bundle.** Because the edge and hub are physically separated, record which
   `run_ids` were in the carried bundle, and run `mark_synced` on the edge **only
   once you have the hub's accepted summary in hand**. If you mark synced before
   confirming ingest and the bundle is later rejected, those runs silently fall
   out of the watermark — so confirm first, then mark.

> **Watermark semantics, both transports.** `synced_at` is **per-instance**: it
> records when *this* edge last pushed a run, and is the watermark
> `list_unsynced_terminal_runs` filters on. The hub leaves `synced_at` **NULL**
> on ingested rows (a hub is a sink; it does not re-push by default) and
> **preserves the edge's original `created_at`/`updated_at`**. The `now` argument
> to `ingest_sync_bundle` is reserved for hub-side ingest/audit timestamps; it
> does not stamp `synced_at`.

---

## 6. Operating procedures

### 6.1 Enroll an edge

See §2.3. Summary: export the edge's `edge_id` + fingerprint (or PEM), pin it in
the hub's trusted-edges allowlist out of band, confirm the fingerprint.

### 6.2 Run a sync

- **Online:** §5.1 — build the watermark set, push to the hub with the edge API
  key, read the summary, `mark_synced` on accepted.
- **Offline:** §5.2 — build, export `.scbundle`, carry out, ingest at the hub,
  confirm accepted, then `mark_synced` on the edge.

### 6.3 Verify what landed at the hub

After ingest, read back on the hub via the runs read API, filtered by project /
site / **edge**. The hub stamps `edge_id` on every ingested run, so a
`GET /runs?...` filtered by `edge_id` answers "what has edge-A1 delivered?" and a
filter by `project_id`/`site_id` answers "show me everything for this project
across all edges" (the multi-project hub view). Spot-check the counts against the
`IngestSummary.inserted_run_ids` from the push. The full run record (issues,
discovery rows, `result_summary['integrity']`) round-trips faithfully
(`test_full_roundtrip_hub_matches_edge`).

### 6.4 What to do on a rejected bundle

Read `IngestSummary.rejected_reason` / the counters and act on the cause:

| Symptom (summary) | Cause | What to do |
| --- | --- | --- |
| `rejected_untrusted = 1` | `edge_id` not in the allowlist, **or** the embedded key's fingerprint ≠ the pinned value (possibly a swapped/forged key). | If it is a legitimately new edge, enroll it (§2.3). If the edge is enrolled but the fingerprint changed, the signing key was rotated or swapped — verify out of band before re-pinning; treat an unexpected change as a possible key-compromise incident (`docs/security-posture.md`). |
| `rejected_bad_signature = 1` | The signature did not verify against the (trusted) key — corrupted manifest or a signature/key mismatch. | Re-build and re-push from the edge. If it persists, the edge's signing key and the pinned key disagree — re-verify enrollment. |
| `rejected_bad_hash = 1` | A run member's bytes do not match the manifest hash — the bundle was altered in transit. | Discard the bundle (nothing was written). Re-build a fresh bundle on the edge and re-transfer. For offline carry, suspect the media. |
| `rejected_immutable ≥ 1` (run-level) | A run with that id already exists on the hub with **different** content. | This is the immutability guard, **not** a bug: the hub will not overwrite a record. Investigate why the same `run_id` has divergent content (a re-run should get a new id). The hub copy is unchanged. The edge's copy was still delivered, so these may be marked synced. |

A whole-bundle rejection (untrusted / bad-signature / bad-hash) writes **nothing**
— so the safe response is always "fix the cause, rebuild on the edge, re-transfer".

### 6.5 Watermark semantics

`synced_at IS NULL` ⇒ "this edge has not pushed this run yet". `mark_synced`
stamps it after a confirmed-accepted push so the next build skips it. It is
per-instance and idempotent. Re-pushing already-synced runs explicitly (by
`run_ids`) still works — the hub just reports them `skipped_identical`.

### 6.6 Multi-project hub view

Because every ingested run carries its `project_id`, `site_id`, and stamped
`edge_id`, the hub is a single place to read commissioning evidence **across all
projects and all edges**. Filter the hub's runs read API by any combination of
project/site/edge to get a portfolio view, a per-site view, or a per-edge
"what has this laptop delivered" view — without ever mutating the underlying
immutable records.

---

## 7. What is validated vs. what needs live validation

### Validated in-process (no live infra) — `core/tests/test_sync.py`, 19 tests

Run them:

```sh
cd core && python -m pytest tests/test_sync.py -v
```

These exercise the **complete edge→hub round-trip across two temporary SQLite
databases** (an "edge" DB and a "hub" DB, both migrated to head) and via a
FastAPI `TestClient` that shuttles the bundle bytes:

- edge identity create-once + stable id / key / fingerprint;
- build on edge (terminal runs with issues + discovery + integrity) → ingest into
  hub → **hub rows equal edge rows**, with `edge_id` stamped on the hub and NULL
  on the edge;
- idempotent re-ingest (all `skipped_identical`, no duplicate rows);
- tampered run member → `rejected_bad_hash`, nothing written;
- unknown edge / wrong key → `rejected_untrusted`, nothing written;
- trusted `edge_id` but swapped key → rejected; forged self-reported fingerprint
  → still rejected (trust derived from the PEM);
- mutate a run + rebuild same id → `rejected_immutable`, hub copy unchanged;
- in-flight (non-terminal) runs never bundled;
- watermark: after `mark_synced` the unsynced list excludes them and the next
  build is empty;
- offline `.scbundle` file round-trip (write bytes, read back, ingest);
- a FastAPI `TestClient` push round-trip;
- `trusted_edges` accepts a PEM value (not just a fingerprint);
- deterministic bundle bytes;
- migration adds `edge_id` + `synced_at`, idempotent re-upgrade, **zero metadata
  drift** at head.

### Needs on-site / live validation (NOT asserted here) — `live_untested`

- **Online HTTP push over a real network** — the actual `POST` to a remote hub,
  TLS termination at the reverse proxy, the edge API key on the wire, ret/timeout
  behavior. The in-process `TestClient` proves the byte-shuttle contract, **not**
  a real network.
- **A Postgres-backed hub.** The round-trip is proven on **SQLite** on both
  sides. The hub is intended on Postgres (`docs/production-architecture.md`); the
  ingest SQL runs through SQLAlchemy and should port, but Postgres-specific
  behavior (concurrent ingest, large transactions) is not exercised here.
- **Large / real-site bundles** — many runs, large discovery sets, large evidence
  — for build time, memory, ZIP size, and ingest throughput. The tests use small
  fixtures.
- **The wiring** — the edge sync CLI (`python -m app.scripts.sync`), the hub
  ingest endpoint (`POST /api/v1/hub/runs/ingest`), the offline ingest CLI, the
  trusted-edges allowlist config file, and the `deployment_role` setting are
  being finalized by a parallel agent. This document describes their **behavior**
  (which is fixed by the core) and references them generically; the exact flags
  and config format are not invented here.

When you do validate the live paths on site, record the outcome (which edge,
which hub, bundle size, accepted summary) the same way the backup-restore drill
is recorded (`docs/backup-restore.md` §5), so the live behavior becomes
evidence-based rather than assumed.
