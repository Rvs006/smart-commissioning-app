# v0.1.41 - sealed discovery authority and operator evidence

v0.1.41 freezes IP and BACnet discovery plans before live execution, records
progressive observations with typed outcomes, and keeps authorization, source
interface, provider, and output budgets bound to the run. It adds protected raw
evidence and a global-admin operator Nmap authority surface. The external
Windows bundle keeps that provider disabled by default.

The release also carries the existing UDMI capture, report, backup, and
portable/Docker provenance work forward from v0.1.40.

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

Use the [migration rollback guide](migration-rollback-v0.1.41.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.41.md), and
[release validation record](release-validation-v0.1.41.md).
