# v0.1.54 - release-evidence validator hardening

v0.1.54 hardens the shared release-evidence validator that the release gates run
in CI. It is a release-tooling change on top of v0.1.53. There is no functional
change to discovery, the Advanced scanner panels, reporting, or evidence: the
whole v0.1.53 surface is carried forward unchanged, and the portable EXE and
Docker images behave identically to v0.1.53. The version and release identity
move so the published release is bound to the hardened tree.

## What changed

- `scripts/validate_release_evidence.py` now fails closed with a controlled
  validation message on malformed evidence JSON instead of an uncaught Python
  traceback. Three shapes an operator could hand it are covered: a non-object
  (array) evidence root, a CycloneDX SBOM payload whose root is not an object,
  and a `files` field set to `null` or another non-list value. Each returns exit
  1 with a clear "invalid" or "not a list" diagnostic.
- A missing `files` key still defaults to an empty list with no failure, so
  well-formed evidence is unaffected. Regression tests cover the array root, the
  non-object SBOM payload, and the null/string/object `files` cases.

This validator runs during the release ceremony and CI. It is not part of the
shipped application or the portable bundle, so the change does not alter the
runtime behavior of the EXE or the Docker images.

## Carried forward from v0.1.53 (unchanged)

- The release-gate secret scanning and portable-bundle provenance hardening from
  v0.1.53 remain in force: the release and Windows workflows scan the assembled
  evidence and the packaged ZIP and fail closed before upload, and a build from a
  dirty worktree is marked non-publishable.
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

Use the [migration rollback guide](migration-rollback-v0.1.54.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.54.md), and
[release validation record](release-validation-v0.1.54.md).
