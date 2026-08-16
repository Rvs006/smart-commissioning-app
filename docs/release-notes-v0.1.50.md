# v0.1.50 - in-app operator guidance

v0.1.50 updates the Brief and Learning pages so a first-time engineer can
follow the protected discovery workflow and understand when final evidence is
ready. The portable EXE and Docker images share the same release identity.

## What changed

- Brief now explains dry previews, sealing, approval timing, authorization
  selection, live submission, terminal confirmation, failures, and safe
  equivalent retries.
- Learning adds numbered IP discovery and retry procedures, all operator-visible
  scan states, BACnet/MQTT/UDMI/Nmap boundaries, report and export timing, and
  practical troubleshooting for the current application.
- MQTT guidance states that a blank Payload type is valid where required and
  that `#` belongs only in a topic or topic-filter field. Private customer
  templates remain outside the public release.
- Nmap guidance confirms that Built-in TCP connect is the default. Nmap is
  optional, separately installed, and unrelated to the retry and preview/live
  evidence fixes delivered in v0.1.49.

## Compatibility and scope

This release adds no database migration, protocol behavior change, or BACnet
write capability. It retains the v0.1.49 run-isolation and equivalent-retry
behavior. The product does not package, install, or download Nmap or Npcap.

## Validation boundary

Automated checks cover the in-app content contracts, interactions, responsive
layout, version identity, portable artifact, and Docker evidence paths. They do
not approve a live customer network scan. Record approved field evidence in the
private project record.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.50.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.50.md), and
[release validation record](release-validation-v0.1.50.md).
