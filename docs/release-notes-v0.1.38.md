# v0.1.38 - UDMI evidence reconciliation and release provenance

v0.1.38 carries the evidence-led fixes prepared from the v0.1.37 engineering
review. It makes capture outcomes, report products, raw evidence, and release
bytes refer to the same bounded run identity. Field acceptance remains separate
and is not claimed by this release note.

## Changes

- Distinguish a completed cadence window from a one-minute operator Stop or
  another partial capture, while retaining the partial evidence honestly.
- Keep applicability, expected, observed, mismatch, topic, and payload-type
  outcomes separate so unresolved scope is not reported as a failure.
- Add client and technical UDMI report products. Technical evidence-pack ZIPs
  include redacted raw records, deterministic evidence IDs, and a finding index.
- Add deterministic selected-report bundle manifests with member hashes, sizes,
  report IDs, and shared evidence-set IDs.
- Carry portable build version, source commit, and final EXE SHA-256 into the
  bundle and launcher environment. Docker images carry the same OCI provenance.
- Accept an optional `Floor` column in the MQTT asset register. Floor is kept as
  explicit metadata and report context; it does not change asset identity,
  expected topics, or payload capture scope. `Room` is never used as a Floor
  value.
- Treat the newest MQTT register upload as authoritative. A fully rejected
  upload now stops a register-driven validation run with the row-level import
  reason instead of silently reusing an older accepted register.

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

There is no schema migration in v0.1.38. Follow the [migration and rollback
guide](migration-rollback-v0.1.38.md) and [Docker deployment and rollback
guide](docker-deployment-rollback-v0.1.38.md).
