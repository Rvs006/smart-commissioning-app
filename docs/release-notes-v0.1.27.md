# v0.1.27 - Portable runs keep their lease

A 32,400-second capture should never die at 63 seconds because its executor
forgot to check in. v0.1.27 fixes that Windows portable failure while preserving
real crash recovery, Stop run, and immutable terminal evidence.

## Field symptom and cause

The portable application accepted a nine-hour UDMI capture, returned its run ID,
then failed after about 1 minute 3 seconds. The run monitor showed `lease expired`
and `execution owner lease expired`.

Portable mode uses asynchronous inline execution. v0.1.26 claimed each inline
run with a 60-second database lease, but the generic inline dispatcher started no
lease heartbeat. The lifecycle maintenance service checks for expired owners
every five seconds, so a healthy capture could be sealed as failed 60 to 65
seconds after the claim. Progress updates changed `updated_at`; they did not renew
`lease_expires_at`.

The separately reported v0.1.25 startup problem still needs its own Windows error
or application log. Current evidence does not connect that launch path to this
lease failure.

## What changed

- Every successfully claimed inline run starts an ownership heartbeat before
  frozen-context resolution or engine execution.
- The default 60-second lease renews every 15 seconds. Configuration keeps the
  lease between 15 and 300 seconds and rejects a non-finite heartbeat or an
  interval above one third of the lease duration.
- Asynchronous portable and synchronous inline execution use the same guard.
- Heartbeats are independent of MQTT messages, network activity, and progress
  writes. A quiet broker can remain quiet for the whole capture window.
- A transient database error is logged by exception class, without connection
  text or credentials, and retried with bounded timing.
- A heartbeat that returns `False` without an already-committed,
  non-conflicting terminal outcome confirms ownership loss. The old executor is
  fenced from later progress and terminal writes. A `False` result after normal
  finalization is treated as completed cleanup.
- Owner-held success, failure, cancellation, context-resolution failure, and
  processor exceptions stop and join the heartbeat after terminal finalization.
  A confirmed stale owner writes no competing terminal result and joins its
  heartbeat during cleanup.

The API still returns the accepted run ID immediately, the Stop run control
still works, and the five-second dead-owner recovery service remains active.

## How to read the supplied field report

The supplied report labels its validation scope incomplete because its source
run failed. Its missing devices, missing payloads, missing points, and compliance
figures cover only the evidence retained before that early failure. They describe
a short window of partial observation. Repeat the full nine-hour validation after
upgrading.

A report-generation run and its validation source run have different IDs by
design. For example, `run_report_example` may render evidence from
`run_validation_example`. Keep both identifiers in audit records. The report ID
identifies the rendering job; the source-run ID identifies the field execution.

## User impact

Windows portable operators can run long, silent inline captures beyond the
original lease boundary. Live owners remain protected, dead owners still recover,
and stale owners still lose the right to write. Existing reports and terminal
runs remain immutable.

v0.1.27 changes no database table, column, Alembic revision, secret format, or
API payload schema. Existing v0.1.26 portable and hosted state stays readable.
Back up first and follow `docs/migration-rollback-v0.1.27.md` for the complete
replacement and rollback procedure.

## Windows portable download

Use the ZIP attached to the GitHub release. Release assets must come from the
workflow for the merged tag commit; a locally built executable is diagnostic
output only.

Extract the ZIP into a new empty directory, including when an older portable
version already exists. State under `%LOCALAPPDATA%\SmartCommissioning` survives
the replacement. The bundle is unsigned, so Windows SmartScreen may show a
warning. Check the ZIP and EXE SHA-256 values against this release, then choose
**More info** and **Run anyway** only when the file came from the GitHub release.
Company application allow-listing may require IT approval for the recorded EXE
hash.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

## Known boundary

Sync v2 is scheduled for v0.1.28 together with shared API/worker heartbeat
infrastructure and Docker lifecycle parity. v0.1.27 keeps the compatible v1 sync
path. It leaves digest conflicts visible and unsynchronized, and it does not send
the exact report bytes or signed artifact manifest to a hub. See
`docs/sync-v2-v0.1.28.md`.

Publication also carries `SHA256SUMS.txt`, Windows and hosted release-evidence
JSON, the migration and rollback guide, and CycloneDX inventories for Python,
npm, API image, worker image, and frontend image. The tag, workflow source,
evidence records, bundle, checksums, and release body must all resolve to the same
40-character commit before an operator uses the build.
