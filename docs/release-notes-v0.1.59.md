# v0.1.59 - the scanner screens show what the register expects

v0.1.59 rebuilds the IP, BACnet and MQTT scanner screens as their own pages,
puts the register verdict on every row, and fixes two field problems: a slow
Reports tab whose bulk delete failed with "Internal Server Error", and UDMI
validation reporting that went wrong after the observation-only MQTT lane hit
its limit. No database migration (Alembic head `a6b7c8d9e0f1`, Sync v2 head
`a7b8c9d0e1f2`).

## What changed

- Scanner pages (#220, #221, #222): each scanner is one scrolling page with a
  Scan setup card, a results table with six register counters (Expected,
  Reachable, Match, Partial, Missing, Rogue) and RAG filter chips, and a sticky
  resizable detail panel beside the table instead of a dialog under it. MQTT
  opens on the live topic tree and connects on arrival when Configuration has a
  broker. The old scanner branches of the shared module page are deleted; the
  built-in discovery lanes, UDMI validation, data validation and reports keep
  their wizard and sealed preview unchanged.
- Register verdicts on the rows (#219): IP and BACnet rows are coloured by the
  verdict the scan reached, a device that answered but is not in the register
  reads "Rogue (not in register)", and every register device that never
  answered gets a red "Missing" row. An IP register host outside the scanned
  range reads "Not probed" instead of "Unreachable". The signed inventory report
  lists the same expected-but-silent devices.
- Register as a file (#218): "Save scan as register" offers a `register.csv`
  download rebuilt from the run that was saved. The MQTT live explorer can save
  a register straight from the live session. A direct config send to live
  equipment now asks for confirmation, showing the topic, QoS, retain flag and
  payload first.
- Reports tab (#224): the report list and run list no longer take the SQLite
  write lock to read, so they stop blocking other requests with
  `database is locked`, and bulk delete takes the lock once. The tab shows the
  newest 10 reports, and "Show older reports" pages further back by offset
  (#226), so reports older than the newest 100 are reachable again.
- UDMI secondary-lane reporting (#223): wrong-topic detection, the asset
  topic-discovery ledger and the unexpected-device count stay correct after the
  observation-only lane overflows, and a count that could not be completed is
  reported as a lower bound ("at least N").
- Dev server only (#225): `npm run dev` no longer shows every API call aborted
  under React.StrictMode. Production builds were not affected.

## What did not change

- No engine, backend route, run parameter or discovery logic moved for the
  scanners. A completed scan still persists as a real `ip_scanner`,
  `bacnet_scanner` or `mqtt_scanner` run, and Run History and Reports fill in as
  before.
- The retained `scanner_raw_action` and `scanner_raw_write` job types still
  render old Advanced-panel rows in Run History.

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

Use the [migration rollback guide](migration-rollback-v0.1.59.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.59.md), and
[release validation record](release-validation-v0.1.59.md).
