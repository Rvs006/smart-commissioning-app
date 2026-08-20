# v0.1.51 - native scanner parity and a frictionless field mode

v0.1.51 brings the three standalone discovery tools (IP, BACnet, MQTT) into the
Smart Commissioning Tool as native modules, on top of SCT's authorization,
evidence, and reporting. It also adds a deployment switch that removes the
authorization prompts for field engineers. The portable EXE and Docker images
share the same release identity.

## What changed

- BACnet Discovery surfaces the routers and BBMDs that answered during a scan,
  with the remote network numbers each advertises, in a Routers / BBMDs panel
  and a matching report section. It also adds Browse live objects in a device's
  result detail: it reads that device's live object list and present values on
  demand, without changing the sealed scan results.
- IP Discovery adds Save scan as register: a succeeded scan's responding devices
  become an accepted ip_scanner_register, reusable exactly like an uploaded one.
- MQTT Discovery adds a Live Topic Tree: start a live broker session and watch
  topics arrive in real time, focus an asset to see its live points and last
  config payload, filter the tree by topic, asset, or payload text, and change
  the live subscription without restarting. It also adds Publish message: send
  one message to a topic through a preview that shows the exact bytes and sends
  nothing, an admin approval of that exact message, then a send that replays only
  the approved bytes.
- Frictionless authorization mode: a deployment can set
  SCT_REQUIRE_SCAN_AUTHORIZATION=0 to run scans and device writes with no
  "authorized" checkbox and no two-person approval. The run still records who
  started it and the full evidence. The portable build ships with this enabled,
  so a field engineer starts scans and writes directly. Set the variable to 1 to
  restore the enforced preview-and-approval ceremony. Hosted and Docker
  deployments default to enforced.
- The BACnet and MQTT sidecar scans now bind their uploaded or saved register so
  the scan compares against it. Only IP bound its register before.
- The IP scan report no longer drops typed hosts (for example a host classified
  as a server) from the inventory.

## Compatibility and scope

This release adds no database migration. It retains the run-isolation and
equivalent-retry behavior of earlier releases. The scanner modules run on the
local inline executor. The authorization mode is a deployment setting; the
source and hosted defaults stay enforced, so an existing deployment is unchanged
unless it sets the variable.

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

Use the [migration rollback guide](migration-rollback-v0.1.51.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.51.md), and
[release validation record](release-validation-v0.1.51.md).
