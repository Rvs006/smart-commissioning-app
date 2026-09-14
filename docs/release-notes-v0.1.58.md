# v0.1.58 - the MQTT discovery sweep fits a 5,000-asset site

v0.1.58 is a small capacity release. The built-in MQTT discovery engine now
retains up to 30,000 distinct topics per capture instead of 10,000, so a
registerless `#` sweep of a site with 5,000 or more assets publishing three
UDMI topics per asset (15,000 distinct topics) is captured whole instead of
stopping at the 10,000th topic with `topic_limit_reached` (#216). The release
secret scan now fails closed when a requested bundle path is missing,
unreadable, or empty (#212), and the same guard is backported to the older
`scan_v0138` through `scan_v0156` wrappers (#213). No database migration
(Alembic head `a6b7c8d9e0f1`, Sync v2 head `a7b8c9d0e1f2`).

## What changed

- MQTT discovery topic ceiling: `MAX_TOPIC_CAP` in the built-in
  `mqtt_discovery` engine rises from 10,000 to 30,000 distinct topics, and the
  default `max_messages` follows it, so a default run sweeps a whole site
  (#216). Under retain-latest the cap bounds distinct topics, not raw messages.
  The transport's 256 MiB retained-bytes ceiling still bounds memory, and
  operators can still pass a smaller `max_messages`.
- Release secret scan fails closed: `scan_v0157_release_secrets.py` returns
  nonzero when a requested `--path` is missing, unreadable, or expands to zero
  files, and validates each explicit path on its own so a populated bundle
  cannot mask an empty one (#212). The v0.1.38 through v0.1.56 wrappers carry
  the same guard (#213). Release-gate hardening only.
- Frontend test de-flakes for the ModulePage scoped-access focus assertion and
  the UDMI epoch download-abort assertion (#214, #215). Test-only; no shipped
  behaviour changed.

## What did not change

- UDMI validation still sizes its own capture ceiling to the imported register
  (one slot per expected topic), and the native `mqtt-scanner` lane keeps its
  own limits. Only the registerless built-in MQTT discovery lane changed.
- No IP, BACnet, evidence, or reporting behaviour changed. The single-page
  native scanner screens from v0.1.57 are unchanged, and the retained
  `scanner_raw_action` and `scanner_raw_write` job types still render old
  Advanced-panel rows in Run History.

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

Use the [migration rollback guide](migration-rollback-v0.1.58.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.58.md), and
[release validation record](release-validation-v0.1.58.md).
