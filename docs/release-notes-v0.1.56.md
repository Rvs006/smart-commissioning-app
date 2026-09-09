# v0.1.56 - the scanners become native SCT screens

v0.1.56 takes the scanner work the rest of the way. The IP, BACnet, and MQTT
scanners are now native screens in the app rather than the embedded standalone
tools shown in an in-app panel, and the old reverse-proxy embed stack is deleted.
A completed scan is still saved as a real scanner run, so Results, run history,
and reports fill in as before. No database migration (Alembic head a6b7c8d9e0f1,
Sync v2 head a7b8c9d0e1f2).

## What changed

- Native scanners: the IP, BACnet, and MQTT scanners each render as a single
  configure-and-scan SCT screen (PRs #201, #203, #204). IP and BACnet re-compare,
  save-as-register, BACnet per-row object browse, and the BACnet asset export all
  carry over, and the MQTT live topic view is unchanged. The built-in discovery
  modules are untouched.
- Reverse proxy removed: the Advanced scanner panel and its `/scanners/{proto}/raw`
  routes are gone, together with the panel session and the per-write confirmation
  token behind them (PR #205). The native scanners replace them.
- Live-scan persistence foundation (GAP-C3): live IP and BACnet sidecar scans now
  emit progressive device observations to the durable observation store as they
  run (PR #202). Final results and RAG are unchanged; the read and render path for
  those live rows lands in a follow-up.
- MQTT register import accepts an optional `Section` column (UDMI
  `system.location.section`) alongside `Room` and `Floor` (PR #198). It appears in
  the downloadable template and is captured on accepted rows; leaving it blank
  never rejects a row, and registers without the column import exactly as before.
- The BACnet scanner gained the same "Ignore register for this run" toggle the IP
  scanner already had, so a BACnet scan can run without freezing a register into
  it (PR #208).

## Fixes in this release

- MQTT discovery no longer truncates a full-site capture at 500 distinct topics.
  The registerless discovery engine's default distinct-topic cap now equals the
  memory-safe ceiling `MAX_TOPIC_CAP` (10,000) instead of 500 (PR #196). A capture
  cut short by the 256 MB retained-payload backstop is surfaced with
  `byte_limit_reached` and a `byte_cap` detail rather than reported as complete
  (PRs #199, #200).
- BACnet discovery saves its discovered points. A native BACnet scan counted
  points in its summary while the Points / Live Data view came back empty, because
  the points were written to the device table instead of the points table; they
  now persist correctly (PR #206).
- More scanner evidence-integrity fixes (PR #207): IP results no longer error
  while reading back observed ports; a half-filled or inverted BACnet
  device-instance range is rejected at the Run button instead of scanning every
  instance; an MQTT live session compares against this workspace's register; a
  BACnet point export cut short by a deadline or the per-device object cap is
  flagged rather than reported as a complete zero-point network; and two MQTT live
  connects starting at once no longer cross workspaces.
- The native scanner Run button (IP, BACnet, MQTT) stays disabled until scan
  authorization is confirmed, matching the server, which already refused an
  unauthorized run (PR #208).
- Saved raw evidence downloads with a filename extension that matches its type: a
  captured MQTT export as `.zip`, Nmap XML or stderr as `.xml` or `.txt`, instead
  of always `.bin` (PR #208).
- An embedded scanner recovers on a sidecar outage and clears a stale write dialog
  (PR #197).

## Compatibility and scope

The native scanners run on the local inline executor and authenticate via the
local principal, so they are available in the portable and local deployments.
Built-in TCP connect remains the default; Nmap stays optional, locally installed,
and unbundled. Scans recorded by the old Advanced panel stay readable in Run
History: their evidence rows and the `scanner_raw_action` and `scanner_raw_write`
job types are kept, so old rows still render. This release adds no BACnet write
capability.

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

Use the [migration rollback guide](migration-rollback-v0.1.56.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.56.md), and
[release validation record](release-validation-v0.1.56.md).
