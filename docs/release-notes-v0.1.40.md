# v0.1.40 - truthful UDMI capture and usable field evidence

v0.1.40 restores legacy blank-register applicability, preserves the full
requested finite measurement window, and separates retained expected evidence
from bounded diagnostic evidence. It also adds a condensed metrics-only PDF,
individual redacted JSON payload files with a manifest, and clearer operator
results.

## Validation boundary

Automated checks and a short synthetic broker run do not establish field
acceptance. The approved register, long cadence window, independent collector,
and private broker evidence remain required. Do not put credentials, site data,
or operator identity in this repository.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.40.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.40.md), and
[release validation record](release-validation-v0.1.40.md).
