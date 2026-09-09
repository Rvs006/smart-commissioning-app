# v0.1.57 - the native scanners become one page

v0.1.57 is a presentation-only release. The three native scanner screens (IP,
BACnet, and MQTT) now show as one configure-and-scan page instead of the
Setup / Run / Results step wizard, so an operator sets up a scan, watches it
run, and reads the results without stepping between tabs (#210). Nothing behind
the screen moved: no engine, backend route, API call, run parameter, or
discovery logic changed. No database migration (Alembic head `a6b7c8d9e0f1`,
Sync v2 head `a7b8c9d0e1f2`).

## What changed

- Single-page native scanners: the `ip-scanner`, `bacnet-scanner`, and
  `mqtt-scanner` lanes drop the Setup / Run / Results stepper and render
  configuration, live progress, and results together on one scrolling page
  (#210). The Run Controls heading reads "Scan setup" on these lanes, and a
  footer line points at the saved run in Run History and Reports. Every piece
  that used to sit behind a step is reused in place; it just lives on one page
  now.

## What did not change

- No engine, discovery, evidence, or reporting behaviour changed. A completed
  scan still saves as a real `ip_scanner` / `bacnet_scanner` / `mqtt_scanner`
  run; IP and BACnet re-compare, save-as-register, BACnet object browse, and the
  BACnet asset export are exactly where they were, and the MQTT live topic view
  is unchanged.
- The sealed built-in discovery lanes (`ip-scanner-sct`, `bacnet-discovery-sct`,
  `mqtt-discovery-sct`), the dry-run preview, and the reports page keep their
  stepped layout. Only the native single-page scanner lanes changed.
- The retained `scanner_raw_action` and `scanner_raw_write` job types still
  render old Advanced-panel rows in Run History, the same as in v0.1.56.

## Compatibility and scope

The native scanners run on the local inline executor and authenticate via the
local principal, so they are available in the portable and local deployments.
Built-in TCP connect remains the default; Nmap stays optional, locally
installed, and unbundled. This release adds no BACnet write capability and no
database migration.

## Validation boundary

CI builds and boot-smokes the portable bundle on a Windows Server 2022 runner.
Field acceptance for this release remains open (UNPROVEN); it will be recorded
privately once the field-acceptance checklist, evidence hashes, and owner
sign-off are complete.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.57.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.57.md), and
[release validation record](release-validation-v0.1.57.md).
