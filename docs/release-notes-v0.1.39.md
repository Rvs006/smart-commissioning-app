# v0.1.39 - Asset-register reconciliation patch

v0.1.39 publishes the corrected register reconciliation behavior from the
v0.1.38 candidate without rewriting the existing v0.1.38 release. Field
acceptance remains separate and is not claimed by this release note.

## Changes

- Accept an optional `Floor` column in the MQTT asset register. Floor is kept as
  explicit metadata and report context; it does not change asset identity,
  expected topics, or payload capture scope. `Room` is never used as a Floor
  value.
- Treat the newest MQTT register upload as authoritative. A fully rejected
  upload now stops a register-driven validation run with the row-level import
  reason instead of silently reusing an older accepted register.
- Carry the corrected application, package, portable, Docker, and evidence
  identity as v0.1.39 while retaining source-commit provenance.

## Validation boundary

The local application suites and release contracts must pass before controlled
field validation. A successful automated report or hosted Compose run does not
replace the approved register, independent collector, broker evidence, or
longest-cadence field gate. Do not put broker credentials, site identifiers,
device identifiers, or operator details in this public repository.

## Portable release

The Windows portable and hosted workflow artifacts must be built from the exact
release commit and checked by `scripts/release-portable.ps1` before publication.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

## Hosted images

- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

There is no schema migration in v0.1.39. Follow the [migration and rollback
guide](migration-rollback-v0.1.39.md) and [Docker deployment and rollback
guide](docker-deployment-rollback-v0.1.39.md).
