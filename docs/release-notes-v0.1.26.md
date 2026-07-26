# v0.1.26: completed evidence stays completed

This reliability release closes the race conditions that could let a delayed
worker, browser request, or report download show evidence from the wrong point
in time.

The best change is deliberately boring: download the same report twice and you
get the same bytes.

## What you will notice

- A completed run is sealed. Delayed queue messages and stale workers cannot
  change its status, counts, devices, points, topics, or issues.
- A report is generated from one frozen completed run. Every later download
  returns the same stored bytes and SHA-256, even after configuration or imports
  change.
- MQTT profiles and BACnet bind addresses have active-run protection. A second
  conflicting request receives HTTP 409 and the ID of the run already using the
  connection.
- Hosted workers load the same stored context and encrypted certificate material
  as the API. Production Compose uses Redis queue execution with no inline
  fallback after an uncertain publish.
- Route changes, sign-in changes, and delayed SSE events stay attached to the
  session and run that created them.
- Pass, Fail, and completed metrics appear after the final issues, results, and
  MQTT topics have been fetched for that run.
- Passwords and tokens are extracted from database snapshots into versioned,
  encrypted `secret://` material. JSON exports exclude passwords, certificates,
  and private keys; transfer a full installation with the encrypted runtime
  backup instead.

## Deployment changes

Hosted deployments must follow `docs/migration-rollback-v0.1.26.md`. Back up
Postgres plus the runtime, secret, artifact, and report-signing volumes, drain
workers, apply the additive migration, verify the terminal backfill, and then
start v0.1.26 workers.

MQTT brokers must allow the dynamic client-ID prefix described in
`docs/mqtt-client-id-and-acl.md`. Exact IDs are unique per run and stay within
the MQTT 3.1.1 limit of 23 ASCII bytes.

Portable Windows data remains under
`%LOCALAPPDATA%\SmartCommissioning`: SQLite, imports, encrypted secrets, report
artifacts, and the report-signing key all survive replacement of the extracted
application folder. Portable mode stays inline and does not require Redis.

## Known limit

Sync v2 is scheduled for v0.1.27. v0.1.26 keeps the compatible v1 transfer path
but does not mark an immutable conflict as synchronized or let sync replace a
sealed terminal result. Exact report bytes and signed manifests are not yet
transferred to a hub. See `docs/sync-v2-v0.1.27.md`.

## Windows portable evidence

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

The release carries CycloneDX Python, npm, API-image, worker-image, and
frontend-image SBOMs, `SHA256SUMS.txt`, both workflow evidence records, and the
migration/rollback guide. Publication is blocked unless the hosted and Windows
workflows, tag, release assets, and required checks all name the same commit.
