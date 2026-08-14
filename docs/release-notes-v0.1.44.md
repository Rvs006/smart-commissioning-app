# v0.1.44 - BACnet child scans and Nmap approval convergence

v0.1.44 keeps the single portable Windows EXE introduced in v0.1.43 and fixes
four release-blocking workflows: legacy BACnet BBMD endpoint entry, BACnet
property-child scan creation, BACnet target-limit validation, and concurrent
Nmap approval records.

## What changed

- Foreign Device configuration accepts a valid IPv4 `BBMD address:port` value.
  A separately entered BBMD UDP Port is explicit and takes precedence.
- A property scan now accepts the sealed `property_expansion` parent relation,
  retains the parent transport, and narrows the child scan to the selected
  device.
- Contracts reject more than 1,024 BACnet expected unicast targets before a
  contract or run can be created, matching the execution ceiling.
- Two global administrators approving the same detected Nmap installation at
  once now receive the same canonical recorded authority instead of competing
  approval revisions.

## Compatibility and scope

The portable EXE is one unified build for internal and external use. It can
detect an already-installed local Nmap copy, but it does not package, install,
or download Nmap or Npcap. UDMI behavior is unchanged. The release does not
include a database migration.

## Validation boundary

Automated checks cover the corrected contracts, packaged version identity, and
release evidence. They do not prove a particular customer BACnet network,
MQTT broker, or site authorization. Record those approvals and on-site results
in the private commissioning record.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.44.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.44.md), and
[release validation record](release-validation-v0.1.44.md).
