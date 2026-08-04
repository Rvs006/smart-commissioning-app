# v0.1.37 - field contract and report integrity

v0.1.37 tightens the application-side evidence contract and prepares the
portable release path. It does not claim field acceptance until both gates in
the [field checklist](v0.1.37-field-acceptance-checklist.md) pass.

## Changes

- Group state-shaped payloads found on metadata topics into one actionable
  routing diagnostic with raw topic and key evidence.
- Preserve exact, case-sensitive expected-topic, alternate-topic, and no-match
  outcomes without fabricating observations for missing payloads.
- Bound Reports API responses and the generated-report table while retaining
  report history and frozen run provenance.
- Add a sanitized pre-run manifest, release evidence entry points, and explicit
  Gate A/Gate B termination rules.

## Validation boundary

The application test gate must pass before field work is accepted. A completed
report job remains distinct from publisher or field acceptance. The private
field run must use the canonical register, its SHA-256, one approved scope, and
paired append-only evidence. No broker credentials, site identifiers, device
identifiers, or operator details belong in this repository or release notes.

## Portable release

The Windows portable and hosted workflow artifacts must be built from the exact
release commit and checked by `scripts/release-portable.ps1` in prepare-only
mode before any publication action.

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`

## Hosted images

- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

There is no schema migration in v0.1.37. Follow the [migration and rollback
guide](migration-rollback-v0.1.37.md) and [Docker deployment and rollback
guide](docker-deployment-rollback-v0.1.37.md).
