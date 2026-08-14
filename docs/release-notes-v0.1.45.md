# v0.1.45 - one version identity and simpler Nmap guidance

v0.1.45 fixes the source-build fallback that could show `dev` in the frontend
while the backend reported the release version. It also adds the short Nmap
operator guide used by the Product Brief, Learning page, and repository docs.

## What changed

- When a frontend build stamp is absent or empty, the app now shows its package
  release version. The fallback matches the backend health and report-renderer
  version instead of showing `dev`.
- The Product Brief, Learning path, root README, and documentation index now
  explain the one-click Nmap flow in plain language.
- A global administrator approves one detected local Nmap installation for the
  selected project and site. Engineers then select only an approved fixed
  profile in IP Discovery. There are no Nmap path, flag, script, or command
  fields to complete.

## Compatibility and scope

The portable EXE is one unified build for internal and external use. It detects
an already-installed local Nmap copy, but it does not package, install, or
download Nmap or Npcap. An approval is requested again only when the detected
installed files change. UDMI behavior is unchanged. This release has no
database migration.

## Validation boundary

Automated checks cover the version fallback, packaged version identity, Nmap
guidance, release contracts, and evidence. They do not prove a particular
customer BACnet network, MQTT broker, or site authorization. Record approvals
and on-site outcomes in the private commissioning record.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [Nmap operator guide](nmap-one-click-operator-guide.md),
[migration rollback guide](migration-rollback-v0.1.45.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.45.md), and
[release validation record](release-validation-v0.1.45.md).
