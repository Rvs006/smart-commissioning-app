# v0.1.61 - Reports list opens quickly on large sites

v0.1.61 fixes one problem seen in the field: on a large UDMI site the Reports
page sat on "Loading reports..." for about two minutes. It also relabels a
capture status that read like a failure. No database migration (Alembic head
`a6b7c8d9e0f1`, Sync v2 head `a7b8c9d0e1f2`).

## What changed

- Reports list (#234): `GET /reports` used to load every listed report's full
  stored evidence and canonically re-hash its frozen snapshot, twice per row, on
  each page load. A UDMI report freezes whole source-run snapshots, many MB on a
  large site, so a ten-row page took minutes. The list now reads through
  `page_report_summaries`, which keeps the structural checks (result, seal and
  evidence-contract rows present, contract project and site scope) and reads
  only the fields the list shows from the sealed snapshot copy. A local profile
  with ten 1.7 MB reports went from 2.29 s to 0.11 s, and the time no longer
  grows with each report's size.
- Integrity still runs where evidence leaves the app: opening, downloading,
  exporting or verifying a report fully re-checks it, as before. A tampered
  report now lists with its sealed title and fails closed when opened, instead
  of failing the whole list with 409.
- Asset topic discovery: a completed capture whose side store for non-register
  topics filled now shows "Completed (non-register topic store full)" instead
  of "secondary byte limit reached" or "secondary topic limit reached". Topic
  matches, wrong-topic payloads and the unexpected-device inventory are counted
  before that store applies, so they cover the whole capture window.

## What did not change

- No engine, route or run parameter was removed. Scans, UDMI validation and
  reports persist the same runs and evidence as v0.1.60.
- The secondary payload store keeps its 2 MiB budget.

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

Use the [migration rollback guide](migration-rollback-v0.1.61.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.61.md), and
[release validation record](release-validation-v0.1.61.md).
