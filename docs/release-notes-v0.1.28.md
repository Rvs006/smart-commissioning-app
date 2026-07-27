# v0.1.28: one lease contract, immutable Sync v2 evidence

v0.1.27 stopped healthy Windows captures from expiring near 63 seconds. v0.1.28
applies that ownership model to every execution profile, then completes the
deferred Sync v2 evidence path for hosted deployments.

## What changed

### Shared run ownership

- Backend asynchronous inline, backend synchronous inline, and queued workers use
  one owned-run heartbeat guard and one timing policy.
- The default remains a 60-second lease renewed every 15 seconds. Unsafe,
  non-finite, or greater-than-one-third heartbeat intervals are rejected.
- Heartbeat protection starts immediately after claim, before frozen-context
  loading or engine work.
- API and worker recompute the frozen-context digest and require the stored and
  leased digests to match.
- Worker actors use canonical job types, including validate-only MQTT
  configuration, so inline and queued execution derive the same context and MQTT
  client identity.
- Success, failure, cancellation, missing terminal results, ownership loss, and
  process shutdown use the same fenced finalization and heartbeat cleanup rules.
- A short database interruption can recover within the lease. Confirmed ownership
  loss fences the old executor, and dead-owner maintenance records one actionable
  terminal recovery.
- Redis publication ambiguity stays on the durable queued dispatch. It never
  starts an inline copy, even when the broker accepted a message before the client
  observed an error.

### Sync v2

Sync v2 sends complete, immutable evidence rather than a mutable run projection:

- sealed run ID and canonical terminal-result digest;
- frozen execution context or frozen report snapshot;
- exact `RunResult` and `RunSeal` records;
- signed report-artifact manifest;
- exact report bytes, filename, media type, byte size, renderer version, SHA-256,
  origin, signing key ID, and signature metadata.

The hub verifies the edge signature, credential scope, run/result/seal coherence,
context and snapshot digests, artifact-manifest signature, exact size, and exact
hash before one transaction stores the item. Hub downloads serve the imported
bytes directly.

Each item receives its own receipt. Only `accepted` and `byte_identical` advance
the edge delivery watermark. Conflict, authorization, malformed, signature,
hash, size, missing-artifact, and partial-bundle results stay visible and
unsynchronized. A bundle-level success flag was too coarse for mixed outcomes;
that behavior is removed from the v2 path.

Dedicated sync credentials are stored as hashes and bound to one edge signing
key plus explicit project/site pairs. Unauthorized project and site submissions
return the same small receipt without disclosing hub records.

The sender selects v2 only after a valid capability response. A genuine older
hub may use the unchanged v1 endpoint. Authentication failures, timeouts,
malformed responses, and server errors do not trigger downgrade, and v1 delivery
cannot set a v2 acknowledgement.

Wire format, credential scope, retry receipts, and mixed-version behavior ship as
separate release documents.

### Docker parity and publication

The final hosted gate uses real Compose containers for API, worker, frontend,
Postgres, and password-protected Redis. It covers queued and deliberate inline
execution, a long quiet capture, worker termination and recovery, duplicate
delivery, Redis interruption, encrypted mTLS resolution, Sync v2 transfer and
exact-byte download, frontend controls, browser console, logs, and cleanup.

The workflow publishes three GHCR images from the exact merged commit. Use these
immutable references, never a mutable tag by itself:

- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

`docker-image-evidence.json` records the same references and their OCI version,
revision, and source labels. The three image CycloneDX SBOMs are generated after
pulling those published digests.

### Windows portable changes

The complete v0.1.27 portable suite runs again against a fresh workflow-built
v0.1.28 ZIP. It checks a path containing spaces, root page, health, readiness,
SQLite lease timing, a quiet run beyond two shortened lease windows, Stop,
successful repeat completion, report provenance, byte-identical repeat download,
logs, and thread cleanup. The public acceptance also launches the exact extracted
EXE from File Explorer.

### Migration and rollback

v0.1.28 adds only Sync v2 credential, scope, artifact, receipt, and delivery-state
storage. Existing run, context, result, seal, report, secret, and v1 Sync records
remain in place. Back up the database and all runtime volumes before upgrade.

An application rollback should keep the upgraded database when the prior version
can read it safely. A schema downgrade removes the new Sync v2 tables and their
audit state, so export required receipts and artifacts first. Follow
`MIGRATION_ROLLBACK.md` and `DOCKER_DEPLOYMENT_ROLLBACK.md` for the exact order.

### Known boundaries

- Legacy report runs that lack a v0.1.28 terminal result, seal, frozen snapshot,
  or exact signed artifact are not relabelled as Sync v2 evidence.
- V1 remains available only for mixed-version operation and cannot acknowledge
  the complete v2 contract.
- Real controller, broker ACL, certificate-chain, and site-network validation
  still belongs in the commissioning environment.

## Windows portable download

Download `Smart_Commissioning_App_Windows_Portable.zip`, extract it into a new
empty directory, and run `SmartCommissioningApp.exe`. Existing settings and run
history remain under `%LOCALAPPDATA%\SmartCommissioning`.

The bundle is unsigned. Confirm the ZIP and EXE SHA-256 values below before
choosing Windows SmartScreen's **More info** and **Run anyway**. Managed laptops
may need the recorded EXE hash added to an application allow-list.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

`SHA256SUMS.txt` covers every release payload except itself. Windows evidence,
hosted evidence, five CycloneDX inventories, Docker image evidence, migration and
rollback guidance, and the three Sync v2 documents all resolve to the same source
commit.
