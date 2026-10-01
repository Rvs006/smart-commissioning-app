# v0.1.60 - fewer locked-database errors and fuller BACnet object lists

v0.1.60 fixes four problems seen in the field. Pages that only read data stop
failing with `database is locked` while a scan or report is writing, BACnet
discovery lists the objects of a device that cannot send its object list in one
reply, UDMI validation counts unexpected devices on large sites instead of
reporting "at least N", and Configuration shows when the Source Interface on
screen has not been saved. No database migration (Alembic head `a6b7c8d9e0f1`,
Sync v2 head `a7b8c9d0e1f2`).

## What changed

- Plain reads (#227, #232): Run History (`GET /runs`), the latest import, the
  UDMI schema list and the per-request scope and ownership checks now read
  through the query-only session. They used to queue behind a running scan or
  report write and give up after the 5 second busy timeout with
  `database is locked`. Anything that reads and then writes still takes the
  write lock first. A named user's last-used time is stamped at most once a
  minute on reads; changes still stamp it every time.
- BACnet object lists (#229): when a device Aborts the whole-array
  `object-list` read (segmentation not supported, buffer overflow, APDU too
  long) or the read times out, discovery reads the length and then each entry by
  index, with the same timeout, throttle and Stop handling as point reads, up to
  10,000 entries. The device row records `object_list_indexed_read` with the
  entries read and the total. If an entry read fails or the cap is hit, the
  objects already read are kept and a `bacnet_object_list_partial` issue says
  how many were missed. Present-value is no longer read on object types that do
  not have one (file, network-port, device and the like); those objects are
  still listed.
- UDMI unexpected devices (#230): the secondary lane's payload cap is sized to
  the register, so a site with more unregistered publishers than that used to
  report the unexpected-device count as a lower bound. The capture now keeps a
  list of unexpected publisher topic names only (no payloads, up to 100,000
  roots or 32 MiB of names) and reports a measured count when that list did not
  overflow. Past either limit the count stays an honest lower bound, flagged as
  `capture_retention.unexpected_root_inventory_truncated`.
- Configuration and UDMI run time (#231): a "Not saved yet" note sits under the
  Source Interface dropdown until Save Configuration when the value shown is
  not the saved one. Scans read the saved value, so an unsaved pick used to look
  set while scans failed with "No Source Interface selected". The informational
  BACnet "BBMD" toggle is gone; Foreign Device still registers with a BBMD, and
  a saved or imported configuration that carries the old key still loads. UDMI
  validation warns before a run when the run time is shorter than the largest
  Expected reporting interval in the imported register, and
  `GET /imports/latest` returns `max_expected_reporting_interval_seconds` for an
  MQTT register import. The warning does not block the run.

## What did not change

- No engine, route or run parameter was removed. Scans, UDMI validation and
  reports persist the same runs and evidence as v0.1.59.
- The BBMD Address and BBMD UDP Port fields are unchanged.

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

Use the [migration rollback guide](migration-rollback-v0.1.60.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.60.md), and
[release validation record](release-validation-v0.1.60.md).
