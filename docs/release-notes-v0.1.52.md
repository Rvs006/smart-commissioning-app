# v0.1.52 - the standalone scanners, embedded in SCT

v0.1.52 puts the full standalone IP, BACnet, and MQTT discovery tools inside the
Smart Commissioning Tool as an Advanced tab on each discovery module, with SCT on
top: reads run freely, device writes are confirmed and recorded, and every action
lands as SCT evidence. It also adds the sidecar-lane operator inputs the field
asked for and stabilizes the MQTT live tree. The portable EXE and Docker images
share the same release identity.

## What changed

- Advanced tab on the IP, BACnet, and MQTT discovery modules embeds each
  standalone scanner tool's own UI inside SCT through a backend reverse proxy
  (/api/v1/scanners/{proto}/raw/). Reads (scans, browses, exports) run freely for
  the engineer role. Device writes, MQTT publish and config, pause for an in-app
  confirmation that shows the exact topic and value and is gated by a single-use
  token bound to the request bytes. Every action is recorded as an SCT run row:
  scanner_raw_action for reads and scanner_raw_write for writes.
- IP Discovery accepts an operator-entered target IP range, and BACnet Discovery
  freezes the configured Source Interface into the run, so both sidecar scans run
  without the earlier "no scan range provided" and "no source interface" failures.
  The dry-run step is removed from the three sidecar lanes.
- The MQTT live topic tree holds a stable A-Z order instead of reshuffling as
  message rates change, and the embedded MQTT panel carries the standalone tool's
  latest topic-order fix and copy-payload button.

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

Use the [migration rollback guide](migration-rollback-v0.1.52.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.52.md), and
[release validation record](release-validation-v0.1.52.md).
