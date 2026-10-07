# v0.1.63 - Ignore payloads outside Payload applicability

v0.1.63 adds one site setting asked for in the field. A register row can limit
an asset to some UDMI payload types through its Payload applicability column,
for example `state, metadata`. Until now, if that device still published
pointset, the run raised a high "not approved for this asset" issue and then
checked the pointset contents anyway. No database migration (Alembic head
`a6b7c8d9e0f1`, Sync v2 head `a7b8c9d0e1f2`).

## What changed

- Ignore Payloads Outside Applicability (#241): Configuration > Validation Rules has a
  new Enabled/Disabled setting, Disabled by default. When Enabled, a payload
  type that a register row's Payload applicability leaves out is reviewed as if
  it never arrived: no `payload_not_applicable` issue, and no identity, unit,
  point or freshness checks on it. The payload still shows in the run's
  payload view as received evidence and never counts as a validated payload.
- Each UDMI run freezes the setting from the same configuration read as its
  configuration snapshot, so a later change never alters an old verdict. The
  client cannot override it.

## What did not change

- With the setting Disabled, validation behaves exactly as in v0.1.62.
- Capture is unchanged: the register `/#` filter still subscribes to every
  payload topic, so the raw evidence is still recorded.
- Payload applicability itself is unchanged: blank Payload type and blank
  Payload applicability still mean all three payload types.

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

Use the [migration rollback guide](migration-rollback-v0.1.63.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.63.md), and
[release validation record](release-validation-v0.1.63.md).
