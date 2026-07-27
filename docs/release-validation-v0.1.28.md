# v0.1.28 release validation record

Every unchecked row blocks publication. Record the exact 40-character commit,
workflow URL, artifact digest, and test result beside each item. A green local
build is useful, but it is not release evidence.

## Release identity

- [ ] The branch starts at the published v0.1.27 commit.
- [ ] Local `main`, `origin/main`, GitHub `main`, merged PR commit, signed tag
  target, Windows workflow source, hosted workflow source, release evidence,
  portable bundle source, and all three OCI revision labels are identical.
- [ ] The annotated `v0.1.28` tag verifies locally and GitHub reports a valid
  signature.
- [ ] Exactly one v0.1.28 release branch and PR were used.
- [ ] No unrelated branch, worktree, release, or asset was changed.

## Shared lifecycle parity

- [ ] Backend asynchronous inline, backend synchronous inline, and worker
  execution use the same owned-run heartbeat guard and timing validator.
- [ ] A 60-second lease and 15-second heartbeat are accepted. Unsafe, non-finite,
  too-short, too-long, and greater-than-one-third combinations are rejected.
- [ ] The guard starts immediately after claim and before frozen-context loading.
- [ ] API and worker recompute the stored context digest and require it to match
  both the stored and leased digests.
- [ ] Worker actors pass canonical job types, including validate-only
  `mqtt_config_publish`.
- [ ] Inline and queued execution use the same rules for success, failure,
  cancellation, missing terminal result, confirmed ownership loss, and cleanup.
- [ ] Every success, failure, cancellation, context error, shutdown, and ownership
  loss joins the heartbeat thread.
- [ ] API shutdown abandons local ownership cleanly; maintenance recovers it once
  after the lease expires.
- [ ] A short database interruption retries within the lease without printing a
  connection string. A longer interruption fences the old owner and produces one
  actionable recovery.
- [ ] Redis accepts-then-raises and publish-record failure leave one durable
  dispatch. They never start an inline executor.
- [ ] Concurrent duplicate deliveries produce one processor call, one terminal
  result, and one seal.
- [ ] A live owner survives maintenance beyond its original lease boundary; a
  dead owner is recovered after its final renewed boundary.
- [ ] API and worker resolve the same frozen values and the same opaque encrypted
  certificate references. Decrypted bytes do not appear in logs or evidence.

## Sync v2

- [ ] A new report-generation run has one frozen report snapshot, one terminal
  result, one seal, one signed artifact manifest, and exact stored bytes.
- [ ] The v2 wire bundle contains canonical item JSON and content-addressed report
  bytes, with no raw mutable parameters or secret material.
- [ ] The hub verifies the outer edge signature, key ID, run/result/seal
  coherence, execution-context digest, report-manifest signature, artifact hash,
  artifact size, and safe member paths before commit.
- [ ] A dedicated hashed sync credential is bound to an edge signing key and an
  explicit project/site allow-list.
- [ ] Unauthorized project and site submissions return the same small receipt and
  reveal no hub record or artifact metadata.
- [ ] Each item receives one of the documented receipt classes. Only `accepted`
  and `byte_identical` acknowledge a run.
- [ ] A mixed bundle advances only its acknowledged item IDs. Conflict, malformed,
  signature, hash, size, missing-artifact, and partial-bundle items remain
  visible and unsynchronized.
- [ ] A lost response followed by retry returns `byte_identical` without a second
  result, seal, artifact, or receipt identity.
- [ ] Hub download bytes equal the edge bytes. Two hub downloads also match each
  other byte for byte.
- [ ] v2 is selected only after a valid positive capability response. A genuine
  old-hub response can use v1; authentication errors, timeouts, malformed
  capability payloads, and server errors never downgrade.
- [ ] V1 compatibility remains readable and cannot set a v2 acknowledgement.
- [ ] Request, response, logs, database rows, artifacts, and evidence contain no
  API key, private key, plaintext password, decrypted certificate, owner token,
  or connection URL.

## Database migration and rollback

- [ ] Backup includes Postgres or SQLite plus runtime, encrypted secret store,
  report artifacts, and report-signing material.
- [ ] Upgrade from the v0.1.27 Alembic head reaches the single v0.1.28 head.
- [ ] A second upgrade is a no-op and schema comparison reports zero drift.
- [ ] The documented downgrade removes only additive Sync v2 tables and indexes.
- [ ] Rollback is blocked while v2 delivery state or imported evidence must remain
  available to the prior application.
- [ ] Restore rehearsal verifies counts and SHA-256 values before replacing live
  state.

## Real Docker acceptance

- [ ] A fresh Compose project and fresh volumes start API, worker, frontend,
  Postgres, and password-protected Redis.
- [ ] Alembic upgrade completes; API and worker health checks pass; `/`, health,
  and readiness return HTTP 200.
- [ ] A canonical queued UDMI smoke finishes once.
- [ ] A quiet worker capture runs beyond the original shortened lease with
  multiple renewals.
- [ ] The same controlled capture passes through a deliberately configured
  Docker inline API.
- [ ] Killing the worker, waiting for expiry, and redelivering yields one recovery
  and one immutable terminal result.
- [ ] Redis interruption preserves the durable dispatch and starts no inline copy.
- [ ] API and worker resolve the same encrypted mTLS material.
- [ ] A real edge-to-hub v2 transfer passes exact-byte download, retry, conflict,
  and credential-scope checks.
- [ ] Frontend routes and live controls work against the stack. Browser console,
  API logs, worker logs, and container states contain no error or secret sentinel.
- [ ] `docker compose down -v` removes only the release-test project. The existing
  user Compose project is untouched.

## Published container images

- [ ] API, worker, and frontend are pushed to GHCR from the exact merged SHA.
- [ ] Release evidence records immutable `name@sha256:digest` references. Mutable
  tags alone are never accepted.
- [ ] Pulling each digest returns an OCI image whose version, revision, and source
  labels identify v0.1.28 and the exact repository commit.
- [ ] CycloneDX image SBOMs are generated from those pulled digests, not from a
  local pre-push tag.
- [ ] The digest evidence and all three image SBOMs are present in hosted evidence,
  `SHA256SUMS.txt`, and the public release.

## Fresh Windows portable acceptance

- [ ] The Windows workflow builds `v0.1.28` from the merged SHA and records
  `ProductVersion`, frontend stamp, ZIP size/hash, and EXE size/hash.
- [ ] The complete v0.1.27 suite passes from a new extraction path containing
  spaces: boot, root page, health, readiness, quiet long run, Stop, successful
  repeat run, report provenance, repeated bytes, SQLite checks, logs, and thread
  cleanup.
- [ ] File Explorer launches the exact extracted EXE. Its parent is
  `explorer.exe`, its process path is the fresh directory, and it binds only to
  loopback.
- [ ] `%LOCALAPPDATA%\SmartCommissioning` remains readable and SQLite
  `PRAGMA quick_check` returns `ok`.
- [ ] The release-test process and listener are stopped after validation.

## Repository gates and publication

- [ ] Ruff and complete core, backend, and worker unit suites pass with counts and
  skips recorded.
- [ ] Frontend install, lint, typecheck, Vitest, and production build pass with
  the Vitest count recorded.
- [ ] Migration upgrade/downgrade, release-contract, evidence-validator, Windows,
  Docker, Sync v2, cancellation, recovery, report, and repeated-download tests
  pass.
- [ ] Python, npm, and three published-image CycloneDX inventories validate.
- [ ] The release contains the portable ZIP, checksum manifest, migration guide,
  Windows evidence, hosted evidence, five SBOMs, three Sync v2 documents, Docker
  image evidence, Docker deployment/rollback guide, and complete notes.
- [ ] `scripts/release-portable.ps1` publishes only workflow bytes, then
  `-VerifyExisting` passes.
- [ ] A separate unauthenticated download verifies every public asset size and
  SHA-256 value.
- [ ] The release branch is deleted only after merge and is not attached to a
  worktree. No open PR or worktree created by this release remains.

Keep this record open while any box is unchecked or any digest names a different
commit.
