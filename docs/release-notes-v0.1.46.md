# v0.1.46 - one validated release identity

v0.1.46 completes the frontend fallback fix with a hard runtime boundary. A
frontend source build, API health result, generated report, and run evidence
now identify the same packaged release. A different runtime stamp stops the
backend before it can publish mixed identity data.

## What changed

- The shared core package is the canonical runtime version source.
- Portable and Docker releases use the matching `v`-prefixed build stamp. Any
  different non-empty value is rejected before API startup, report rendering,
  or run-context creation.
- A portable build without `-Version` now stamps the canonical package version,
  while source commit and dirty-tree state stay in separate provenance fields.
- The Product Brief, Learning path, README, and Nmap operator guide keep the
  one-click Nmap workflow: a global administrator approves one detected local
  installation and engineers choose an approved fixed profile. There are no
  Nmap path, flag, script, or command fields to complete.

## Compatibility and scope

The portable EXE remains one unified build for internal and external use. It
detects an already-installed local Nmap copy, but does not package, install, or
download Nmap or Npcap. UDMI behavior is unchanged. This release has no
database migration.

## Validation boundary

Automated checks verify the fail-closed identity boundary, version fallback,
portable identity, release contracts, and evidence. They do not approve a live
site scan. Record scan approval and on-site IP, BACnet, MQTT, and UDMI outcomes
in the private commissioning record.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [Nmap operator guide](nmap-one-click-operator-guide.md),
[migration rollback guide](migration-rollback-v0.1.46.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.46.md), and
[release validation record](release-validation-v0.1.46.md).
