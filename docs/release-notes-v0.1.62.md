# v0.1.62 - Scanner pages recover after an upgrade

v0.1.62 fixes two problems seen in the field. After moving to v0.1.61, the IP,
BACnet and MQTT pages showed "Unexpected Application Error! Failed to fetch
dynamically imported module", and a hard refresh did not clear it. After a
14-hour UDMI capture, the UDMI Workbench showed nothing below the Inspector on
first open. No database migration (Alembic head `a6b7c8d9e0f1`, Sync v2 head
`a7b8c9d0e1f2`).

## What changed

- Stale page recovery (#236): page chunk names change with every release. A
  page loaded from an older release, either a tab left open across the upgrade
  or an older copy still holding port 8000, asks the current server for chunks
  it does not have. The app now reloads once to pick up the current
  `index.html`. If the chunk still fails, it shows a "This page did not load"
  panel with a Reload button instead of the router's raw error. A short
  session guard stops it reloading in a loop.
- `index.html` is served with `Cache-Control: no-cache` from `/`, the SPA
  fallback, a direct `/index.html` request, and the Docker image's nginx, so a
  browser always revalidates the page that names the current chunks.
- `.js` and `.css` are always served as `text/javascript` and `text/css`, so a
  Windows registry mapping of `.js` to `text/plain` cannot block module scripts.
- Portable launcher: when port 8000 is already taken it prints a warning naming
  the Smart Commissioning version holding it (or "another program") and says
  that tabs on port 8000 will not reach the new copy. Before, it moved to the
  next free port without saying so. The warning never blocks startup.
- UDMI run detail reads without the writer lock (#239): `GET
  /validation/runs/{id}`, `/issues` and `/export.json` loaded the whole
  `result_summary` through a `BEGIN IMMEDIATE` session. The UDMI page asks for
  the run and its issues together, so after a long capture one request could
  wait out SQLite's 5 s busy timeout and fail with `database is locked`,
  leaving out the asset topic discovery panel, the wrong-topic assets table and
  the Generate Report card. They now read through the query-only session.
- Single report download (#239): "Generate report from this run" now offers a
  direct download for one format (PDF, Word, Excel or evidence pack), as
  Generate All already did with its combined ZIP.

## What did not change

- No engine, run parameter or API shape changed. Scans, UDMI validation and
  reports persist the same runs and evidence as v0.1.61.
- The launcher still starts on the next free port when 8000 is taken; it now
  says so.

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

Use the [migration rollback guide](migration-rollback-v0.1.62.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.62.md), and
[release validation record](release-validation-v0.1.62.md).
