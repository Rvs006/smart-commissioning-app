# v0.1.42 - IP, BACnet, and MQTT recovery

v0.1.42 repairs the discovery and Results flows reported during the IP discovery
troubleshooting session. A blank IP target editor now deliberately scans the
accepted imported register. BACnet Preview follows the sealed-preview and
authorization path, the saved BACnet UDP port reaches the transport, and
persisted BACnet point data renders in **Points / Live Data**. Completed MQTT
captures reach Results from their terminal snapshot even when a supplementary
topic refresh is temporarily unavailable.

The release also retains the sealed discovery authority, protected evidence,
operator Nmap controls, and portable/Docker provenance introduced after v0.1.41.
The portable build stamps the requested application version into API health,
reports, and the Windows executable.

## Validation boundary

Automated checks and a synthetic broker run do not establish field acceptance.
The approved register, BACnet segment, finite broker capture, independent
collector, and private evidence remain required. Do not put credentials, site
data, or operator identity in this repository.

## Release artifacts

- Source commit: `{{COMMIT}}`
- EXE SHA-256: `{{EXE_SHA256}}`
- ZIP SHA-256: `{{ZIP_SHA256}}`
- API: `{{API_IMAGE}}@{{API_IMAGE_DIGEST}}`
- Worker: `{{WORKER_IMAGE}}@{{WORKER_IMAGE_DIGEST}}`
- Frontend: `{{FRONTEND_IMAGE}}@{{FRONTEND_IMAGE_DIGEST}}`

Use the [migration rollback guide](migration-rollback-v0.1.42.md),
[Docker rollback guide](docker-deployment-rollback-v0.1.42.md), and
[release validation record](release-validation-v0.1.42.md).
