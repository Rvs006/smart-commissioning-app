# v0.1.27 release validation record

Every unchecked row blocks publication. Record the 40-character commit and a
durable evidence link beside each result. A green local build is useful; the
workflow-built ZIP is the artifact that matters.

## Release identity

- [ ] Local `main`, `origin/main`, GitHub `main`, merged PR commit, signed tag
  target, workflow source, release evidence, and bundle source are identical.
- [ ] The starting v0.1.26 commit is recorded and the worktree contains only the
  reviewed v0.1.27 changes.
- [ ] The annotated `v0.1.27` tag signature verifies locally and GitHub identifies
  the signing key.
- [ ] Windows and hosted workflow URLs, conclusions, and source SHAs are recorded.
- [ ] The release branch, PR, tag, and release URL are recorded in the handoff.

## Targeted heartbeat contracts

- [ ] A controlled clock claims at `t0`, renews before expiry, survives recovery
  after the original boundary, then recovers after the renewed lease expires.
- [ ] Asynchronous inline execution records multiple renewals while an
  event-controlled processor is blocked, completes exactly once, and stops its
  heartbeat.
- [ ] Synchronous inline execution renews while its caller is blocked.
- [ ] Crash, cancellation, context-resolution failure, processor exception, and
  confirmed ownership loss each stop and join the heartbeat thread.
- [ ] A transient database heartbeat error is retried without logging connection
  text or credentials. The real SQLite test must hold its write lock through the
  busy timeout before proving a later renewal succeeds.
- [ ] Claim, heartbeat, progress, and first finalization wait for the lifecycle
  row lock before sampling validity time; a lock held across expiry cannot revive
  or write from the expired owner.
- [ ] Non-finite heartbeat intervals and lease overrides above 300 seconds fail
  validation instead of creating a tight loop or masking dead owners.
- [ ] A real repository integration test uses shortened injected intervals and
  proves renewal through the production dispatcher and lifecycle store.
- [ ] A quiet-broker test proves that no MQTT message or progress write is needed
  for ownership.
- [ ] The portable asynchronous UDMI path retains `capture_seconds=32400` while
  an event-controlled capture stays active beyond its original lease.
- [ ] Maintenance recovery leaves a live renewed owner running and recovers a
  dead owner after its final lease expires.
- [ ] Late progress and finalization from a stale owner cannot replace the
  recovered terminal result.
- [ ] No heartbeat continues after success, failure, cancellation, or ownership
  loss; existing dead-owner recovery tests still pass.

## Repository gates

- [ ] `ruff check backend worker core` passes.
- [ ] Core `unittest` discovery passes. Record pass and skip counts.
- [ ] Backend `unittest` discovery passes. Record pass and skip counts.
- [ ] Worker tests pass. Record pass and skip counts.
- [ ] `npm ci` completes with the Node version used by release CI.
- [ ] Frontend lint, typecheck, Vitest, and production build pass. Record the
  Vitest count.
- [ ] Migration and rollback smoke passes with no new Alembic revision.
- [ ] Report immutability, repeated-download, cancellation, and SSE suites pass.
- [ ] Windows portable build, boot smoke, long inline heartbeat smoke, and
  canonical UDMI smoke pass.
- [ ] Python and npm CycloneDX inventories validate.
- [ ] API, worker, and frontend image SBOMs match the hosted workflow images.
- [ ] `SHA256SUMS.txt` verifies every release payload except its intentional
  self-reference exclusion.

## Fresh Windows portable acceptance

- [ ] `packaging/windows_portable/build.ps1 -Version v0.1.27` succeeds in the
  release workflow for the merged commit.
- [ ] The workflow ZIP is downloaded into a new directory and extracted into a
  completely empty folder. No v0.1.24, v0.1.25, or v0.1.26 folder is reused.
- [ ] ZIP and EXE byte sizes and SHA-256 values match the release body,
  `SHA256SUMS.txt`, and Windows evidence JSON.
- [ ] `README_FIRST.txt`, the frontend build stamp, and EXE `ProductVersion` all
  identify v0.1.27.
- [ ] File Explorer launches the exact downloaded EXE, and the process path
  points to the fresh extraction.
- [ ] A second launch from a path containing spaces passes without reading files
  from an older extraction.
- [ ] The home page returns HTTP 200; `/api/v1/health` returns `ok`; and
  `/api/v1/ready` returns `ready` with a passing database check.
- [ ] Existing state below `%LOCALAPPDATA%\SmartCommissioning` remains readable,
  including configuration, imports, history, secrets, reports, and signing data.
- [ ] A controlled silent inline run remains active beyond 65 seconds and the
  monitor never reports `lease_expired` while the owner is alive.
- [ ] Stop run reaches one terminal result and the run's heartbeat stops.
- [ ] A separate controlled inline run completes successfully exactly once after
  crossing the original lease boundary.
- [ ] A generated report retains the selected validation source-run ID. A sample
  `run_report_example` and `run_validation_example` remain distinct in the audit
  record.
- [ ] Two downloads of the same stored report have identical bytes and SHA-256.
- [ ] Repeat downloads of the release ZIP have identical bytes and SHA-256.
- [ ] `app.log` and crash logs contain no unhandled exception, repeated heartbeat
  failure, SQLite lock storm, credential material, or leaked background thread.
- [ ] SmartScreen and unsigned-build guidance matches the exact release source
  and hashes; managed-device allow-listing guidance is still accurate.

## Field-report interpretation

- [ ] Acceptance evidence identifies the field source run as failed and the
  generated report as an incomplete validation scope.
- [ ] Missing devices, payloads, points, and compliance figures are recorded as
  observations from the short retained window.
- [ ] No release statement describes that failed partial report as a completed
  32,400-second validation.
- [ ] Report-generation and validation source-run IDs are both retained, with
  their different purposes explained.
- [ ] No site name, device ID, network address, person, credential, or commercial
  detail from field evidence appears in the public repository or release body.

## Migration, rollback, and security

- [ ] The pre-upgrade portable state or hosted database and volumes are backed up
  and the backup manifest hashes verify.
- [ ] The Alembic head and stored record counts are unchanged by the upgrade.
- [ ] A v0.1.26 application rollback reads existing state without rewriting
  sealed v0.1.27 terminal results or report artifacts.
- [ ] Heartbeat logs expose no database URL, broker credential, certificate,
  token, private key, or decrypted secret material.
- [ ] The five-second recovery service and ownership fencing remain enabled.

## Publication and independent verification

- [ ] The portable ZIP, `SHA256SUMS.txt`, migration guide, Windows evidence JSON,
  hosted evidence JSON, five CycloneDX SBOMs, and complete release notes are
  attached to the GitHub release.
- [ ] `scripts/release-portable.ps1` publishes from the successful exact-SHA
  workflows and replaces all three publisher tokens in the release notes.
- [ ] `scripts/release-portable.ps1 -VerifyExisting` passes after publication.
- [ ] Release assets are downloaded independently into a fresh directory and
  every recorded digest is recomputed.
- [ ] No locally produced executable or ZIP is attached as a release asset.
- [ ] The final handoff records test counts and skips, ZIP and EXE sizes and
  hashes, ProductVersion, long-run and cancellation results, remaining limits,
  and the complete SHA equality chain.
- [ ] Open PR, branch, worktree, process, and container audits show no stale item
  created by this release work. Unrelated user branches and files remain intact.

Keep this record unfinished while any checkbox is blank or any digest names a
different commit.
