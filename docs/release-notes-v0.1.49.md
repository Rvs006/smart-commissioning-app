# v0.1.49 - epoch-scoped run evidence

v0.1.49 prevents a completed preview from leaking terminal status, evidence,
or downloads into a later live submission when an adapter reuses the same run
ID. The portable EXE and Docker images share the same release identity.

## What changed

- Every accepted or restored active run has a submission epoch. SSE events,
  terminal catch-up, results, issues, BACnet points and comparison evidence,
  MQTT topics, property actions, reports, and active-run downloads must match
  the current run ID, epoch, session, and workspace before they can update UI.
- IP discovery continues to retry an equivalent idempotent request, including
  the exact v0.1.48 raw replay representation. A conflicting key reuse remains
  rejected.
- MQTT template guidance uses a blank Payload type. `#` belongs only in a topic
  filter; keep corrected local copies private unless an approved asset list says
  they are release assets.

## Compatibility and scope

This release adds no database migration and no BACnet write capability. Builtin
TCP remains the default discovery provider. Nmap remains opt-in, locally
installed, and unbundled: the product never packages, installs, or downloads
Nmap or Npcap.

## Validation boundary

Automated checks cover same-ID preview/live epoch isolation, scoped cache and
download cancellation, equivalent IP retries, version identity, portable
artifact, and Docker evidence paths. They do not approve a live site scan.
Record approved IP, BACnet, MQTT, and UDMI field evidence privately.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.49.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.49.md), and
[release validation record](release-validation-v0.1.49.md).
