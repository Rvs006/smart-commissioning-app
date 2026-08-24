# v0.1.55 - release secret-scan hardening

v0.1.55 hardens the shared release secret scanner that the release gates run in
CI. It is a release-tooling change on top of v0.1.54. There is no functional
change to discovery, the Advanced scanner panels, reporting, or evidence: the
whole v0.1.54 surface is carried forward unchanged, and the portable EXE and
Docker images behave identically to v0.1.54. The version and release identity
move so the published release is bound to the hardened tree.

## What changed

- `scripts/scan_v0137_release_secrets.py` (the base scanner every
  `scan_vXXXX_release_secrets.py` wrapper delegates to) now catches a credential
  written as a quoted-key JSON field, for example `"password": "value"`, in both
  the assembled evidence directory and the packaged release ZIP. The previous
  matcher required the assignment separator immediately after the keyword, so a
  JSON key's closing quote between the keyword and its colon let the value slip
  through unscanned.
- Suppression is precise, not shape-based. A value is treated as a schema field
  name or a label about the credential, and skipped, only when it spells a
  credential keyword as a whole word and carries no digit, such as
  `"mqtt_password": "password"` or `"MQTT Password": "Broker password (masked)"`.
  Opaque tokens, keys, and passphrases, including `abcdefghijk`,
  `SomeIdentifier`, and `correct horse battery staple`, still report. Value shape
  alone never suppresses a finding.

This scanner runs during the release ceremony and CI. It is not part of the
shipped application or the portable bundle, so the change does not alter the
runtime behavior of the EXE or the Docker images.

## Carried forward from v0.1.54 (unchanged)

- The v0.1.54 release-evidence validator hardening (malformed evidence JSON fails
  closed with a controlled message) and the v0.1.53 release-gate secret scanning
  and dirty-worktree provenance hardening remain in force.
- The Advanced tab on the IP, BACnet, and MQTT discovery modules still embeds each
  standalone scanner tool's own UI through the backend reverse proxy
  (/api/v1/scanners/{proto}/raw/). Reads run freely for the engineer role; device
  writes pause for an in-app confirmation gated by a single-use, request-bound
  token. Every action is still recorded as an SCT run row: scanner_raw_action for
  reads and scanner_raw_write for writes.
- The sidecar-lane operator inputs (IP target range, frozen BACnet Source
  Interface) and the stable A-Z MQTT live topic tree are unchanged.

## Compatibility and scope

This release adds no database migration and retains the run-isolation and
equivalent-retry behavior of earlier releases. The scanner modules and the
Advanced panel run on the local inline executor. The Advanced panel authenticates
via the local principal, so it is available in the portable and local
deployments; a keyed hosted deployment needs the deferred panel-session
credential.

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

Use the [migration rollback guide](migration-rollback-v0.1.55.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.55.md), and
[release validation record](release-validation-v0.1.55.md).
