# v0.1.47 - idempotent IP discovery submissions

v0.1.47 prevents an IP-discovery retry from creating duplicate runs. The
portable EXE and Docker images have the same release identity and ship the same
database migration.

## What changed

- `POST /api/v1/discovery/ip/runs` accepts an optional `Idempotency-Key`.
  A repeat request with the same authenticated principal, project, site,
  operation, key, and request content returns the original run and does not
  dispatch a second discovery job.
- A reused key with different request content returns HTTP 409, making an
  accidental retry distinguishable from a new request.
- The new `run_idempotency_keys` table binds the request key and fingerprint to
  the canonical run inside the same transaction. Concurrent submissions
  converge on that authority record.
- Requests without an `Idempotency-Key` remain intentional new runs. The web
  application creates a fresh key for each IP Scan or Preview action and reuses
  it only while that action is being retried.

## Compatibility and scope

The portable EXE remains one unified build for internal and external use. It
detects an already-installed local Nmap copy, but does not package, install, or
download Nmap or Npcap. IP, BACnet/IP, and MQTT workflows remain available.
UDMI behavior is unchanged.

## Validation boundary

Automated checks cover repeat, conflicting, concurrent, and
authorization-consumed IP submissions, plus the migration, version identity,
portable artifact, and Docker evidence paths. They do not approve a live site
scan. Record approved IP, BACnet, MQTT, and UDMI field evidence privately.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [Nmap operator guide](nmap-one-click-operator-guide.md),
[migration rollback guide](migration-rollback-v0.1.47.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.47.md), and
[release validation record](release-validation-v0.1.47.md).
