# v0.1.53 - release-gate secret-scan and provenance hardening

v0.1.53 is a security-hardening release on top of v0.1.52. It tightens the
release-gate secret scanning and the portable-bundle provenance checks and
scrubs a placeholder from the vendored MQTT discovery page. There is no
functional change to discovery, the Advanced scanner panels, reporting, or
evidence: the whole v0.1.52 surface is carried forward unchanged. The portable
EXE and Docker images share the same release identity.

## What changed

- Hardened the release-gate secret scanning: the release and Windows workflows
  scan the assembled evidence and the packaged ZIP for credential material with
  a stricter scanner and fail closed before any artifact upload.
- Hardened portable-bundle provenance: a build from a dirty worktree is marked
  non-publishable, and the release evidence contract requires a clean rebuild at
  the authorized release commit before publication.
- Scrubbed a placeholder from the vendored mqtt-discovery page so no private-looking
  template identifier ships in the bundled Advanced MQTT panel.

## Carried forward from v0.1.52 (unchanged)

- The Advanced tab on the IP, BACnet, and MQTT discovery modules still embeds each
  standalone scanner tool's own UI through the backend reverse proxy
  (/api/v1/scanners/{proto}/raw/). Reads run freely for the engineer role; device
  writes pause for an in-app confirmation gated by a single-use, request-bound
  token. Every action is still recorded as an SCT run row: scanner_raw_action for
  reads and scanner_raw_write for writes.
- The sidecar-lane operator inputs (IP target range, frozen BACnet Source
  Interface) and the stable A-Z MQTT live topic tree are unchanged.

## Compatibility and scope

This release adds no database migration and retains the run-isolation and
equivalent-retry behavior of earlier releases. The scanner modules and the
Advanced panel run on the local inline executor. The Advanced panel authenticates
via the local principal, so it is available in the portable and local
deployments; a keyed hosted deployment needs the deferred panel-session
credential.

## Validation boundary

CI builds and boot-smokes the portable bundle on a Windows Server 2022 runner.
Field acceptance for this release is recorded privately.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.53.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.53.md), and
[release validation record](release-validation-v0.1.53.md).
