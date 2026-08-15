# v0.1.48 - sealed preview approval from Run Controls

v0.1.48 lets a global administrator create the one-use authorization required
by an IP discovery preview directly from Run Controls. The portable EXE and
Docker images share the same release identity.

## What changed

- A completed IP dry run with no usable authorization now shows a global
  administrator the required change-ticket, purpose, and authorization-window
  fields. Submitting them creates an approval bound to that exact preview and
  selects it for the live run.
- An engineer who does not hold global-admin access sees an explicit approval
  requirement instead of an empty required selector.
- The live request remains sealed: it contains the preview and authorization
  references only. It cannot modify the approved scan parameters.

## Compatibility and scope

The portable EXE remains one unified build for internal and external use. It
detects an already-installed local Nmap copy, but does not package, install, or
download Nmap or Npcap. IP, BACnet/IP, and MQTT workflows remain available.
UDMI behavior is unchanged.

## Validation boundary

Automated checks cover the administrator approval path, the sealed live request,
version identity, portable artifact, and Docker evidence paths. They do not
approve a live site scan. Record approved IP, BACnet, MQTT, and UDMI field
evidence privately.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [Nmap operator guide](nmap-one-click-operator-guide.md),
[migration rollback guide](migration-rollback-v0.1.48.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.48.md), and
[release validation record](release-validation-v0.1.48.md).
